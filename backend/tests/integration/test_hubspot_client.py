"""Integration tests of the HubSpot CRM connector against contract-checked fake portals.

AAP 0.11.2 row ``integration/test_hubspot_client.py``; each guarantee maps to named tests:

* **Paths and batching**: :func:`test_every_surface_uses_2026_09_path`,
  :func:`test_object_batch_reads_at_most_100`, :func:`test_association_batches_at_most_1000`.
* **Associations contract** (AAP 0.2.4 "Associations batch read, exact contract"):
  :func:`test_initial_body_is_ids_only`,
  :func:`test_continuation_resends_id_and_after_only_for_paged_inputs`,
  :func:`test_207_errors_mark_inputs_error`, :func:`test_no_associations_found_is_ok_empty`,
  :func:`test_status_not_complete_fails_call`, :func:`test_failed_continuation_is_partial`,
  :func:`test_documented_sample_parses_verbatim`.
* **Target reads** (AAP 0.3.3 "Target reads", "Contact refresh"):
  :func:`test_target_read_omitting_an_id_is_partial`,
  :func:`test_contact_refresh_missing_contact_is_error`,
  :func:`test_contacts_by_email_not_found_vs_failed`,
  :func:`test_sim_invalid_never_sent_to_live_portal`, and the startup property verification
  of AAP 0.3.3 and 0.5.4.7: :func:`test_verify_bindings_resolves_stage_labels`,
  :func:`test_unresolved_binding_is_unavailable`, :func:`test_property_mode_binding`.
* **Search overflow** (AAP 0.3.3 "Deal search"): :func:`test_day_over_10000_splits_to_hours`,
  :func:`test_hour_over_10000_read_to_ceiling_and_incomplete`,
  :func:`test_search_bodies_shape`.
* **Google contacts** (AAP 0.3.3 "Google-to-CRM link"):
  :func:`test_google_contact_discovery_only_when_configured_and_verified`, which runs the real
  ``CrmSyncJob`` on the PostgreSQL 18 test database.
* **Throttle and redaction** (AAP 0.3.3 "Version and throttle", AAP 0.3.2 log redaction):
  :func:`test_throttle_4_requests_per_second`, :func:`test_429_retried_with_retry_after`,
  :func:`test_token_and_emails_never_logged`.
* **Traceability** (AAP 0.11.2 "External fixtures are traceable to verified contracts"):
  :func:`test_documented_samples_parse`, :func:`test_captured_payloads_parse`.

How the tests run:

* Every request goes through the conftest's ``contract_checked_transport("hubspot", …)``,
  which identifies the surface, validates the request against
  ``backend/tests/contracts/hubspot/*.json`` and validates every 2xx body before the client
  sees it. A fake that answered with a field its contract does not define would fail the test.
* :class:`FakePortal` is an in-memory HubSpot portal: contact property definitions with
  ``lifecyclestage`` options, contacts, meetings, deals, associations paged per input with
  ``{id, after}`` continuation, HubSpot's 207 ``NO_ASSOCIATIONS_FOUND`` report for an input
  without associations, and a CRM Search index honouring ``GTE``/``LT``/``HAS_PROPERTY``,
  ``limit``, numeric ``after`` cursors and the query's ``total``. Per-test overrides inject
  429s, 5xx, 207 errors and batch statuses other than ``COMPLETE``.
* The path test answers from the simulator (``mock_transport_for``) instead, so the client's
  paths are checked against the same contracts the simulator renders from.
* No real network and no real sleeps: the conftest's network guard refuses real transports,
  every :class:`~ads_engine.connectors.hubspot.client.RateLimiter` runs on a fake monotonic
  clock, and every :class:`~ads_engine.connectors.http.RetryPolicy` records its waits instead
  of sleeping.

Out of scope: any write to HubSpot (the connector only reads) and numbered ``/v1`` to ``/v4``
paths, whose absence is asserted.
"""

import bisect
import copy
import itertools
import json
import re
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import unquote

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.analysis.thresholds import Thresholds
from ads_engine.connectors.errors import TransientError
from ads_engine.connectors.http import RetryPolicy, build_client
from ads_engine.connectors.hubspot.client import (
    HUBSPOT_API_VERSION,
    MAX_ASSOCIATION_INPUTS,
    MAX_BATCH_READ_INPUTS,
    MAX_SEARCH_BODY_CHARS,
    MAX_SEARCH_LIMIT,
    SEARCH_RESULT_CEILING,
    SHARED_LIMITER,
    STAGE_DATE_PROPERTY_PREFIX,
    HubSpotClient,
    RateLimiter,
)
from ads_engine.connectors.hubspot.parser import (
    DEAL_PROPERTIES,
    MEETING_PROPERTIES,
    NO_ASSOCIATIONS_FOUND,
    AssociationResult,
    ContactPropertyMap,
    email_sha256,
    parse_association_batch,
    parse_batch_read,
    parse_property,
    parse_search_page,
)
from ads_engine.connectors.hubspot.search import (
    CEILING_REASON,
    deal_search_filter,
    epoch_ms,
)
from ads_engine.db.models.crm import ASSOCIATION_FETCH_STATUSES, CrmContact
from ads_engine.db.models.ops import SourceDataset
from ads_engine.settings import get_settings
from ads_engine.simulation import SIM_EMAIL_DOMAIN, payloads
from ads_engine.sync import freshness
from ads_engine.sync.jobs import CrmSyncJob, JobContext
from ads_engine.sync.mode import SourceModeController

pytestmark = [pytest.mark.anyio, pytest.mark.contract]

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

SOURCE: Final = "hubspot"
HOST: Final = "api.hubapi.com"
TOKEN: Final = "test-token"
STAMP: Final = "2026-09-04T13:05:22.123Z"
"""``createdAt``/``updatedAt``/``startedAt`` of every record and envelope the fake serves."""

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
DAY0: Final = datetime(2026, 9, 1, tzinfo=UTC)
DAY: Final = timedelta(days=1)
HOUR: Final = timedelta(hours=1)
NOW: Final = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The run instant of the CRM sync job test."""

FMB_STAGE_ID: Final = "1406943076"
"""The portal's custom "First meeting booked" lifecycle stage (custom stages are numeric)."""
QL_STAGE_ID: Final = "1406943077"
"""The portal's custom "Qualified Lead" lifecycle stage."""
MQL_PROPERTY: Final = f"{STAGE_DATE_PROPERTY_PREFIX}{FMB_STAGE_ID}"
QL_PROPERTY: Final = f"{STAGE_DATE_PROPERTY_PREFIX}{QL_STAGE_ID}"
PROPS: Final = ContactPropertyMap(mql_property=MQL_PROPERTY, ql_property=QL_PROPERTY)

CAMPAIGN_PROPERTY: Final = "google_campaign_id"
TOUCH_PROPERTY: Final = "google_touch_date"
PROPERTY_MODE_MQL: Final = "date_first_meeting_was_booked"

DEFAULT_STAGES: Final = (
    ("subscriber", "Subscriber"),
    ("lead", "Lead"),
    ("marketingqualifiedlead", "Marketing Qualified Lead"),
    ("salesqualifiedlead", "Sales Qualified Lead"),
    ("opportunity", "Opportunity"),
    ("customer", "Customer"),
    ("evangelist", "Evangelist"),
    ("other", "Other"),
)
"""HubSpot's default ``lifecyclestage`` options as ``(value, label)``."""

CUSTOM_STAGES: Final = ((FMB_STAGE_ID, "First meeting booked"), (QL_STAGE_ID, "Qualified Lead"))

SURFACE_PATHS: Final[Mapping[str, tuple[str, re.Pattern[str]]]] = {
    "objects_batch_read": (
        "POST",
        re.compile(r"/crm/objects/2026-09/(contacts|meetings|deals)/batch/read"),
    ),
    "crm_search": ("POST", re.compile(r"/crm/objects/2026-09/(deals|contacts)/search")),
    "associations_batch_read": (
        "POST",
        re.compile(
            r"/crm/associations/2026-09/(contacts|deals)/(meetings|deals|contacts)/batch/read"
        ),
    ),
    "properties": ("GET", re.compile(r"/crm/properties/2026-09/contacts/[^/]+")),
}
"""The exact ``2026-09`` path of every surface the connector calls (AAP 0.2.4 surfaces table)."""

NUMBERED_PATH: Final = re.compile(r"/v[0-9]+/")


# ---------------------------------------------------------------------------------------------
# Fake clock and recorded waits
# ---------------------------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that only its own :meth:`sleep` advances; nothing really sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        """Return the fake monotonic time in seconds."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Advance the fake time instead of sleeping."""
        self.sleeps.append(seconds)
        self.now += max(seconds, 0.0)


def fake_limiter(clock: FakeClock) -> RateLimiter:
    """Return a 4 req/s :class:`RateLimiter` on ``clock``, the product's rate."""
    return RateLimiter(4.0, clock=clock, sleep=clock.sleep)


# ---------------------------------------------------------------------------------------------
# Wire builders (only fields the contracts in backend/tests/contracts/hubspot/ declare)
# ---------------------------------------------------------------------------------------------


def iso_ms(milliseconds: int) -> str:
    """Render epoch milliseconds as HubSpot's ISO-8601 wire instant, ``…T…sssZ``."""
    instant = EPOCH + timedelta(milliseconds=milliseconds)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.") + f"{instant.microsecond // 1000:03d}Z"


def ms_of(value: str) -> int:
    """Return an ISO-8601 instant or epoch-millisecond string as epoch milliseconds."""
    if value.isdigit():
        return int(value)
    return (datetime.fromisoformat(value) - EPOCH) // timedelta(milliseconds=1)


def crm_record(
    hs_id: str, properties: Mapping[str, str | None], created: str = STAMP
) -> dict[str, Any]:
    """Build one CRM object exactly as the batch read and search contracts define it."""
    return {
        "id": hs_id,
        "properties": dict(properties),
        "createdAt": created,
        "updatedAt": created,
        "archived": False,
    }


def error_entry(
    category: str,
    message: str,
    context: Mapping[str, list[str]],
    sub_category: str | None = None,
) -> dict[str, Any]:
    """Build one 207 ``errors[]`` entry ``{status, category, subCategory?, message, context}``."""
    entry: dict[str, Any] = {"status": "error", "category": category, "message": message}
    if sub_category is not None:
        entry["subCategory"] = sub_category
    entry["context"] = {key: list(values) for key, values in context.items()}
    return entry


def batch_envelope(
    results: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]] = (),
    *,
    status: str = "COMPLETE",
) -> dict[str, Any]:
    """Build a batch response body; ``numErrors`` accompanies ``errors`` as on a real 207."""
    body: dict[str, Any] = {
        "status": status,
        "results": [dict(result) for result in results],
        "startedAt": STAMP,
        "completedAt": STAMP,
    }
    if errors:
        body["errors"] = [dict(entry) for entry in errors]
        body["numErrors"] = len(errors)
    return body


