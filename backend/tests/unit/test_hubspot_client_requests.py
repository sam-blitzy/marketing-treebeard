"""Request, batching and status tests for ``ads_engine.connectors.hubspot.client``.

Covers every call :class:`HubSpotClient` makes and the cross-module contracts it keeps:

* paths: each surface's dated ``2026-09`` path, matched against the URL template of its
  contract file, never a numbered ``/v1`` to ``/v4`` path; the Bearer token and JSON headers;
* objects batch reads (contacts by email or id, meetings, deals): at most 100 inputs per call,
  ``idProperty=email`` only for the email lookup, ``OBJECT_NOT_FOUND`` read through the
  parser's hashed ``BatchError.email_sha256s`` so no plaintext address is kept, every other
  error or failed call recorded per input, and the ``@sim.invalid`` isolation rule;
* target reads: ``ok``, ``partial`` when an id is omitted or a chunk fails, ``error`` when
  none is read; a contact refresh that omits a contact reads ``error``;
* associations batch reads: at most 1,000 inputs, an initial body of ids only, continuations
  re-sending ``{id, after}`` only for paged inputs, ``NO_ASSOCIATIONS_FOUND`` as ``ok_empty``,
  every other 207 error as ``error``, a status other than ``COMPLETE`` as a failed call, a
  failed continuation as ``partial`` and a repeated cursor stopped as ``partial``;
* CRM Search: the body checks made before sending (fewer than 3,000 characters, ``limit``
  from 1 to 200, no cursor at the 10,000-result ceiling) and the partitioned search through
  the client: a day above 10,000 split into hours, an hour above 10,000 read to the ceiling
  and marked incomplete;
* binding verification: stage labels resolved against ``lifecyclestage`` options, unavailable
  bindings with their reasons (never zero), property mode with a 404, the Google link;
* the recovery probe; the 4 req/s throttle on every attempt; a 429 ``Retry-After`` retried
  after the platform's wait; no token or email in any captured log event.

Every success body is rendered from ``backend/tests/contracts/hubspot/*.json``: it is the
contract's ``documented_sample`` verbatim where a test serves one, or uses only fields the
contract declares, and it is checked against the contract's response schema before it is
served, so a body carrying an undeclared field fails the test. HubSpot's error envelope
``{status, message, category}`` of 4xx and 5xx responses is the one body the contracts do not
cover. Every request is checked against its contract's request shape: method, path template,
required headers, the request body schema, the input limit and the search body limits.

Every call goes through ``build_client("hubspot", httpx.MockTransport(...))`` and the real
``send_json``. Retries record their waits instead of sleeping, and the throttle runs on a fake
clock. No test touches the network, the database or a real token.
"""

import copy
import functools
import json
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from structlog.typing import EventDict

from ads_engine.analysis.thresholds import (
    CrmBindings,
    GoogleCrmBinding,
    HubSpotMapping,
    ResolvedBinding,
    StageBinding,
)
from ads_engine.connectors.errors import AccessDenied, QueryDefect, TransientError
from ads_engine.connectors.http import RetryPolicy, build_client
from ads_engine.connectors.hubspot.client import (
    HUBSPOT_API_VERSION,
    MAX_ASSOCIATION_INPUTS,
    MAX_BATCH_READ_INPUTS,
    MAX_SEARCH_BODY_CHARS,
    MAX_SEARCH_LIMIT,
    PROBE_EMAIL,
    SEARCH_RESULT_CEILING,
    SHARED_LIMITER,
    STAGE_DATE_PROPERTY_PREFIX,
    ContactBatch,
    EmailLookup,
    HubSpotClient,
    InputAssociations,
    RateLimiter,
    TargetRead,
    contact_property_map,
)
from ads_engine.connectors.hubspot.parser import (
    DEAL_PROPERTIES,
    MEETING_PROPERTIES,
    NO_ASSOCIATIONS_FOUND,
    ContactPropertyMap,
    email_sha256,
)
from ads_engine.connectors.hubspot.search import (
    CEILING_REASON,
    deal_search_filter,
    epoch_ms,
    google_contacts_filter,
    search_body,
)
from ads_engine.settings import Settings

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "hubspot"
SURFACES = ("objects_batch_read", "crm_search", "associations_batch_read", "properties")
BASE_URL = "https://api.hubapi.com"
TOKEN = "test-token"
STAMP = "2026-09-04T13:05:22.123Z"
DAY0 = datetime(2026, 9, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
HOUR_MS = 3_600_000

CAMPAIGN_PROPERTY = "google_campaign_id"
TOUCH_PROPERTY = "google_touch_date"
MQL_PROPERTY = "date_first_meeting_was_booked"
QL_STAGE_ID = "1406943076"
QL_PROPERTY = f"{STAGE_DATE_PROPERTY_PREFIX}{QL_STAGE_ID}"
PROPS = ContactPropertyMap(mql_property=MQL_PROPERTY, ql_property=QL_PROPERTY)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture
def captured() -> Iterator[list[EventDict]]:
    """Capture every structlog event as its raw event dict."""
    with capture_logs() as entries:
        yield entries


def _events(entries: Sequence[EventDict], name: str) -> list[EventDict]:
    """Return the captured events called ``name``, in order."""
    return [entry for entry in entries if entry.get("event") == name]


# ---------------------------------------------------------------------------------------------
# Contract files: loading, the response-schema check and the request-shape check
# ---------------------------------------------------------------------------------------------

# The JSON Schema keywords the HubSpot contracts use; any other keyword fails the check.
_SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "required", "items", "enum", "additionalProperties", "description"}
)
_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: (
        isinstance(value, int | float | Decimal) and not isinstance(value, bool)
    ),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


@functools.cache
def _contract(surface: str) -> Mapping[str, Any]:
    """Load ``contracts/hubspot/{surface}.json``."""
    loaded: Mapping[str, Any] = json.loads((CONTRACTS / f"{surface}.json").read_text("utf-8"))
    return loaded


def _sample(surface: str) -> Any:
    """Return a private deep copy of the contract's documented sample response."""
    sample = _contract(surface)["response"]["documented_sample"]
    assert sample is not None, f"{surface} has no documented sample"
    return copy.deepcopy(sample)


def _schema_errors(value: object, schema: Mapping[str, Any], path: str) -> list[str]:
    """Validate ``value`` against a contract schema; objects are closed unless declared open."""
    unknown = sorted(set(schema) - _SCHEMA_KEYWORDS)
    if unknown:
        return [f"{path}: unsupported schema keywords {unknown}"]
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_JSON_TYPES[name](value) for name in names):
            return [f"{path}: {type(value).__name__} is not {names}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not a declared enum value")
    if isinstance(value, Mapping):
        properties: Mapping[str, Any] = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        errors.extend(
            f"{path}: required field {key!r} is missing"
            for key in schema.get("required", ())
            if key not in value
        )
        for key, item in value.items():
            if key in properties:
                errors.extend(_schema_errors(item, properties[key], f"{path}.{key}"))
            elif isinstance(additional, Mapping):
                errors.extend(_schema_errors(item, additional, f"{path}.{key}"))
            elif additional is not True:
                errors.append(f"{path}: field {key!r} is not defined by the contract")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


@functools.cache
def _routes() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Compile each contract's URL template into a path pattern with named segments."""
    routes: list[tuple[str, re.Pattern[str]]] = []
    for surface in SURFACES:
        template: str = _contract(surface)["request"]["url"]
        assert template.startswith(BASE_URL), template
        escaped = re.escape(template[len(BASE_URL) :])
        pattern = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", escaped)
        routes.append((surface, re.compile(pattern)))
    return tuple(routes)


@dataclass(frozen=True, slots=True)
class Call:
    """One request the fake portal received, decoded and checked against its contract.

    Attributes:
        surface: The contract surface whose URL template the path matched.
        path: The raw request path, percent-encoding kept.
        params: The template's path segments, for example ``objectType``.
        body: The decoded JSON body; ``None`` for a ``GET``.
        request: The request itself.
    """

    surface: str
    path: str
    params: Mapping[str, str]
    body: Any
    request: httpx.Request


def _decode_call(request: httpx.Request) -> Call:
    """Match ``request`` to its contract and assert it has the contract's request shape."""
    assert request.url.scheme == "https"
    assert request.url.host == "api.hubapi.com"
    raw_path, _, query = request.url.raw_path.decode("ascii").partition("?")
    assert query == "", f"unexpected query {query!r}"
    assert re.search(r"/v[0-9]+/", raw_path) is None, raw_path
    matched = [
        (surface, found.groupdict())
        for surface, pattern in _routes()
        if (found := pattern.fullmatch(raw_path)) is not None
    ]
    assert len(matched) == 1, f"no single contract path matches {raw_path}"
    surface, params = matched[0]
    contract = _contract(surface)["request"]
    assert request.method == contract["method"], (surface, request.method)
    for header in contract["required_headers"]:
        assert header in request.headers, f"{surface}: missing header {header}"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept"] == "application/json"
    body: Any = None
    if contract["body"] is None:
        assert request.content == b""
        assert "Content-Type" not in request.headers
    else:
        assert request.headers["Content-Type"] == "application/json"
        body = json.loads(request.content)
        errors = _schema_errors(body, contract["body"], f"{surface} request")
        assert errors == [], errors
        limits: Mapping[str, int] = contract["limits"]
        if "max_inputs" in limits:
            assert 1 <= len(body["inputs"]) <= limits["max_inputs"], len(body["inputs"])
        if surface == "crm_search":
            assert len(request.content) < limits["max_body_chars"]
            assert 1 <= body["limit"] <= limits["page_size"]
    return Call(surface=surface, path=raw_path, params=params, body=body, request=request)


# ---------------------------------------------------------------------------------------------
# The fake portal and the client under test
# ---------------------------------------------------------------------------------------------

type Outcome = httpx.Response | Exception
type Route = Callable[[Call], Outcome]


