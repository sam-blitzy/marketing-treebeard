"""Integration tests of ``CrmSyncJob``'s failure propagation and refresh population.

AAP 0.3.3 (transient row of the error table, "Contact refresh", "Associations", "Deal search",
"Google-to-CRM link") and AAP 0.11.1 ("sync/mode.py alone decides"), on the PostgreSQL 18
test database. Each test names the guarantee it proves:

* **Exhausted transient failures fail the refresh** (``SearchOutcome``, ``ContactBatch``,
  ``AssociationFetch``, ``TargetRead``, ``MatchReport`` ``interrupted_by``): after a good sync,
  a persistent 503 on the deal search
  (:func:`test_deal_search_503_fails_the_dataset_and_keeps_the_rows_read`), the Google
  discovery search
  (:func:`test_discovery_search_503_fails_google_contacts_and_keeps_the_rows_read`), the
  contacts read by id (:func:`test_contact_refresh_503_fails_contacts_and_keeps_unknown_state`)
  or an association, meeting, deal or deal-contact read
  (:func:`test_association_or_target_503_fails_associations`) reaches
  ``SourceModeController.on_error``: the job fails with disposition ``stale``, the connection
  turns ``stale``, the failed dataset's ``last_success_at`` and coverage stand while its
  ``last_attempt_at`` moves on, what was read is stored with its per-object unknown state,
  and every other dataset of the run records its own success. An interruption noted before a
  later step raises is still handed over
  (:func:`test_interruption_before_a_raised_error_is_still_handed_over`), also when the
  dataset is retired before a later write, its read-only freshness rows left untouched
  (:func:`test_interruption_before_a_retired_dataset_is_still_handed_over`), or a later step
  fails with a non-connector error, which then propagates unchanged
  (:func:`test_interruption_before_an_unexpected_error_is_still_handed_over`).
* **A ceiling is not a failure**: a one-hour partition above 10,000 results stays an
  ``incomplete`` success without ``on_error``
  (:func:`test_hour_above_the_ceiling_is_an_incomplete_success`), and a stored discovery
  ceiling outlives a narrower clean discovery
  (:func:`test_discovery_ceiling_outlives_a_narrower_clean_discovery`).
* **Google-only contacts are refreshed by id whatever their touch date**: a contact touched
  60 days ago, outside the current discovery range, gets its new QL date and meeting on the
  next normal sync, and ``hubspot.google_contacts`` vouches for the whole stored range
  (:func:`test_old_google_only_contact_is_refreshed_by_id`), also while HubSpot has cleared
  its campaign property (:func:`test_cleared_campaign_is_seen_again_by_the_refresh`); a
  Google-touch contact the read
  by id does not return makes the dataset ``incomplete`` and the Google pipeline refresh
  unknown until a clean sync (:func:`test_unrefreshed_google_contact_is_unknown_until_read`).
* **Matching interruptions reach on_error** with no dataset, for both jobs and both sources
  (:func:`test_matching_interruption_reaches_on_error`), also when a later batch of the pass
  raises another source's defect, whose consequence then stands
  (:func:`test_hubspot_matching_interruption_survives_a_later_linkedin_defect`,
  :func:`test_linkedin_matching_interruption_survives_a_later_hubspot_defect`) or its own
  source's (:func:`test_a_later_defect_of_the_same_source_stands_over_its_interruption`), and
  from a build's own matching pass in ``sync/mode.py``, on a normal return and before a
  raised error (:func:`test_build_matching_interruption_reaches_on_error`,
  :func:`test_build_matching_interruption_is_handed_over_before_a_raised_error`).

How the tests run: HubSpot is :class:`Portal`, an in-memory portal answering the four
``2026-09`` surfaces; LinkedIn's lead re-fetch is :class:`LeadPlatform`. Both are wrapped in
the conftest's ``contract_checked_transport``, so every request and every 2xx body is checked
against ``backend/tests/contracts``. Per-test overrides answer 503 to every retry of a chosen
call. Clocks are fixed: each run builds its controller on the run's instant, the retry policy
records its waits, and the HubSpot throttle runs on a fake monotonic clock.
"""

import bisect
import copy
import hashlib
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from urllib.parse import unquote

import httpx
import pytest
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.analysis import attribution, metrics, runner
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.connectors.errors import QueryDefect
from ads_engine.connectors.http import RetryPolicy
from ads_engine.connectors.hubspot.client import STAGE_DATE_PROPERTY_PREFIX, RateLimiter
from ads_engine.connectors.hubspot.parser import NO_ASSOCIATIONS_FOUND
from ads_engine.connectors.hubspot.search import CEILING_REASON, epoch_ms
from ads_engine.db.models.accounts import AdAccount, Campaign, LeadForm, LeadFormVersion
from ads_engine.db.models.crm import (
    CrmAssociation,
    CrmAssociationFetch,
    CrmContact,
    CrmDeal,
    CrmMeeting,
    Lead,
    LeadMatch,
)
from ads_engine.db.models.ops import DatasetFreshness, SourceConnection, SourceDataset, SyncRun
from ads_engine.db.repositories import DatasetNotWritable
from ads_engine.settings import Settings, Source, get_settings
from ads_engine.simulation import payloads
from ads_engine.sync import freshness, lead_match
from ads_engine.sync.jobs import JOB_CRM, CrmSyncJob, JobContext, JobResult, LeadMatchJob
from ads_engine.sync.lead_match import REASON_LOOKUP_FAILED, RETRY_DELAYS, create_pending
from ads_engine.sync.mode import SourceModeController, _MatchPass

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type Handler = Callable[[httpx.Request], httpx.Response]
type CheckedTransportFactory = Callable[[str, Handler], httpx.MockTransport]

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

STAMP: Final = "2026-09-04T13:05:22.123Z"
"""``createdAt``/``updatedAt``/``startedAt`` of every record and envelope the portal serves."""
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

T1: Final = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The first (good) sync."""
T2: Final = T1 + timedelta(hours=3)
"""The next 3-hourly sync."""
T3: Final = T2 + timedelta(hours=3)
"""The sync after that."""

FMB_STAGE_ID: Final = "1406943076"
"""The portal's custom "First meeting booked" lifecycle stage (the MQL binding)."""
QL_STAGE_ID: Final = "1406943077"
"""The portal's custom "Qualified Lead" lifecycle stage (the QL binding)."""
MQL_PROPERTY: Final = f"{STAGE_DATE_PROPERTY_PREFIX}{FMB_STAGE_ID}"
QL_PROPERTY: Final = f"{STAGE_DATE_PROPERTY_PREFIX}{QL_STAGE_ID}"
STAGES: Final = (
    ("subscriber", "Subscriber"),
    ("lead", "Lead"),
    ("marketingqualifiedlead", "Marketing Qualified Lead"),
    ("salesqualifiedlead", "Sales Qualified Lead"),
    ("opportunity", "Opportunity"),
    ("customer", "Customer"),
    ("evangelist", "Evangelist"),
    ("other", "Other"),
    (FMB_STAGE_ID, "First meeting booked"),
    (QL_STAGE_ID, "Qualified Lead"),
)
"""HubSpot's default ``lifecyclestage`` options and the portal's two custom stages."""
CAMPAIGN_PROPERTY: Final = "google_campaign_id"
TOUCH_PROPERTY: Final = "google_touch_date"
GOOGLE_CAMPAIGN: Final = "123456789"

ACCOUNT_ID: Final = "990000001"
"""The fake ``LINKEDIN_AD_ACCOUNT_IDS`` of the conftest's ``live_credentials("linkedin")``."""
LI_CAMPAIGN_ID: Final = "614000001"
LI_CAMPAIGN_URN: Final = f"urn:li:sponsoredCampaign:{LI_CAMPAIGN_ID}"
CREATIVE_URN: Final = "urn:li:sponsoredCreative:715000001"
FORM_ID: Final = 7001
FORM_URN: Final = f"urn:li:leadGenForm:{FORM_ID}"
NAME_QID: Final = 1
EMAIL_QID: Final = 2

MATCHED: Final = "5001"
"""The contact a LinkedIn lead is matched to on the first sync."""
MATCHED_EMAIL: Final = "matched.lead@example.com"
COLLEAGUE: Final = "5009"
"""A contact only reached through a searched deal's associations."""
GOOGLE: Final = "7001"
"""A Google-touch contact without a lead, touched three days before :data:`T1`."""
MEETING: Final = "8001"
GOOGLE_MEETING: Final = "8002"
DEAL: Final = "9001"
"""A searched deal associated with :data:`MATCHED` and :data:`COLLEAGUE`."""
UNLINKED_DEAL: Final = "9002"
OLD_DEAL: Final = "9005"
"""A deal of :data:`MATCHED` created before the deal search range (read as a target)."""

HUBSPOT_NAMES: Final = (
    "hubspot.contacts",
    "hubspot.associations",
    "hubspot.deal_search",
    "hubspot.google_contacts",
)


def ms(instant: datetime) -> int:
    """Epoch milliseconds of a tz-aware instant, as the search filters carry them."""
    return int(epoch_ms(instant))


def iso(instant: datetime) -> str:
    """Render an instant as HubSpot's ISO-8601 wire instant, ``…T…sssZ``."""
    value = instant.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def ms_of(value: str) -> int:
    """Return an ISO-8601 instant or epoch-millisecond string as epoch milliseconds."""
    if value.isdigit():
        return int(value)
    return (datetime.fromisoformat(value) - EPOCH) // timedelta(milliseconds=1)