def platform_error(status: int, message: str, category: str) -> httpx.Response:
    """HubSpot's ``{status, message, category}`` error body, which no success schema covers."""
    return httpx.Response(
        status, json={"status": "error", "message": message, "category": category}
    )


def property_definition(
    name: str, data_type: str, options: Sequence[tuple[str, str]] = ()
) -> dict[str, Any]:
    """Build a property definition with every field the properties contract requires."""
    return {
        "name": name,
        "label": name.replace("_", " ").title(),
        "type": data_type,
        "fieldType": "radio"
        if options
        else ("date" if data_type in {"date", "datetime"} else "text"),
        "groupName": "contactinformation",
        "description": "",
        "options": [
            {"label": label, "value": value, "hidden": False, "displayOrder": index}
            for index, (value, label) in enumerate(options)
        ],
    }


# ---------------------------------------------------------------------------------------------
# The fake portal
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Call:
    """One request the fake portal answered.

    Attributes:
        surface: The contract surface ``payloads.identify_surface`` names for the request.
        method: The HTTP method.
        path: The decoded request path.
        segments: The path segments after ``/crm/{kind}/2026-09/``.
        body: The decoded JSON body; ``None`` for a ``GET``.
        at: The fake clock reading when the request reached the transport.
        request: The request itself.
    """

    surface: str
    method: str
    path: str
    segments: tuple[str, ...]
    body: Any
    at: float
    request: httpx.Request


type Override = Callable[[Call], httpx.Response | None]
"""A per-test responder consulted before the portal's own behaviour; ``None`` falls through."""


class FakePortal:
    """An in-memory HubSpot portal answering the four ``2026-09`` surfaces like HubSpot does.

    * ``properties``: ``GET /crm/properties/2026-09/contacts/{name}`` returns the stored
      definition, or HubSpot's 404 for an unknown property.
    * ``objects_batch_read``: contacts by ``idProperty=email`` (matched on the normalised
      ``email``) or by id, meetings and deals by id. A missing input is reported as a 207
      ``OBJECT_NOT_FOUND`` entry naming it in ``context.ids``, or silently omitted when
      :attr:`report_missing` is false. :attr:`email_errors` lists addresses answered with
      another error category.
    * ``associations_batch_read``: each input's targets, :attr:`association_page_size` per
      page, with ``paging.next.after`` cursors ``c1``, ``c2``… that the input re-sends as
      ``{id, after}``. An input without associations gets HubSpot's 207 entry
      (``OBJECT_NOT_FOUND`` / ``crm.associations.NO_ASSOCIATIONS_FOUND``, the input in
      ``context.fromObjectId``); :attr:`association_errors` lists inputs answered with other
      errors.
    * ``crm_search``: records whose range property falls in ``[GTE, LT)`` and that carry every
      ``HAS_PROPERTY`` property, sorted ascending, paged by numeric ``after`` offsets with
      ``limit`` results per page and the query's real ``total``.

    Every request is recorded in :attr:`calls` and every JSON reply in :attr:`replies`.
    Overrides in :attr:`overrides` are consulted first, in order.
    """

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.retry_waits: list[float] = []
        self.calls: list[Call] = []
        self.replies: list[tuple[int, Any]] = []
        self.overrides: list[Override] = []
        self.contact_properties: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, dict[str, dict[str, str | None]]] = {
            "contacts": {},
            "meetings": {},
            "deals": {},
        }
        self.report_missing = True
        self.email_errors: dict[str, str] = {}
        self.links: dict[tuple[str, str, str], list[str]] = {}
        self.association_page_size = 500
        self.association_errors: dict[str, tuple[str, str | None]] = {}
        self._search: dict[str, list[dict[str, Any]]] = {"deals": [], "contacts": []}
        self._sorted: dict[tuple[str, str], tuple[list[int], list[dict[str, Any]]]] = {}

    # -- seeding -------------------------------------------------------------------------

    def add_lifecycle(self, stages: Iterable[tuple[str, str]]) -> None:
        """Define ``lifecyclestage`` with ``stages`` (value, label) as its options."""
        self.contact_properties["lifecyclestage"] = property_definition(
            "lifecyclestage", "enumeration", tuple(stages)
        )

    def add_property(self, name: str, data_type: str = "datetime") -> None:
        """Define one contact property, for example a stage's ``hs_v2_date_entered_{id}``."""
        self.contact_properties[name] = property_definition(name, data_type)

    def add_standard_portal(self) -> None:
        """Default stages plus the custom "First meeting booked" and "Qualified Lead" stages.

        Every stage gets its "date entered" property, as HubSpot calculates one per stage.
        """
        stages = (*DEFAULT_STAGES, *CUSTOM_STAGES)
        self.add_lifecycle(stages)
        for value, _label in stages:
            self.add_property(f"{STAGE_DATE_PROPERTY_PREFIX}{value}")

    def add_object(
        self, object_type: str, hs_id: str, properties: Mapping[str, str | None]
    ) -> None:
        """Store one contact, meeting or deal readable by id (and contacts by email)."""
        self.objects[object_type][hs_id] = dict(properties)

    def link(self, from_type: str, to_type: str, from_id: str, targets: Iterable[str]) -> None:
        """Associate ``from_id`` with ``targets``, in that order."""
        self.links[(from_type, to_type, from_id)] = list(targets)

    def index(self, object_type: str, hs_id: str, properties: Mapping[str, str | None]) -> None:
        """Add one record to the CRM Search index of ``object_type``."""
        created = properties.get("createdate") or STAMP
        self._search[object_type].append(crm_record(hs_id, properties, created))
        self._sorted = {key: value for key, value in self._sorted.items() if key[0] != object_type}

    # -- handler -------------------------------------------------------------------------

    async def record_wait(self, seconds: float) -> None:
        """The retry policy's sleep: record the wait and return at once, never sleeping."""
        self.retry_waits.append(seconds)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one request that the contract-checked transport already validated."""
        surface = payloads.identify_surface(SOURCE, request)
        path = unquote(request.url.path)
        segments = tuple(path.strip("/").split("/")[3:])
        body = json.loads(request.content) if request.content else None
        call = Call(surface, request.method, path, segments, body, self.clock.now, request)
        self.calls.append(call)
        response: httpx.Response | None = None
        for override in self.overrides:
            response = override(call)
            if response is not None:
                break
        if response is None:
            response = self._answer(call)
        reply = json.loads(response.content) if response.content else None
        self.replies.append((response.status_code, reply))
        return response

    def calls_to(self, surface: str, *segments: str) -> list[Call]:
        """The recorded calls of ``surface`` whose path segments start with ``segments``."""
        return [
            call
            for call in self.calls
            if call.surface == surface and call.segments[: len(segments)] == segments
        ]

    def _answer(self, call: Call) -> httpx.Response:
        """Dispatch to the surface's behaviour."""
        match call.surface:
            case "properties":
                return self._property(call)
            case "objects_batch_read":
                return self._batch_read(call)
            case "associations_batch_read":
                return self._associations(call)
            case "crm_search":
                return self._crm_search(call)
        raise AssertionError(f"unexpected surface {call.surface}")

    def _property(self, call: Call) -> httpx.Response:
        object_type, name = call.segments
        definition = self.contact_properties.get(name) if object_type == "contacts" else None
        if definition is None:
            return platform_error(404, f"Unable to find property {name}", "OBJECT_NOT_FOUND")
        return httpx.Response(200, json=definition)

    def _batch_read(self, call: Call) -> httpx.Response:
        object_type = call.segments[0]
        store = self.objects[object_type]
        requested: list[str] = list(call.body["properties"])
        results: list[dict[str, Any]] = []
        missing: list[str] = []
        errors: list[dict[str, Any]] = []
        by_email = call.body.get("idProperty") == "email"
        for entry in call.body["inputs"]:
            key: str = entry["id"]
            if by_email and key.strip().lower() in self.email_errors:
                category = self.email_errors[key.strip().lower()]
                errors.append(error_entry(category, f"Invalid input {key}", {"ids": [key]}))
                continue
            if by_email:
                hs_id = next(
                    (
                        hs_id
                        for hs_id, props in store.items()
                        if (props.get("email") or "").lower() == key.strip().lower()
                    ),
                    None,
                )
            else:
                hs_id = key if key in store else None
            if hs_id is None:
                missing.append(key)
                continue
            props = store[hs_id]
            results.append(crm_record(hs_id, {name: props.get(name) for name in requested}))
        if missing and self.report_missing:
            noun = object_type.rstrip("s").upper()
            errors.append(
                error_entry(
                    "OBJECT_NOT_FOUND",
                    f"Could not get some {noun} objects, they may be deleted or not exist.",
                    {"ids": missing},
                )
            )
        return httpx.Response(207 if errors else 200, json=batch_envelope(results, errors))

    def _associations(self, call: Call) -> httpx.Response:
        from_type, to_type = call.segments[0], call.segments[1]
        size = self.association_page_size
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for entry in call.body["inputs"]:
            input_id: str = entry["id"]
            if input_id in self.association_errors:
                category, sub_category = self.association_errors[input_id]
                errors.append(
                    error_entry(
                        category, "Invalid input", {"fromObjectId": [input_id]}, sub_category
                    )
                )
                continue
            targets = self.links.get((from_type, to_type, input_id), [])
            if not targets:
                errors.append(
                    error_entry(
                        "OBJECT_NOT_FOUND",
                        f"No {to_type} is associated with {from_type} {input_id}.",
                        {"fromObjectId": [input_id]},
                        NO_ASSOCIATIONS_FOUND,
                    )
                )
                continue
            after = entry.get("after")
            page = 0 if after is None else int(after.removeprefix("c"))
            chunk = targets[page * size : (page + 1) * size]
            result: dict[str, Any] = {
                "from": {"id": input_id},
                "to": [
                    {
                        # HubSpot sends numeric target ids as JSON integers.
                        "toObjectId": int(target) if target.isdigit() else target,
                        "associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": 200}],
                    }
                    for target in chunk
                ],
            }
            if (page + 1) * size < len(targets):
                result["paging"] = {"next": {"after": f"c{page + 1}"}}
            results.append(result)
        return httpx.Response(207 if errors else 200, json=batch_envelope(results, errors))

    def _sorted_index(self, object_type: str, prop: str) -> tuple[list[int], list[dict[str, Any]]]:
        """The search index of ``object_type`` sorted by ``prop``; records without it skipped."""
        key = (object_type, prop)
        if key not in self._sorted:
            keyed = sorted(
                (ms_of(record["properties"][prop]), record["id"], record)
                for record in self._search[object_type]
                if record["properties"].get(prop)
            )
            self._sorted[key] = ([item[0] for item in keyed], [item[2] for item in keyed])
        return self._sorted[key]

    def _crm_search(self, call: Call) -> httpx.Response:
        object_type = call.segments[0]
        body = call.body
        (group,) = body["filterGroups"]
        ranges = [item for item in group["filters"] if item["operator"] in {"GTE", "LT"}]
        required = [
            item["propertyName"] for item in group["filters"] if item["operator"] == "HAS_PROPERTY"
        ]
        (prop,) = {item["propertyName"] for item in ranges}
        lo = next(int(item["value"]) for item in ranges if item["operator"] == "GTE")
        hi = next(int(item["value"]) for item in ranges if item["operator"] == "LT")
        keys, records = self._sorted_index(object_type, prop)
        matched = [
            record
            for record in records[bisect.bisect_left(keys, lo) : bisect.bisect_left(keys, hi)]
            if all(record["properties"].get(name) for name in required)
        ]
        offset = int(body.get("after", "0"))
        limit: int = body["limit"]
        reply: dict[str, Any] = {
            "total": len(matched),
            "results": [copy.deepcopy(record) for record in matched[offset : offset + limit]],
        }
        if offset + limit < len(matched):
            reply["paging"] = {"next": {"after": str(offset + limit)}}
        return httpx.Response(200, json=reply)