class Server:
    """Answers each checked request through ``route`` and records every call.

    ``waits`` records the seconds the retry policy would sleep. ``limiter`` is a 4 req/s
    :class:`RateLimiter` on a fake clock that only its own sleeps advance, and
    ``throttle_waits`` records those sleeps.
    """

    def __init__(self, route: Route) -> None:
        self._route = route
        self.calls: list[Call] = []
        self.waits: list[float] = []
        self.throttle_waits: list[float] = []
        self.now = 0.0
        self.limiter = RateLimiter(4.0, clock=self._clock, sleep=self._throttle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Check one request against its contract and answer it."""
        call = _decode_call(request)
        self.calls.append(call)
        outcome = self._route(call)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sleep(self, seconds: float) -> None:
        """Record a retry wait instead of sleeping."""
        self.waits.append(seconds)

    def _clock(self) -> float:
        """Return the fake monotonic time."""
        return self.now

    async def _throttle(self, seconds: float) -> None:
        """Advance the fake clock instead of sleeping."""
        self.throttle_waits.append(seconds)
        self.now += seconds

    def bodies(self, surface: str) -> list[Any]:
        """Return the request bodies sent to ``surface``, in order."""
        return [call.body for call in self.calls if call.surface == surface]


def scripted(*outcomes: Outcome) -> Route:
    """Return a route answering each request with the next outcome; extra requests fail."""
    pending = list(outcomes)

    def route(call: Call) -> Outcome:
        if not pending:
            raise AssertionError(f"unexpected request {call.request.method} {call.path}")
        return pending.pop(0)

    return route


@asynccontextmanager
async def _client(server: Server, *, live: bool = True) -> AsyncIterator[HubSpotClient]:
    """Yield a client sending through ``server``, with no-sleep retries and a fake throttle."""
    transport = httpx.MockTransport(server.handle)
    async with build_client("hubspot", transport, settings=Settings()) as http:
        yield HubSpotClient(
            http,
            SecretStr(TOKEN),
            live=live,
            retry=RetryPolicy(sleep=server.sleep),
            limiter=server.limiter,
        )


# ---------------------------------------------------------------------------------------------
# Contract-rendered bodies
# ---------------------------------------------------------------------------------------------


def _respond(surface: str, body: Mapping[str, Any], status: int = 200) -> httpx.Response:
    """Serve ``body`` after asserting it satisfies the surface's response contract."""
    response_contract = _contract(surface)["response"]
    assert status in response_contract["status"], (surface, status)
    errors = _schema_errors(body, response_contract["body"], f"{surface} response")
    assert errors == [], errors
    return httpx.Response(status, json=body)


def _hubspot_error(status: int, category: str, message: str, **headers: str) -> httpx.Response:
    """Return HubSpot's error envelope ``{status, message, category}``."""
    body = {"status": "error", "message": message, "category": category}
    return httpx.Response(status, json=body, headers=headers)


def _unavailable() -> httpx.Response:
    """Return a transient HTTP 503."""
    return _hubspot_error(503, "SERVICE_UNAVAILABLE", "Service Unavailable")


def _outage() -> tuple[httpx.Response, ...]:
    """Return one 503 per attempt of the default policy, so the call fails transiently."""
    return tuple(_unavailable() for _ in range(RetryPolicy().attempts))


def _record(hs_id: str, properties: Mapping[str, str | None]) -> dict[str, Any]:
    """Return one CRM object in the batch read and search result shape."""
    return {
        "id": hs_id,
        "properties": {"hs_object_id": hs_id, **properties},
        "createdAt": STAMP,
        "updatedAt": STAMP,
        "archived": False,
    }


def _contact(
    hs_id: str, email: str | None, extra: Mapping[str, str | None] | None = None
) -> dict[str, Any]:
    """Return a contact with its email, stage and last-modified date."""
    properties: dict[str, str | None] = {
        "email": email,
        "lifecyclestage": "lead",
        "lastmodifieddate": STAMP,
    }
    properties.update(extra or {})
    return _record(hs_id, properties)


def _meeting(hs_id: str) -> dict[str, Any]:
    """Return a scheduling-page meeting carrying every requested meeting property."""
    return _record(
        hs_id,
        {
            "hs_createdate": STAMP,
            "hs_timestamp": "2026-09-10T15:00:00Z",
            "hs_meeting_start_time": "2026-09-10T15:00:00Z",
            "hs_meeting_outcome": "scheduled",
            "hs_meeting_source": "MEETINGS_PUBLIC",
        },
    )


def _deal(hs_id: str) -> dict[str, Any]:
    """Return a deal carrying every requested deal property."""
    return _record(
        hs_id,
        {
            "createdate": STAMP,
            "dealstage": "appointmentscheduled",
            "pipeline": "default",
            "amount": "1500.50",
        },
    )


def _batch_error(
    category: str,
    ids: Sequence[str],
    *,
    key: str = "ids",
    sub_category: str | None = None,
    message: str = "Could not get some objects, they may be deleted or not exist.",
) -> dict[str, Any]:
    """Return one ``errors[]`` entry of a 207 multi-status response."""
    entry: dict[str, Any] = {
        "status": "error",
        "category": category,
        "message": message,
        "context": {key: list(ids)},
    }
    if sub_category is not None:
        entry["subCategory"] = sub_category
    return entry


def _no_associations(*ids: str) -> dict[str, Any]:
    """Return HubSpot's live "no associations" entry for ``ids``."""
    return _batch_error(
        "OBJECT_NOT_FOUND",
        ids,
        key="fromObjectId",
        sub_category=NO_ASSOCIATIONS_FOUND,
        message="No associations found for the requested records.",
    )


def _batch(
    surface: str,
    results: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]] = (),
    *,
    status: str = "COMPLETE",
) -> httpx.Response:
    """Return a batch read response: 200, or 207 when it carries ``errors``."""
    body: dict[str, Any] = {
        "status": status,
        "results": list(results),
        "startedAt": STAMP,
        "completedAt": STAMP,
    }
    if errors:
        body["errors"] = list(errors)
        body["numErrors"] = len(errors)
    return _respond(surface, body, 207 if errors else 200)


def _objects(
    results: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]] = (),
    *,
    status: str = "COMPLETE",
) -> httpx.Response:
    """Return an objects batch read response."""
    return _batch("objects_batch_read", results, errors, status=status)


def _assoc(from_id: str, targets: Sequence[int], after: str | None = None) -> dict[str, Any]:
    """Return one input's associations; target ids are JSON numbers, as live responses send."""
    result: dict[str, Any] = {
        "from": {"id": from_id},
        "to": [
            {
                "toObjectId": target,
                "associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": 200, "label": None}],
            }
            for target in targets
        ],
    }
    if after is not None:
        result["paging"] = {"next": {"after": after}}
    return result


def _associations(
    results: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]] = (),
    *,
    status: str = "COMPLETE",
) -> httpx.Response:
    """Return an associations batch read response."""
    return _batch("associations_batch_read", results, errors, status=status)


def _page(
    records: Sequence[Mapping[str, Any]], total: int, after: str | None = None
) -> httpx.Response:
    """Return one CRM Search page."""
    body: dict[str, Any] = {"total": total, "results": list(records)}
    if after is not None:
        body["paging"] = {"next": {"after": after}}
    return _respond("crm_search", body)


type Option = tuple[str, str, bool]


def _definition(
    name: str, data_type: str, field_type: str, options: Sequence[Option] = ()
) -> dict[str, Any]:
    """Return a property definition; ``options`` are ``(label, value, hidden)``."""
    return {
        "description": "",
        "fieldType": field_type,
        "groupName": "contactinformation",
        "label": name,
        "name": name,
        "options": [
            {"hidden": hidden, "label": label, "value": value} for label, value, hidden in options
        ],
        "type": data_type,
    }


LIFECYCLE_OPTIONS: tuple[Option, ...] = (
    ("Subscriber", "subscriber", False),
    ("Lead", "lead", False),
    ("Marketing Qualified Lead", "marketingqualifiedlead", False),
    ("Qualified Lead", QL_STAGE_ID, False),
    ("Sales Qualified Lead", "salesqualifiedlead", False),
    ("Customer", "customer", False),
)


def _lifecycle(options: Sequence[Option] = LIFECYCLE_OPTIONS) -> dict[str, Any]:
    """Return the ``lifecyclestage`` definition of a portal like the configured one."""
    return _definition("lifecyclestage", "enumeration", "radio", options)


def _datetime_property(name: str, data_type: str = "datetime") -> dict[str, Any]:
    """Return a date or datetime contact property definition."""
    return _definition(name, data_type, "date")


def _properties_route(definitions: Mapping[str, Mapping[str, Any]]) -> Route:
    """Serve contact property definitions by name; any other name is a 404."""

    def route(call: Call) -> Outcome:
        assert call.surface == "properties"
        assert call.params["objectType"] == "contacts"
        definition = definitions.get(unquote(call.params["propertyName"]))
        if definition is None:
            return _hubspot_error(404, "OBJECT_NOT_FOUND", "Unable to find the property")
        return _respond("properties", definition)

    return route


def _property_names(server: Server) -> list[str]:
    """Return the property names the client read, in order."""
    return [unquote(call.params["propertyName"]) for call in server.calls]


def _stage(*labels: str) -> StageBinding:
    """Return a ``stage_label`` binding."""
    return StageBinding(mode="stage_label", stage_labels=labels, property=None)


def _property_binding(name: str, *labels: str) -> StageBinding:
    """Return a ``property`` binding with optional display labels."""
    return StageBinding(mode="property", stage_labels=labels, property=name)


def _mapping(mql: StageBinding, ql: StageBinding) -> HubSpotMapping:
    """Return a HubSpot mapping with the given bindings."""
    return HubSpotMapping(
        mql=mql,
        ql=ql,
        booking_sources=("MEETINGS_PUBLIC",),
        meeting_outcome_exclusions=("CANCELED", "NO_SHOW"),
    )