def midnight(instant: datetime) -> datetime:
    """00:00 UTC of ``instant``'s UTC day."""
    day = instant.astimezone(UTC).date()
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def digest(email: str) -> str:
    """SHA-256 of the normalised email, as ``lead.email_sha256`` holds it."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Logging isolation (as in test_lead_matching.py)
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _uncached_loggers() -> Iterator[None]:
    """Keep every logger used here lazy, then restore structlog's configuration.

    A logger first used here would otherwise cache its processor chain and write past
    ``structlog.testing.capture_logs`` in the modules that run after this one.
    """
    was_configured = structlog.is_configured()
    saved = structlog.get_config()
    structlog.configure(cache_logger_on_first_use=False)
    try:
        yield
    finally:
        if was_configured:
            structlog.configure(**saved)
        else:
            structlog.reset_defaults()


# ---------------------------------------------------------------------------------------------
# The HubSpot portal (only fields the contracts in backend/tests/contracts/hubspot/ declare)
# ---------------------------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that only its own :meth:`sleep` advances; nothing really sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        """Return the fake monotonic time in seconds."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Advance the fake time instead of sleeping."""
        self.now += max(seconds, 0.0)


def crm_record(hs_id: str, properties: Mapping[str, str | None]) -> dict[str, Any]:
    """One CRM object exactly as the batch read and search contracts define it."""
    created = properties.get("createdate") or STAMP
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
    """One 207 ``errors[]`` entry ``{status, category, subCategory?, message, context}``."""
    entry: dict[str, Any] = {"status": "error", "category": category, "message": message}
    if sub_category is not None:
        entry["subCategory"] = sub_category
    entry["context"] = {key: list(values) for key, values in context.items()}
    return entry