# ---------------------------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------------------------

type TransportFactory = Callable[[str, Any], httpx.MockTransport]
type ClientOpener = Callable[..., AbstractAsyncContextManager[HubSpotClient]]


@pytest.fixture
def portal() -> FakePortal:
    """A fresh fake portal with the default and custom lifecycle stages and their dates."""
    fake = FakePortal()
    fake.add_standard_portal()
    return fake


@pytest.fixture
def hubspot(portal: FakePortal, contract_checked_transport: TransportFactory) -> ClientOpener:
    """Return ``hubspot(**options)``: an async context opening a client on :func:`portal`.

    The client sends through ``build_client("hubspot", contract_checked_transport(...))`` with
    the token ``SecretStr("test-token")``, ``live=True``, a retry policy that records its
    waits in ``portal.retry_waits`` and a 4 req/s limiter on ``portal.clock``.

    Options:
        live: Whether the portal is a real one (``True``).
        limiter: The throttle; a fresh fake-clock limiter when ``None``. Never the real
            process-wide limiter, which would really sleep.
        contact_properties: The bound contact property map (:data:`PROPS`); ``None`` leaves it
            unbound until ``verify_bindings``.
    """

    @asynccontextmanager
    async def _open(
        *,
        live: bool = True,
        limiter: RateLimiter | None = None,
        contact_properties: ContactPropertyMap | None = PROPS,
    ) -> AsyncIterator[HubSpotClient]:
        transport = contract_checked_transport(SOURCE, portal)
        async with build_client(SOURCE, transport) as http:
            yield HubSpotClient(
                http,
                SecretStr(TOKEN),
                live=live,
                retry=RetryPolicy(sleep=portal.record_wait),
                limiter=limiter if limiter is not None else fake_limiter(portal.clock),
                contact_properties=contact_properties,
            )

    return _open


def input_counts(calls: Sequence[Call]) -> list[int]:
    """The number of ``inputs`` each batch call sent, in order."""
    return [len(call.body["inputs"]) for call in calls]


def partition_of(body: Mapping[str, Any]) -> tuple[int, int]:
    """The ``[GTE, LT)`` epoch-millisecond bounds of a search body's range filters."""
    (group,) = body["filterGroups"]
    bounds = {item["operator"]: int(item["value"]) for item in group["filters"] if "value" in item}
    return bounds["GTE"], bounds["LT"]


def ms(instant: datetime) -> int:
    """Epoch milliseconds of a tz-aware instant, as the search filters carry them."""
    return int(epoch_ms(instant))


def index_deals(
    portal: FakePortal, start: datetime, count: int, step: timedelta, first: int
) -> list[str]:
    """Index ``count`` deals created ``step`` apart from ``start``; return their ids."""
    ids: list[str] = []
    for offset in range(count):
        hs_id = str(first + offset)
        created = iso_ms(ms(start + offset * step))
        portal.index(
            "deals",
            hs_id,
            {
                "createdate": created,
                "dealstage": "appointmentscheduled",
                "pipeline": "default",
                "amount": "1000",
            },
        )
        ids.append(hs_id)
    return ids


async def no_sleep(seconds: float) -> None:
    """A retry sleep that returns at once."""
    del seconds


# ---------------------------------------------------------------------------------------------
# Phase 1: paths and batching (AAP 0.2.4 surfaces table)
# ---------------------------------------------------------------------------------------------


async def test_every_surface_uses_2026_09_path(
    contract_checked_transport: TransportFactory,
    mock_transport_for: Callable[..., httpx.MockTransport],
    make_world: Callable[..., Any],
    contract: Callable[..., payloads.Contract],
    thresholds: Thresholds,
) -> None:
    """Paths and batching: every surface's ``2026-09`` path, traced to its contract file.

    The simulator answers (realistic seeded data); the contract check validates every request
    and response. Every recorded path is one of ``POST /crm/objects/2026-09/{contacts|meetings|
    deals}/batch/read``, ``POST /crm/objects/2026-09/{deals|contacts}/search``, ``POST
    /crm/associations/2026-09/{from}/{to}/batch/read`` or ``GET /crm/properties/2026-09/
    contacts/{name}``, none is numbered (``/v1/`` to ``/v4/``), and each surface's contract
    names its reference page and last-modified date (associations: ``2026-09-09``).
    """
    # The simulator's handler is asynchronous (simulation/transport.py).
    inner = cast(
        "Callable[[httpx.Request], Awaitable[httpx.Response]]",
        mock_transport_for(SOURCE, make_world("default"), strict=True).handler,
    )
    seen: list[httpx.Request] = []

    async def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await inner(request)

    clock = FakeClock()
    async with build_client(SOURCE, contract_checked_transport(SOURCE, recording)) as http:
        client = HubSpotClient(
            http,
            SecretStr(TOKEN),
            live=False,
            retry=RetryPolicy(sleep=no_sleep),
            limiter=fake_limiter(clock),
        )
        bindings = await client.verify_bindings(thresholds.mapping, get_settings())
        assert bindings.mql.available
        assert bindings.ql.available
        page = await client.search_page(
            "deals",
            {
                "filterGroups": [],
                "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
                "properties": list(DEAL_PROPERTIES),
                "limit": 5,
            },
        )
        deal_ids = [str(record["id"]) for record in page.results]
        assert deal_ids, "the default world holds deals"
        await client.search_page(
            "contacts",
            {
                "filterGroups": [],
                "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
                "properties": ["email"],
                "limit": 1,
            },
        )
        deal_contacts = await client.associations("deals", "contacts", deal_ids)
        contacts = await client.contacts_by_id(deal_contacts.all_targets)
        assert set(contacts.read_status.values()) == {"ok"}
        meetings = await client.associations("contacts", "meetings", list(contacts.records))
        contact_deals = await client.associations("contacts", "deals", list(contacts.records))
        await client.meetings(meetings.all_targets or ("0",))
        await client.deals(contact_deals.all_targets or tuple(deal_ids))
        await client.contacts_by_email(["nobody@example.com"])

    assert seen, "no request was sent"
    surfaces: dict[str, set[tuple[str, ...]]] = {}
    for request in seen:
        path = unquote(request.url.path)
        assert request.url.scheme == "https"
        assert request.url.host == HOST
        assert NUMBERED_PATH.search(path) is None, path
        assert f"/{HUBSPOT_API_VERSION}/" in path, path
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        surface = payloads.identify_surface(SOURCE, request)
        method, pattern = SURFACE_PATHS[surface]
        assert request.method == method, (surface, request.method)
        assert pattern.fullmatch(path) is not None, (surface, path)
        surfaces.setdefault(surface, set()).add(tuple(path.strip("/").split("/")[3:-2]))

    assert set(surfaces) == set(payloads.SURFACES[SOURCE])
    assert surfaces["objects_batch_read"] == {("contacts",), ("meetings",), ("deals",)}
    assert surfaces["associations_batch_read"] == {
        ("deals", "contacts"),
        ("contacts", "meetings"),
        ("contacts", "deals"),
    }
    searched = {unquote(r.url.path).split("/")[4] for r in seen if r.url.path.endswith("/search")}
    assert searched == {"deals", "contacts"}
    read_properties = {
        unquote(r.url.path).rsplit("/", 1)[1] for r in seen if "/crm/properties/" in r.url.path
    }
    assert "lifecyclestage" in read_properties

    for name in payloads.SURFACES[SOURCE]:
        loaded = contract(SOURCE, name)
        assert loaded.api_version == HUBSPOT_API_VERSION
        assert loaded.reference_url.startswith(
            "https://developers.hubspot.com/docs/api-reference/latest/crm/"
        ), name
        assert loaded.last_modified is not None
        assert re.fullmatch(r"2026-09-[0-9]{2}", loaded.last_modified), name
        assert f"/{HUBSPOT_API_VERSION}/" in loaded.request.url
    assert contract(SOURCE, "associations_batch_read").last_modified == "2026-09-09"