def _google_settings(
    campaign: str | None = CAMPAIGN_PROPERTY, touch: str | None = TOUCH_PROPERTY
) -> Settings:
    """Return settings naming the Google link properties."""
    return Settings(
        hubspot_google_campaign_property=campaign, hubspot_google_touch_date_property=touch
    )


def _no_google() -> Settings:
    """Return settings without the Google link."""
    return _google_settings(None, None)


def _inputs(body: Mapping[str, Any]) -> list[str]:
    """Return the input ids of a batch request body."""
    return [item["id"] for item in body["inputs"]]


def _range(body: Mapping[str, Any], property_name: str) -> tuple[int, int]:
    """Return the ``[GTE, LT)`` epoch-millisecond range a search body filters on."""
    bounds = {
        item["operator"]: int(item["value"])
        for item in body["filterGroups"][0]["filters"]
        if item["propertyName"] == property_name and item["operator"] in {"GTE", "LT"}
    }
    return bounds["GTE"], bounds["LT"]


# ---------------------------------------------------------------------------------------------
# Contract alignment, paths and headers
# ---------------------------------------------------------------------------------------------


def test_constants_follow_the_contract_limits() -> None:
    assert HUBSPOT_API_VERSION == "2026-09"
    for surface in SURFACES:
        contract = _contract(surface)
        assert contract["api_version"] == HUBSPOT_API_VERSION
        assert contract["reference_url"].startswith("https://developers.hubspot.com/docs/")
        assert contract["last_modified"]
        assert f"/{HUBSPOT_API_VERSION}/" in contract["request"]["url"]
    objects_limits = _contract("objects_batch_read")["request"]["limits"]
    assert objects_limits["max_inputs"] == MAX_BATCH_READ_INPUTS
    associations_limits = _contract("associations_batch_read")["request"]["limits"]
    assert associations_limits["max_inputs"] == MAX_ASSOCIATION_INPUTS
    search_limits = _contract("crm_search")["request"]["limits"]
    assert search_limits["page_size"] == MAX_SEARCH_LIMIT
    assert search_limits["max_body_chars"] == MAX_SEARCH_BODY_CHARS
    assert search_limits["max_results"] == SEARCH_RESULT_CEILING


@pytest.mark.anyio
async def test_every_surface_uses_its_dated_2026_09_path() -> None:
    def route(call: Call) -> Outcome:
        if call.surface == "properties":
            return _respond("properties", _definition("email", "string", "text"))
        if call.surface == "objects_batch_read":
            return _objects([])
        if call.surface == "associations_batch_read":
            return _associations([])
        return _page([], 0)

    server = Server(route)
    async with _client(server) as client:
        await client.property("contacts", "email")
        await client.contacts_by_id(["1"], PROPS)
        await client.meetings(["2"])
        await client.deals(["3"])
        await client.associations("contacts", "meetings", ["4"])
        await client.associations("deals", "contacts", ["5"])
        await client.search_page("deals", search_body(deal_search_filter(), DAY0, DAY0 + HOUR))

    assert [(call.surface, call.request.method, call.path) for call in server.calls] == [
        ("properties", "GET", "/crm/properties/2026-09/contacts/email"),
        ("objects_batch_read", "POST", "/crm/objects/2026-09/contacts/batch/read"),
        ("objects_batch_read", "POST", "/crm/objects/2026-09/meetings/batch/read"),
        ("objects_batch_read", "POST", "/crm/objects/2026-09/deals/batch/read"),
        (
            "associations_batch_read",
            "POST",
            "/crm/associations/2026-09/contacts/meetings/batch/read",
        ),
        (
            "associations_batch_read",
            "POST",
            "/crm/associations/2026-09/deals/contacts/batch/read",
        ),
        ("crm_search", "POST", "/crm/objects/2026-09/deals/search"),
    ]
    assert client.calls == len(server.calls)


@pytest.mark.anyio
async def test_property_name_is_sent_as_one_encoded_path_segment() -> None:
    server = Server(scripted(_hubspot_error(404, "OBJECT_NOT_FOUND", "Unable to find")))
    async with _client(server) as client:
        assert await client.property("contacts", "bad name/x") is None
    assert server.calls[0].path == "/crm/properties/2026-09/contacts/bad%20name%2Fx"


@pytest.mark.anyio
async def test_property_decodes_the_documented_sample() -> None:
    server = Server(scripted(_respond("properties", _sample("properties"))))
    async with _client(server) as client:
        definition = await client.property("contacts", "lifecyclestage")
    assert definition is not None
    assert definition.name == "<string>"
    assert definition.type == "<string>"
    assert definition.field_type == "<string>"
    assert [(option.label, option.value, option.hidden) for option in definition.options] == [
        ("<string>", "<string>", True)
    ]
    assert definition.is_datetime is False


@pytest.mark.anyio
async def test_invalid_arguments_raise_before_any_call() -> None:
    server = Server(scripted())
    async with _client(server) as client:
        with pytest.raises(ValueError, match="property name must not be blank"):
            await client.property("contacts", "  ")
        with pytest.raises(ValueError, match="object_type must be a CRM object type"):
            await client.property("contacts/../owners", "email")
        with pytest.raises(ValueError, match="from_type must be a CRM object type"):
            await client.associations("Contacts", "meetings", ["1"])
        with pytest.raises(ValueError, match="to_type must be a CRM object type"):
            await client.associations("contacts", "", ["1"])
        with pytest.raises(ValueError, match="not one string"):
            await client.associations("contacts", "meetings", "501")
        with pytest.raises(ValueError, match="emails must be an iterable"):
            await client.contacts_by_email("lead.one@example.com", PROPS)
        with pytest.raises(ValueError, match="every email must be a string"):
            await client.contacts_by_email([42], PROPS)  # type: ignore[list-item]
        with pytest.raises(ValueError, match="not one string"):
            await client.contacts_by_id("201", PROPS)
        with pytest.raises(ValueError, match="must not contain a blank id"):
            await client.contacts_by_id(["201", "  "], PROPS)
        with pytest.raises(ValueError, match="must be a string id"):
            await client.meetings([True])  # type: ignore[list-item]
        with pytest.raises(ValueError, match="must be a string id"):
            await client.deals([1.5])  # type: ignore[list-item]
        with pytest.raises(ValueError, match="object_type must be a CRM object type"):
            await client.search_page("deals/x", {"limit": 1})
    assert server.calls == []


def test_client_construction_and_from_settings() -> None:
    http = httpx.AsyncClient()
    with pytest.raises(ValueError, match="token must not be blank"):
        HubSpotClient(http, SecretStr("  "), live=True)
    with pytest.raises(ValueError, match="HUBSPOT_ACCESS_TOKEN is not configured"):
        HubSpotClient.from_settings(Settings(hubspot_access_token=None), http)
    with pytest.raises(ValueError, match="HUBSPOT_ACCESS_TOKEN is not configured"):
        HubSpotClient.from_settings(Settings(hubspot_access_token=SecretStr(" ")), http)
    live = HubSpotClient.from_settings(Settings(hubspot_access_token=SecretStr(TOKEN)), http)
    simulated = HubSpotClient.from_settings(
        Settings(hubspot_access_token=SecretStr(TOKEN)), http, live=False
    )
    assert live.live is True
    assert simulated.live is False
    assert live.calls == 0


@pytest.mark.anyio
async def test_from_settings_sends_the_configured_token_as_bearer() -> None:
    server = Server(scripted(_respond("properties", _definition("email", "string", "text"))))
    transport = httpx.MockTransport(server.handle)
    async with build_client("hubspot", transport, settings=Settings()) as http:
        client = HubSpotClient.from_settings(
            Settings(hubspot_access_token=SecretStr(TOKEN)),
            http,
            retry=RetryPolicy(sleep=server.sleep),
            limiter=server.limiter,
        )
        definition = await client.property("contacts", "email")
    assert definition is not None
    assert definition.name == "email"
    assert server.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------------------------------------
# Throttle, retries and redaction
# ---------------------------------------------------------------------------------------------