def batch_envelope(
    results: Sequence[Mapping[str, Any]], errors: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """A ``COMPLETE`` batch body; ``numErrors`` accompanies ``errors`` as on a real 207."""
    body: dict[str, Any] = {
        "status": "COMPLETE",
        "results": [dict(result) for result in results],
        "startedAt": STAMP,
        "completedAt": STAMP,
    }
    if errors:
        body["errors"] = [dict(entry) for entry in errors]
        body["numErrors"] = len(errors)
    return body


def platform_error(status: int, message: str, category: str) -> httpx.Response:
    """HubSpot's ``{status, message, category}`` error body."""
    return httpx.Response(
        status, json={"status": "error", "message": message, "category": category}
    )


def unavailable() -> httpx.Response:
    """A 503, which every retry of the call receives while its override is in place."""
    return platform_error(503, "Service Unavailable", "SERVICE_UNAVAILABLE")


def property_definition(
    name: str, data_type: str, options: Sequence[tuple[str, str]] = ()
) -> dict[str, Any]:
    """A property definition with every field the properties contract requires."""
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


@dataclass(frozen=True, slots=True)
class Call:
    """One request the portal answered.

    Attributes:
        surface: The contract surface ``payloads.identify_surface`` names for the request.
        segments: The path segments after ``/crm/{kind}/2026-09/``.
        body: The decoded JSON body; ``None`` for a ``GET``.
    """

    surface: str
    segments: tuple[str, ...]
    body: Any


type Override = Callable[[Call], httpx.Response | None]
"""A per-test responder consulted before the portal's own behaviour; ``None`` falls through."""


class Portal:
    """An in-memory HubSpot portal answering the four ``2026-09`` surfaces like HubSpot does.

    Contact properties (``lifecyclestage`` with :data:`STAGES`, every stage's date-entered
    property and the two Google properties); contacts, meetings and deals readable by id
    (contacts by email too), a missing id reported as a 207 ``OBJECT_NOT_FOUND`` entry;
    associations, an input without any answered with HubSpot's 207
    ``NO_ASSOCIATIONS_FOUND``; and a CRM Search index honouring ``GTE``/``LT``/
    ``HAS_PROPERTY``, ``limit``, numeric ``after`` cursors and the query's ``total``.
    """

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.calls: list[Call] = []
        self.overrides: list[Override] = []
        self.properties: dict[str, dict[str, Any]] = {
            "lifecyclestage": property_definition("lifecyclestage", "enumeration", STAGES),
            CAMPAIGN_PROPERTY: property_definition(CAMPAIGN_PROPERTY, "string"),
            TOUCH_PROPERTY: property_definition(TOUCH_PROPERTY, "datetime"),
        }
        for value, _label in STAGES:
            name = f"{STAGE_DATE_PROPERTY_PREFIX}{value}"
            self.properties[name] = property_definition(name, "datetime")
        self.objects: dict[str, dict[str, dict[str, str | None]]] = {
            "contacts": {},
            "meetings": {},
            "deals": {},
        }
        self.links: dict[tuple[str, str, str], list[str]] = {}
        self.search_index: dict[str, dict[str, dict[str, str | None]]] = {
            "contacts": {},
            "deals": {},
        }

    # -- seeding -------------------------------------------------------------------------

    def add(
        self,
        object_type: str,
        hs_id: str,
        properties: Mapping[str, str | None],
        *,
        readable: bool = True,
        searchable: bool = False,
    ) -> None:
        """Store (or replace) one object, readable by id and/or in the search index."""
        if readable:
            self.objects[object_type][hs_id] = dict(properties)
        if searchable:
            self.search_index[object_type][hs_id] = dict(properties)

    def link(self, from_type: str, to_type: str, from_id: str, targets: Iterable[str]) -> None:
        """Associate ``from_id`` with ``targets``, in that order."""
        self.links[(from_type, to_type, from_id)] = list(targets)

    async def record_wait(self, seconds: float) -> None:
        """The retry policy's sleep: return at once, never sleeping."""
        del seconds

    # -- handler -------------------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one request that the contract-checked transport already validated."""
        surface = payloads.identify_surface("hubspot", request)
        segments = tuple(unquote(request.url.path).strip("/").split("/")[3:])
        body = json.loads(request.content) if request.content else None
        call = Call(surface, segments, body)
        self.calls.append(call)
        for override in self.overrides:
            response = override(call)
            if response is not None:
                return response
        return self.answer(call)

    def answer(self, call: Call) -> httpx.Response:
        """The portal's own answer to ``call``, without overrides."""
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

    def calls_to(self, surface: str, *segments: str) -> list[Call]:
        """The recorded calls of ``surface`` whose path segments start with ``segments``."""
        return [
            call
            for call in self.calls
            if call.surface == surface and call.segments[: len(segments)] == segments
        ]

    def by_id_reads(self, object_type: str = "contacts") -> list[str]:
        """Every id a by-id batch read of ``object_type`` requested, in call order."""
        return [
            entry["id"]
            for call in self.calls_to("objects_batch_read", object_type)
            if call.body.get("idProperty") is None
            for entry in call.body["inputs"]
        ]

    def _property(self, call: Call) -> httpx.Response:
        object_type, name = call.segments
        definition = self.properties.get(name) if object_type == "contacts" else None
        if definition is None:
            return platform_error(404, f"Unable to find property {name}", "OBJECT_NOT_FOUND")
        return httpx.Response(200, json=definition)

    def _batch_read(self, call: Call) -> httpx.Response:
        object_type = call.segments[0]
        store = self.objects[object_type]
        requested: list[str] = list(call.body["properties"])
        by_email = call.body.get("idProperty") == "email"
        results: list[dict[str, Any]] = []
        missing: list[str] = []
        for entry in call.body["inputs"]:
            key: str = entry["id"]
            if by_email:
                hs_id = next(
                    (
                        candidate
                        for candidate, props in store.items()
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
        errors = []
        if missing:
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
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for entry in call.body["inputs"]:
            input_id: str = entry["id"]
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
            results.append(
                {
                    "from": {"id": input_id},
                    "to": [
                        {
                            # HubSpot sends numeric target ids as JSON integers.
                            "toObjectId": int(target) if target.isdigit() else target,
                            "associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": 200}],
                        }
                        for target in targets
                    ],
                }
            )
        return httpx.Response(207 if errors else 200, json=batch_envelope(results, errors))

    def _crm_search(self, call: Call) -> httpx.Response:
        object_type = call.segments[0]
        body = call.body
        (group,) = body["filterGroups"]
        ranges = [item for item in group["filters"] if item["operator"] in {"GTE", "LT"}]
        required = [
            item["propertyName"] for item in group["filters"] if item["operator"] == "HAS_PROPERTY"
        ]
        (prop,) = {item["propertyName"] for item in ranges}
        lo, hi = partition_of(body)
        keyed = sorted(
            (ms_of(props[prop] or ""), hs_id, props)
            for hs_id, props in self.search_index[object_type].items()
            if props.get(prop)
        )
        keys = [item[0] for item in keyed]
        in_range = keyed[bisect.bisect_left(keys, lo) : bisect.bisect_left(keys, hi)]
        matched = [
            crm_record(hs_id, props)
            for _key, hs_id, props in in_range
            if all(props.get(name) for name in required)
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


def partition_of(body: Mapping[str, Any]) -> tuple[int, int]:
    """The ``[GTE, LT)`` epoch-millisecond bounds of a search body's range filters."""
    (group,) = body["filterGroups"]
    bounds = {item["operator"]: int(item["value"]) for item in group["filters"] if "value" in item}
    return bounds["GTE"], bounds["LT"]


def search_unavailable(object_type: str, *, from_instant: datetime) -> Override:
    """503 for every search of ``object_type`` partition starting at or after ``from_instant``.

    Partitions are read in ascending order, so the earlier ones are read before the search
    is interrupted.
    """
    cutoff = ms(from_instant)

    def override(call: Call) -> httpx.Response | None:
        if (
            call.surface == "crm_search"
            and call.segments[0] == object_type
            and partition_of(call.body)[0] >= cutoff
        ):
            return unavailable()
        return None

    return override


def batch_read_unavailable(
    object_type: str, *, by_email: bool = False, containing: str | None = None
) -> Override:
    """503 for every batch read of ``object_type`` by id (or by email), optionally only those
    whose inputs include ``containing``."""

    def override(call: Call) -> httpx.Response | None:
        if call.surface != "objects_batch_read" or call.segments[0] != object_type:
            return None
        if (call.body.get("idProperty") == "email") != by_email:
            return None
        inputs = {entry["id"] for entry in call.body["inputs"]}
        if containing is not None and containing not in inputs:
            return None
        return unavailable()

    return override


def associations_unavailable(from_type: str, to_type: str) -> Override:
    """503 for every associations read from ``from_type`` to ``to_type``."""

    def override(call: Call) -> httpx.Response | None:
        if call.surface == "associations_batch_read" and call.segments[:2] == (
            from_type,
            to_type,
        ):
            return unavailable()
        return None

    return override


def contact(
    email: str,
    *,
    stage: str = "lead",
    mql_at: datetime | None = None,
    ql_at: datetime | None = None,
    touch_at: datetime | None = None,
) -> dict[str, str | None]:
    """A contact's properties; a touch date comes with the Google campaign reference."""
    props: dict[str, str | None] = {"email": email, "lifecyclestage": stage}
    if mql_at is not None:
        props[MQL_PROPERTY] = iso(mql_at)
    if ql_at is not None:
        props[QL_PROPERTY] = iso(ql_at)
    if touch_at is not None:
        props[CAMPAIGN_PROPERTY] = GOOGLE_CAMPAIGN
        props[TOUCH_PROPERTY] = iso(touch_at)
    return props


def deal(created: datetime) -> dict[str, str | None]:
    """A deal's properties, created at ``created``."""
    return {
        "createdate": iso(created),
        "dealstage": "appointmentscheduled",
        "pipeline": "default",
        "amount": "1000",
    }


def meeting(start: datetime) -> dict[str, str | None]:
    """A meeting's properties, starting at ``start``."""
    return {
        "hs_createdate": iso(start - timedelta(days=1)),
        "hs_timestamp": iso(start),
        "hs_meeting_start_time": iso(start),
        "hs_meeting_outcome": "COMPLETED",
        "hs_meeting_source": "MEETINGS_PUBLIC",
    }


def standard_portal() -> Portal:
    """The portal of every test: a matched lead's contact, a Google contact, deals, meetings.

    * :data:`MATCHED` (the LinkedIn lead's contact) has meeting :data:`MEETING` and deals
      :data:`DEAL` (searched) and :data:`OLD_DEAL` (created 100 days ago, before the
      default search range, so it is read as a target).
    * :data:`DEAL` is associated with :data:`MATCHED` and :data:`COLLEAGUE`.
    * :data:`GOOGLE`, touched three days before :data:`T1`, has meeting :data:`GOOGLE_MEETING`.
    * :data:`UNLINKED_DEAL` has no associations (a known orphan).
    """
    portal = Portal()
    portal.add("contacts", MATCHED, contact(MATCHED_EMAIL, mql_at=T1 - timedelta(days=2)))
    portal.add("contacts", COLLEAGUE, contact("colleague@example.com"))
    portal.add(
        "contacts",
        GOOGLE,
        contact("google.lead@example.com", touch_at=T1 - timedelta(days=3)),
        searchable=True,
    )
    portal.add("meetings", MEETING, meeting(T1 - timedelta(days=1)))
    portal.add("meetings", GOOGLE_MEETING, meeting(T1 - timedelta(days=2)))
    portal.add("deals", DEAL, deal(T1 - timedelta(days=2)), searchable=True)
    portal.add("deals", UNLINKED_DEAL, deal(T1 - timedelta(days=10)), searchable=True)
    portal.add("deals", OLD_DEAL, deal(T1 - timedelta(days=100)))
    portal.link("contacts", "meetings", MATCHED, [MEETING])
    portal.link("contacts", "deals", MATCHED, [DEAL, OLD_DEAL])
    portal.link("deals", "contacts", DEAL, [MATCHED, COLLEAGUE])
    portal.link("contacts", "meetings", GOOGLE, [GOOGLE_MEETING])
    return portal


# ---------------------------------------------------------------------------------------------
# LinkedIn's lead re-fetch (contract shapes of lead_form_responses_batch.json)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeadSpec:
    """One lead response LinkedIn holds: plain response id, email and submission instant."""

    response_id: str
    email: str
    submitted_at: datetime


def _text_answer(question_id: int, answer: str) -> dict[str, Any]:
    """One text answer of ``formResponse.answers``."""
    return {"questionId": question_id, "answerDetails": {"textQuestionAnswer": {"answer": answer}}}


def _element(spec: LeadSpec) -> dict[str, Any]:
    """One lead form response under the ``LEAD_FIELDS`` projection, version 1 of the form."""
    return {
        "id": spec.response_id,
        "submittedAt": ms(spec.submitted_at),
        "testLead": False,
        "leadType": "SPONSORED",
        "owner": {"sponsoredAccount": f"urn:li:sponsoredAccount:{ACCOUNT_ID}"},
        "ownerInfo": {"sponsoredAccountInfo": {"name": "Acme LinkedIn"}},
        "versionedLeadGenFormUrn": f"urn:li:versionedLeadGenForm:({FORM_URN},1)",
        "leadMetadataInfo": {
            "sponsoredLeadMetadataInfo": {
                "campaign": {
                    "id": LI_CAMPAIGN_URN,
                    "name": "White paper - Lead gen",
                    "type": "SPONSORED_UPDATES",
                }
            }
        },
        "associatedEntityInfo": {
            "associatedCreative": {"id": CREATIVE_URN, "intendedStatus": "ACTIVE", "content": {}}
        },
        "formResponse": {
            "answers": [_text_answer(NAME_QID, "Ada"), _text_answer(EMAIL_QID, spec.email)]
        },
        "form": {
            "id": FORM_ID,
            "name": "White paper download",
            "content": {
                "questions": [
                    {"questionId": NAME_QID, "name": "firstName", "predefinedField": "FIRST_NAME"},
                    {"questionId": EMAIL_QID, "name": "email", "predefinedField": "EMAIL"},
                ]
            },
        },
    }


def _restli_ids(request: httpx.Request) -> list[str]:
    """The plain response ids of a batch re-fetch's ``ids=List(...)``, quotes removed."""
    value = request.url.params["ids"]
    assert value.startswith("List(")
    assert value.endswith(")")
    ids: list[str] = []
    for item in value[len("List(") : -1].split(","):
        plain = unquote(item)
        if len(plain) >= 2 and plain.startswith('"') and plain.endswith('"'):
            plain = plain[1:-1]
        ids.append(plain)
    return ids


@dataclass
class LeadPlatform:
    """LinkedIn's batch re-fetch of lead responses by id.

    Attributes:
        leads: The responses LinkedIn holds, by plain response id.
        status: An error status every re-fetch answers with, or ``None``.
        refetches: The ids of every re-fetch, in call order.
    """

    leads: dict[str, LeadSpec] = field(default_factory=dict)
    status: int | None = None
    refetches: list[list[str]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer a batch re-fetch with the documented ``{results, statuses, errors}``."""
        assert "ids" in request.url.params, "only the lead re-fetch is expected here"
        ids = _restli_ids(request)
        self.refetches.append(ids)
        if self.status is not None:
            return httpx.Response(
                self.status, json={"status": self.status, "message": "Service Unavailable"}
            )
        results = {
            response_id: _element(self.leads[response_id])
            for response_id in ids
            if response_id in self.leads
        }
        return httpx.Response(200, json={"results": results, "statuses": {}, "errors": {}})


# ---------------------------------------------------------------------------------------------
# The rig: datasets, controller per run, and reads
# ---------------------------------------------------------------------------------------------


@dataclass
class Rig:
    """Live HubSpot and LinkedIn on the test database, each run on its own fixed clock.

    Attributes:
        sessions: The application's session factory on the emptied test database.
        settings: Settings with fake HubSpot and LinkedIn credentials and both Google
            properties configured.
        thresholds: The loaded configuration.
        portal: The HubSpot portal.
        linkedin: LinkedIn's lead re-fetch.
        transports: The contract-checked live transports.
        hubspot_id: The active live HubSpot primary.
        linkedin_id: The active live LinkedIn primary.
    """

    sessions: Sessions
    settings: Settings
    thresholds: Thresholds
    portal: Portal
    linkedin: LeadPlatform
    transports: dict[Source, httpx.MockTransport]
    hubspot_id: int
    linkedin_id: int

    def controller(self, now: datetime) -> SourceModeController:
        """A controller whose clock stands at ``now`` and whose clients never wait."""
        return SourceModeController(
            self.settings,
            sessionmaker=self.sessions,
            clock=lambda: now,
            transports=dict(self.transports),
            retry=RetryPolicy(sleep=self.portal.record_wait),
            hubspot_limiter=RateLimiter(
                4.0, clock=self.portal.clock, sleep=self.portal.clock.sleep
            ),
            thresholds=self.thresholds,
        )

    async def crm_sync(self, now: datetime, *, backfill: bool = False) -> JobResult:
        """Run ``CrmSyncJob`` on the HubSpot primary at ``now``."""
        controller = self.controller(now)
        async with controller.open_clients("hubspot", "live") as clients:
            ctx = await JobContext.for_dataset(controller, self.hubspot_id, clients, now=now)
            return await CrmSyncJob(ctx, backfill=backfill).run()

    async def lead_match(self, now: datetime) -> JobResult:
        """Run ``LeadMatchJob`` on the HubSpot primary at ``now``."""
        controller = self.controller(now)
        async with controller.open_clients("hubspot", "live") as clients:
            ctx = await JobContext.for_dataset(controller, self.hubspot_id, clients, now=now)
            return await LeadMatchJob(ctx).run()

    async def freshness(self) -> dict[str, DatasetFreshness]:
        """The HubSpot primary's freshness rows, by dataset name."""
        async with self.sessions() as session:
            return dict(await freshness.load_freshness(session, self.hubspot_id))

    async def connection(self, source: Source) -> SourceConnection:
        """The source's connection row."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(SourceConnection).where(SourceConnection.source == source)
            )
        assert row is not None, source
        return row

    async def contact(self, hs_id: str) -> CrmContact | None:
        """The stored contact ``hs_id`` of the HubSpot primary."""
        async with self.sessions() as session:
            return await session.scalar(
                select(CrmContact).where(
                    CrmContact.dataset_id == self.hubspot_id, CrmContact.hs_id == hs_id
                )
            )

    async def hs_ids(self, model: type[CrmDeal] | type[CrmMeeting] | type[CrmContact]) -> set[str]:
        """HubSpot ids of every stored row of ``model`` in the HubSpot primary."""
        async with self.sessions() as session:
            result = await session.scalars(
                select(model.hs_id).where(model.dataset_id == self.hubspot_id)
            )
            return set(result)

    async def fetch(self, object_type: str, object_id: str, to_type: str) -> CrmAssociationFetch:
        """The stored fetch row of one (object, target type)."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(CrmAssociationFetch).where(
                    CrmAssociationFetch.dataset_id == self.hubspot_id,
                    CrmAssociationFetch.object_type == object_type,
                    CrmAssociationFetch.object_id == object_id,
                    CrmAssociationFetch.to_type == to_type,
                )
            )
        assert row is not None, (object_type, object_id, to_type)
        return row

    async def targets(self, from_type: str, from_id: str, to_type: str) -> set[str]:
        """The stored association targets of one (object, target type)."""
        async with self.sessions() as session:
            result = await session.scalars(
                select(CrmAssociation.to_id).where(
                    CrmAssociation.dataset_id == self.hubspot_id,
                    CrmAssociation.from_type == from_type,
                    CrmAssociation.from_id == from_id,
                    CrmAssociation.to_type == to_type,
                )
            )
            return set(result)

    async def match_row(self, lead_id: int) -> LeadMatch:
        """The lead's ``lead_match`` row in the HubSpot primary."""
        async with self.sessions() as session:
            row = await session.get(LeadMatch, (lead_id, self.hubspot_id))
        assert row is not None, lead_id
        return row

    async def add_lead(self, spec: LeadSpec, *, staged_at: datetime) -> int:
        """Store a LinkedIn lead (email as its SHA-256 only), stage it ``pending``, serve it."""
        self.linkedin.leads[spec.response_id] = spec
        async with self.sessions() as session, session.begin():
            campaign_id = await session.scalar(
                select(Campaign.id).where(
                    Campaign.dataset_id == self.linkedin_id,
                    Campaign.external_id == LI_CAMPAIGN_ID,
                )
            )
            assert campaign_id is not None
            lead = Lead(
                source="linkedin",
                dataset_id=self.linkedin_id,
                external_id=spec.response_id,
                submitted_at=spec.submitted_at,
                campaign_id=campaign_id,
                creative_id=None,
                lead_form_urn=FORM_URN,
                lead_form_version=1,
                email_sha256=digest(spec.email),
            )
            session.add(lead)
            await session.flush()
            await create_pending(session, [lead.id], self.hubspot_id, now=staged_at)
            return lead.id

    async def google_refresh_problem(self, now: datetime, *, days: int | None = None) -> str | None:
        """What the analysis says about the Google-touch refresh, over the default window or
        a custom window of ``days`` ending on the default window's end."""
        async with self.sessions() as session:
            window = await runner.default_window(
                now=now, thresholds=self.thresholds, session=session
            )
            if days is not None:
                window = window.custom(window.end - timedelta(days=days - 1), window.end)
            ctx = await metrics.load_context(
                session,
                window,
                self.thresholds,
                kind="default" if days is None else "custom",
                now=now,
            )
        assert ctx.google_crm is not None
        return attribution._google_refresh_problem(ctx, ctx.google_crm)


async def _dataset(sessions: Sessions, source: Source) -> int:
    """Insert an active live primary dataset and point its source's ``ok`` connection at it."""
    async with sessions() as session, session.begin():
        dataset = SourceDataset(
            source=source,
            mode="live",
            state="active",
            role="primary",
            reason="seeded by the CRM sync tests",
            created_at=T1 - timedelta(days=200),
            activated_at=T1 - timedelta(days=200),
        )
        session.add(dataset)
        await session.flush()
        session.add(
            SourceConnection(
                source=source,
                mode="live",
                status="ok",
                reason="seeded by the CRM sync tests",
                active_dataset_id=dataset.id,
            )
        )
        return dataset.id


async def _linkedin_entities(sessions: Sessions, dataset_id: int) -> None:
    """Store the ad account, campaign, lead form and its version-1 map leads are placed under."""
    async with sessions() as session, session.begin():
        account = AdAccount(
            source="linkedin",
            dataset_id=dataset_id,
            external_id=ACCOUNT_ID,
            name="Acme LinkedIn",
            currency="USD",
            timezone="UTC",
        )
        session.add(account)
        await session.flush()
        campaign = Campaign(
            source="linkedin",
            dataset_id=dataset_id,
            account_id=account.id,
            external_id=LI_CAMPAIGN_ID,
            name="White paper - Lead gen",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
            daily_budget=Decimal("50.00"),
        )
        form = LeadForm(
            source="linkedin",
            dataset_id=dataset_id,
            canonical_urn=FORM_URN,
            name="White paper download",
            current_version=1,
        )
        session.add_all([campaign, form])
        await session.flush()
        session.add(
            LeadFormVersion(
                source="linkedin",
                dataset_id=dataset_id,
                lead_form_id=form.id,
                version=1,
                email_question_ids=[str(EMAIL_QID)],
                first_seen_at=T1 - timedelta(days=200),
            )
        )


@pytest.fixture
async def rig(
    db: Sessions,
    contract_checked_transport: CheckedTransportFactory,
    live_credentials: Callable[[str], Mapping[str, str]],
    set_env: Callable[..., None],
    thresholds: Thresholds,
) -> Rig:
    """The :class:`Rig` of one test: the standard portal and a pending LinkedIn lead.

    The lead (:data:`MATCHED_EMAIL`) is matched to :data:`MATCHED` by the first sync.
    """
    live_credentials("hubspot")
    live_credentials("linkedin")
    set_env(
        HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY=CAMPAIGN_PROPERTY,
        HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY=TOUCH_PROPERTY,
    )
    settings = get_settings()
    assert settings.linkedin_ad_account_ids[0] == ACCOUNT_ID
    portal = standard_portal()
    linkedin = LeadPlatform()
    hubspot_id = await _dataset(db, "hubspot")
    linkedin_id = await _dataset(db, "linkedin")
    await _linkedin_entities(db, linkedin_id)
    built = Rig(
        sessions=db,
        settings=settings,
        thresholds=thresholds,
        portal=portal,
        linkedin=linkedin,
        transports={
            "hubspot": contract_checked_transport("hubspot", portal),
            "linkedin": contract_checked_transport("linkedin", linkedin),
        },
        hubspot_id=hubspot_id,
        linkedin_id=linkedin_id,
    )
    await built.add_lead(
        LeadSpec("resp-0001", MATCHED_EMAIL, T1 - timedelta(hours=5)),
        staged_at=T1 - timedelta(hours=5),
    )
    return built


async def good_first_sync(rig: Rig, *, backfill: bool = False) -> dict[str, DatasetFreshness]:
    """Run the first sync at :data:`T1`, assert every dataset succeeded, return the rows."""
    result = await rig.crm_sync(T1, backfill=backfill)
    assert result.status == "succeeded", result.error
    assert result.disposition is None
    rows = await rig.freshness()
    assert set(HUBSPOT_NAMES) <= set(rows)
    for name in HUBSPOT_NAMES:
        assert rows[name].last_success_at == T1, name
        assert rows[name].last_attempt_at == T1, name
    assert (await rig.connection("hubspot")).status == "ok"
    rig.portal.calls.clear()
    return rows


def assert_failed_attempt(
    before: DatasetFreshness, after: DatasetFreshness, *, attempted_at: datetime
) -> None:
    """A failed attempt: ``last_attempt_at`` moved on, the last good standing stands."""
    assert after.last_attempt_at == attempted_at
    assert after.last_success_at == before.last_success_at
    assert (after.covered_from, after.covered_through) == (
        before.covered_from,
        before.covered_through,
    )
    assert after.completeness == before.completeness
    assert after.reason is not None


def assert_succeeded(after: DatasetFreshness, *, at: datetime) -> None:
    """A success recorded at ``at``."""
    assert after.last_success_at == at
    assert after.last_attempt_at == at


async def assert_handed_to_on_error(rig: Rig, result: JobResult, *failed: str) -> None:
    """The job failed the named datasets and ``on_error`` turned HubSpot ``stale``."""
    assert result.status == "failed"
    assert result.disposition == "stale"
    for name in failed:
        assert not result.freshness[name].succeeded, name
    connection = await rig.connection("hubspot")
    assert connection.status == "stale"
    assert connection.last_attempt_at == T2


# ---------------------------------------------------------------------------------------------
# Exhausted transient failures (AAP 0.3.3 error table, transient row)
# ---------------------------------------------------------------------------------------------


async def test_deal_search_503_fails_the_dataset_and_keeps_the_rows_read(rig: Rig) -> None:
    """A deal search a 503 interrupts after its retries is a failed attempt, not a success.

    The partitions before the failure are read and their deals stored, and their
    deal-to-contact associations are still read; ``hubspot.deal_search`` keeps its last good
    standing and turns stale through ``on_error``, while the contacts and associations of the
    same run record their own successes.
    """
    before = await good_first_sync(rig)
    # New since the good sync: one in a partition read before the failure, one after it.
    rig.portal.add("deals", "9003", deal(T1 - timedelta(days=8)), searchable=True)
    rig.portal.add("deals", "9004", deal(T1 - timedelta(days=1)), searchable=True)
    rig.portal.overrides.append(
        search_unavailable("deals", from_instant=midnight(T1 - timedelta(days=5)))
    )

    result = await rig.crm_sync(T2)

    await assert_handed_to_on_error(rig, result, "hubspot.deal_search")
    after = await rig.freshness()
    assert_failed_attempt(
        before["hubspot.deal_search"], after["hubspot.deal_search"], attempted_at=T2
    )
    for name in ("hubspot.contacts", "hubspot.associations", "hubspot.google_contacts"):
        assert_succeeded(after[name], at=T2)
        assert after[name].completeness == "complete", (name, after[name].reason)
    deals = await rig.hs_ids(CrmDeal)
    assert "9003" in deals, "a deal read before the interruption is stored"
    assert "9004" not in deals, "a deal of a partition not read is not invented"
    # The deals read still get their deal-to-contact reads: a known orphan here.
    assert (await rig.fetch("deal", "9003", "contact")).status == "ok_empty"


async def test_discovery_search_503_fails_google_contacts_and_keeps_the_rows_read(
    rig: Rig,
) -> None:
    """A Google-touch discovery a 503 interrupts fails ``hubspot.google_contacts``.

    The contacts of the partitions read are stored (and refreshed by id with the rest), the
    Google dataset keeps its last good standing and turns stale, and ``hubspot.contacts``
    records its own success.
    """
    before = await good_first_sync(rig)
    rig.portal.add(
        "contacts",
        "7003",
        contact("early@example.com", touch_at=T1 - timedelta(days=6)),
        searchable=True,
    )
    rig.portal.add(
        "contacts",
        "7004",
        contact("late@example.com", touch_at=T1 - timedelta(days=1)),
        searchable=True,
    )
    rig.portal.overrides.append(
        search_unavailable("contacts", from_instant=midnight(T1 - timedelta(days=2)))
    )

    result = await rig.crm_sync(T2)

    await assert_handed_to_on_error(rig, result, "hubspot.google_contacts")
    after = await rig.freshness()
    assert_failed_attempt(
        before["hubspot.google_contacts"], after["hubspot.google_contacts"], attempted_at=T2
    )
    for name in ("hubspot.contacts", "hubspot.associations", "hubspot.deal_search"):
        assert_succeeded(after[name], at=T2)
    early = await rig.contact("7003")
    assert early is not None, "a contact read before the interruption is stored"
    assert early.google_touch_at == T1 - timedelta(days=6)
    assert await rig.contact("7004") is None
    assert "7003" in rig.portal.by_id_reads()


async def test_contact_refresh_503_fails_contacts_and_keeps_unknown_state(rig: Rig) -> None:
    """A contact refresh by id a 503 interrupts fails ``hubspot.contacts``.

    The matched contact keeps its previous stage values with ``contact_read_status`` ``error``
    (unknown, never a fresh zero); the Google dataset fails too, as its contacts were not
    read; the deal-to-contact target read of the same contact fails ``hubspot.associations``;
    the deal search records its own success.
    """
    before = await good_first_sync(rig)
    (lead_id,) = await _lead_ids(rig)
    assert (await rig.match_row(lead_id)).contact_read_status == "ok"
    rig.portal.add("contacts", MATCHED, contact(MATCHED_EMAIL, stage="opportunity"))
    rig.portal.overrides.append(batch_read_unavailable("contacts"))

    result = await rig.crm_sync(T2)

    await assert_handed_to_on_error(
        rig, result, "hubspot.contacts", "hubspot.google_contacts", "hubspot.associations"
    )
    after = await rig.freshness()
    for name in ("hubspot.contacts", "hubspot.google_contacts", "hubspot.associations"):
        assert_failed_attempt(before[name], after[name], attempted_at=T2)
    assert_succeeded(after["hubspot.deal_search"], at=T2)
    row = await rig.match_row(lead_id)
    assert (row.status, row.contact_read_status) == ("matched", "error")
    stored = await rig.contact(MATCHED)
    assert stored is not None
    assert stored.lifecycle_stage == "lead", "a failed read keeps the previous values"
    assert stored.mql_at == T1 - timedelta(days=2)


async def _lead_ids(rig: Rig) -> list[int]:
    """Ids of every lead of the LinkedIn primary."""
    async with rig.sessions() as session:
        return list(
            await session.scalars(select(Lead.id).where(Lead.dataset_id == rig.linkedin_id))
        )


@pytest.mark.parametrize(
    ("override", "fetch_key", "expected"),
    [
        pytest.param(
            associations_unavailable("contacts", "meetings"),
            ("contact", MATCHED, "meeting"),
            ("error", None, []),
            id="contact_meetings_fetch",
        ),
        pytest.param(
            batch_read_unavailable("meetings"),
            ("contact", MATCHED, "meeting"),
            ("ok", "error", [MEETING]),
            id="meeting_target_read",
        ),
        pytest.param(
            batch_read_unavailable("deals"),
            ("contact", MATCHED, "deal"),
            ("ok", "partial", [OLD_DEAL]),
            id="deal_target_read",
        ),
        pytest.param(
            batch_read_unavailable("contacts", containing=COLLEAGUE),
            ("deal", DEAL, "contact"),
            ("ok", "partial", [COLLEAGUE]),
            id="deal_contact_target_read",
        ),
    ],
)
async def test_association_or_target_503_fails_associations(
    rig: Rig,
    override: Override,
    fetch_key: tuple[str, str, str],
    expected: tuple[str, str | None, list[str]],
) -> None:
    """An association read or a target read a 503 interrupts fails ``hubspot.associations``.

    The fetch rows keep their unknown state (``error``, or ``ok`` with target reads
    ``error``/``partial`` and the missing ids) and the stored associations stand, while the
    contacts and the deal search record their own successes.
    """
    before = await good_first_sync(rig)
    good = await rig.fetch(*fetch_key)
    assert (good.status, good.target_read_status, good.missing_target_ids) == ("ok", "ok", [])
    stored_targets = await rig.targets(*fetch_key)
    assert stored_targets
    rig.portal.overrides.append(override)

    result = await rig.crm_sync(T2)

    await assert_handed_to_on_error(rig, result, "hubspot.associations")
    after = await rig.freshness()
    assert_failed_attempt(
        before["hubspot.associations"], after["hubspot.associations"], attempted_at=T2
    )
    for name in ("hubspot.contacts", "hubspot.deal_search", "hubspot.google_contacts"):
        assert_succeeded(after[name], at=T2)
    row = await rig.fetch(*fetch_key)
    assert (row.status, row.target_read_status, row.missing_target_ids) == expected
    assert await rig.targets(*fetch_key) == stored_targets


async def test_interruption_before_a_raised_error_is_still_handed_over(rig: Rig) -> None:
    """An interruption noted before a later step raises is handed over, then the raised error.

    The discovery is interrupted (503) and the deal search then fails with a defect (400):
    ``hubspot.google_contacts`` still records its failed attempt through ``on_error``, the
    datasets the defect left unwritten record theirs, the contacts refreshed in between keep
    their success, and the defect's disposition is the job's.
    """
    before = await good_first_sync(rig)
    rig.portal.overrides.append(
        search_unavailable("contacts", from_instant=midnight(T1 - timedelta(days=2)))
    )

    def defect(call: Call) -> httpx.Response | None:
        if call.surface == "crm_search" and call.segments[0] == "deals":
            return platform_error(400, "Invalid filter", "VALIDATION_ERROR")
        return None

    rig.portal.overrides.append(defect)

    result = await rig.crm_sync(T2)

    assert (result.status, result.disposition) == ("failed", "defect")
    after = await rig.freshness()
    for name in ("hubspot.google_contacts", "hubspot.deal_search", "hubspot.associations"):
        assert_failed_attempt(before[name], after[name], attempted_at=T2)
    assert_succeeded(after["hubspot.contacts"], at=T2)
    assert (await rig.connection("hubspot")).status == "error"


async def _sync_runs(rig: Rig, started_at: datetime) -> dict[str | None, SyncRun]:
    """The CRM sync's ``sync_run`` rows of the run started at ``started_at``, by dataset."""
    async with rig.sessions() as session:
        rows = await session.scalars(
            select(SyncRun).where(
                SyncRun.source == "hubspot",
                SyncRun.job == JOB_CRM,
                SyncRun.started_at == started_at,
            )
        )
        return {row.dataset: row for row in rows}


async def test_interruption_before_a_retired_dataset_is_still_handed_over(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interruption noted before the dataset is retired mid-job is still handed over.

    The deal search is interrupted (503), then the dataset is retired before the associations
    step writes: the job ends failed with the not-writable reason, ``on_error`` still turns the
    connection ``stale`` (disposition ``stale``), and the retired dataset's freshness rows of
    the unfinished steps are not touched, as its rows are read-only history.
    """
    before = await good_first_sync(rig)
    rig.portal.overrides.append(
        search_unavailable("deals", from_instant=midnight(T1 - timedelta(days=5)))
    )
    associations = CrmSyncJob._associations

    async def retire_then_associate(job: CrmSyncJob, *args: Any, **kwargs: Any) -> None:
        async with rig.sessions() as session, session.begin():
            await session.execute(
                update(SourceDataset)
                .where(SourceDataset.id == rig.hubspot_id)
                .values(state="retired", retired_at=T2)
            )
        await associations(job, *args, **kwargs)

    monkeypatch.setattr(CrmSyncJob, "_associations", retire_then_associate)

    result = await rig.crm_sync(T2)

    not_writable = str(DatasetNotWritable(rig.hubspot_id, "retired"))
    assert (result.status, result.disposition, result.error) == ("failed", "stale", not_writable)
    connection = await rig.connection("hubspot")
    assert (connection.status, connection.last_attempt_at) == ("stale", T2)
    after = await rig.freshness()
    for name in ("hubspot.deal_search", "hubspot.associations"):
        assert (after[name].last_success_at, after[name].last_attempt_at) == (T1, T1), name
        assert (after[name].covered_from, after[name].covered_through) == (
            before[name].covered_from,
            before[name].covered_through,
        ), name
        assert (after[name].completeness, after[name].reason) == (
            before[name].completeness,
            before[name].reason,
        ), name
    for name in ("hubspot.contacts", "hubspot.google_contacts"):
        assert_succeeded(after[name], at=T2)
    runs = await _sync_runs(rig, T2)
    assert {name: run.status for name, run in runs.items()} == {
        "hubspot.contacts": "succeeded",
        "hubspot.google_contacts": "succeeded",
        "hubspot.deal_search": "failed",
        "hubspot.associations": "failed",
    }
    assert runs["hubspot.associations"].error == not_writable


async def test_interruption_before_an_unexpected_error_is_still_handed_over(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interruption noted before a non-connector error is handed over; the error propagates.

    The deal search is interrupted (503), then the associations step fails with an error no
    connector raised (a bug): ``run`` raises it unchanged, after ``on_error`` turned the
    connection ``stale`` and recorded the deal search's failed attempt, and the run's
    unfinished ``sync_run`` rows end failed.
    """
    before = await good_first_sync(rig)
    rig.portal.overrides.append(
        search_unavailable("deals", from_instant=midnight(T1 - timedelta(days=5)))
    )
    crash = RuntimeError("associations step crashed")

    async def crashing_associations(job: CrmSyncJob, *args: Any, **kwargs: Any) -> None:
        raise crash

    monkeypatch.setattr(CrmSyncJob, "_associations", crashing_associations)

    with pytest.raises(RuntimeError, match="associations step crashed") as raised:
        await rig.crm_sync(T2)

    assert raised.value is crash
    connection = await rig.connection("hubspot")
    assert (connection.status, connection.last_attempt_at) == ("stale", T2)
    after = await rig.freshness()
    assert_failed_attempt(
        before["hubspot.deal_search"], after["hubspot.deal_search"], attempted_at=T2
    )
    associations = after["hubspot.associations"]
    assert (associations.last_success_at, associations.last_attempt_at) == (T1, T1)
    runs = await _sync_runs(rig, T2)
    assert {name: run.status for name, run in runs.items()} == {
        "hubspot.contacts": "succeeded",
        "hubspot.google_contacts": "succeeded",
        "hubspot.deal_search": "failed",
        "hubspot.associations": "failed",
    }
    assert runs["hubspot.associations"].error == "job failed unexpectedly"


# ---------------------------------------------------------------------------------------------
# Search ceilings stay incomplete successes (AAP 0.3.3 "Deal search")
# ---------------------------------------------------------------------------------------------


def ceiling_hour(portal: Portal, object_type: str, hour: datetime) -> Override:
    """Report more than 10,000 results for every search of ``object_type`` containing ``hour``.

    Longer partitions split down to the one-hour partition ``[hour, hour + 1 h)``, which is
    read and stays incomplete (``CEILING_REASON``); the portal's own records are returned.
    """
    lo_ms, hi_ms = ms(hour), ms(hour + timedelta(hours=1))

    def override(call: Call) -> httpx.Response | None:
        if call.surface != "crm_search" or call.segments[0] != object_type:
            return None
        lo, hi = partition_of(call.body)
        if not (lo <= lo_ms and hi_ms <= hi):
            return None
        body = json.loads(portal.answer(call).content)
        body["total"] = 10_001
        body.pop("paging", None)
        return httpx.Response(200, json=body)

    return override


async def test_hour_above_the_ceiling_is_an_incomplete_success(rig: Rig) -> None:
    """A one-hour partition above 10,000 results is a completed read: no ``on_error``.

    ``hubspot.deal_search`` records an ``incomplete`` success naming the ceiling, the deals
    read are stored, the connection stays ``ok`` and the job succeeds.
    """
    rig.portal.overrides.append(ceiling_hour(rig.portal, "deals", T1 - timedelta(days=2)))

    result = await rig.crm_sync(T1)

    assert result.status == "succeeded", result.error
    assert result.disposition is None
    row = (await rig.freshness())["hubspot.deal_search"]
    assert_succeeded(row, at=T1)
    assert row.completeness == "incomplete"
    assert row.reason is not None
    assert freshness.REASON_SEARCH_CEILING in row.reason
    assert CEILING_REASON in row.reason
    assert DEAL in await rig.hs_ids(CrmDeal)
    connection = await rig.connection("hubspot")
    assert (connection.status, connection.reason) == ("ok", "seeded by the CRM sync tests")


async def test_discovery_ceiling_outlives_a_narrower_clean_discovery(rig: Rig) -> None:
    """A stored discovery ceiling stands until a discovery re-reads its range.

    The backfill's discovery hits the ceiling 60 days back; the next normal sync searches
    only the default range, so its clean discovery must not clear that shortfall, although
    every stored Google-touch contact is refreshed by id.
    """
    old_touch = T1 - timedelta(days=60)
    rig.portal.add(
        "contacts", "7060", contact("old.touch@example.com", touch_at=old_touch), searchable=True
    )
    rig.portal.overrides.append(ceiling_hour(rig.portal, "contacts", old_touch))
    first = await rig.crm_sync(T1, backfill=True)
    assert first.status == "succeeded", first.error
    stored = (await rig.freshness())["hubspot.google_contacts"]
    assert stored.completeness == "incomplete"
    assert stored.reason is not None
    assert freshness.REASON_SEARCH_CEILING in stored.reason
    rig.portal.overrides.clear()
    rig.portal.calls.clear()

    second = await rig.crm_sync(T2)

    assert second.status == "succeeded", second.error
    row = (await rig.freshness())["hubspot.google_contacts"]
    assert_succeeded(row, at=T2)
    assert row.completeness == "incomplete"
    assert row.reason is not None
    assert freshness.REASON_SEARCH_CEILING in row.reason
    assert row.covered_from == stored.covered_from
    assert "7060" in rig.portal.by_id_reads()


# ---------------------------------------------------------------------------------------------
# Google-only contacts are refreshed by id (AAP 0.3.3 "Contact refresh", "Google-to-CRM link")
# ---------------------------------------------------------------------------------------------


async def test_old_google_only_contact_is_refreshed_by_id(rig: Rig) -> None:
    """A Google-only contact touched 60 days ago keeps refreshing after it leaves the range.

    The backfill discovers it; its QL date and a meeting appear afterwards. The next normal
    sync's discovery searches only the default range, which no longer holds the touch, yet
    the contact is read by id (new stage values stored) and its associations are fetched,
    and ``hubspot.google_contacts`` vouches for the whole stored range, so a 90-day custom
    run trusts the evidence.
    """
    old_touch = T1 - timedelta(days=60)
    rig.portal.add(
        "contacts", "7060", contact("old.touch@example.com", touch_at=old_touch), searchable=True
    )
    backfill = await good_first_sync(rig, backfill=True)
    stored = await rig.contact("7060")
    assert stored is not None
    assert stored.ql_at is None
    assert (await rig.fetch("contact", "7060", "meeting")).status == "ok_empty"
    ql_at = T1 + timedelta(hours=1)
    rig.portal.add(
        "contacts",
        "7060",
        contact("old.touch@example.com", stage=QL_STAGE_ID, ql_at=ql_at, touch_at=old_touch),
        searchable=True,
    )
    rig.portal.add("meetings", "8060", meeting(T1 + timedelta(hours=2)))
    rig.portal.link("contacts", "meetings", "7060", ["8060"])

    result = await rig.crm_sync(T2)

    assert result.status == "succeeded", result.error
    searches = rig.portal.calls_to("crm_search", "contacts")
    assert searches
    assert all(partition_of(call.body)[0] > ms(old_touch) for call in searches), (
        "the default discovery range no longer holds the touch"
    )
    assert "7060" in rig.portal.by_id_reads()
    refreshed = await rig.contact("7060")
    assert refreshed is not None
    assert (refreshed.lifecycle_stage, refreshed.ql_at) == (QL_STAGE_ID, ql_at)
    assert await rig.targets("contact", "7060", "meeting") == {"8060"}
    fetch = await rig.fetch("contact", "7060", "meeting")
    assert (fetch.status, fetch.target_read_status) == ("ok", "ok")
    assert "8060" in await rig.hs_ids(CrmMeeting)
    rows = await rig.freshness()
    google = rows["hubspot.google_contacts"]
    assert_succeeded(google, at=T2)
    assert google.completeness == "complete", google.reason
    assert google.covered_from == backfill["hubspot.google_contacts"].covered_from
    assert google.covered_from is not None
    assert google.covered_from <= old_touch.date()
    assert google.covered_through == T2.date()
    for name in ("hubspot.contacts", "hubspot.associations"):
        assert rows[name].completeness == "complete", (name, rows[name].reason)
        assert rows[name].covered_from == backfill[name].covered_from
    assert await rig.google_refresh_problem(T2, days=90) is None


async def test_cleared_campaign_is_seen_again_by_the_refresh(rig: Rig) -> None:
    """A stored Google-touch contact stays in the refresh while its campaign is cleared.

    The population is selected on the touch date alone: once HubSpot clears the campaign
    property of a contact touched outside the discovery range, the refresh stores that, and
    when HubSpot sets it again the next refresh by id brings it back, although no discovery
    finds the contact any more.
    """
    old_touch = T1 - timedelta(days=60)
    email = "old.touch@example.com"
    rig.portal.add("contacts", "7060", contact(email, touch_at=old_touch), searchable=True)
    await good_first_sync(rig, backfill=True)
    cleared = contact(email, touch_at=old_touch)
    cleared[CAMPAIGN_PROPERTY] = None
    rig.portal.add("contacts", "7060", cleared)

    assert (await rig.crm_sync(T2)).status == "succeeded"
    stored = await rig.contact("7060")
    assert stored is not None
    assert (stored.google_campaign_ref, stored.google_touch_at) == (None, old_touch)

    rig.portal.add("contacts", "7060", contact(email, touch_at=old_touch))
    rig.portal.calls.clear()
    assert (await rig.crm_sync(T3)).status == "succeeded"
    assert "7060" in rig.portal.by_id_reads()
    restored = await rig.contact("7060")
    assert restored is not None
    assert restored.google_campaign_ref == GOOGLE_CAMPAIGN


async def test_unrefreshed_google_contact_is_unknown_until_read(rig: Rig) -> None:
    """A Google-touch contact the read by id does not return is unknown, never trusted.

    ``hubspot.google_contacts`` records an ``incomplete`` success counting it, so the
    analysis reports the Google pipeline refresh unknown; once the contact is read again,
    the next sync makes the dataset complete and the refresh known.
    """
    touch = T1 - timedelta(days=4)
    unread = contact("vanished@example.com", touch_at=touch)
    rig.portal.add("contacts", "7002", unread, readable=False, searchable=True)

    first = await rig.crm_sync(T1)

    assert first.status == "succeeded", first.error
    assert first.disposition is None
    assert "7002" in rig.portal.by_id_reads()
    rows = await rig.freshness()
    google = rows["hubspot.google_contacts"]
    assert_succeeded(google, at=T1)
    assert (google.completeness, google.reason) == (
        "incomplete",
        "1 Google-touch contact not refreshed",
    )
    assert (rows["hubspot.contacts"].completeness, rows["hubspot.contacts"].reason) == (
        "incomplete",
        "1 contact not read",
    )
    assert await rig.google_refresh_problem(T1) == (
        "Google contact refresh unknown: hubspot.google_contacts incomplete: "
        "1 Google-touch contact not refreshed"
    )
    assert (await rig.connection("hubspot")).status == "ok"

    rig.portal.add("contacts", "7002", unread)
    second = await rig.crm_sync(T2)

    assert second.status == "succeeded", second.error
    rows = await rig.freshness()
    google = rows["hubspot.google_contacts"]
    assert_succeeded(google, at=T2)
    assert (google.completeness, google.reason) == ("complete", None)
    assert rows["hubspot.contacts"].completeness == "complete"
    assert await rig.google_refresh_problem(T2) is None


# ---------------------------------------------------------------------------------------------
# Matching interruptions (AAP 0.3.3 "Lead-to-contact matching", error table)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("job", ["crm_sync", "lead_match"])
@pytest.mark.parametrize(("failing", "healthy"), [("linkedin", "hubspot"), ("hubspot", "linkedin")])
async def test_matching_interruption_reaches_on_error(
    rig: Rig, job: str, failing: Source, healthy: Source
) -> None:
    """A re-fetch or lookup a 503 interrupts reaches ``on_error``, with no dataset.

    LinkedIn's re-fetch or HubSpot's lookup by email answers 503 to every retry: the row
    ends ``error``, due again in an hour; the failing source's connection turns ``stale``
    (the other stays ``ok``) and the job reports ``stale``. No freshness dataset fails:
    matching state lives in ``lead_match``, and the CRM sync's own datasets succeed.
    """
    await good_first_sync(rig)
    spec = LeadSpec("resp-0002", "pending.lead@example.com", T2 - timedelta(hours=2))
    rig.portal.add("contacts", "5002", contact(spec.email))
    lead_id = await rig.add_lead(spec, staged_at=T2 - timedelta(hours=2))
    if failing == "linkedin":
        rig.linkedin.status = 503
    else:
        rig.portal.overrides.append(batch_read_unavailable("contacts", by_email=True))

    result = await (rig.crm_sync(T2) if job == "crm_sync" else rig.lead_match(T2))

    assert result.disposition == "stale"
    assert result.status == "succeeded", result.error
    assert (await rig.connection(failing)).status == "stale"
    assert (await rig.connection(healthy)).status == "ok"
    row = await rig.match_row(lead_id)
    assert (row.status, row.next_attempt_at, row.contact_id) == (
        "error",
        T2 + RETRY_DELAYS[0],
        None,
    )
    if failing == "hubspot":
        assert row.reason == REASON_LOOKUP_FAILED
    rows = await rig.freshness()
    expected_success = T2 if job == "crm_sync" else T1
    for name in HUBSPOT_NAMES:
        assert rows[name].last_success_at == expected_success, name
        assert rows[name].last_attempt_at == expected_success, name


FIRST_BATCH: Final = "resp-extra-000"
"""A lead of the first matching batch (``lead_match.BATCH_SIZE`` rows, by lead id)."""
SECOND_BATCH: Final = "resp-extra-100"
"""The one lead the second matching batch claims."""


async def two_batches(rig: Rig) -> list[int]:
    """Stage 101 pending leads due at :data:`T2`: one full batch of 100, then one more.

    Due rows are claimed in lead-id order, so the first batch holds ``resp-extra-000`` to
    ``resp-extra-099`` (emails ``lead0@`` to ``lead99@``) and the second ``resp-extra-100``.
    """
    staged = T2 - timedelta(hours=2)
    return [
        await rig.add_lead(
            LeadSpec(f"resp-extra-{n:03d}", f"lead{n}@example.com", staged), staged_at=staged
        )
        for n in range(lead_match.BATCH_SIZE + 1)
    ]


def lead_platform(rig: Rig, failures: Mapping[str, httpx.Response]) -> Handler:
    """LinkedIn's re-fetch, answering ``failures[id]`` to every re-fetch that asks for ``id``."""

    def handler(request: httpx.Request) -> httpx.Response:
        for response_id in _restli_ids(request):
            if response_id in failures:
                return failures[response_id]
        return rig.linkedin(request)

    return handler


def email_lookup_containing(email: str, response: httpx.Response) -> Override:
    """``response`` to every contacts batch read by email whose inputs include ``email``."""

    def override(call: Call) -> httpx.Response | None:
        if call.surface != "objects_batch_read" or call.body.get("idProperty") != "email":
            return None
        return response if email in {entry["id"] for entry in call.body["inputs"]} else None

    return override


def linkedin_defect() -> httpx.Response:
    """LinkedIn's 400 ``{status, message}``: a defect, never retried."""
    return httpx.Response(400, json={"status": 400, "message": "Invalid request"})


def linkedin_unavailable() -> httpx.Response:
    """LinkedIn's 503, which every retry of the re-fetch receives."""
    return httpx.Response(503, json={"status": 503, "message": "Service Unavailable"})


async def assert_batches_kept(
    rig: Rig, ids: Sequence[int], *, released_due: datetime | None, reason: str | None = None
) -> None:
    """The first batch committed retried ``error``; the failing batch's lease was released.

    Every first-batch row has one attempt, ``error`` and the first retry delay; the second
    batch's row is ``pending`` again, unattempted and due when it was before the run.
    """
    for lead_id in ids[: lead_match.BATCH_SIZE]:
        row = await rig.match_row(lead_id)
        assert (row.status, row.attempts, row.next_attempt_at, row.contact_id) == (
            "error",
            1,
            T2 + RETRY_DELAYS[0],
            None,
        ), lead_id
        if reason is not None:
            assert row.reason == reason, lead_id
    released = await rig.match_row(ids[lead_match.BATCH_SIZE])
    assert (released.status, released.attempts, released.next_attempt_at) == (
        "pending",
        0,
        released_due,
    )


@pytest.mark.parametrize("job", ["crm_sync", "lead_match"])
async def test_hubspot_matching_interruption_survives_a_later_linkedin_defect(
    rig: Rig, contract_checked_transport: CheckedTransportFactory, job: str
) -> None:
    """A HubSpot lookup 503 in a committed batch reaches ``on_error`` though a later batch
    raises LinkedIn's 400.

    The first batch's lookup by email answers 503 to every retry, so its 100 rows commit as
    retried ``error``; the second batch's re-fetch is a LinkedIn defect. HubSpot turns
    ``stale`` and LinkedIn ``error``, the defect being handed over last: the CRM sync goes on
    and records its own datasets, the matching job fails on the defect.
    """
    await good_first_sync(rig)
    ids = await two_batches(rig)
    due = (await rig.match_row(ids[-1])).next_attempt_at
    rig.transports["linkedin"] = contract_checked_transport(
        "linkedin", lead_platform(rig, {SECOND_BATCH: linkedin_defect()})
    )
    rig.portal.overrides.append(email_lookup_containing("lead0@example.com", unavailable()))

    result = await (rig.crm_sync(T2) if job == "crm_sync" else rig.lead_match(T2))

    assert result.disposition == "defect"
    assert (await rig.connection("hubspot")).status == "stale"
    assert (await rig.connection("linkedin")).status == "error"
    await assert_batches_kept(rig, ids, released_due=due, reason=REASON_LOOKUP_FAILED)
    rows = await rig.freshness()
    if job == "crm_sync":
        # LinkedIn's defect is LinkedIn's: the CRM refresh still records its datasets.
        assert result.status == "succeeded", result.error
        for name in HUBSPOT_NAMES:
            assert_succeeded(rows[name], at=T2)
    else:
        assert result.status == "failed"
        for name in HUBSPOT_NAMES:
            assert rows[name].last_success_at == T1, name


@pytest.mark.parametrize("job", ["crm_sync", "lead_match"])
async def test_linkedin_matching_interruption_survives_a_later_hubspot_defect(
    rig: Rig, contract_checked_transport: CheckedTransportFactory, job: str
) -> None:
    """A LinkedIn re-fetch 503 in a committed batch reaches ``on_error`` before the HubSpot 400
    a later batch raises.

    The first batch's re-fetch answers 503 to every retry, so its 100 rows commit as retried
    ``error`` with no lookup; the second batch's lookup by email is a HubSpot defect. LinkedIn
    turns ``stale``, HubSpot ``error``: the defect is the job's consequence and it fails, its
    unwritten datasets recording a failed attempt.
    """
    before = await good_first_sync(rig)
    ids = await two_batches(rig)
    due = (await rig.match_row(ids[-1])).next_attempt_at
    rig.transports["linkedin"] = contract_checked_transport(
        "linkedin", lead_platform(rig, {FIRST_BATCH: linkedin_unavailable()})
    )
    rig.portal.overrides.append(
        email_lookup_containing(
            "lead100@example.com", platform_error(400, "Invalid batch", "VALIDATION_ERROR")
        )
    )

    result = await (rig.crm_sync(T2) if job == "crm_sync" else rig.lead_match(T2))

    assert (result.status, result.disposition) == ("failed", "defect")
    assert (await rig.connection("linkedin")).status == "stale"
    assert (await rig.connection("hubspot")).status == "error"
    await assert_batches_kept(rig, ids, released_due=due)
    if job == "crm_sync":
        after = await rig.freshness()
        for name in ("hubspot.contacts", "hubspot.associations", "hubspot.deal_search"):
            assert_failed_attempt(before[name], after[name], attempted_at=T2)


@pytest.mark.parametrize("job", ["crm_sync", "lead_match"])
@pytest.mark.parametrize(("failing", "healthy"), [("linkedin", "hubspot"), ("hubspot", "linkedin")])
async def test_a_later_defect_of_the_same_source_stands_over_its_interruption(
    rig: Rig,
    contract_checked_transport: CheckedTransportFactory,
    job: str,
    failing: Source,
    healthy: Source,
) -> None:
    """A source's 503 in a committed batch is handed over before its own later 400.

    The first batch's re-fetch (LinkedIn) or lookup (HubSpot) answers 503 to every retry and
    commits; the second batch's call to the same source is a defect. The defect's
    consequence stands: the source ends ``error``, not ``stale``, and the other stays ``ok``.
    A LinkedIn defect lets the CRM sync go on; any other ends the job failed.
    """
    await good_first_sync(rig)
    ids = await two_batches(rig)
    due = (await rig.match_row(ids[-1])).next_attempt_at
    if failing == "linkedin":
        rig.transports["linkedin"] = contract_checked_transport(
            "linkedin",
            lead_platform(
                rig, {FIRST_BATCH: linkedin_unavailable(), SECOND_BATCH: linkedin_defect()}
            ),
        )
    else:
        rig.portal.overrides.append(email_lookup_containing("lead0@example.com", unavailable()))
        rig.portal.overrides.append(
            email_lookup_containing(
                "lead100@example.com", platform_error(400, "Invalid batch", "VALIDATION_ERROR")
            )
        )

    result = await (rig.crm_sync(T2) if job == "crm_sync" else rig.lead_match(T2))

    assert result.disposition == "defect"
    expected = "succeeded" if (job, failing) == ("crm_sync", "linkedin") else "failed"
    assert result.status == expected, result.error
    failed = await rig.connection(failing)
    assert (failed.status, failed.last_attempt_at) == ("error", T2)
    assert (await rig.connection(healthy)).status == "ok"
    await assert_batches_kept(rig, ids, released_due=due)


def build_pass(rig: Rig) -> _MatchPass:
    """The matching pass a live LinkedIn build runs in HubSpot's active primary."""
    return _MatchPass(rig.hubspot_id, "live", "live", rig.linkedin_id)


async def test_build_matching_interruption_reaches_on_error(rig: Rig) -> None:
    """A build's own matching pass hands a HubSpot lookup 503 to ``on_error``, with no dataset.

    ``SourceModeController._matching_pass`` (a live LinkedIn build's rows in HubSpot's active
    primary): the row ends retried ``error``, HubSpot turns ``stale``, LinkedIn stays ``ok``,
    and no freshness row records the failure, since matching state lives in ``lead_match``.
    """
    await good_first_sync(rig)
    spec = LeadSpec("resp-0002", "pending.lead@example.com", T2 - timedelta(hours=2))
    lead_id = await rig.add_lead(spec, staged_at=T2 - timedelta(hours=2))
    rig.portal.overrides.append(batch_read_unavailable("contacts", by_email=True))

    await rig.controller(T2)._matching_pass(build_pass(rig))

    hubspot = await rig.connection("hubspot")
    assert (hubspot.status, hubspot.last_attempt_at) == ("stale", T2)
    assert (await rig.connection("linkedin")).status == "ok"
    row = await rig.match_row(lead_id)
    assert (row.status, row.reason, row.next_attempt_at) == (
        "error",
        REASON_LOOKUP_FAILED,
        T2 + RETRY_DELAYS[0],
    )
    rows = await rig.freshness()
    for name in HUBSPOT_NAMES:
        assert rows[name].last_attempt_at == T1, name


async def test_build_matching_interruption_is_handed_over_before_a_raised_error(
    rig: Rig, contract_checked_transport: CheckedTransportFactory
) -> None:
    """A build's matching pass hands a committed batch's LinkedIn 503 over, then raises.

    The first batch's re-fetch answers 503 to every retry and commits; the second batch's
    lookup is a HubSpot defect, which propagates unchanged for the build to classify. LinkedIn
    is already ``stale``; HubSpot's connection is left to that classification.
    """
    await good_first_sync(rig)
    ids = await two_batches(rig)
    due = (await rig.match_row(ids[-1])).next_attempt_at
    rig.transports["linkedin"] = contract_checked_transport(
        "linkedin", lead_platform(rig, {FIRST_BATCH: linkedin_unavailable()})
    )
    rig.portal.overrides.append(
        email_lookup_containing(
            "lead100@example.com", platform_error(400, "Invalid batch", "VALIDATION_ERROR")
        )
    )

    with pytest.raises(QueryDefect) as raised:
        await rig.controller(T2)._matching_pass(build_pass(rig))

    assert raised.value.source == "hubspot"
    linkedin = await rig.connection("linkedin")
    assert (linkedin.status, linkedin.last_attempt_at) == ("stale", T2)
    assert (await rig.connection("hubspot")).status == "ok"
    await assert_batches_kept(rig, ids, released_due=due)