async def test_object_batch_reads_at_most_100(portal: FakePortal, hubspot: ClientOpener) -> None:
    """Paths and batching: objects batch reads carry at most 100 inputs per call.

    250 emails, 250 contact ids, 250 meeting ids and 250 deal ids each take three calls of
    100, 100 and 50 inputs. Only the email lookup carries ``"idProperty": "email"``.
    """
    emails = [f"lead{index:03d}@example.com" for index in range(250)]
    contact_ids = [str(50_000 + index) for index in range(250)]
    meeting_ids = [str(70_000 + index) for index in range(250)]
    deal_ids = [str(80_000 + index) for index in range(250)]
    for index, hs_id in enumerate(contact_ids):
        portal.add_object("contacts", hs_id, {"email": emails[index], "lifecyclestage": "lead"})
    for hs_id in meeting_ids:
        portal.add_object(
            "meetings", hs_id, {"hs_createdate": STAMP, "hs_meeting_source": "MEETINGS_PUBLIC"}
        )
    for hs_id in deal_ids:
        portal.add_object("deals", hs_id, {"createdate": STAMP, "amount": "1000"})

    async with hubspot() as client:
        lookup = await client.contacts_by_email(emails)
        email_calls = list(portal.calls)
        refreshed = await client.contacts_by_id(contact_ids)
        meetings = await client.meetings(meeting_ids)
        deals = await client.deals(deal_ids)

    expected = [MAX_BATCH_READ_INPUTS, MAX_BATCH_READ_INPUTS, 50]
    assert input_counts(email_calls) == expected
    assert all(call.body["idProperty"] == "email" for call in email_calls)
    assert all(call.segments == ("contacts", "batch", "read") for call in email_calls)
    by_id = portal.calls[len(email_calls) :]
    for object_type in ("contacts", "meetings", "deals"):
        calls = [call for call in by_id if call.segments[0] == object_type]
        assert input_counts(calls) == expected, object_type
        assert all("idProperty" not in call.body for call in calls), object_type
    assert all(count <= MAX_BATCH_READ_INPUTS for count in input_counts(portal.calls))

    assert set(lookup.found) == {email_sha256(email) for email in emails}
    assert lookup.not_found == frozenset()
    assert lookup.failed == frozenset()
    assert lookup.skipped == frozenset()
    assert set(refreshed.read_status.values()) == {"ok"}
    assert set(refreshed.records) == set(contact_ids)
    assert (meetings.status, deals.status) == ("ok", "ok")
    assert set(meetings.records) == set(meeting_ids)
    assert set(deals.records) == set(deal_ids)


async def test_association_batches_at_most_1000(portal: FakePortal, hubspot: ClientOpener) -> None:
    """Paths and batching: associations batch reads carry at most 1,000 inputs per call.

    2,500 contact ids take three initial calls of 1,000, 1,000 and 500 inputs; no input is
    paged, so no continuation follows, and every input reads ``ok``.
    """
    contact_ids = [str(100_000 + index) for index in range(2_500)]
    for index, hs_id in enumerate(contact_ids):
        portal.link("contacts", "meetings", hs_id, [str(300_000 + index)])

    async with hubspot() as client:
        fetch = await client.associations("contacts", "meetings", contact_ids)

    assert input_counts(portal.calls) == [MAX_ASSOCIATION_INPUTS, MAX_ASSOCIATION_INPUTS, 500]
    assert all(call.segments == ("contacts", "meetings", "batch", "read") for call in portal.calls)
    assert all(set(entry) == {"id"} for call in portal.calls for entry in call.body["inputs"])
    sent = [entry["id"] for call in portal.calls for entry in call.body["inputs"]]
    assert sent == contact_ids
    assert list(fetch.per_input) == contact_ids
    assert fetch.status_counts() == {"ok": 2_500}
    assert len(fetch.all_targets) == 2_500


# ---------------------------------------------------------------------------------------------
# Phase 2: associations contract (AAP 0.2.4 exact contract; AAP 0.3.3 "Associations")
# ---------------------------------------------------------------------------------------------


async def test_initial_body_is_ids_only(portal: FakePortal, hubspot: ClientOpener) -> None:
    """Associations contract: the initial request body is exactly ``{"inputs":[{"id"}, …]}``.

    No cursor, no other key; an unpaged input is never re-sent, so one call settles all three
    inputs (``ok``, the genuine zero ``ok_empty``, ``ok``).
    """
    portal.link("contacts", "meetings", "301", ["901"])
    portal.link("contacts", "meetings", "303", ["902", "903"])

    async with hubspot() as client:
        fetch = await client.associations("contacts", "meetings", ["301", "302", "303"])

    (call,) = portal.calls
    assert call.path == "/crm/associations/2026-09/contacts/meetings/batch/read"
    assert call.body == {"inputs": [{"id": "301"}, {"id": "302"}, {"id": "303"}]}
    assert json.loads(call.request.content) == call.body
    assert {key: read.status for key, read in fetch.per_input.items()} == {
        "301": "ok",
        "302": "ok_empty",
        "303": "ok",
    }
    assert fetch.per_input["303"].targets == ("902", "903")
    assert fetch.all_targets == ("901", "902", "903")


async def test_continuation_resends_id_and_after_only_for_paged_inputs(
    portal: FakePortal, hubspot: ClientOpener
) -> None:
    """Associations contract: a continuation re-sends ``{id, after}`` only for paged inputs.

    Input A (``401``) has five meetings at two per page and B (``402``) one. The first call
    sends both ids; only A returns ``paging.next.after``, so the second call is exactly
    ``{"inputs":[{"id":"401","after":"c1"}]}`` and the third ``…"after":"c2"…``, each cursor
    taken from the previous reply. Paging stops when no paging object comes back; A's targets
    from all pages are merged in order and A ends ``ok``.
    """
    portal.association_page_size = 2
    portal.link("contacts", "meetings", "401", ["9001", "9002", "9003", "9004", "9005"])
    portal.link("contacts", "meetings", "402", ["9101"])

    async with hubspot() as client:
        fetch = await client.associations("contacts", "meetings", ["401", "402"])

    assert [call.body for call in portal.calls] == [
        {"inputs": [{"id": "401"}, {"id": "402"}]},
        {"inputs": [{"id": "401", "after": "c1"}]},
        {"inputs": [{"id": "401", "after": "c2"}]},
    ]
    for (_status, reply), call in zip(portal.replies[:-1], portal.calls[1:], strict=True):
        paged = [result for result in reply["results"] if "paging" in result]
        assert [result["from"]["id"] for result in paged] == ["401"]
        assert call.body["inputs"][0]["after"] == paged[0]["paging"]["next"]["after"]
    assert all("paging" not in result for result in portal.replies[-1][1]["results"])

    first = fetch.per_input["401"]
    assert first.status == "ok"
    assert first.targets == ("9001", "9002", "9003", "9004", "9005")
    assert first.error is None
    assert fetch.per_input["402"].status == "ok"
    assert fetch.per_input["402"].targets == ("9101",)


async def test_207_errors_mark_inputs_error(portal: FakePortal, hubspot: ClientOpener) -> None:
    """Associations contract: an input named by a 207 ``errors[]`` entry reads ``error``.

    C (``503``) is named through ``context.fromObjectId`` with a validation error and D
    (``504``) with a bare ``OBJECT_NOT_FOUND`` (no "no associations" subcategory): both are
    ``error`` with the category as the stored reason and no targets; A (``501``) is ``ok``.
    """
    portal.link("contacts", "deals", "501", ["9201"])
    portal.link("contacts", "deals", "503", ["9202"])
    portal.association_errors["503"] = ("VALIDATION_ERROR", None)
    portal.association_errors["504"] = ("OBJECT_NOT_FOUND", None)

    async with hubspot() as client:
        fetch = await client.associations("contacts", "deals", ["501", "503", "504"])

    (call,) = portal.calls
    status, reply = portal.replies[0]
    assert status == 207
    assert reply["numErrors"] == 2
    assert [entry["context"]["fromObjectId"] for entry in reply["errors"]] == [["503"], ["504"]]
    assert call.body == {"inputs": [{"id": "501"}, {"id": "503"}, {"id": "504"}]}

    assert fetch.per_input["501"].status == "ok"
    assert fetch.per_input["501"].targets == ("9201",)
    for input_id, label in (("503", "VALIDATION_ERROR"), ("504", "OBJECT_NOT_FOUND")):
        read = fetch.per_input[input_id]
        assert (read.status, read.targets, read.error) == ("error", (), label), input_id
        assert read.is_known is False
    assert fetch.all_targets == ("9201",)


async def test_no_associations_found_is_ok_empty(portal: FakePortal, hubspot: ClientOpener) -> None:
    """Associations contract: HubSpot's ``NO_ASSOCIATIONS_FOUND`` 207 entry is ``ok_empty``.

    Flagged plan correction (connectors plan): AAP 0.3.3 reads every 207 ``errors[]`` input as
    ``error``, but HubSpot reports an input that simply has no associations exactly that way
    (``OBJECT_NOT_FOUND`` / ``crm.associations.NO_ASSOCIATIONS_FOUND``). It must be the genuine
    zero, known evidence, or W2 and C1b would be withheld forever for every contact without
    meetings or deals.
    """
    portal.link("contacts", "meetings", "602", ["9301"])

    async with hubspot() as client:
        fetch = await client.associations("contacts", "meetings", ["601", "602"])

    status, reply = portal.replies[0]
    assert status == 207
    (entry,) = reply["errors"]
    assert entry["category"] == "OBJECT_NOT_FOUND"
    assert entry["subCategory"] == NO_ASSOCIATIONS_FOUND == "crm.associations.NO_ASSOCIATIONS_FOUND"
    assert entry["context"] == {"fromObjectId": ["601"]}

    empty = fetch.per_input["601"]
    assert (empty.status, empty.targets, empty.error) == ("ok_empty", (), None)
    assert empty.is_known is True
    assert "ok_empty" in ASSOCIATION_FETCH_STATUSES
    assert fetch.per_input["602"].status == "ok"
    assert len(portal.calls) == 1


@pytest.mark.parametrize("batch_status", ["CANCELED", "PENDING", "PROCESSING"])
async def test_status_not_complete_fails_call(
    portal: FakePortal, hubspot: ClientOpener, batch_status: str
) -> None:
    """Associations contract: a batch ``status`` other than ``COMPLETE`` fails the whole call.

    Every input of that call reads ``error`` with the status as its reason, even though the
    body is valid and lists results; nothing from it is read and no continuation follows.
    """
    portal.link("contacts", "meetings", "701", ["9401"])
    portal.association_page_size = 1
    portal.link("contacts", "meetings", "702", ["9402", "9403"])

    def unfinished(call: Call) -> httpx.Response | None:
        if call.surface != "associations_batch_read":
            return None
        result = {
            "from": {"id": "701"},
            "to": [
                {
                    "toObjectId": 9401,
                    "associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": 200}],
                }
            ],
        }
        return httpx.Response(200, json=batch_envelope([result], status=batch_status))

    portal.overrides.append(unfinished)
    async with hubspot() as client:
        fetch = await client.associations("contacts", "meetings", ["701", "702"])

    assert len(portal.calls) == 1
    for input_id in ("701", "702"):
        read = fetch.per_input[input_id]
        assert (read.status, read.targets) == ("error", ()), input_id
        assert read.error is not None
        assert f"batch status {batch_status}" in read.error
    assert fetch.all_targets == ()