def test_rate_limiter_rejects_invalid_rates() -> None:
    for rate in (0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite number above 0"):
            RateLimiter(rate)
    for wrong in (True, "4"):
        with pytest.raises(ValueError, match="must be a number"):
            RateLimiter(wrong)  # type: ignore[arg-type]
    assert SHARED_LIMITER.rate_per_second == 4.0
    assert RateLimiter(2).rate_per_second == 2.0


@pytest.mark.anyio
async def test_rate_limiter_spaces_requests_and_skips_waits_after_idle() -> None:
    now = [10.0]
    waits: list[float] = []

    async def advance(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(4.0, clock=lambda: now[0], sleep=advance)
    for _ in range(3):
        await limiter.acquire()
    assert waits == [0.25, 0.25]
    now[0] += 5.0
    await limiter.acquire()
    assert waits == [0.25, 0.25]


@pytest.mark.anyio
async def test_every_attempt_awaits_the_4_per_second_throttle() -> None:
    definition = _definition("email", "string", "text")
    server = Server(
        scripted(
            _respond("properties", definition),
            _unavailable(),
            _respond("properties", definition),
            _respond("properties", definition),
        )
    )
    async with _client(server) as client:
        for _ in range(3):
            await client.property("contacts", "email")
    assert len(server.calls) == 4
    # Four attempts, the retry included, each reserve the next 0.25 s slot.
    assert server.throttle_waits == [0.25, 0.25, 0.25]
    assert client.calls == 3


@pytest.mark.anyio
async def test_429_waits_the_retry_after_then_retries() -> None:
    rate_limited = _hubspot_error(
        429, "RATE_LIMITS", "You have reached your secondly limit.", **{"Retry-After": "7"}
    )
    server = Server(
        scripted(rate_limited, _respond("properties", _definition("email", "string", "text")))
    )
    async with _client(server) as client:
        definition = await client.property("contacts", "email")
    assert definition is not None
    assert server.waits == [7.0]
    assert len(server.calls) == 2
    assert client.calls == 1


@pytest.mark.anyio
async def test_429_past_the_retry_after_bound_is_not_retried_and_reads_error() -> None:
    rate_limited = _hubspot_error(
        429, "RATE_LIMITS", "You have reached your daily limit.", **{"Retry-After": "600"}
    )
    server = Server(scripted(rate_limited))
    async with _client(server) as client:
        batch = await client.contacts_by_id(["201"], PROPS)
    assert batch.read_status == {"201": "error"}
    assert server.waits == []
    assert len(server.calls) == 1


@pytest.mark.anyio
async def test_logs_never_carry_the_token_or_an_email(captured: list[EventDict]) -> None:
    email = "lead.one@example.com"
    server = Server(
        scripted(
            *_outage(),
            _objects([], [_batch_error("OBJECT_NOT_FOUND", [email])]),
            _hubspot_error(401, "INVALID_AUTHENTICATION", "Authentication credentials not found."),
        )
    )
    async with _client(server) as client:
        failed = await client.contacts_by_email([email], PROPS)
        missing = await client.contacts_by_email([email], PROPS)
        with pytest.raises(AccessDenied) as denied:
            await client.contacts_by_id(["201"], PROPS)
    assert failed.failed == {email_sha256(email)}
    assert missing.not_found == {email_sha256(email)}
    assert denied.value.http_status == 401
    assert _events(captured, "http.retry")
    assert _events(captured, "connector.error")
    text = repr(captured)
    assert TOKEN not in text
    assert email not in text
    assert "Bearer" not in text
    assert TOKEN not in str(denied.value)


# ---------------------------------------------------------------------------------------------
# Contacts by email (lead matching)
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_contacts_by_email_sends_idproperty_email_and_maps_back_by_digest(
    captured: list[EventDict],
) -> None:
    emails = [
        "  Lead.One@Example.com ",
        "lead.one@example.com",
        "lead.two@example.com",
        "lead.three@example.com",
        "lead.four@example.com",
        "alias.five@example.com",
    ]
    contact_one = _contact("101", "lead.one@example.com", {MQL_PROPERTY: "2026-09-05"})
    contact_five = _contact(
        "105",
        "primary.five@example.com",
        {"hs_additional_emails": "other@example.com;alias.five@example.com"},
    )
    server = Server(
        scripted(
            _objects(
                [contact_one, contact_five],
                [
                    _batch_error("OBJECT_NOT_FOUND", ["lead.two@example.com"]),
                    _batch_error(
                        "VALIDATION_ERROR", ["lead.three@example.com"], message="Invalid input"
                    ),
                ],
            )
        )
    )
    async with _client(server) as client:
        lookup = await client.contacts_by_email(emails, PROPS)

    [body] = server.bodies("objects_batch_read")
    assert body == {
        "idProperty": "email",
        "inputs": [
            {"id": "lead.one@example.com"},
            {"id": "lead.two@example.com"},
            {"id": "lead.three@example.com"},
            {"id": "lead.four@example.com"},
            {"id": "alias.five@example.com"},
        ],
        "properties": list(PROPS.request_properties()),
    }
    assert server.calls[0].path == "/crm/objects/2026-09/contacts/batch/read"
    one, two, three, four, five = (
        email_sha256(address)
        for address in (
            "lead.one@example.com",
            "lead.two@example.com",
            "lead.three@example.com",
            "lead.four@example.com",
            "alias.five@example.com",
        )
    )
    assert set(lookup.found) == {one, five}
    assert lookup.found[one].hs_id == "101"
    assert lookup.found[one].mql_at == datetime(2026, 9, 5, tzinfo=UTC)
    assert lookup.found[five].hs_id == "105"
    assert lookup.not_found == {two}
    assert lookup.failed == {three, four}
    assert lookup.skipped == frozenset()
    # Results hold digests only: no plaintext address survives the call.
    keys = {*lookup.found, *lookup.not_found, *lookup.failed}
    assert all(re.fullmatch(r"[0-9a-f]{64}", key) for key in keys)
    assert repr(lookup) == "EmailLookup(found=2, not_found=1, failed=2, skipped=0)"
    [event] = _events(captured, "hubspot.batch_read")
    assert event["id_property"] == "email"
    assert event["calls"] == 1
    assert (event["found"], event["not_found"], event["failed"]) == (2, 1, 2)


@pytest.mark.anyio
async def test_contacts_by_email_precedence_found_then_failure_then_not_found() -> None:
    returned = "returned@example.com"
    conflicting = "conflicting@example.com"
    server = Server(
        scripted(
            _objects(
                [_contact("110", returned)],
                [
                    _batch_error("VALIDATION_ERROR", [returned], message="Invalid input"),
                    _batch_error("OBJECT_NOT_FOUND", [conflicting]),
                    _batch_error("VALIDATION_ERROR", [conflicting], message="Invalid input"),
                ],
            )
        )
    )
    async with _client(server) as client:
        lookup = await client.contacts_by_email([returned, conflicting], PROPS)
    assert set(lookup.found) == {email_sha256(returned)}
    assert lookup.failed == {email_sha256(conflicting)}
    assert lookup.not_found == frozenset()


@pytest.mark.anyio
async def test_contacts_by_email_reads_at_most_100_addresses_per_call() -> None:
    emails = [f"lead.{index:03d}@example.com" for index in range(101)]

    def route(call: Call) -> Outcome:
        return _objects([], [_batch_error("OBJECT_NOT_FOUND", _inputs(call.body))])

    server = Server(route)
    async with _client(server) as client:
        lookup = await client.contacts_by_email(emails, PROPS)
    assert [len(body["inputs"]) for body in server.bodies("objects_batch_read")] == [100, 1]
    assert [_inputs(body) for body in server.bodies("objects_batch_read")] == [
        emails[:100],
        emails[100:],
    ]
    assert lookup.not_found == {email_sha256(address) for address in emails}
    assert client.calls == 2


@pytest.mark.anyio
async def test_live_client_never_sends_simulated_addresses(captured: list[EventDict]) -> None:
    def route(call: Call) -> Outcome:
        return _objects([], [_batch_error("OBJECT_NOT_FOUND", _inputs(call.body))])

    server = Server(route)
    async with _client(server, live=True) as client:
        lookup = await client.contacts_by_email(
            [
                "sim-lead@sim.invalid",
                " SIM-2@SIM.INVALID ",
                "real@example.com",
                # Only the exact simulated domain is withheld; a domain containing it is sent.
                "x@sim.invalid.example.com",
            ],
            PROPS,
        )
        only_simulated = await client.contacts_by_email(["other@sim.invalid"], PROPS)

    assert server.bodies("objects_batch_read") == [
        {
            "idProperty": "email",
            "inputs": [{"id": "real@example.com"}, {"id": "x@sim.invalid.example.com"}],
            "properties": list(PROPS.request_properties()),
        }
    ]
    assert lookup.skipped == {
        email_sha256("sim-lead@sim.invalid"),
        email_sha256("sim-2@sim.invalid"),
    }
    assert lookup.not_found == {
        email_sha256("real@example.com"),
        email_sha256("x@sim.invalid.example.com"),
    }
    assert only_simulated.skipped == {email_sha256("other@sim.invalid")}
    assert len(server.calls) == 1
    withheld = _events(captured, "hubspot.batch_read.simulated_withheld")
    assert [event["count"] for event in withheld] == [2, 1]


@pytest.mark.anyio
async def test_simulated_client_sends_simulated_addresses() -> None:
    def route(call: Call) -> Outcome:
        return _objects([_contact("120", "sim-lead@sim.invalid")])

    server = Server(route)
    async with _client(server, live=False) as client:
        lookup = await client.contacts_by_email(["sim-lead@sim.invalid"], PROPS)
    assert _inputs(server.bodies("objects_batch_read")[0]) == ["sim-lead@sim.invalid"]
    assert set(lookup.found) == {email_sha256("sim-lead@sim.invalid")}
    assert lookup.skipped == frozenset()


@pytest.mark.anyio
async def test_contacts_by_email_failed_call_marks_only_its_chunk_failed(
    captured: list[EventDict],
) -> None:
    emails = [f"lead.{index:03d}@example.com" for index in range(101)]
    server = Server(
        scripted(*_outage(), _objects([], [_batch_error("OBJECT_NOT_FOUND", emails[100:])]))
    )
    async with _client(server) as client:
        lookup = await client.contacts_by_email(emails, PROPS)
    assert lookup.failed == {email_sha256(address) for address in emails[:100]}
    assert lookup.not_found == {email_sha256(emails[100])}
    assert len(server.waits) == RetryPolicy().attempts - 1
    [warning] = _events(captured, "hubspot.batch_read.call_failed")
    assert warning["inputs"] == 100
    assert warning["error_kind"] == "transient"


@pytest.mark.anyio
async def test_contacts_by_email_incomplete_batch_is_a_failed_call() -> None:
    # The reference page's rendered example carries status CANCELED: a failed call.
    server = Server(scripted(_respond("objects_batch_read", _sample("objects_batch_read"), 207)))
    async with _client(server) as client:
        lookup = await client.contacts_by_email(["lead.one@example.com"], PROPS)
    assert lookup.failed == {email_sha256("lead.one@example.com")}
    assert lookup.found == {}


@pytest.mark.anyio
async def test_contacts_by_email_propagates_access_denied_and_defects() -> None:
    server = Server(
        scripted(
            _hubspot_error(403, "MISSING_SCOPES", "This app hasn't been granted all scopes."),
            _hubspot_error(400, "VALIDATION_ERROR", "Invalid input JSON."),
        )
    )
    async with _client(server) as client:
        with pytest.raises(AccessDenied) as denied:
            await client.contacts_by_email(["lead.one@example.com"], PROPS)
        with pytest.raises(QueryDefect) as defect:
            await client.contacts_by_email(["lead.one@example.com"], PROPS)
    assert denied.value.code == "MISSING_SCOPES"
    assert denied.value.endpoint == "objects_batch_read contacts"
    assert defect.value.http_status == 400
    assert len(server.calls) == 2


# ---------------------------------------------------------------------------------------------
# Contact refresh and target reads by id
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_contact_refresh_marks_missing_contacts_error() -> None:
    server = Server(
        scripted(
            _objects(
                [
                    _contact("201", "lead.one@example.com", {QL_PROPERTY: "2026-09-06T10:00:00Z"}),
                    _contact("202", "lead.two@example.com"),
                    _contact("999", "merged@example.com"),
                ],
                [_batch_error("OBJECT_NOT_FOUND", ["203"])],
            )
        )
    )
    async with _client(server) as client:
        batch = await client.contacts_by_id([" 201 ", 202, "201", "203", "204"], PROPS)  # type: ignore[list-item]

    [body] = server.bodies("objects_batch_read")
    assert body == {
        "inputs": [{"id": "201"}, {"id": "202"}, {"id": "203"}, {"id": "204"}],
        "properties": list(PROPS.request_properties()),
    }
    assert "idProperty" not in body
    assert batch.read_status == {"201": "ok", "202": "ok", "203": "error", "204": "error"}
    assert set(batch.records) == {"201", "202"}
    assert batch.records["201"].ql_at == datetime(2026, 9, 6, 10, tzinfo=UTC)
    assert batch.records["201"].email_sha256 == email_sha256("lead.one@example.com")


@pytest.mark.anyio
async def test_contact_refresh_failed_call_or_incomplete_batch_reads_error() -> None:
    server = Server(
        scripted(*_outage(), _objects([_contact("201", "lead.one@example.com")], status="PENDING"))
    )
    async with _client(server) as client:
        failed = await client.contacts_by_id(["201", "202"], PROPS)
        pending = await client.contacts_by_id(["201"], PROPS)
    assert failed.read_status == {"201": "error", "202": "error"}
    assert failed.records == {}
    assert pending.read_status == {"201": "error"}


@pytest.mark.anyio
async def test_meetings_read_ok_partial_and_error(captured: list[EventDict]) -> None:
    server = Server(
        scripted(
            _objects([_meeting("301"), _meeting("302")]),
            _objects([_meeting("301")], [_batch_error("OBJECT_NOT_FOUND", ["302"])]),
            _objects([], [_batch_error("OBJECT_NOT_FOUND", ["301", "302"])]),
        )
    )
    async with _client(server) as client:
        complete = await client.meetings(["301", "302"])
        partial = await client.meetings(["301", "302"])
        failed = await client.meetings(["301", "302"])

    for body in server.bodies("objects_batch_read"):
        assert body == {
            "inputs": [{"id": "301"}, {"id": "302"}],
            "properties": list(MEETING_PROPERTIES),
        }
    assert {call.params["objectType"] for call in server.calls} == {"meetings"}
    assert complete.status == "ok"
    assert complete.missing_ids == frozenset()
    assert complete.records["301"].meeting_source == "MEETINGS_PUBLIC"
    assert complete.records["301"].outcome == "SCHEDULED"
    assert partial.status == "partial"
    assert partial.missing_ids == {"302"}
    assert set(partial.records) == {"301"}
    assert failed.status == "error"
    assert failed.missing_ids == {"301", "302"}
    assert failed.records == {}
    events = _events(captured, "hubspot.batch_read")
    assert [(event["log_level"], event["missing"]) for event in events] == [
        ("info", 0),
        ("warning", 1),
        ("warning", 2),
    ]


@pytest.mark.anyio
async def test_deals_batch_100_per_call_and_a_failed_chunk_is_partial() -> None:
    ids = [str(40_000 + index) for index in range(101)]

    def route(call: Call) -> Outcome:
        requested = _inputs(call.body)
        if len(requested) == 1:
            return _unavailable()
        return _objects([_deal(hs_id) for hs_id in requested])

    server = Server(route)
    async with _client(server) as client:
        read = await client.deals(ids)
    bodies = server.bodies("objects_batch_read")
    assert [len(body["inputs"]) for body in bodies] == [100, 1, 1, 1]
    assert all(body["properties"] == list(DEAL_PROPERTIES) for body in bodies)
    assert {call.params["objectType"] for call in server.calls} == {"deals"}
    assert read.status == "partial"
    assert read.missing_ids == {ids[100]}
    assert len(read.records) == 100
    assert read.records[ids[0]].amount == Decimal("1500.50")


@pytest.mark.anyio
async def test_deals_with_an_incomplete_batch_or_no_ids() -> None:
    server = Server(scripted(_objects([_deal("401")], status="PROCESSING")))
    async with _client(server) as client:
        failed = await client.deals(["401"])
        nothing = await client.deals([])
    assert failed.status == "error"
    assert failed.missing_ids == {"401"}
    assert nothing.status == "ok"
    assert nothing.records == {}
    assert len(server.calls) == 1


def test_result_types_reject_statuses_the_database_refuses() -> None:
    with pytest.raises(ValueError, match="contact read status must be one of"):
        ContactBatch(records={}, read_status={"1": "partial"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="target read status must be one of"):
        TargetRead(records={}, missing_ids=frozenset(), status="ok_empty")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="association status must be one of"):
        InputAssociations(targets=(), status="unknown", error=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ok_empty association read cannot carry targets"):
        InputAssociations(targets=("1",), status="ok_empty", error=None)
    lookup = EmailLookup(
        found={}, not_found=frozenset({"a"}), failed=frozenset(), skipped=frozenset()
    )
    assert repr(lookup) == "EmailLookup(found=0, not_found=1, failed=0, skipped=0)"


# ---------------------------------------------------------------------------------------------
# Associations batch read
# ---------------------------------------------------------------------------------------------


def _paged_portal(pages: Mapping[str, Sequence[Sequence[int]]]) -> Route:
    """Serve each input's association pages; page ``n``'s cursor is ``"{id}-{n}"``."""

    def route(call: Call) -> Outcome:
        assert call.surface == "associations_batch_read"
        results = []
        for item in call.body["inputs"]:
            input_id = item["id"]
            index = int(item["after"].rpartition("-")[2]) if "after" in item else 0
            input_pages = pages[input_id]
            after = f"{input_id}-{index + 1}" if index + 1 < len(input_pages) else None
            results.append(_assoc(input_id, input_pages[index], after))
        return _associations(results)

    return route


@pytest.mark.anyio
async def test_associations_initial_body_and_207_statuses(captured: list[EventDict]) -> None:
    server = Server(
        scripted(
            _associations(
                [_assoc("501", [601, 602, 601]), _assoc("502", [])],
                [
                    _no_associations("503"),
                    _batch_error(
                        "VALIDATION_ERROR", ["504"], key="fromObjectId", message="Invalid id"
                    ),
                ],
            )
        )
    )
    async with _client(server) as client:
        fetch = await client.associations(
            "contacts", "meetings", ["501", "502", "503", "504", "505"]
        )

    assert server.bodies("associations_batch_read") == [
        {"inputs": [{"id": "501"}, {"id": "502"}, {"id": "503"}, {"id": "504"}, {"id": "505"}]}
    ]
    assert (fetch.from_type, fetch.to_type) == ("contacts", "meetings")
    reads = fetch.per_input
    assert list(reads) == ["501", "502", "503", "504", "505"]
    assert reads["501"] == InputAssociations(targets=("601", "602"), status="ok", error=None)
    assert reads["502"] == InputAssociations(targets=(), status="ok_empty", error=None)
    assert reads["503"] == InputAssociations(targets=(), status="ok_empty", error=None)
    assert reads["504"] == InputAssociations(targets=(), status="error", error="VALIDATION_ERROR")
    assert reads["505"] == InputAssociations(
        targets=(), status="error", error="input missing from response"
    )
    assert [reads[key].is_known for key in reads] == [True, True, True, False, False]
    assert fetch.all_targets == ("601", "602")
    assert dict(fetch.status_counts()) == {"ok": 1, "ok_empty": 2, "error": 2}
    [event] = _events(captured, "hubspot.associations")
    assert event["log_level"] == "warning"
    assert (event["calls"], event["ok"], event["ok_empty"], event["error"]) == (1, 1, 2, 2)


@pytest.mark.anyio
async def test_associations_continue_only_paged_inputs_with_their_own_cursor() -> None:
    server = Server(
        _paged_portal({"501": [[601], [604]], "502": [[602]], "503": [[603], [605], [606]]})
    )
    async with _client(server) as client:
        fetch = await client.associations("contacts", "deals", ["501", "502", "503"])

    assert server.bodies("associations_batch_read") == [
        {"inputs": [{"id": "501"}, {"id": "502"}, {"id": "503"}]},
        {"inputs": [{"id": "501", "after": "501-1"}, {"id": "503", "after": "503-1"}]},
        {"inputs": [{"id": "503", "after": "503-2"}]},
    ]
    assert fetch.per_input["501"].targets == ("601", "604")
    assert fetch.per_input["502"].targets == ("602",)
    assert fetch.per_input["503"].targets == ("603", "605", "606")
    assert {read.status for read in fetch.per_input.values()} == {"ok"}
    assert fetch.all_targets == ("601", "604", "602", "603", "605", "606")


@pytest.mark.anyio
async def test_associations_batch_at_most_1000_inputs_per_call() -> None:
    ids = [str(70_000 + index) for index in range(1_001)]
    pages = {input_id: [[int(input_id) + 100_000], [int(input_id) + 200_000]] for input_id in ids}
    server = Server(_paged_portal(pages))
    async with _client(server) as client:
        fetch = await client.associations("contacts", "deals", ids)
    bodies = server.bodies("associations_batch_read")
    assert [len(body["inputs"]) for body in bodies] == [1_000, 1, 1_000, 1]
    assert all("after" not in item for body in bodies[:2] for item in body["inputs"])
    assert all("after" in item for body in bodies[2:] for item in body["inputs"])
    assert dict(fetch.status_counts()) == {"ok": 1_001}
    assert fetch.per_input[ids[-1]].targets == (
        str(int(ids[-1]) + 100_000),
        str(int(ids[-1]) + 200_000),
    )


@pytest.mark.anyio
async def test_associations_failed_continuation_is_partial() -> None:
    server = Server(
        scripted(
            _associations(
                [
                    _assoc("501", [601], "c-1"),
                    _assoc("502", [], "d-1"),
                    _assoc("503", [603], "e-1"),
                    _assoc("504", [604]),
                ]
            ),
            *_outage(),
        )
    )
    async with _client(server) as client:
        fetch = await client.associations("contacts", "meetings", ["501", "502", "503", "504"])
    reads = fetch.per_input
    assert (reads["501"].status, reads["501"].targets) == ("partial", ("601",))
    assert (reads["502"].status, reads["502"].targets) == ("error", ())
    assert (reads["503"].status, reads["503"].targets) == ("partial", ("603",))
    assert reads["504"] == InputAssociations(targets=("604",), status="ok", error=None)
    reason = reads["501"].error
    assert reason is not None
    assert reason.startswith("hubspot associations_batch_read contacts/meetings")
    assert reads["502"].error == reason
    continuation = server.bodies("associations_batch_read")[1]
    assert continuation == {
        "inputs": [
            {"id": "501", "after": "c-1"},
            {"id": "502", "after": "d-1"},
            {"id": "503", "after": "e-1"},
        ]
    }


@pytest.mark.anyio
async def test_associations_continuation_with_incomplete_status_is_partial() -> None:
    server = Server(
        scripted(
            _associations([_assoc("501", [601], "c-1")]),
            _associations([_assoc("501", [602])], status="CANCELED"),
        )
    )
    async with _client(server) as client:
        fetch = await client.associations("contacts", "meetings", ["501"])
    assert fetch.per_input["501"] == InputAssociations(
        targets=("601",),
        status="partial",
        error="associations_batch_read contacts/meetings: batch status CANCELED",
    )


@pytest.mark.anyio
async def test_associations_continuation_errors_and_empty_pages() -> None:
    server = Server(
        scripted(
            _associations(
                [
                    _assoc("501", [601], "a-1"),
                    _assoc("503", [603], "b-1"),
                    _assoc("504", [604], "c-1"),
                    _assoc("505", [605], "d-1"),
                ]
            ),
            _associations(
                [_assoc("503", [607])],
                [
                    _batch_error(
                        "VALIDATION_ERROR", ["501"], key="fromObjectId", message="Invalid cursor"
                    ),
                    _no_associations("504"),
                ],
            ),
        )
    )
    async with _client(server) as client:
        fetch = await client.associations("contacts", "deals", ["501", "503", "504", "505"])
    reads = fetch.per_input
    assert reads["501"] == InputAssociations(
        targets=("601",), status="partial", error="VALIDATION_ERROR"
    )
    assert reads["503"] == InputAssociations(targets=("603", "607"), status="ok", error=None)
    assert reads["504"] == InputAssociations(targets=("604",), status="ok", error=None)
    assert reads["505"] == InputAssociations(
        targets=("605",), status="partial", error="input missing from response"
    )


@pytest.mark.anyio
async def test_associations_repeated_cursor_stops_as_partial() -> None:
    server = Server(
        scripted(
            _associations([_assoc("501", [601], "c-1")]),
            _associations([_assoc("501", [602], "c-1")]),
        )
    )
    async with _client(server) as client:
        fetch = await client.associations("contacts", "meetings", ["501"])
    assert fetch.per_input["501"] == InputAssociations(
        targets=("601", "602"), status="partial", error="paging cursor did not advance"
    )
    assert len(server.calls) == 2


@pytest.mark.anyio
async def test_associations_failed_initial_call_marks_every_input_error() -> None:
    server = Server(
        scripted(
            # The reference page's rendered 207 example, verbatim: status CANCELED.
            _respond("associations_batch_read", _sample("associations_batch_read"), 207),
            *_outage(),
        )
    )
    async with _client(server) as client:
        canceled = await client.associations("contacts", "meetings", ["501", "502"])
        failed = await client.associations("deals", "contacts", ["601"])
    assert {read.status for read in canceled.per_input.values()} == {"error"}
    assert canceled.per_input["501"].error == (
        "associations_batch_read contacts/meetings: batch status CANCELED"
    )
    assert canceled.all_targets == ()
    assert failed.per_input["601"].status == "error"
    error = failed.per_input["601"].error
    assert error is not None
    assert error.startswith("hubspot associations_batch_read deals/contacts")
    assert len(server.calls) == 1 + RetryPolicy().attempts


@pytest.mark.anyio
async def test_associations_propagate_access_denied_and_skip_empty_input() -> None:
    server = Server(scripted(_hubspot_error(401, "INVALID_AUTHENTICATION", "Expired token.")))
    async with _client(server) as client:
        empty = await client.associations("contacts", "meetings", [])
        with pytest.raises(AccessDenied):
            await client.associations("contacts", "meetings", ["501"])
    assert empty.per_input == {}
    assert len(server.calls) == 1


# ---------------------------------------------------------------------------------------------
# CRM Search pages and partitioned search
# ---------------------------------------------------------------------------------------------


def _search_body_of_length(length: int) -> dict[str, object]:
    """Return a valid search body whose compact JSON has exactly ``length`` characters."""
    body: dict[str, object] = {"filterGroups": [], "sorts": [], "properties": [""], "limit": 1}
    base = len(json.dumps(body, separators=(",", ":")))
    body["properties"] = ["x" * (length - base)]
    assert len(json.dumps(body, separators=(",", ":"))) == length
    return body


@pytest.mark.anyio
async def test_search_page_decodes_the_documented_sample(captured: list[EventDict]) -> None:
    server = Server(scripted(_respond("crm_search", _sample("crm_search"))))
    body = search_body(
        google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, ["email"]), DAY0, DAY0 + HOUR
    )
    async with _client(server) as client:
        page = await client.search_page("contacts", body)
    assert server.calls[0].path == "/crm/objects/2026-09/contacts/search"
    assert server.calls[0].body == body
    assert page.total == 2
    assert [record["id"] for record in page.results] == ["100451", "57156923994"]
    assert page.after is None
    [event] = _events(captured, "hubspot.search")
    assert (event["scope"], event["results"], event["total"], event["more"]) == (
        "page",
        2,
        2,
        False,
    )


@pytest.mark.anyio
async def test_search_page_body_must_stay_under_3000_characters() -> None:
    server = Server(scripted(_page([], 0)))
    async with _client(server) as client:
        await client.search_page("deals", _search_body_of_length(MAX_SEARCH_BODY_CHARS - 1))
        with pytest.raises(QueryDefect) as defect:
            await client.search_page("deals", _search_body_of_length(MAX_SEARCH_BODY_CHARS))
    assert defect.value.code == "BODY_TOO_LARGE"
    assert len(server.calls) == 1
    assert len(server.calls[0].request.content) == MAX_SEARCH_BODY_CHARS - 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"properties": ["createdate"]}, "INVALID_LIMIT"),
        ({"limit": 0}, "INVALID_LIMIT"),
        ({"limit": 201}, "INVALID_LIMIT"),
        ({"limit": True}, "INVALID_LIMIT"),
        ({"limit": "10"}, "INVALID_LIMIT"),
        ({"limit": 1, "after": ""}, "INVALID_CURSOR"),
        ({"limit": 1, "after": "  "}, "INVALID_CURSOR"),
        ({"limit": 1, "after": 5}, "INVALID_CURSOR"),
        ({"limit": 1, "after": "10000"}, "RESULT_CEILING"),
        ({"limit": 1, "after": "12000"}, "RESULT_CEILING"),
        ({"limit": 1, "properties": {"createdate"}}, "CLIENT"),
    ],
)
@pytest.mark.anyio
async def test_search_page_rejects_bodies_hubspot_would_refuse(
    body: dict[str, object], code: str
) -> None:
    server = Server(scripted())
    async with _client(server) as client:
        with pytest.raises(QueryDefect) as defect:
            await client.search_page("deals", body)
    assert defect.value.code == code
    assert defect.value.endpoint == "crm_search deals"
    assert server.calls == []