@pytest.mark.parametrize("failure", ["http_500", "processing"])
async def test_failed_continuation_is_partial(
    portal: FakePortal, hubspot: ClientOpener, failure: str
) -> None:
    """Associations contract: a continuation that fails after page 1 leaves the input ``partial``.

    A (``801``) is paged; its continuation either answers HTTP 500 on all three attempts
    (retried with recorded, never slept, backoff) or returns a ``PROCESSING`` batch. A keeps
    the targets of page 1 but is ``partial`` (unknown evidence); B (``802``), unpaged, is
    ``ok``.
    """
    portal.association_page_size = 2
    portal.link("contacts", "deals", "801", ["9501", "9502", "9503"])
    portal.link("contacts", "deals", "802", ["9601"])

    def failing_continuation(call: Call) -> httpx.Response | None:
        if call.surface != "associations_batch_read":
            return None
        if not any("after" in entry for entry in call.body["inputs"]):
            return None
        if failure == "http_500":
            return platform_error(500, "internal error", "INTERNAL_ERROR")
        return httpx.Response(200, json=batch_envelope([], status="PROCESSING"))

    portal.overrides.append(failing_continuation)
    async with hubspot() as client:
        fetch = await client.associations("contacts", "deals", ["801", "802"])

    continuations = [call for call in portal.calls if "after" in call.body["inputs"][0]]
    assert all(call.body == {"inputs": [{"id": "801", "after": "c1"}]} for call in continuations)
    if failure == "http_500":
        assert len(continuations) == 3
        assert len(portal.retry_waits) == 2
    else:
        assert len(continuations) == 1
        assert portal.retry_waits == []

    paged = fetch.per_input["801"]
    assert paged.status == "partial"
    assert paged.targets == ("9501", "9502")
    assert paged.error
    assert paged.is_known is False
    assert fetch.per_input["802"].status == "ok"


async def test_documented_sample_parses_verbatim(
    portal: FakePortal, hubspot: ClientOpener, contract: Callable[..., payloads.Contract]
) -> None:
    """Associations contract: the reference page's documented sample, served verbatim.

    The sample is HubSpot's rendered 207 example with status ``CANCELED``: through the client
    it is a failed call (every input ``error``). The parser leaves it unchanged, decodes it as
    a failed call, and its own ``results`` inside a ``COMPLETE`` envelope give the documented
    ``from.id`` → ``toObjectId`` pairs and cursor.
    """
    documented = contract(SOURCE, "associations_batch_read").response.documented_sample
    assert isinstance(documented, dict)
    sample: dict[str, Any] = documented
    pristine = copy.deepcopy(sample)
    portal.overrides.append(lambda call: httpx.Response(207, json=sample))

    async with hubspot() as client:
        fetch = await client.associations("contacts", "meetings", ["111", "112"])

    assert portal.replies == [(207, pristine)]
    for read in fetch.per_input.values():
        assert read.status == "error"
        assert read.error is not None
        assert "batch status CANCELED" in read.error

    decoded = parse_association_batch(sample)
    assert sample == pristine
    assert decoded.status == "CANCELED"
    assert decoded.is_complete is False
    assert dict(decoded.results) == {}
    assert decoded.empty_ids == frozenset()

    complete = {**copy.deepcopy(sample), "status": "COMPLETE", "numErrors": len(sample["errors"])}
    batch = parse_association_batch(complete)
    pairs = {
        result["from"]["id"]: tuple(str(target["toObjectId"]) for target in result["to"])
        for result in sample["results"]
    }
    assert {key: result.targets for key, result in batch.results.items()} == pairs
    assert dict(batch.results) == {
        "<string>": AssociationResult(from_id="<string>", targets=("<string>",), after="<string>")
    }
    # The sample's error entry has an empty context, so it names no input.
    assert dict(batch.error_ids) == {}
    assert batch.empty_ids == frozenset()


# ---------------------------------------------------------------------------------------------
# Phase 3: target and contact reads (AAP 0.3.3 "Target reads", "Contact refresh")
# ---------------------------------------------------------------------------------------------

TARGET_READS: Final = (
    pytest.param(
        "meetings",
        (
            "hs_createdate",
            "hs_timestamp",
            "hs_meeting_start_time",
            "hs_meeting_outcome",
            "hs_meeting_source",
        ),
        id="meetings",
    ),
    pytest.param("deals", ("createdate", "dealstage", "pipeline", "amount"), id="deals"),
)


@pytest.mark.parametrize(("object_type", "properties"), TARGET_READS)
@pytest.mark.parametrize("report_missing", [False, True], ids=["omitted", "listed_in_errors"])
async def test_target_read_omitting_an_id_is_partial(
    portal: FakePortal,
    hubspot: ClientOpener,
    object_type: str,
    properties: tuple[str, ...],
    report_missing: bool,
) -> None:
    """Target reads: an associated meeting or deal the batch read does not return is unread.

    ``[x1, x2, x3]`` with ``x2`` absent (omitted from the 200, or listed in a 207 ``errors[]``)
    is ``partial`` with ``missing_ids == {x2}``; nothing read is ``error``; everything read is
    ``ok``. An unread target is never absent. The requested properties are exactly the ones
    ``crm_meeting`` and ``crm_deal`` store (AAP 0.3.3).
    """
    prefix = object_type[0]
    portal.report_missing = report_missing
    for suffix in ("1", "3"):
        portal.add_object(
            object_type,
            f"{prefix}{suffix}",
            {
                "hs_createdate": STAMP,
                "hs_timestamp": STAMP,
                "hs_meeting_start_time": STAMP,
                "hs_meeting_outcome": "COMPLETED",
                "hs_meeting_source": "MEETINGS_PUBLIC",
                "createdate": STAMP,
                "dealstage": "closedwon",
                "pipeline": "default",
                "amount": "1500.50",
            },
        )
    async with hubspot() as client:
        reader = client.meetings if object_type == "meetings" else client.deals
        partial = await reader([f"{prefix}1", f"{prefix}2", f"{prefix}3"])
        none_read = await reader([f"{prefix}8", f"{prefix}9"])
        all_read = await reader([f"{prefix}1", f"{prefix}3"])

    assert partial.status == "partial"
    assert partial.missing_ids == {f"{prefix}2"}
    assert set(partial.records) == {f"{prefix}1", f"{prefix}3"}
    assert none_read.status == "error"
    assert none_read.missing_ids == {f"{prefix}8", f"{prefix}9"}
    assert dict(none_read.records) == {}
    assert (all_read.status, all_read.missing_ids) == ("ok", frozenset())

    expected = MEETING_PROPERTIES if object_type == "meetings" else DEAL_PROPERTIES
    assert tuple(properties) == tuple(expected)
    for call in portal.calls:
        assert call.segments == (object_type, "batch", "read")
        assert call.body["properties"] == list(properties)
        assert "idProperty" not in call.body
    status, reply = portal.replies[0]
    assert status == (207 if report_missing else 200)
    assert ("errors" in reply) is report_missing


async def test_contact_refresh_missing_contact_is_error(
    portal: FakePortal, hubspot: ClientOpener
) -> None:
    """Target reads: a contact refresh that does not return a contact reads ``error``.

    ``contacts_by_id(["c1", "c2"])`` with only ``c1`` in the portal gives
    ``{"c1": "ok", "c2": "error"}`` (``lead_match.contact_read_status``), whether ``c2`` is
    omitted or listed; a call still failing after its retries makes every id ``error`` and
    reads nothing, so previous stage values are kept as unknown, never zeroed.
    """
    portal.add_object(
        "contacts",
        "c1",
        {
            "email": "c1@example.com",
            "lifecyclestage": FMB_STAGE_ID,
            MQL_PROPERTY: "2026-09-10T10:00:00.000Z",
            QL_PROPERTY: "",
        },
    )

    async with hubspot() as client:
        listed = await client.contacts_by_id(["c1", "c2"])
        portal.report_missing = False
        omitted = await client.contacts_by_id(["c1", "c2"])
        portal.overrides.append(
            lambda call: platform_error(503, "service unavailable", "INTERNAL_ERROR")
        )
        failed = await client.contacts_by_id(["c1", "c2"])

    for batch in (listed, omitted):
        assert dict(batch.read_status) == {"c1": "ok", "c2": "error"}
        assert set(batch.records) == {"c1"}
        row = batch.records["c1"]
        assert row.email_sha256 == email_sha256("c1@example.com")
        assert row.mql_at == datetime(2026, 9, 10, 10, tzinfo=UTC)
        assert row.ql_at is None
    assert dict(failed.read_status) == {"c1": "error", "c2": "error"}
    assert dict(failed.records) == {}
    assert len(portal.retry_waits) == 2
    for call in portal.calls:
        assert call.body["properties"] == list(PROPS.request_properties())
        assert MQL_PROPERTY in call.body["properties"]
        assert QL_PROPERTY in call.body["properties"]


async def test_contacts_by_email_not_found_vs_failed(
    portal: FakePortal, hubspot: ClientOpener
) -> None:
    """Target reads: ``OBJECT_NOT_FOUND`` is ``not_found``; other failures are ``failed``.

    A 207 ``OBJECT_NOT_FOUND`` entry naming an address puts it in ``not_found``
    (``lead_match`` ``no_match``, retried later); another error category puts it in ``failed``
    (``error``); a call failing after its retries fails every address of the call. Results are
    keyed by the SHA-256 of the normalised address, never the plaintext.
    """
    portal.add_object("contacts", "9001", {"email": "found@example.com", "lifecyclestage": "lead"})
    portal.email_errors["broken@example.com"] = "VALIDATION_ERROR"
    emails = ["  Found@Example.COM ", "missing@example.com", "broken@example.com"]

    async with hubspot() as client:
        lookup = await client.contacts_by_email(emails)
        portal.overrides.append(
            lambda call: platform_error(500, "internal error", "INTERNAL_ERROR")
        )
        failed = await client.contacts_by_email(emails)

    found, missing, broken = (email_sha256(email) for email in emails)
    assert set(lookup.found) == {found}
    assert lookup.found[found].hs_id == "9001"
    assert lookup.not_found == {missing}
    assert lookup.failed == {broken}
    assert lookup.skipped == frozenset()
    status, reply = portal.replies[0]
    assert status == 207
    assert sorted(entry["category"] for entry in reply["errors"]) == [
        "OBJECT_NOT_FOUND",
        "VALIDATION_ERROR",
    ]
    assert portal.calls[0].body["inputs"] == [
        {"id": "found@example.com"},
        {"id": "missing@example.com"},
        {"id": "broken@example.com"},
    ]
    assert dict(failed.found) == {}
    assert failed.failed == {found, missing, broken}
    assert failed.not_found == frozenset()
    assert all(email not in repr(lookup) for email in emails)