@pytest.mark.anyio
async def test_search_page_accepts_cursors_below_the_ceiling_and_opaque_ones() -> None:
    server = Server(scripted(_page([], 0), _page([], 0)))
    base = search_body(deal_search_filter(), DAY0, DAY0 + HOUR)
    async with _client(server) as client:
        await client.search_page("deals", {**base, "after": "9999"})
        await client.search_page("deals", {**base, "after": "opaque-cursor"})
    assert [call.body["after"] for call in server.calls] == ["9999", "opaque-cursor"]


@pytest.mark.anyio
async def test_search_page_propagates_defects_and_transient_failures() -> None:
    body = search_body(deal_search_filter(), DAY0, DAY0 + HOUR)
    server = Server(
        scripted(_hubspot_error(400, "VALIDATION_ERROR", "Invalid filter."), *_outage())
    )
    async with _client(server) as client:
        with pytest.raises(QueryDefect) as defect:
            await client.search_page("deals", body)
        with pytest.raises(TransientError) as transient:
            await client.search_page("deals", body)
    assert defect.value.http_status == 400
    assert transient.value.http_status == 503
    assert len(server.calls) == 1 + RetryPolicy().attempts


@pytest.mark.anyio
async def test_deal_search_day_above_ceiling_splits_into_hourly_partitions() -> None:
    day0_ms = int(epoch_ms(DAY0))

    def route(call: Call) -> Outcome:
        assert call.params["objectType"] == "deals"
        lo, hi = _range(call.body, "createdate")
        if hi - lo > HOUR_MS:
            return _page([], 12_000)
        hour = (lo - day0_ms) // HOUR_MS
        return _page([_deal(str(800_000 + 2 * hour)), _deal(str(800_001 + 2 * hour))], 2)

    server = Server(route)
    async with _client(server) as client:
        outcome = await client.search_partitioned(
            "deals", deal_search_filter(), (DAY0, DAY0 + 24 * HOUR)
        )

    assert outcome.completeness == "complete"
    assert outcome.reason is None
    assert outcome.partitions_incomplete == ()
    assert len(outcome.rows) == 48
    # 24 one-hour leaves of a binary split tree have 23 split nodes above them.
    assert outcome.calls == 47
    assert len(server.calls) == 47
    leaves = sorted(
        span
        for span in (_range(call.body, "createdate") for call in server.calls)
        if span[1] - span[0] <= HOUR_MS
    )
    assert leaves == [
        (day0_ms + hour * HOUR_MS, day0_ms + (hour + 1) * HOUR_MS) for hour in range(24)
    ]
    for call in server.calls:
        assert len(call.request.content) < MAX_SEARCH_BODY_CHARS
        assert call.body["sorts"] == [{"propertyName": "createdate", "direction": "ASCENDING"}]
        assert call.body["properties"] == list(DEAL_PROPERTIES)
        assert call.body["limit"] == MAX_SEARCH_LIMIT
        assert "after" not in call.body