async def test_sim_invalid_never_sent_to_live_portal(
    portal: FakePortal, hubspot: ClientOpener
) -> None:
    """Target reads: a live client never sends a ``@sim.invalid`` address to the portal.

    With ``live=True`` the simulated address is withheld into ``skipped`` and no request body
    mentions ``sim.invalid``, while the real address is looked up; alone it causes no request
    at all. A simulated portal (``live=False``) receives it and can match it.
    """
    simulated = f"x@{SIM_EMAIL_DOMAIN}"
    assert SIM_EMAIL_DOMAIN == "sim.invalid"
    portal.add_object("contacts", "9101", {"email": "real@example.com", "lifecyclestage": "lead"})
    portal.add_object("contacts", "9102", {"email": simulated, "lifecyclestage": "lead"})

    async with hubspot(live=True) as client:
        mixed = await client.contacts_by_email([simulated, "real@example.com"])
        calls_after_mixed = len(portal.calls)
        alone = await client.contacts_by_email([simulated])

    assert calls_after_mixed == 1
    assert len(portal.calls) == 1, "a lone simulated address must cause no request"
    assert all(SIM_EMAIL_DOMAIN not in call.request.content.decode() for call in portal.calls)
    assert portal.calls[0].body["inputs"] == [{"id": "real@example.com"}]
    assert mixed.skipped == {email_sha256(simulated)}
    assert set(mixed.found) == {email_sha256("real@example.com")}
    assert alone.skipped == {email_sha256(simulated)}
    assert dict(alone.found) == {}
    assert alone.not_found == frozenset()
    assert alone.failed == frozenset()

    async with hubspot(live=False) as client:
        offline = await client.contacts_by_email([simulated])

    (sent,) = portal.calls[1:]
    assert sent.body["inputs"] == [{"id": simulated}]
    assert set(offline.found) == {email_sha256(simulated)}
    assert offline.skipped == frozenset()


# ---------------------------------------------------------------------------------------------
# Phase 4: property verification (AAP 0.3.3 "Startup property verification"; AAP 0.5.4.7)
# ---------------------------------------------------------------------------------------------


def property_reads(portal: FakePortal) -> list[str]:
    """The property names the client read, in order."""
    return [call.segments[1] for call in portal.calls if call.surface == "properties"]


async def test_verify_bindings_resolves_stage_labels(
    portal: FakePortal, hubspot: ClientOpener, thresholds: Thresholds
) -> None:
    """Target reads (stage binding): configured labels resolve to the portal's stage ids.

    ``config/hubspot_mapping.yaml`` binds MQL to "First meeting booked" and QL to "Qualified
    Lead" then "Sales Qualified Lead". Against a portal whose custom stages have numeric ids,
    MQL resolves to ``hs_v2_date_entered_1406943076`` once the properties API confirms that
    property, QL to the first matching label's ``hs_v2_date_entered_1406943077`` (never the
    later default stage), ``lifecyclestage`` is read once, and ``verified_at`` is tz-aware.
    """
    mapping = thresholds.mapping
    assert mapping.mql.stage_labels == ("First meeting booked",)
    assert mapping.ql.stage_labels == ("Qualified Lead", "Sales Qualified Lead")

    async with hubspot(contact_properties=None) as client:
        bindings = await client.verify_bindings(mapping, get_settings())
        bound = client.contact_properties

    assert bindings.mql.available is True
    assert (bindings.mql.property, bindings.mql.stage_id, bindings.mql.label) == (
        MQL_PROPERTY,
        FMB_STAGE_ID,
        "First meeting booked",
    )
    assert bindings.ql.available is True
    assert (bindings.ql.property, bindings.ql.stage_id, bindings.ql.label) == (
        QL_PROPERTY,
        QL_STAGE_ID,
        "Qualified Lead",
    )
    assert bindings.google.available is False
    assert bindings.google.reason is not None
    assert "not configured" in bindings.google.reason
    assert bindings.verified_at.tzinfo is not None
    assert bindings.verified_at.utcoffset() == timedelta(0)

    reads = property_reads(portal)
    assert reads.count("lifecyclestage") == 1
    assert reads == ["lifecyclestage", MQL_PROPERTY, QL_PROPERTY]
    assert f"{STAGE_DATE_PROPERTY_PREFIX}salesqualifiedlead" not in reads
    assert all(call.method == "GET" and call.body is None for call in portal.calls)
    assert bound == ContactPropertyMap(mql_property=MQL_PROPERTY, ql_property=QL_PROPERTY)


async def test_unresolved_binding_is_unavailable(
    portal: FakePortal, hubspot: ClientOpener, thresholds: Thresholds
) -> None:
    """Target reads (stage binding): an unresolved stage is unavailable, never zero.

    Without a "First meeting booked" option, MQL is unavailable with a reason naming the
    stage and no property, and the portal's default "Marketing Qualified Lead" is never read
    in its place. QL still resolves on its own. A label that resolves but whose "date
    entered" property is missing (404) is unavailable too, naming the property.
    """
    portal.add_lifecycle((*DEFAULT_STAGES, (QL_STAGE_ID, "Qualified Lead")))

    async with hubspot(contact_properties=None) as client:
        bindings = await client.verify_bindings(thresholds.mapping, get_settings())
        bound = client.contact_properties

    assert bindings.mql.available is False
    assert bindings.mql.property is None
    assert bindings.mql.reason is not None
    assert "First meeting booked" in bindings.mql.reason
    assert f"{STAGE_DATE_PROPERTY_PREFIX}marketingqualifiedlead" not in property_reads(portal)
    assert bindings.ql.available is True
    assert bindings.ql.property == QL_PROPERTY
    assert bound == ContactPropertyMap(mql_property=None, ql_property=QL_PROPERTY)
    assert bound is not None
    assert MQL_PROPERTY not in bound.request_properties()

    portal.add_standard_portal()
    del portal.contact_properties[MQL_PROPERTY]
    portal.calls.clear()
    async with hubspot(contact_properties=None) as client:
        missing_date = await client.verify_bindings(thresholds.mapping, get_settings())

    assert missing_date.mql.available is False
    assert missing_date.mql.stage_id == FMB_STAGE_ID
    assert missing_date.mql.reason is not None
    assert MQL_PROPERTY in missing_date.mql.reason
    assert MQL_PROPERTY in property_reads(portal)
    assert missing_date.ql.available is True


@pytest.mark.parametrize(
    ("data_type", "available", "reason_part"),
    [
        pytest.param("datetime", True, None, id="datetime"),
        pytest.param("date", True, None, id="date"),
        pytest.param(None, False, "not found on contacts", id="missing_404"),
        pytest.param("string", False, "not a date or datetime", id="not_a_date"),
    ],
)
async def test_property_mode_binding(
    portal: FakePortal,
    hubspot: ClientOpener,
    make_thresholds: Callable[..., Thresholds],
    data_type: str | None,
    available: bool,
    reason_part: str | None,
) -> None:
    """Target reads (stage binding): ``mode: property`` confirms the named property exists.

    With ``mql: {mode: property, property: date_first_meeting_was_booked}`` the properties API
    must confirm a date or datetime property of that name; a 404 or another type leaves MQL
    unavailable with a reason, and no ``lifecyclestage`` option stands in. QL resolves
    independently.
    """
    configured = make_thresholds({"mql.mode": "property", "mql.property": PROPERTY_MODE_MQL})
    assert configured.mapping.mql.mode == "property"
    assert configured.mapping.mql.property == PROPERTY_MODE_MQL
    if data_type is not None:
        portal.add_property(PROPERTY_MODE_MQL, data_type)

    async with hubspot(contact_properties=None) as client:
        bindings = await client.verify_bindings(configured.mapping, get_settings())

    assert property_reads(portal)[0] == PROPERTY_MODE_MQL
    assert bindings.mql.available is available
    if available:
        assert bindings.mql.property == PROPERTY_MODE_MQL
        assert bindings.mql.reason is None
    else:
        assert bindings.mql.property is None
        assert bindings.mql.reason is not None
        assert PROPERTY_MODE_MQL in bindings.mql.reason
        assert reason_part is not None
        assert reason_part in bindings.mql.reason
    assert bindings.mql.stage_id is None
    assert bindings.ql.available is True
    assert bindings.ql.property == QL_PROPERTY


# ---------------------------------------------------------------------------------------------
# Phase 5: CRM Search (AAP 0.3.3 "Deal search", "Google-to-CRM link"; AAP 0.2.4 limits)
# ---------------------------------------------------------------------------------------------


def hour_ms(hours: int) -> int:
    """Epoch milliseconds of ``DAY0`` plus ``hours``."""
    return ms(DAY0 + hours * HOUR)


def first_pages(calls: Sequence[Call]) -> list[tuple[int, int]]:
    """The partitions searched, in order: the bounds of every page request without ``after``."""
    return [partition_of(call.body) for call in calls if "after" not in call.body]


async def test_day_over_10000_splits_to_hours(portal: FakePortal, hubspot: ClientOpener) -> None:
    """Search overflow: a day whose ``total`` exceeds 10,000 is split down to hourly partitions.

    12,000 deals fall in the first two hours of one day (6,000 each). The day reports
    ``total: 12000`` and is halved at whole hours (24 → 12 → 6 → 3 → 1 + 2) until each
    partition holds at most 10,000, so the first hour is read as a one-hour partition. Every
    partition lies inside the day, every deal is returned exactly once and the outcome is
    ``complete``.
    """
    step = timedelta(milliseconds=600)
    first = index_deals(portal, DAY0, 6_000, step, 1_000_000)
    second = index_deals(portal, DAY0 + HOUR, 6_000, step, 2_000_000)

    async with hubspot() as client:
        outcome = await client.search_partitioned("deals", deal_search_filter(), (DAY0, DAY0 + DAY))

    calls = portal.calls_to("crm_search", "deals")
    assert first_pages(calls) == [
        (hour_ms(0), hour_ms(24)),
        (hour_ms(0), hour_ms(12)),
        (hour_ms(0), hour_ms(6)),
        (hour_ms(0), hour_ms(3)),
        (hour_ms(0), hour_ms(1)),
        (hour_ms(1), hour_ms(3)),
        (hour_ms(3), hour_ms(6)),
        (hour_ms(6), hour_ms(12)),
        (hour_ms(12), hour_ms(24)),
    ]
    assert all(
        hour_ms(0) <= lo < hi <= hour_ms(24)
        for lo, hi in map(partition_of, (c.body for c in calls))
    )
    totals = [reply["total"] for _status, reply in portal.replies]
    assert totals[:4] == [12_000, 12_000, 12_000, 12_000]
    assert max(totals[4:]) <= SEARCH_RESULT_CEILING
    hourly = [call for call in calls if partition_of(call.body) == (hour_ms(0), hour_ms(1))]
    assert len(hourly) == 30

    ids = [str(record["id"]) for record in outcome.rows]
    assert ids == first + second
    assert len(set(ids)) == 12_000
    assert outcome.completeness == "complete"
    assert outcome.is_complete is True
    assert outcome.reason is None
    assert outcome.partitions_incomplete == ()
    assert outcome.calls == len(calls) == 4 + 30 + 30 + 3


async def test_hour_over_10000_read_to_ceiling_and_incomplete(
    portal: FakePortal, hubspot: ClientOpener
) -> None:
    """Search overflow: a one-hour partition above 10,000 is read to the ceiling, incomplete.

    10,500 deals fall in one hour. That hour cannot be split further, so it is paged to
    exactly 10,000 rows (50 pages of 200, cursors ``200`` to ``9800``) and no request ever asks
    for a page past the 10,000-result ceiling. The outcome is ``incomplete`` with the ceiling
    reason and lists that hour in ``partitions_incomplete``.
    """
    ids = index_deals(portal, DAY0, 10_500, timedelta(milliseconds=300), 3_000_000)

    async with hubspot() as client:
        outcome = await client.search_partitioned("deals", deal_search_filter(), (DAY0, DAY0 + DAY))

    calls = portal.calls_to("crm_search", "deals")
    hour = [call for call in calls if partition_of(call.body) == (hour_ms(0), hour_ms(1))]
    assert len(hour) == 50
    assert [call.body.get("after") for call in hour] == [None] + [
        str(200 * page) for page in range(1, 50)
    ]
    assert all(call.body["limit"] == MAX_SEARCH_LIMIT for call in hour)
    assert all(int(call.body.get("after", "0")) < SEARCH_RESULT_CEILING for call in calls)
    hour_replies = [
        reply
        for call, (_status, reply) in zip(calls, portal.replies, strict=True)
        if partition_of(call.body) == (hour_ms(0), hour_ms(1))
    ]
    assert {reply["total"] for reply in hour_replies} == {10_500}
    assert hour_replies[-1]["paging"]["next"]["after"] == str(SEARCH_RESULT_CEILING)

    rows = [str(record["id"]) for record in outcome.rows]
    assert rows == ids[:SEARCH_RESULT_CEILING]
    assert outcome.completeness == "incomplete"
    assert outcome.reason == CEILING_REASON
    assert outcome.partitions_incomplete == ((DAY0, DAY0 + HOUR),)
    assert outcome.calls == len(calls) == 4 + 50 + 4


async def test_search_bodies_shape(portal: FakePortal, hubspot: ClientOpener) -> None:
    """Search overflow: every CRM Search body has the contract's shape and limits.

    Each body serialises to fewer than 3,000 characters, asks for ``limit`` 200, filters
    ``createdate`` ``GTE``/``LT`` with epoch-millisecond strings of its partition, sorts by
    ``createdate`` ascending, requests the deal properties ``crm_deal`` stores, and pages with
    ``after`` set to the previous reply's ``paging.next.after``.
    """
    day_one = index_deals(portal, DAY0, 450, timedelta(minutes=3), 4_000_000)
    day_two = index_deals(portal, DAY0 + DAY, 30, timedelta(minutes=40), 5_000_000)

    async with hubspot() as client:
        outcome = await client.search_partitioned(
            "deals", deal_search_filter(), (DAY0, DAY0 + 2 * DAY)
        )

    calls = portal.calls_to("crm_search", "deals")
    assert len(calls) == 4
    partitions = [(DAY0, DAY0 + DAY)] * 3 + [(DAY0 + DAY, DAY0 + 2 * DAY)]
    previous_after: str | None = None
    for index, (call, (lo, hi)) in enumerate(zip(calls, partitions, strict=True)):
        assert call.method == "POST"
        assert call.path == "/crm/objects/2026-09/deals/search"
        assert len(call.request.content) < MAX_SEARCH_BODY_CHARS
        assert len(json.dumps(call.body, separators=(",", ":"))) < MAX_SEARCH_BODY_CHARS
        assert set(call.body) <= {"filterGroups", "sorts", "properties", "limit", "after"}
        assert call.body["limit"] == MAX_SEARCH_LIMIT == 200
        assert call.body["filterGroups"] == [
            {
                "filters": [
                    {"propertyName": "createdate", "operator": "GTE", "value": epoch_ms(lo)},
                    {"propertyName": "createdate", "operator": "LT", "value": epoch_ms(hi)},
                ]
            }
        ]
        assert all(item["value"].isdigit() for item in call.body["filterGroups"][0]["filters"])
        assert call.body["sorts"] == [{"propertyName": "createdate", "direction": "ASCENDING"}]
        assert call.body["properties"] == list(DEAL_PROPERTIES)
        new_partition = index == 0 or partitions[index - 1] != (lo, hi)
        if new_partition:
            assert "after" not in call.body
        else:
            assert call.body["after"] == previous_after
        reply = portal.replies[index][1]
        previous_after = reply.get("paging", {}).get("next", {}).get("after")

    assert [len(reply["results"]) for _status, reply in portal.replies] == [200, 200, 50, 30]
    assert [str(record["id"]) for record in outcome.rows] == day_one + day_two
    assert outcome.completeness == "complete"


GOOGLE_PROPERTIES: Final = ((CAMPAIGN_PROPERTY, "string"), (TOUCH_PROPERTY, "datetime"))
BOTH_CONFIGURED: Final = {
    "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY": CAMPAIGN_PROPERTY,
    "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY": TOUCH_PROPERTY,
}


@pytest.mark.parametrize(
    ("configured", "portal_properties", "discovers"),
    [
        pytest.param({}, GOOGLE_PROPERTIES, False, id="neither_configured"),
        pytest.param(
            {"HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY": CAMPAIGN_PROPERTY},
            GOOGLE_PROPERTIES,
            False,
            id="campaign_property_only",
        ),
        pytest.param(
            {"HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY": TOUCH_PROPERTY},
            GOOGLE_PROPERTIES,
            False,
            id="touch_property_only",
        ),
        pytest.param(BOTH_CONFIGURED, GOOGLE_PROPERTIES, True, id="both_configured_and_verified"),
        pytest.param(
            BOTH_CONFIGURED,
            ((CAMPAIGN_PROPERTY, "string"),),
            False,
            id="touch_property_missing_404",
        ),
        pytest.param(
            BOTH_CONFIGURED,
            ((CAMPAIGN_PROPERTY, "string"), (TOUCH_PROPERTY, "date")),
            False,
            id="touch_property_date_only",
        ),
    ],
)
async def test_google_contact_discovery_only_when_configured_and_verified(
    db: async_sessionmaker[AsyncSession],
    portal: FakePortal,
    contract_checked_transport: TransportFactory,
    live_credentials: Callable[[str], Mapping[str, str]],
    set_env: Callable[..., None],
    thresholds: Thresholds,
    configured: Mapping[str, str],
    portal_properties: tuple[tuple[str, str], ...],
    discovers: bool,
) -> None:
    """Google contacts: discovery runs only when both properties are configured and verified.

    The real ``CrmSyncJob`` runs on a live HubSpot dataset of the test database, its client
    injected through ``SourceModeController(transports=...)``. With neither or only one of
    ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` and ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`` set, or
    with the touch-date property missing from the portal (404) or a ``date`` property rather
    than a ``datetime`` one (its midnight would anchor the causal check before a same-day
    touch), the Google link is unavailable and no ``contacts/search`` request is sent. With
    both set and confirmed, the job searches contacts by touch date with ``HAS_PROPERTY`` on
    the campaign property, stores the Google-only contact and writes
    ``hubspot.google_contacts`` freshness.
    """
    live_credentials(SOURCE)
    set_env(
        HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY=configured.get("HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY"),
        HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY=configured.get("HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY"),
    )
    for name, data_type in portal_properties:
        portal.add_property(name, data_type)
    touch = NOW - timedelta(days=3)
    portal.index(
        "contacts",
        "7001",
        {
            "email": "google.lead@example.com",
            "lifecyclestage": "lead",
            CAMPAIGN_PROPERTY: "123456789",
            TOUCH_PROPERTY: iso_ms(ms(touch)),
        },
    )
    # Touched but without a Google campaign: HAS_PROPERTY must exclude it.
    portal.index(
        "contacts",
        "7002",
        {
            "email": "organic@example.com",
            "lifecyclestage": "lead",
            TOUCH_PROPERTY: iso_ms(ms(touch)),
        },
    )
    settings = get_settings()
    controller = SourceModeController(
        settings,
        sessionmaker=db,
        clock=lambda: NOW,
        transports={SOURCE: contract_checked_transport(SOURCE, portal)},
        retry=RetryPolicy(sleep=portal.record_wait),
        hubspot_limiter=fake_limiter(portal.clock),
        thresholds=thresholds,
    )
    dataset_id = await controller.datasets.begin(SOURCE, "live", reason="integration test")
    async with controller.open_clients(SOURCE, "live") as clients:
        ctx = await JobContext.for_dataset(controller, dataset_id, clients, now=NOW)
        result = await CrmSyncJob(ctx).run()

    assert result.status == "succeeded", result.error
    contact_searches = portal.calls_to("crm_search", "contacts")
    assert portal.calls_to("crm_search", "deals"), "the deal search runs either way"
    async with db() as session:
        rows = await freshness.load_freshness(session, dataset_id)
        dataset = await session.get(SourceDataset, dataset_id)
        stored = list(
            await session.scalars(select(CrmContact).where(CrmContact.dataset_id == dataset_id))
        )
    assert dataset is not None
    assert dataset.crm_bindings is not None
    google = dataset.crm_bindings["google"]
    assert {"hubspot.contacts", "hubspot.associations", "hubspot.deal_search"} <= set(rows)

    if not discovers:
        assert contact_searches == []
        assert "hubspot.google_contacts" not in rows
        assert stored == []
        assert google["available"] is False
        assert google["reason"]
        if configured == BOTH_CONFIGURED:
            touch_type = dict(portal_properties).get(TOUCH_PROPERTY)
            problem = (
                "not found on contacts"
                if touch_type is None
                else f"has type '{touch_type}', not a datetime property"
            )
            assert google["reason"] == (
                f"HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY property '{TOUCH_PROPERTY}' {problem}"
            )
            assert TOUCH_PROPERTY in property_reads(portal)
        else:
            assert "not configured" in google["reason"]
        return

    assert google == {
        "available": True,
        "campaign_property": CAMPAIGN_PROPERTY,
        "touch_date_property": TOUCH_PROPERTY,
        "reason": None,
    }
    assert contact_searches
    for call in contact_searches:
        (group,) = call.body["filterGroups"]
        assert [(item["propertyName"], item["operator"]) for item in group["filters"]] == [
            (TOUCH_PROPERTY, "GTE"),
            (TOUCH_PROPERTY, "LT"),
            (CAMPAIGN_PROPERTY, "HAS_PROPERTY"),
        ]
        assert "value" not in group["filters"][2]
        assert call.body["sorts"] == [{"propertyName": TOUCH_PROPERTY, "direction": "ASCENDING"}]
        assert {CAMPAIGN_PROPERTY, TOUCH_PROPERTY} <= set(call.body["properties"])
        assert len(call.request.content) < MAX_SEARCH_BODY_CHARS
    spans = first_pages(contact_searches)
    assert spans[0][0] <= ms(touch) < spans[-1][1] == ms(NOW)
    assert all(left[1] == right[0] for left, right in itertools.pairwise(spans))

    discovered = rows["hubspot.google_contacts"]
    assert discovered.completeness == "complete"
    assert discovered.last_success_at is not None
    assert [contact.hs_id for contact in stored] == ["7001"]
    (contact,) = stored
    assert contact.google_campaign_ref == "123456789"
    assert contact.google_touch_at == touch
    assert contact.email_sha256 == email_sha256("google.lead@example.com")