@pytest.mark.anyio
async def test_deal_search_hour_above_ceiling_reads_to_the_ceiling_and_is_incomplete() -> None:
    total = 10_500

    def route(call: Call) -> Outcome:
        offset = int(call.body.get("after", "0"))
        limit = call.body["limit"]
        assert offset + limit <= SEARCH_RESULT_CEILING
        records = [_deal(str(900_000 + offset + index)) for index in range(limit)]
        following = offset + limit
        return _page(records, total, str(following) if following < total else None)

    server = Server(route)
    async with _client(server) as client:
        outcome = await client.search_partitioned(
            "deals", deal_search_filter(), (DAY0, DAY0 + HOUR)
        )

    assert outcome.completeness == "incomplete"
    assert outcome.reason == CEILING_REASON
    assert outcome.partitions_incomplete == ((DAY0, DAY0 + HOUR),)
    assert len(outcome.rows) == SEARCH_RESULT_CEILING
    assert outcome.calls == SEARCH_RESULT_CEILING // MAX_SEARCH_LIMIT
    cursors = [int(call.body.get("after", "0")) for call in server.calls]
    assert cursors == list(range(0, SEARCH_RESULT_CEILING, MAX_SEARCH_LIMIT))
    assert all(len(call.request.content) < MAX_SEARCH_BODY_CHARS for call in server.calls)