# ---------------------------------------------------------------------------------------------
# Phase 6: throttle, retry and redaction (AAP 0.3.3 "Version and throttle"; AAP 0.3.2)
# ---------------------------------------------------------------------------------------------


def assert_throttled(starts: Sequence[float]) -> None:
    """Assert 4 req/s spacing: ≥ 0.25 s apart, never five starts within one second."""
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert min(gaps) >= 0.25 - 1e-9, gaps
    assert all(starts[index + 4] - starts[index] >= 1.0 for index in range(len(starts) - 4))
    assert starts[-1] - starts[0] >= 2.0


async def test_throttle_4_requests_per_second(
    portal: FakePortal,
    hubspot: ClientOpener,
    contract_checked_transport: TransportFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Throttle and redaction: HubSpot traffic is throttled to 4 requests per second.

    Nine requests through one ``RateLimiter(rate_per_second=4.0)`` on a fake monotonic clock
    need at least 2 s of fake time, at least 0.25 s apart and never five in one second. The
    module default limiter is one throttle for the whole process: two clients built without
    a limiter share it, so nine requests alternating between them are spaced the same way.
    """
    limiter = fake_limiter(portal.clock)
    assert limiter.rate_per_second == 4.0
    assert SHARED_LIMITER.rate_per_second == 4.0

    async with hubspot(limiter=limiter) as client:
        for _ in range(9):
            await client.property("contacts", "lifecyclestage")

    assert len(portal.calls) == 9
    assert_throttled([call.at for call in portal.calls])
    assert portal.clock.sleeps, "the limiter waited on the fake clock, never for real"

    shared = fake_limiter(portal.clock)
    monkeypatch.setattr("ads_engine.connectors.hubspot.client.SHARED_LIMITER", shared)
    async with build_client(SOURCE, contract_checked_transport(SOURCE, portal)) as http:
        clients = [
            HubSpotClient(http, SecretStr(TOKEN), live=True, retry=RetryPolicy(sleep=no_sleep))
            for _ in range(2)
        ]
        for index in range(9):
            await clients[index % 2].property("contacts", "lifecyclestage")

    alternating = portal.calls[9:]
    assert len(alternating) == 9
    assert_throttled([call.at for call in alternating])


def rate_limited(times: int) -> Override:
    """An override answering the first ``times`` requests with 429 and ``Retry-After: 2``."""
    answered = [0]

    def respond(call: Call) -> httpx.Response | None:
        del call
        if answered[0] >= times:
            return None
        answered[0] += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json={
                "status": "error",
                "message": "You have reached your secondly limit.",
                "category": "RATE_LIMITS",
            },
        )

    return respond


async def test_429_retried_with_retry_after(portal: FakePortal, hubspot: ClientOpener) -> None:
    """Throttle and redaction: a 429 is retried after the platform's ``Retry-After``.

    One 429 with ``Retry-After: 2`` then a 200: two attempts, the injected sleep receives at
    least 2 s and the second attempt is throttled again. Three 429s exhaust the attempts and
    raise ``TransientError`` from a property read; a batch read records the same exhaustion
    per input (``failed``) instead of raising.
    """
    portal.overrides.append(rate_limited(1))
    async with hubspot() as client:
        definition = await client.property("contacts", "lifecyclestage")

    assert definition is not None
    assert definition.name == "lifecyclestage"
    assert len(portal.calls) == 2
    assert portal.retry_waits == [2.0]
    assert portal.calls[1].at - portal.calls[0].at >= 0.25

    portal.calls.clear()
    portal.retry_waits.clear()
    portal.overrides[:] = [rate_limited(3)]
    async with hubspot() as client:
        with pytest.raises(TransientError) as raised:
            await client.property("contacts", "lifecyclestage")

    assert len(portal.calls) == 3
    assert portal.retry_waits == [2.0, 2.0]
    assert raised.value.http_status == 429
    assert raised.value.source == SOURCE
    assert TOKEN not in str(raised.value)

    portal.calls.clear()
    portal.overrides[:] = [rate_limited(3)]
    async with hubspot() as client:
        lookup = await client.contacts_by_email(["someone@example.com"])
    assert len(portal.calls) == 3
    assert lookup.failed == {email_sha256("someone@example.com")}


async def test_token_and_emails_never_logged(
    portal: FakePortal, hubspot: ClientOpener, captured_logs: Any, thresholds: Thresholds
) -> None:
    """Throttle and redaction: neither the token nor any email reaches the logs.

    Bindings, email lookups (found, not found, failed, simulated), a contact refresh,
    associations, target reads, a failed batch whose 500 body quotes an address and a retried
    429 all run with DEBUG logging captured as the product writes it. The captured JSON lines
    name the HubSpot events, yet contain neither ``test-token`` nor any address.
    """
    captured_logs.configure(level="DEBUG")
    emails = [
        "found@example.com",
        "missing@example.com",
        "broken@example.com",
        f"x@{SIM_EMAIL_DOMAIN}",
    ]
    portal.add_object("contacts", "9001", {"email": emails[0], "lifecyclestage": "lead"})
    portal.email_errors["broken@example.com"] = "VALIDATION_ERROR"
    portal.link("contacts", "meetings", "9001", ["9701"])
    portal.add_object(
        "meetings", "9701", {"hs_createdate": STAMP, "hs_meeting_source": "MEETINGS_PUBLIC"}
    )

    def failing_with_email(call: Call) -> httpx.Response | None:
        if call.surface != "objects_batch_read":
            return None
        return platform_error(500, "lookup of missing@example.com failed", "INTERNAL_ERROR")

    async with hubspot(contact_properties=None) as client:
        await client.verify_bindings(thresholds.mapping, get_settings())
        await client.contacts_by_email(emails)
        await client.contacts_by_id(["9001", "9002"])
        fetch = await client.associations("contacts", "meetings", ["9001", "9003"])
        await client.meetings(fetch.all_targets)
        portal.overrides.append(failing_with_email)
        await client.contacts_by_email(emails[:2])
        portal.overrides[:] = [rate_limited(1)]
        await client.property("contacts", "lifecyclestage")

    text: str = captured_logs.text
    events = {record.get("event") for record in captured_logs()}
    assert {
        "hubspot.verify",
        "hubspot.batch_read",
        "hubspot.associations",
        "connector.error",
        "http.request",
        "http.response",
    } <= events
    assert TOKEN not in text
    assert f"Bearer {TOKEN}" not in text
    lowered = text.lower()
    for email in emails:
        assert email not in lowered, email
    assert "@example.com" not in lowered
    assert SIM_EMAIL_DOMAIN not in lowered


# ---------------------------------------------------------------------------------------------
# Phase 7: traceability (AAP 0.11.2 "External fixtures are traceable to verified contracts")
# ---------------------------------------------------------------------------------------------

DECODERS: Final[Mapping[str, Callable[[Any], Any]]] = {
    "objects_batch_read": parse_batch_read,
    "crm_search": parse_search_page,
    "associations_batch_read": parse_association_batch,
    "properties": parse_property,
}
"""The decoder of every HubSpot contract surface."""


async def test_documented_samples_parse(contract: Callable[..., payloads.Contract]) -> None:
    """Traceability: every HubSpot contract's documented sample conforms and decodes verbatim.

    The samples are the reference pages' own renderings: the objects and associations samples
    carry status ``CANCELED`` and decode as failed calls; the search sample decodes its two
    records; the property sample its definition. Decoding never mutates a sample.
    """
    assert set(DECODERS) == set(payloads.SURFACES[SOURCE])
    decoded: dict[str, Any] = {}
    for name in payloads.SURFACES[SOURCE]:
        loaded = contract(SOURCE, name)
        sample = loaded.response.documented_sample
        if sample is None:
            continue
        payloads.validate_documented_sample(loaded)
        pristine = copy.deepcopy(sample)
        decoded[name] = DECODERS[name](sample)
        assert sample == pristine, name
    assert set(decoded) == set(DECODERS), "every HubSpot contract carries its documented sample"

    objects = decoded["objects_batch_read"]
    assert (objects.status, objects.is_complete, objects.records) == ("CANCELED", False, ())
    associations = decoded["associations_batch_read"]
    assert (associations.status, associations.is_complete) == ("CANCELED", False)
    page = decoded["crm_search"]
    assert page.total == 2
    assert page.after is None
    assert [record["id"] for record in page.results] == ["100451", "57156923994"]
    definition = decoded["properties"]
    assert (definition.name, definition.type) == ("<string>", "<string>")
    assert [(option.label, option.hidden) for option in definition.options] == [("<string>", True)]


async def test_captured_payloads_parse(
    captured_payloads: Callable[[str], list[dict[str, Any]]],
) -> None:
    """Traceability: every redacted live HubSpot capture conforms and decodes.

    Each capture written by ``ads-engine capture-fixtures`` is validated against its contract
    (request and response) and its success body decoded by its surface's decoder; its path is
    a ``2026-09`` path. With no capture yet (only ``.gitkeep``), the list is empty and the test
    passes.
    """
    captures = captured_payloads(SOURCE)
    assert isinstance(captures, list)
    for capture in captures:
        loaded = payloads.validate_capture(capture)
        assert loaded.source == SOURCE
        assert capture["surface"] == loaded.surface
        path = unquote(httpx.URL(str(capture["request"]["url"])).path)
        assert NUMBERED_PATH.search(path) is None, path
        assert f"/{HUBSPOT_API_VERSION}/" in path
        status = capture["response"]["status"]
        if not 200 <= status < 300:
            continue
        assert DECODERS[loaded.name](capture["response"]["body"]) is not None