@pytest.mark.anyio
async def test_google_contact_discovery_through_the_client() -> None:
    server = Server(lambda call: _respond("crm_search", _sample("crm_search")))
    search_filter = google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, ["email"])
    async with _client(server) as client:
        outcome = await client.search_partitioned(
            "contacts", search_filter, (DAY0, DAY0 + timedelta(days=2))
        )
    assert outcome.completeness == "complete"
    assert [record["id"] for record in outcome.rows] == ["100451", "57156923994"]
    assert outcome.calls == 2
    for call in server.calls:
        assert call.path == "/crm/objects/2026-09/contacts/search"
        filters = call.body["filterGroups"][0]["filters"]
        assert {"propertyName": CAMPAIGN_PROPERTY, "operator": "HAS_PROPERTY"} in filters
        assert call.body["properties"] == ["email", CAMPAIGN_PROPERTY, TOUCH_PROPERTY]
    assert [_range(call.body, TOUCH_PROPERTY) for call in server.calls] == [
        (int(epoch_ms(DAY0)), int(epoch_ms(DAY0 + timedelta(days=1)))),
        (int(epoch_ms(DAY0 + timedelta(days=1))), int(epoch_ms(DAY0 + timedelta(days=2)))),
    ]


@pytest.mark.anyio
async def test_partitioned_search_keeps_rows_read_before_a_transient_failure() -> None:
    server = Server(scripted(_page([_deal("801")], 1), *_outage()))
    day1 = DAY0 + timedelta(days=1)
    async with _client(server) as client:
        outcome = await client.search_partitioned(
            "deals", deal_search_filter(), (DAY0, DAY0 + timedelta(days=2))
        )
    assert outcome.completeness == "incomplete"
    assert [record["id"] for record in outcome.rows] == ["801"]
    assert outcome.partitions_incomplete == ((day1, DAY0 + timedelta(days=2)),)
    assert outcome.reason is not None
    assert outcome.reason.startswith("hubspot crm_search deals")


# ---------------------------------------------------------------------------------------------
# Binding verification and the contact property map
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_resolves_stage_labels_and_reports_unavailable_ones(
    captured: list[EventDict],
) -> None:
    server = Server(
        _properties_route(
            {"lifecyclestage": _lifecycle(), QL_PROPERTY: _datetime_property(QL_PROPERTY)}
        )
    )
    mapping = _mapping(
        _stage("First meeting booked"), _stage("Qualified Lead", "Sales Qualified Lead")
    )
    before = datetime.now(UTC)
    async with _client(server) as client:
        bindings = await client.verify_bindings(mapping, _no_google())
    after = datetime.now(UTC)

    # The default "Marketing Qualified Lead" stage never stands in for "First meeting booked".
    assert bindings.mql == ResolvedBinding(
        available=False, reason="stage 'First meeting booked' not found in lifecyclestage options"
    )
    assert bindings.ql == ResolvedBinding(
        available=True, property=QL_PROPERTY, stage_id=QL_STAGE_ID, label="Qualified Lead"
    )
    assert bindings.google == GoogleCrmBinding(
        available=False,
        reason="HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY and HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY "
        "not configured",
    )
    assert before <= bindings.verified_at <= after
    assert bindings.verified_at.tzinfo is UTC
    # lifecyclestage is read once and shared by both bindings.
    assert _property_names(server) == ["lifecyclestage", QL_PROPERTY]
    assert {call.request.method for call in server.calls} == {"GET"}
    [event] = _events(captured, "hubspot.verify")
    assert (event["mql_available"], event["ql_available"], event["google_available"]) == (
        False,
        True,
        False,
    )
    assert event["properties_read"] == 2
    # An unavailable count is left out of the read, never read as 0.
    assert contact_property_map(bindings, _no_google()) == ContactPropertyMap(
        ql_property=QL_PROPERTY
    )


@pytest.mark.anyio
async def test_verify_names_every_missing_label_and_a_missing_stage_date_property() -> None:
    server = Server(_properties_route({"lifecyclestage": _lifecycle()}))
    mapping = _mapping(_stage("First meeting booked", "Booked"), _stage("Sales Qualified Lead"))
    async with _client(server) as client:
        bindings = await client.verify_bindings(mapping, _no_google())
    assert bindings.mql == ResolvedBinding(
        available=False,
        reason="stages 'First meeting booked', 'Booked' not found in lifecyclestage options",
    )
    assert bindings.ql == ResolvedBinding(
        available=False,
        stage_id="salesqualifiedlead",
        label="Sales Qualified Lead",
        reason="stage 'Sales Qualified Lead' resolved to id 'salesqualifiedlead', but property "
        "'hs_v2_date_entered_salesqualifiedlead' not found",
    )


@pytest.mark.anyio
async def test_verify_first_matching_label_wins_and_no_later_label_stands_in() -> None:
    sql_property = f"{STAGE_DATE_PROPERTY_PREFIX}salesqualifiedlead"
    server = Server(
        _properties_route(
            {"lifecyclestage": _lifecycle(), sql_property: _datetime_property(sql_property)}
        )
    )
    mapping = _mapping(_stage("Customer"), _stage("Qualified Lead", "Sales Qualified Lead"))
    async with _client(server) as client:
        bindings = await client.verify_bindings(mapping, _no_google())
    assert bindings.ql.available is False
    assert bindings.ql.stage_id == QL_STAGE_ID
    assert sql_property not in _property_names(server)


@pytest.mark.anyio
async def test_verify_matches_labels_trimmed_and_case_insensitive() -> None:
    options: tuple[Option, ...] = (
        ("Qualified Lead", "111", True),
        ("qualified lead", "222", False),
        ("  Sales Qualified Lead ", "333", False),
    )
    server = Server(
        _properties_route(
            {
                "lifecyclestage": _lifecycle(options),
                **{
                    f"{STAGE_DATE_PROPERTY_PREFIX}{value}": _datetime_property(
                        f"{STAGE_DATE_PROPERTY_PREFIX}{value}"
                    )
                    for _label, value, _hidden in options
                },
            }
        )
    )
    async with _client(server) as client:
        exact = await client.verify_bindings(
            _mapping(_stage("Qualified Lead"), _stage("sales qualified lead")), _no_google()
        )
        visible = await client.verify_bindings(
            _mapping(_stage("QUALIFIED LEAD"), _stage("Qualified Lead")), _no_google()
        )
    # An exact label wins over a case-folded one, even when hidden.
    assert (exact.mql.stage_id, exact.mql.label) == ("111", "Qualified Lead")
    assert (exact.ql.stage_id, exact.ql.label) == ("333", "  Sales Qualified Lead ")
    assert exact.ql.property == f"{STAGE_DATE_PROPERTY_PREFIX}333"
    # With no exact label, a visible option wins over a hidden one.
    assert (visible.mql.stage_id, visible.mql.label) == ("222", "qualified lead")
    assert visible.ql.stage_id == "111"


@pytest.mark.anyio
async def test_verify_property_mode_bindings() -> None:
    server = Server(
        _properties_route(
            {
                "lifecyclestage": _lifecycle(),
                MQL_PROPERTY: _datetime_property(MQL_PROPERTY, "date"),
                "ql_date": _sample("properties"),
                "ql_text": _definition("ql_text", "string", "text"),
            }
        )
    )
    async with _client(server) as client:
        available = await client.verify_bindings(
            _mapping(
                _property_binding(MQL_PROPERTY, "First meeting booked"),
                _property_binding("ql_date", "Qualified Lead"),
            ),
            _no_google(),
        )
        missing = await client.verify_bindings(
            _mapping(_property_binding("absent_date"), _property_binding("ql_text")),
            _no_google(),
        )
    assert available.mql == ResolvedBinding(
        available=True, property=MQL_PROPERTY, label="First meeting booked"
    )
    # The documented sample's type is the placeholder "<string>": not a date property.
    assert available.ql == ResolvedBinding(
        available=False,
        label="Qualified Lead",
        reason="property 'ql_date' has type '<string>', not a date or datetime property",
    )
    assert missing.mql == ResolvedBinding(
        available=False, reason="property 'absent_date' not found on contacts"
    )
    assert missing.ql == ResolvedBinding(
        available=False,
        reason="property 'ql_text' has type 'string', not a date or datetime property",
    )
    assert "lifecyclestage" not in _property_names(server)


@pytest.mark.anyio
async def test_verify_reports_a_hand_built_property_binding_without_a_property() -> None:
    # StageBinding refuses this shape on construction; one built around that check is still
    # reported as unavailable rather than crashing verification.
    binding = object.__new__(StageBinding)
    object.__setattr__(binding, "mode", "property")
    object.__setattr__(binding, "stage_labels", ("First meeting booked",))
    object.__setattr__(binding, "property", None)
    server = Server(_properties_route({}))
    async with _client(server) as client:
        bindings = await client.verify_bindings(
            _mapping(binding, _property_binding(MQL_PROPERTY)), _no_google()
        )
    assert bindings.mql == ResolvedBinding(
        available=False, label="First meeting booked", reason="property mode names no property"
    )
    assert _property_names(server) == [MQL_PROPERTY]


@pytest.mark.anyio
async def test_verify_without_lifecyclestage_makes_stage_bindings_unavailable() -> None:
    server = Server(_properties_route({}))
    async with _client(server) as client:
        bindings = await client.verify_bindings(
            _mapping(_stage("First meeting booked"), _stage("Qualified Lead")), _no_google()
        )
    expected = ResolvedBinding(available=False, reason="lifecyclestage property not found")
    assert bindings.mql == expected
    assert bindings.ql == expected
    assert _property_names(server) == ["lifecyclestage"]


@pytest.mark.anyio
async def test_verify_google_link_and_its_property_map() -> None:
    definitions = {
        "lifecyclestage": _lifecycle(),
        QL_PROPERTY: _datetime_property(QL_PROPERTY),
        CAMPAIGN_PROPERTY: _definition(CAMPAIGN_PROPERTY, "string", "text"),
        TOUCH_PROPERTY: _datetime_property(TOUCH_PROPERTY),
    }
    settings = _google_settings()
    server = Server(_properties_route(definitions))
    async with _client(server) as client:
        bindings = await client.verify_bindings(
            _mapping(_stage("Qualified Lead"), _stage("Qualified Lead")), settings
        )
    assert bindings.google == GoogleCrmBinding(
        available=True, campaign_property=CAMPAIGN_PROPERTY, touch_date_property=TOUCH_PROPERTY
    )
    expected = ContactPropertyMap(
        mql_property=QL_PROPERTY,
        ql_property=QL_PROPERTY,
        google_campaign_property=CAMPAIGN_PROPERTY,
        google_touch_property=TOUCH_PROPERTY,
    )
    assert contact_property_map(bindings, settings) == expected
    assert HubSpotClient.contact_property_map(bindings, settings) == expected
    # A configuration changed since verification leaves the link out until re-verified.
    renamed = _google_settings(touch="other_touch_date")
    assert contact_property_map(bindings, renamed) == ContactPropertyMap(
        mql_property=QL_PROPERTY, ql_property=QL_PROPERTY
    )


@pytest.mark.anyio
async def test_verify_google_link_unavailable_reasons() -> None:
    definitions = {
        "lifecyclestage": _lifecycle(),
        TOUCH_PROPERTY: _definition(TOUCH_PROPERTY, "string", "text"),
        CAMPAIGN_PROPERTY: _definition(CAMPAIGN_PROPERTY, "string", "text"),
    }
    mapping = _mapping(_stage("Customer"), _stage("Customer"))
    server = Server(_properties_route(definitions))
    async with _client(server) as client:
        wrong_type = await client.verify_bindings(mapping, _google_settings(campaign="absent_id"))
        missing_touch = await client.verify_bindings(
            mapping, _google_settings(touch="absent_touch")
        )
        touch_unset = await client.verify_bindings(mapping, _google_settings(touch=None))
    assert wrong_type.google == GoogleCrmBinding(
        available=False,
        campaign_property="absent_id",
        touch_date_property=TOUCH_PROPERTY,
        reason="HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY property 'absent_id' not found on contacts; "
        "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY property 'google_touch_date' has type 'string', "
        "not a date or datetime property",
    )
    assert missing_touch.google.reason == (
        "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY property 'absent_touch' not found on contacts"
    )
    assert touch_unset.google == GoogleCrmBinding(
        available=False,
        campaign_property=CAMPAIGN_PROPERTY,
        reason="HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not configured",
    )
    assert contact_property_map(wrong_type, _google_settings(campaign="absent_id")) == (
        ContactPropertyMap()
    )


@pytest.mark.anyio
async def test_verify_propagates_access_denied_and_transient_failures() -> None:
    mapping = _mapping(_stage("First meeting booked"), _stage("Qualified Lead"))
    server = Server(
        scripted(
            _hubspot_error(403, "MISSING_SCOPES", "This app hasn't been granted all scopes."),
            *_outage(),
        )
    )
    async with _client(server) as client:
        with pytest.raises(AccessDenied) as denied:
            await client.verify_bindings(mapping, _no_google())
        with pytest.raises(TransientError) as transient:
            await client.verify_bindings(mapping, _no_google())
    assert denied.value.code == "MISSING_SCOPES"
    assert denied.value.endpoint == "properties contacts/lifecyclestage"
    assert transient.value.http_status == 503


def test_crm_bindings_round_trip_through_the_property_map() -> None:
    bindings = CrmBindings.unavailable("not yet verified", at=DAY0)
    assert contact_property_map(bindings, _google_settings()) == ContactPropertyMap()


# ---------------------------------------------------------------------------------------------
# Recovery-readiness probe
# ---------------------------------------------------------------------------------------------


def _probe_route(deals: Sequence[Mapping[str, Any]]) -> Route:
    """Serve the probe's three checks, asserting each request's exact body."""

    def route(call: Call) -> Outcome:
        if call.surface == "objects_batch_read":
            assert call.body == {
                "idProperty": "email",
                "inputs": [{"id": PROBE_EMAIL}],
                "properties": ["email"],
            }
            return _objects([], [_batch_error("OBJECT_NOT_FOUND", [PROBE_EMAIL])])
        if call.surface == "crm_search":
            assert call.body == {
                "filterGroups": [],
                "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
                "properties": ["createdate"],
                "limit": 1,
            }
            return _page(deals, len(deals))
        assert call.surface == "associations_batch_read"
        [item] = call.body["inputs"]
        return _associations([], [_no_associations(item["id"])])

    return route


@pytest.mark.anyio
async def test_probe_reads_one_email_one_deal_and_its_associations(
    captured: list[EventDict],
) -> None:
    server = Server(_probe_route([_deal("701")]))
    async with _client(server) as client:
        await client.probe()
    assert [call.path for call in server.calls] == [
        "/crm/objects/2026-09/contacts/batch/read",
        "/crm/objects/2026-09/deals/search",
        "/crm/associations/2026-09/deals/contacts/batch/read",
    ]
    assert server.calls[2].body == {"inputs": [{"id": "701"}]}
    [event] = _events(captured, "hubspot.probe")
    assert (event["passed"], event["live"], event["calls"]) == (True, True, 3)


@pytest.mark.anyio
async def test_probe_on_a_portal_without_deals_reads_contact_zero() -> None:
    server = Server(_probe_route([]))
    async with _client(server) as client:
        await client.probe()
    assert server.calls[2].path == "/crm/associations/2026-09/contacts/deals/batch/read"
    assert server.calls[2].body == {"inputs": [{"id": "0"}]}


@pytest.mark.anyio
async def test_probe_incomplete_batches_fail_transiently() -> None:
    contacts = Server(scripted(_respond("objects_batch_read", _sample("objects_batch_read"), 207)))
    async with _client(contacts) as client:
        with pytest.raises(TransientError) as contacts_failed:
            await client.probe()
    assert contacts_failed.value.code == "BATCH_INCOMPLETE"
    assert contacts_failed.value.endpoint == "objects_batch_read contacts"
    assert len(contacts.calls) == 1

    def route(call: Call) -> Outcome:
        if call.surface == "associations_batch_read":
            return _respond("associations_batch_read", _sample("associations_batch_read"), 207)
        return _probe_route([_deal("701")])(call)

    associations = Server(route)
    async with _client(associations) as client:
        with pytest.raises(TransientError) as associations_failed:
            await client.probe()
    assert associations_failed.value.code == "BATCH_INCOMPLETE"
    assert associations_failed.value.endpoint == "associations_batch_read deals/contacts"


@pytest.mark.anyio
async def test_probe_raises_transient_and_access_denied_failures() -> None:
    outage = Server(scripted(*_outage()))
    async with _client(outage) as client:
        with pytest.raises(TransientError) as transient:
            await client.probe()
    assert transient.value.http_status == 503
    assert len(outage.calls) == RetryPolicy().attempts

    def route(call: Call) -> Outcome:
        if call.surface == "crm_search":
            return _hubspot_error(403, "MISSING_SCOPES", "This app hasn't been granted all scopes.")
        return _probe_route([])(call)

    denied = Server(route)
    async with _client(denied) as client:
        with pytest.raises(AccessDenied) as refused:
            await client.probe()
    assert refused.value.endpoint == "crm_search deals"
    assert len(denied.calls) == 2
