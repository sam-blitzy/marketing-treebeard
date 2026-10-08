"""Behavioural tests for the analytics and Lead Sync methods of ``LinkedInClient``.

Covers ``analytics_daily`` (request shape, date chunking below the 15,000-element cap, midpoint
splitting of a capped range, ``next`` links, the one-day partition at the cap, UTC dates and
de-duplication), ``analytics_segments`` with its Statistics Finder request and the Analytics
Finder fallback (``_delivering`` discovery and ``_segments_by_campaign``), ``list_lead_forms``,
``list_lead_responses``, ``get_lead_responses`` and the :class:`LeadListing` and
:class:`LeadBatch` results, and ``LEAD_FIELDS``, which both lead requests send. Its generator
and the packaged module generated from the two lead-response contracts are covered by
``test_linkedin_projections.py``.

Every response is served by an ``httpx.MockTransport`` and shaped from
``backend/tests/contracts/linkedin/*.json``: a documented sample is served verbatim where a
test names it, and every hand-built body uses only fields its contract defines (elements start
from the documented sample where one exists) and is checked against the contract's response
schema before it is served. The deliberately malformed bodies of the defect tests, and the
batch elements of the identity tests that omit or mistype an identity field, are the only
exceptions. Requests are checked
against the contract's path, query keys, headers and ``fields`` projection. Assertions follow
the module, which records live 202609 behaviour where it departs from the plan text: the
``fields`` projection is sent raw, batch ids are unquoted (``ids=List(<id>)``), demographic
segments fall back to one ``q=analytics&pivot=MEMBER_*`` call per campaign after the
Statistics Finder's HTTP 400, and ``WORK_EMAIL`` questions decode emails.

No test touches the network, sleeps or needs a database. Ids are fake ``99000…`` values and
emails are ``example.com`` addresses held in memory only.
"""

import contextlib
import copy
import functools
import hashlib
import inspect
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from pydantic import AliasChoices
from structlog.testing import capture_logs
from structlog.typing import EventDict

from ads_engine.connectors.errors import AccessDenied, QueryDefect, TransientError
from ads_engine.connectors.http import RetryPolicy, build_client
from ads_engine.connectors.linkedin import client as client_module
from ads_engine.connectors.linkedin.auth import StaticTokenProvider, TokenSource
from ads_engine.connectors.linkedin.client import (
    DAILY_FIELDS,
    LEAD_BATCH_SIZE,
    LEAD_FIELDS,
    LEAD_PAGE_SIZE,
    MAX_ANALYTICS_ELEMENTS,
    SEGMENT_DISCOVERY_FIELDS,
    SEGMENT_FIELDS,
    LeadBatch,
    LeadListing,
    LinkedInClient,
)
from ads_engine.connectors.linkedin.parser import (
    REASON_UNKNOWN_FORM_VERSION,
    SEGMENT_PIVOTS,
    FormVersionMap,
    decode_email,
)
from ads_engine.settings import Settings, get_settings

pytestmark = pytest.mark.contract

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "linkedin"

TOKEN = "test-linkedin-access-token"
ACCOUNT_ID = "990000001"
ACCOUNT_URN = "urn:li:sponsoredAccount:990000001"
ACCOUNTS = "List(urn%3Ali%3AsponsoredAccount%3A990000001)"
OWNER = "(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A990000001)"
OTHER_ACCOUNT_URN = "urn:li:sponsoredAccount:990000002"
OTHER_ORGANIZATION_URN = "urn:li:organization:990000003"

CREATIVE_A = "urn:li:sponsoredCreative:990001101"
CREATIVE_B = "urn:li:sponsoredCreative:990001102"
DISCARDED_CREATIVE = "urn:li:sponsoredCreative:990009999"

# Numeric order (99000105 < 990000101) differs from string order, so the discovery sort shows.
CAMPAIGN_LOW = "urn:li:sponsoredCampaign:99000105"
CAMPAIGN_HIGH = "urn:li:sponsoredCampaign:990000101"
CAMPAIGN_THIRD = "urn:li:sponsoredCampaign:990000102"
CAMPAIGN_IDLE = "urn:li:sponsoredCampaign:990000150"

FORM_ID = 990002001
FORM_URN = "urn:li:leadGenForm:990002001"
OTHER_FORM_ID = 990002002
FORM_VERSION = 3
STORED_VERSION = 2

FIRST_NAME_QID = 10
EMAIL_QID = 20
WORK_EMAIL_QID = 30

RAW_EMAIL = "  Lead.One@Example.COM "
EMAIL = "lead.one@example.com"
OTHER_EMAIL = "lead.two@example.com"
THIRD_EMAIL = "lead.three@example.com"
TEST_LEAD_EMAIL = "test.lead@example.com"

LEAD_A = "ffffeeee-0000-dddd-1111-cccc2222bbbb-1"
LEAD_B = "ffffeeee-0000-dddd-1111-cccc2222bbbb-2"
LEAD_C = "ffffeeee-0000-dddd-1111-cccc2222bbbb-3"
TEST_LEAD = "ffffeeee-0000-dddd-1111-cccc2222bbbb-9"
SUBMITTED_MS = 1_790_000_000_123

# The documented Lead Sync sample (lead_form_responses.json, lead_form_responses_batch.json).
SAMPLE_LEAD_ID = "aaaabbbb-0000-cccc-1111-dddd2222eeee-5"
SAMPLE_ACCOUNT_URN = "urn:li:sponsoredAccount:522529623"
SAMPLE_OWNER = "(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A522529623)"

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
EASTERN = timezone(timedelta(hours=-4), "EDT")

WINDOW_28 = (date(2026, 9, 4), date(2026, 10, 1))
WINDOW_14 = (date(2026, 9, 18), date(2026, 10, 1))
SEGMENT_VALUES: Mapping[str, tuple[str, str]] = {
    "MEMBER_JOB_FUNCTION": ("urn:li:function:12", "job_function"),
    "MEMBER_SENIORITY": ("urn:li:seniority:5", "seniority"),
    "MEMBER_INDUSTRY": ("urn:li:industry:4", "industry"),
}


# ---------------------------------------------------------------------------------------------
# Environment, logs and the async backend
# ---------------------------------------------------------------------------------------------


def _settings_env_names() -> frozenset[str]:
    """Return, lowercased, every environment name a ``Settings`` field can be read from."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(field_name)
        for alias in (field.alias, field.validation_alias):
            if isinstance(alias, str):
                names.add(alias)
            elif isinstance(alias, AliasChoices):
                names.update(choice for choice in alias.choices if isinstance(choice, str))
    return frozenset(name.lower() for name in names)


SETTINGS_ENV_NAMES = _settings_env_names()


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every variable ``Settings`` reads and reset its cache.

    The host exports real LinkedIn credentials; the HTTP client and every connector error
    load the settings to collect secrets to redact, so no test may see them.
    """
    for key in list(os.environ):
        if key.lower() in SETTINGS_ENV_NAMES:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture
def captured() -> Iterator[list[EventDict]]:
    """Capture every structlog event as its raw event dict, before any redaction."""
    with capture_logs() as entries:
        yield entries


def _events(entries: Sequence[EventDict], name: str) -> list[EventDict]:
    """Return the captured events called ``name``, in order."""
    return [entry for entry in entries if entry.get("event") == name]


# ---------------------------------------------------------------------------------------------
# Contracts and the conformance checks
# ---------------------------------------------------------------------------------------------

_SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "required", "items", "enum", "additionalProperties", "description"}
)
_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


@functools.cache
def _contract(surface: str) -> Mapping[str, Any]:
    """Load ``contracts/linkedin/{surface}.json``."""
    loaded: Mapping[str, Any] = json.loads(
        (CONTRACTS / f"{surface}.json").read_text(encoding="utf-8")
    )
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


def _conforming[T](surface: str, body: T) -> T:
    """Return a response ``body`` after asserting it satisfies its contract's response schema."""
    errors = _schema_errors(body, _contract(surface)["response"]["body"], surface)
    assert errors == [], errors
    return body


def _pairs(request: httpx.Request) -> list[tuple[str, str]]:
    """Return the raw ``key=value`` pairs of a request query, in order, undecoded."""
    query = request.url.query.decode("ascii")
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        key, _, value = part.partition("=")
        pairs.append((key, value))
    return pairs


def _params(request: httpx.Request) -> dict[str, str]:
    """Return the raw query values of a request keyed by name."""
    return dict(_pairs(request))


def _query(request: httpx.Request) -> str:
    """Return the request query exactly as sent."""
    return request.url.query.decode("ascii")


def _assert_contract_request(request: httpx.Request, surface: str) -> dict[str, str]:
    """Assert a request satisfies its contract: method, path, query keys, headers, fields."""
    contract = _contract(surface)["request"]
    assert request.method == contract["method"]
    assert request.url.host == "api.linkedin.com"
    assert request.url.path == "/rest/" + contract["url"].split("?", 1)[0]
    params = _params(request)
    required: list[str] = contract["required_query"]
    allowed = set(required) | set(contract["optional_query"])
    assert [key for key in required if key not in params] == []
    assert sorted(set(params) - allowed) == []
    for header in contract["required_headers"]:
        assert header in request.headers, header
    assert request.headers["Authorization"].startswith("Bearer ")
    assert request.headers["Linkedin-Version"] == "202609"
    assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
    if contract["fields"] is not None:
        assert params["fields"] == contract["fields"]
    return params


def _restli_range(start: date, end: date) -> str:
    """Spell a Rest.li 2.0 ``dateRange`` value independently of the module's helpers."""
    return (
        f"(start:(year:{start.year},month:{start.month},day:{start.day}),"
        f"end:(year:{end.year},month:{end.month},day:{end.day}))"
    )


def _sha256(email: str) -> str:
    """Return the SHA-256 hex digest the parser stores for a normalised email."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# The mock LinkedIn server and the client under test
# ---------------------------------------------------------------------------------------------

type Handler = Callable[[httpx.Request], httpx.Response]


class Server:
    """Answers LinkedIn requests with a handler and records every request it receives."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record ``request`` and return the handler's response."""
        self.requests.append(request)
        return self._handler(request)

    def queries(self) -> list[str]:
        """Return every request's query exactly as sent, in order."""
        return [_query(request) for request in self.requests]


def _sequence(*responses: httpx.Response) -> Handler:
    """Serve ``responses`` in order; a request beyond them fails the test."""
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        """Return the next scripted response."""
        if not queue:
            pytest.fail(f"unexpected request to {request.url.path}")
        return queue.pop(0)

    return handle


def _json(body: object, status: int = 200) -> httpx.Response:
    """Build a JSON response."""
    return httpx.Response(status, json=body)


def _error(status: int, message: str) -> httpx.Response:
    """Build a LinkedIn error response (``{status, message}``)."""
    return httpx.Response(status, json={"status": status, "message": message})


def _unavailable() -> httpx.Response:
    """Build a transient HTTP 503."""
    return _error(503, "Service Unavailable")


async def _no_sleep(seconds: float) -> None:
    """Stand in for ``asyncio.sleep``: retries never wait in these tests."""


NO_SLEEP_RETRY = RetryPolicy(attempts=2, sleep=_no_sleep)


@contextlib.asynccontextmanager
async def _linkedin(
    server: Server, tokens: TokenSource | None = None
) -> AsyncIterator[LinkedInClient]:
    """Yield a client whose calls reach ``server`` through an ``httpx.MockTransport``."""
    source = StaticTokenProvider(TOKEN) if tokens is None else tokens
    async with build_client("linkedin", httpx.MockTransport(server), settings=Settings()) as http:
        yield LinkedInClient(http, source, retry=NO_SLEEP_RETRY)


class RefreshingTokens:
    """A refreshable token source that issues a new numbered test token after each invalidation."""

    def __init__(self) -> None:
        self.issued = 0
        self.invalidations = 0
        self._current: str | None = None

    @property
    def can_refresh(self) -> bool:
        """Always ``True``: a new token can be obtained."""
        return True

    async def get(self) -> str:
        """Return the cached token, issuing a new one when none is cached."""
        if self._current is None:
            self.issued += 1
            self._current = f"test-linkedin-token-{self.issued}"
        return self._current

    def invalidate(self) -> None:
        """Drop the cached token."""
        self.invalidations += 1
        self._current = None


# ---------------------------------------------------------------------------------------------
# Body builders, starting from the documented samples where the contracts publish one
# ---------------------------------------------------------------------------------------------


def _ymd(day: date) -> dict[str, int]:
    """A Rest.li response date ``{year, month, day}``."""
    return {"year": day.year, "month": day.month, "day": day.day}


def _daily(creative_urn: str, day: date, *, cost: str = "1.5", impressions: int = 10) -> Any:
    """A daily creative element: the ad_analytics sample with its pivot set to a creative."""
    element = _sample("ad_analytics")["elements"][0]
    element["pivotValues"] = [creative_urn]
    element["dateRange"] = {"start": _ymd(day), "end": _ymd(day)}
    element["costInLocalCurrency"] = cost
    element["impressions"] = impressions
    return element


def _links(next_href: str | None) -> list[dict[str, str]]:
    """The Rest.li ``paging.links`` list, with a ``next`` link when one is given."""
    if next_href is None:
        return []
    return [{"rel": "next", "href": next_href, "type": "application/json"}]


def _analytics_body(
    surface: str,
    elements: Sequence[Any],
    *,
    next_href: str | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """An ``adAnalytics`` response; checked against ``surface``'s schema unless very large."""
    body: dict[str, Any] = {
        "elements": list(elements),
        "paging": {"count": 10, "start": 0, "links": _links(next_href)},
    }
    return _conforming(surface, body) if validate else body


def _daily_body(elements: Sequence[Any], *, next_href: str | None = None) -> dict[str, Any]:
    """A daily ``ad_analytics`` response."""
    return _analytics_body("ad_analytics", elements, next_href=next_href)


def _capped_daily_body() -> dict[str, Any]:
    """A daily response holding exactly the 15,000-element cap and no ``next`` link."""
    template = _daily(DISCARDED_CREATIVE, date(2026, 9, 1))
    _conforming("ad_analytics", {"elements": [template]})
    return _analytics_body("ad_analytics", [template] * MAX_ANALYTICS_ELEMENTS, validate=False)


def _segment(pivot_values: Sequence[str], **metrics: Any) -> Any:
    """A segment element: the ad_analytics_statistics sample with its pivots and metrics set."""
    element = _sample("ad_analytics_statistics")["elements"][0]
    element["pivotValues"] = list(pivot_values)
    element.update(metrics)
    return element


def _statistics_body(elements: Sequence[Any], *, validate: bool = True) -> dict[str, Any]:
    """An ``ad_analytics_statistics`` response."""
    return _analytics_body("ad_analytics_statistics", elements, validate=validate)


def _discovery(campaign_urn: str, impressions: int) -> dict[str, Any]:
    """A ``pivot=CAMPAIGN`` discovery element carrying only ``pivotValues`` and impressions."""
    return {"pivotValues": [campaign_urn], "impressions": impressions}


def _discovery_body(
    elements: Sequence[Any], *, next_href: str | None = None, validate: bool = True
) -> dict[str, Any]:
    """An ``ad_analytics_campaign_delivery`` response (the fallback's discovery call)."""
    return _analytics_body(
        "ad_analytics_campaign_delivery", elements, next_href=next_href, validate=validate
    )


def _member_body(
    elements: Sequence[Any], *, next_href: str | None = None, validate: bool = True
) -> dict[str, Any]:
    """An ``ad_analytics_member_segments`` response (the fallback's per-campaign request)."""
    return _analytics_body(
        "ad_analytics_member_segments", elements, next_href=next_href, validate=validate
    )


def _question(question_id: int, predefined: str | None = None) -> dict[str, Any]:
    """A form question with ``questionId``, ``name`` and, when given, ``predefinedField``."""
    question: dict[str, Any] = {"questionId": question_id, "name": f"question{question_id}"}
    if predefined is not None:
        question["predefinedField"] = predefined
    return question


WORK_EMAIL_QUESTIONS = (
    _question(FIRST_NAME_QID, "FIRST_NAME"),
    _question(WORK_EMAIL_QID, "WORK_EMAIL"),
)


def _form(form_id: int, version: int, questions: Sequence[Mapping[str, Any]]) -> Any:
    """A lead form element: the lead_forms sample with its id, version and questions set."""
    element = _sample("lead_forms")["elements"][0]
    element["id"] = form_id
    element["versionId"] = version
    element["name"] = f"Fake white paper form {form_id}"
    element["content"]["questions"] = [dict(question) for question in questions]
    return element


def _page(
    surface: str,
    elements: Sequence[Any],
    *,
    count: int,
    start: int = 0,
    next_href: str | None = None,
) -> dict[str, Any]:
    """A Lead Sync ``q=owner`` page, checked against ``surface``'s response schema."""
    body = {
        "elements": list(elements),
        "paging": {
            "count": count,
            "start": start,
            "total": len(elements),
            "links": _links(next_href),
        },
    }
    return _conforming(surface, body)


def _text_answer(question_id: int, text: str) -> dict[str, Any]:
    """A text answer, shaped as the documented sample's first answer."""
    return {"questionId": question_id, "answerDetails": {"textQuestionAnswer": {"answer": text}}}


def _lead(
    lead_id: str,
    email: str,
    *,
    version: int = FORM_VERSION,
    decorated: bool = True,
    test_lead: bool = False,
) -> Any:
    """A projected (``LEAD_FIELDS``) response built from the documented sample.

    Under the projection LinkedIn omits ``submitter``, ``associatedEntity`` and
    ``leadMetadata`` and adds the ``form`` decoration, so those keys are dropped and the
    decoration added. The form asks ``FIRST_NAME`` and ``WORK_EMAIL``, the live account's only
    email question.
    """
    element = _sample("lead_form_responses")["elements"][0]
    for key in ("submitter", "associatedEntity", "leadMetadata"):
        del element[key]
    element["id"] = lead_id
    element["testLead"] = test_lead
    element["submittedAt"] = SUBMITTED_MS
    element["owner"]["sponsoredAccount"] = ACCOUNT_URN
    element["versionedLeadGenFormUrn"] = (
        f"urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{FORM_ID},{version})"
    )
    element["leadMetadataInfo"]["sponsoredLeadMetadataInfo"]["campaign"]["id"] = CAMPAIGN_LOW
    element["associatedEntityInfo"]["associatedCreative"]["id"] = CREATIVE_A
    element["formResponse"]["answers"] = [
        _text_answer(FIRST_NAME_QID, "Ada"),
        _text_answer(WORK_EMAIL_QID, email),
    ]
    if decorated:
        element["form"] = {
            "id": FORM_ID,
            "name": "Fake white paper form",
            "content": {"questions": [dict(question) for question in WORK_EMAIL_QUESTIONS]},
        }
    return element


def _lead_page(elements: Sequence[Any], *, count: int = LEAD_PAGE_SIZE, start: int = 0) -> Any:
    """A ``lead_form_responses`` page."""
    return _page("lead_form_responses", elements, count=count, start=start)


def _requested_ids(request: httpx.Request) -> list[str]:
    """Return the decoded ids of a batch re-fetch's ``ids=List(...)``."""
    value = _params(request)["ids"]
    assert value.startswith("List(")
    assert value.endswith(")")
    return [unquote(item) for item in value[len("List(") : -1].split(",")]


def _echo_batch(request: httpx.Request) -> httpx.Response:
    """Answer a batch re-fetch with one result per requested id."""
    results = {lead_id: _lead(lead_id, EMAIL) for lead_id in _requested_ids(request)}
    return _json({"results": results, "statuses": {}, "errors": {}})


# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------


def test_request_constants_equal_their_contracts() -> None:
    """Projections, page sizes, the batch size and the element cap equal the contract values."""
    assert _contract("ad_analytics")["request"]["fields"] == DAILY_FIELDS
    assert _contract("ad_analytics_statistics")["request"]["fields"] == SEGMENT_FIELDS
    assert _contract("lead_form_responses")["request"]["fields"] == LEAD_FIELDS
    assert _contract("lead_form_responses_batch")["request"]["fields"] == LEAD_FIELDS
    # LEAD_FIELDS is generated from those files, and both lead requests send that very value.
    assert client_module._LEAD_FIELDS_QUERY_VALUE is LEAD_FIELDS
    assert _contract("lead_forms")["request"]["limits"]["page_size"] == LEAD_PAGE_SIZE
    assert _contract("lead_form_responses")["request"]["limits"]["page_size"] == LEAD_PAGE_SIZE
    batch_limits = _contract("lead_form_responses_batch")["request"]["limits"]
    assert batch_limits["client_max_ids"] == LEAD_BATCH_SIZE
    assert batch_limits["max_ids"] == LEAD_BATCH_SIZE
    assert _contract("ad_analytics_member_segments")["request"]["fields"] == SEGMENT_FIELDS
    assert (
        _contract("ad_analytics_campaign_delivery")["request"]["fields"] == SEGMENT_DISCOVERY_FIELDS
    )
    for surface in (
        "ad_analytics",
        "ad_analytics_statistics",
        "ad_analytics_campaign_delivery",
        "ad_analytics_member_segments",
    ):
        assert _contract(surface)["request"]["limits"]["max_elements"] == MAX_ANALYTICS_ELEMENTS
    assert SEGMENT_DISCOVERY_FIELDS == "pivotValues,impressions"


# ---------------------------------------------------------------------------------------------
# LEAD_FIELDS, generated from the lead-response contracts
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_both_lead_requests_send_the_generated_projection_raw() -> None:
    """``list_lead_responses`` and ``get_lead_responses`` both send ``LEAD_FIELDS`` unencoded."""
    server = Server(
        _sequence(
            _json(_lead_page([])),
            _json({"results": {}, "statuses": {}, "errors": {}}),
        )
    )
    submitted = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC))
    async with _linkedin(server) as client:
        await client.list_lead_responses(ACCOUNT_ID, submitted, known_versions={})
        await client.get_lead_responses(ACCOUNT_ID, [LEAD_A])

    projections = {
        surface: _contract(surface)["request"]["fields"]
        for surface in ("lead_form_responses", "lead_form_responses_batch")
    }
    assert projections == dict.fromkeys(projections, LEAD_FIELDS)
    assert [_params(request)["fields"] for request in server.requests] == [LEAD_FIELDS] * 2
    assert all(f"&fields={LEAD_FIELDS}" in _query(request) for request in server.requests)


# ---------------------------------------------------------------------------------------------
# analytics_daily
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("account", [ACCOUNT_ID, ACCOUNT_URN])
async def test_analytics_daily_request_matches_the_contract_and_keeps_utc_dates(
    account: str,
) -> None:
    """The daily request has the contract's shape and rows keep LinkedIn's UTC days as sent."""
    start, end = date(2026, 9, 30), date(2026, 10, 1)
    server = Server(
        _sequence(
            _json(
                _daily_body(
                    [
                        _daily(CREATIVE_A, start, cost="12.345678", impressions=40),
                        _daily(CREATIVE_A, end, cost="0.0", impressions=0),
                    ]
                )
            )
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(account, (start, end), expected_creatives=2)

    [request] = server.requests
    params = _assert_contract_request(request, "ad_analytics")
    assert _query(request) == (
        "q=analytics&pivot=CREATIVE&timeGranularity=DAILY"
        "&dateRange=(start:(year:2026,month:9,day:30),end:(year:2026,month:10,day:1))"
        f"&accounts={ACCOUNTS}&fields={DAILY_FIELDS}"
    )
    assert "pivotValues" not in params
    assert "dateRange" in params["fields"].split(",")
    assert [(row.creative_urn, row.date) for row in listing.rows] == [
        (CREATIVE_A, start),
        (CREATIVE_A, end),
    ]
    assert listing.rows[0].cost == Decimal("12.345678")
    assert listing.rows[0].impressions == 40
    assert listing.rows[0].clicks is None
    assert listing.complete is True
    assert listing.reason is None
    assert listing.calls == 1
    assert listing.interrupted_by is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("expected_creatives", "date_range", "chunks"),
    [
        pytest.param(
            1000,
            (date(2026, 9, 20), date(2026, 10, 19)),
            [
                (date(2026, 9, 20), date(2026, 10, 3)),
                (date(2026, 10, 4), date(2026, 10, 17)),
                (date(2026, 10, 18), date(2026, 10, 19)),
            ],
            id="14-day-chunks-across-a-month",
        ),
        pytest.param(
            0,
            (date(2026, 9, 1), date(2026, 9, 3)),
            [(date(2026, 9, 1), date(2026, 9, 3))],
            id="no-creatives-one-chunk",
        ),
        pytest.param(
            7500,
            (date(2026, 9, 1), date(2026, 9, 2)),
            [(date(2026, 9, 1), date(2026, 9, 1)), (date(2026, 9, 2), date(2026, 9, 2))],
            id="one-day-chunks",
        ),
        pytest.param(
            20_000,
            (date(2026, 9, 1), date(2026, 9, 3)),
            [
                (date(2026, 9, 1), date(2026, 9, 1)),
                (date(2026, 9, 2), date(2026, 9, 2)),
                (date(2026, 9, 3), date(2026, 9, 3)),
            ],
            id="more-creatives-than-the-cap",
        ),
        pytest.param(
            3,
            (date(2026, 9, 1), date(2026, 9, 1)),
            [(date(2026, 9, 1), date(2026, 9, 1))],
            id="single-day-probe",
        ),
    ],
)
async def test_analytics_daily_chunks_the_range_below_the_element_cap(
    expected_creatives: int,
    date_range: tuple[date, date],
    chunks: list[tuple[date, date]],
) -> None:
    """Chunks of 14,999 // creatives days keep creatives x days under 15,000, in date order."""
    server = Server(lambda request: _json(_daily_body([])))
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(
            ACCOUNT_ID, date_range, expected_creatives=expected_creatives
        )

    assert [_params(request)["dateRange"] for request in server.requests] == [
        _restli_range(start, end) for start, end in chunks
    ]
    for start, end in chunks:
        days = (end - start).days + 1
        assert days == 1 or days * max(expected_creatives, 1) < MAX_ANALYTICS_ELEMENTS
    assert listing.rows == ()
    assert listing.complete is True
    assert listing.calls == len(chunks)


@pytest.mark.anyio
async def test_analytics_daily_splits_a_capped_range_at_its_midpoint(
    captured: list[EventDict],
) -> None:
    """A response at the cap with no next link is discarded and re-read as two halves."""
    first, last = date(2026, 9, 1), date(2026, 9, 5)
    halves = {
        _restli_range(first, last): _capped_daily_body(),
        _restli_range(first, date(2026, 9, 3)): _daily_body(
            [_daily(CREATIVE_A, first), _daily(CREATIVE_B, date(2026, 9, 3))]
        ),
        _restli_range(date(2026, 9, 4), last): _daily_body([_daily(CREATIVE_A, last)]),
    }
    server = Server(lambda request: _json(halves[_params(request)["dateRange"]]))
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(ACCOUNT_ID, (first, last), expected_creatives=1)

    assert [_params(request)["dateRange"] for request in server.requests] == list(halves)
    assert [(row.creative_urn, row.date) for row in listing.rows] == [
        (CREATIVE_A, first),
        (CREATIVE_B, date(2026, 9, 3)),
        (CREATIVE_A, last),
    ]
    assert DISCARDED_CREATIVE not in {row.creative_urn for row in listing.rows}
    assert listing.complete is True
    assert listing.calls == 3
    [split] = _events(captured, "linkedin.analytics.split")
    assert split["days"] == 5
    assert split["surface"] == "ad_analytics"


@pytest.mark.anyio
async def test_analytics_daily_single_day_at_the_cap_keeps_rows_and_is_incomplete(
    captured: list[EventDict],
) -> None:
    """A one-day partition still at the cap keeps its rows and marks the listing incomplete."""
    first, second = date(2026, 9, 1), date(2026, 9, 2)
    template = _daily(CREATIVE_A, first)
    full_day = [
        {**template, "pivotValues": [f"urn:li:sponsoredCreative:{990100000 + index}"]}
        for index in range(MAX_ANALYTICS_ELEMENTS)
    ]
    bodies = {
        _restli_range(first, second): _capped_daily_body(),
        _restli_range(first, first): _analytics_body("ad_analytics", full_day, validate=False),
        _restli_range(second, second): _daily_body([_daily(CREATIVE_B, second)]),
    }
    server = Server(lambda request: _json(bodies[_params(request)["dateRange"]]))
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(ACCOUNT_ID, (first, second), expected_creatives=5000)

    assert [_params(request)["dateRange"] for request in server.requests] == list(bodies)
    assert len(listing.rows) == MAX_ANALYTICS_ELEMENTS + 1
    assert listing.rows[-1].creative_urn == CREATIVE_B
    assert listing.complete is False
    assert listing.reason == "result limit reached at finest partition (one day)"
    assert listing.interrupted_by is None
    [outcome] = _events(captured, "linkedin.listing")
    assert outcome["log_level"] == "warning"
    assert outcome["complete"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "link_base",
    [
        pytest.param("/rest/adAnalytics", id="rest-path"),
        pytest.param("https://api.linkedin.com/rest/adAnalytics", id="absolute-url"),
    ],
)
async def test_analytics_daily_follows_next_links_and_dedupes_rows(
    link_base: str, captured: list[EventDict]
) -> None:
    """A next link is followed at once; a repeated creative and day keeps its first row."""
    day = date(2026, 9, 1)
    first_query = (
        "q=analytics&pivot=CREATIVE&timeGranularity=DAILY"
        f"&dateRange={_restli_range(day, day)}&accounts={ACCOUNTS}&fields={DAILY_FIELDS}"
    )
    next_query = f"{first_query}&start=2&count=2"
    server = Server(
        _sequence(
            _json(
                _daily_body(
                    [_daily(CREATIVE_A, day, cost="1.5")], next_href=f"{link_base}?{next_query}"
                )
            ),
            _json(_daily_body([_daily(CREATIVE_A, day, cost="9.9"), _daily(CREATIVE_B, day)])),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(ACCOUNT_ID, (day, day), expected_creatives=2)

    assert server.queries() == [first_query, next_query]
    assert server.requests[1].url.path == "/rest/adAnalytics"
    assert [(row.creative_urn, row.cost) for row in listing.rows] == [
        (CREATIVE_A, Decimal("1.5")),
        (CREATIVE_B, Decimal("1.5")),
    ]
    assert listing.complete is True
    assert listing.calls == 2
    [outcome] = _events(captured, "linkedin.listing")
    assert outcome["repeats_dropped"] == 1
    assert outcome["rows"] == 2


@pytest.mark.anyio
async def test_analytics_daily_next_link_chain_continues_past_an_empty_page() -> None:
    """An empty page with a next link is not the end: the rows behind it are kept."""
    day = date(2026, 9, 1)
    server = Server(
        _sequence(
            _json(_daily_body([_daily(CREATIVE_A, day)], next_href="/rest/adAnalytics?q=a")),
            _json(_daily_body([], next_href="/rest/adAnalytics?q=b")),
            _json(_daily_body([_daily(CREATIVE_B, day)])),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(ACCOUNT_ID, (day, day), expected_creatives=2)

    assert server.queries()[1:] == ["q=a", "q=b"]
    assert [row.creative_urn for row in listing.rows] == [CREATIVE_A, CREATIVE_B]
    assert listing.complete is True
    assert listing.reason is None
    assert listing.interrupted_by is None
    assert listing.calls == 3


@pytest.mark.anyio
async def test_analytics_daily_transient_failure_after_an_empty_page_is_incomplete() -> None:
    """A transient failure reading past an empty page leaves the listing incomplete."""
    day = date(2026, 9, 1)
    server = Server(
        _sequence(
            _json(_daily_body([_daily(CREATIVE_A, day)], next_href="/rest/adAnalytics?q=a")),
            _json(_daily_body([], next_href="/rest/adAnalytics?q=b")),
            _unavailable(),
            _unavailable(),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(ACCOUNT_ID, (day, day), expected_creatives=2)

    assert server.queries()[1:] == ["q=a", "q=b", "q=b"]
    assert [row.creative_urn for row in listing.rows] == [CREATIVE_A]
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.complete is False
    assert listing.reason == listing.interrupted_by.reason
    assert listing.calls == 3


@pytest.mark.anyio
async def test_analytics_daily_empty_page_next_link_repeating_a_request_is_a_defect() -> None:
    """An empty page linking back to a request already read is a defect, never the end."""
    day = date(2026, 9, 1)
    server = Server(
        _sequence(
            _json(_daily_body([_daily(CREATIVE_A, day)], next_href="/rest/adAnalytics?q=a")),
            _json(_daily_body([], next_href="/rest/adAnalytics?q=a")),
        )
    )
    async with _linkedin(server) as client:
        with pytest.raises(QueryDefect, match="repeats an earlier request") as caught:
            await client.analytics_daily(ACCOUNT_ID, (day, day), expected_creatives=1)

    assert caught.value.endpoint == "ad_analytics"
    assert caught.value.code == "validation"
    assert len(server.requests) == 2


@pytest.mark.anyio
async def test_analytics_daily_chain_of_empty_pages_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty pages that keep returning fresh next links end in a defect at the bound."""
    monkeypatch.setattr(client_module, "_MAX_NEXT_LINKS", 2)
    day = date(2026, 9, 1)
    pages = iter(range(1, 10))

    def handle(request: httpx.Request) -> httpx.Response:
        """Answer every page with no rows and a fresh next link."""
        href = f"/rest/adAnalytics?q=analytics&start={next(pages)}"
        return _json(_daily_body([], next_href=href))

    server = Server(handle)
    async with _linkedin(server) as client:
        with pytest.raises(QueryDefect, match="more than 2 'next' links") as caught:
            await client.analytics_daily(ACCOUNT_ID, (day, day), expected_creatives=1)

    assert caught.value.endpoint == "ad_analytics"
    assert len(server.requests) == 3


@pytest.mark.anyio
async def test_analytics_daily_next_link_repeating_a_request_is_a_defect() -> None:
    """A next link back to a request already read is a defect, so the chain terminates."""
    day = date(2026, 9, 1)
    first_query = (
        "q=analytics&pivot=CREATIVE&timeGranularity=DAILY"
        f"&dateRange={_restli_range(day, day)}&accounts={ACCOUNTS}&fields={DAILY_FIELDS}"
    )
    server = Server(
        _sequence(
            _json(
                _daily_body([_daily(CREATIVE_A, day)], next_href=f"/rest/adAnalytics?{first_query}")
            )
        )
    )
    async with _linkedin(server) as client:
        with pytest.raises(QueryDefect, match="repeats an earlier request") as caught:
            await client.analytics_daily(ACCOUNT_ID, (day, day), expected_creatives=1)

    assert caught.value.endpoint == "ad_analytics"
    assert caught.value.code == "validation"
    assert len(server.requests) == 1


@pytest.mark.anyio
async def test_analytics_daily_next_link_chain_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chain longer than the next-link bound is a defect rather than an endless read."""
    monkeypatch.setattr(client_module, "_MAX_NEXT_LINKS", 2)
    day = date(2026, 9, 1)
    pages = iter(range(1, 10))

    def handle(request: httpx.Request) -> httpx.Response:
        """Answer every page with one row and a fresh next link."""
        href = f"/rest/adAnalytics?q=analytics&start={next(pages)}"
        return _json(_daily_body([_daily(CREATIVE_A, day)], next_href=href))

    server = Server(handle)
    async with _linkedin(server) as client:
        with pytest.raises(QueryDefect, match="more than 2 'next' links"):
            await client.analytics_daily(ACCOUNT_ID, (day, day), expected_creatives=1)

    assert len(server.requests) == 3


@pytest.mark.anyio
async def test_analytics_daily_transient_failure_keeps_the_rows_already_read(
    captured: list[EventDict],
) -> None:
    """A transient failure ends the listing early with every row read so far kept."""
    first_chunk_day, second_chunk_day = date(2026, 9, 1), date(2026, 9, 15)
    server = Server(
        _sequence(
            _json(_daily_body([_daily(CREATIVE_A, first_chunk_day)])),
            _json(
                _daily_body(
                    [_daily(CREATIVE_B, second_chunk_day)],
                    next_href="/rest/adAnalytics?q=analytics&start=1",
                )
            ),
            _unavailable(),
            _unavailable(),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(
            ACCOUNT_ID, (date(2026, 9, 1), date(2026, 9, 30)), expected_creatives=1000
        )

    ranges = [_params(request).get("dateRange") for request in server.requests]
    assert ranges[:2] == [
        _restli_range(date(2026, 9, 1), date(2026, 9, 14)),
        _restli_range(date(2026, 9, 15), date(2026, 9, 28)),
    ]
    assert _restli_range(date(2026, 9, 29), date(2026, 9, 30)) not in ranges
    assert len(server.requests) == 4
    assert listing.calls == 3
    assert [(row.creative_urn, row.date) for row in listing.rows] == [
        (CREATIVE_A, first_chunk_day),
        (CREATIVE_B, second_chunk_day),
    ]
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.interrupted_by.http_status == 503
    assert listing.complete is False
    assert listing.reason == listing.interrupted_by.reason
    [outcome] = _events(captured, "linkedin.listing")
    assert outcome["log_level"] == "warning"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error"),
    [
        pytest.param(403, AccessDenied, id="403-access-denied"),
        pytest.param(400, QueryDefect, id="400-defect"),
    ],
)
async def test_analytics_daily_access_denied_and_defects_propagate(
    status: int, error: type[Exception]
) -> None:
    """A 403 is access denied and a 400 a defect: neither is retried nor swallowed."""
    server = Server(_sequence(_error(status, "Not allowed")))
    async with _linkedin(server) as client:
        with pytest.raises(error):
            await client.analytics_daily(
                ACCOUNT_ID, (date(2026, 9, 1), date(2026, 9, 1)), expected_creatives=1
            )

    assert len(server.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("account", "date_range", "expected_creatives", "message"),
    [
        pytest.param(
            ACCOUNT_ID,
            (date(2026, 9, 2), date(2026, 9, 1)),
            1,
            "must not start after it ends",
            id="reversed-range",
        ),
        pytest.param(
            ACCOUNT_ID,
            (date(2026, 9, 1), date(2026, 9, 1)),
            -1,
            "non-negative integer",
            id="negative-creatives",
        ),
        pytest.param(
            ACCOUNT_ID,
            (date(2026, 9, 1), date(2026, 9, 1)),
            True,
            "non-negative integer",
            id="boolean-creatives",
        ),
        pytest.param(
            "acct-1",
            (date(2026, 9, 1), date(2026, 9, 1)),
            1,
            "account_id",
            id="malformed-account",
        ),
    ],
)
async def test_analytics_daily_rejects_invalid_arguments_without_a_call(
    account: str, date_range: tuple[date, date], expected_creatives: int, message: str
) -> None:
    """A reversed range, a negative or boolean creative count or a bad account is a ValueError."""
    server = Server(_sequence())
    async with _linkedin(server) as client:
        with pytest.raises(ValueError, match=message):
            await client.analytics_daily(account, date_range, expected_creatives=expected_creatives)

    assert server.requests == []


def _creative_element(creative_id: int) -> Any:
    """A creative element: the creatives sample renumbered, in campaign ``CAMPAIGN_LOW``."""
    element = _sample("creatives")["elements"][0]
    element.update(
        id=f"urn:li:sponsoredCreative:{creative_id}", campaign=CAMPAIGN_LOW, account=ACCOUNT_URN
    )
    return element


def _creatives_page(elements: Sequence[Any], token: str | None = None) -> Any:
    """A ``creatives`` cursor page, with ``metadata.nextPageToken`` when another page follows."""
    metadata = {} if token is None else {"nextPageToken": token}
    return _conforming("creatives", {"elements": list(elements), "metadata": metadata})


@pytest.mark.anyio
async def test_analytics_daily_two_arguments_without_a_creative_count_read_one_chunk() -> None:
    """``analytics_daily(acct, range)`` with no creative count known reads the range at once."""
    start, end = date(2026, 4, 5), date(2026, 10, 1)
    server = Server(
        _sequence(_json(_daily_body([_daily(CREATIVE_A, start), _daily(CREATIVE_B, end)])))
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(ACCOUNT_ID, (start, end))

    [request] = server.requests
    params = _assert_contract_request(request, "ad_analytics")
    assert params["dateRange"] == _restli_range(start, end)
    assert (end - start).days + 1 == 180
    assert [(row.creative_urn, row.date) for row in listing.rows] == [
        (CREATIVE_A, start),
        (CREATIVE_B, end),
    ]
    assert listing.complete is True
    assert listing.reason is None
    assert listing.calls == 1


@pytest.mark.anyio
async def test_analytics_daily_two_arguments_still_split_a_capped_range() -> None:
    """Without a creative count, a capped whole-range read is discarded and re-read in halves."""
    first, last = date(2026, 9, 1), date(2026, 9, 5)
    halves = {
        _restli_range(first, last): _capped_daily_body(),
        _restli_range(first, date(2026, 9, 3)): _daily_body(
            [_daily(CREATIVE_A, first), _daily(CREATIVE_B, date(2026, 9, 3))]
        ),
        _restli_range(date(2026, 9, 4), last): _daily_body([_daily(CREATIVE_A, last)]),
    }
    server = Server(lambda request: _json(halves[_params(request)["dateRange"]]))
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(ACCOUNT_ID, (first, last))

    assert [_params(request)["dateRange"] for request in server.requests] == list(halves)
    assert [(row.creative_urn, row.date) for row in listing.rows] == [
        (CREATIVE_A, first),
        (CREATIVE_B, date(2026, 9, 3)),
        (CREATIVE_A, last),
    ]
    assert listing.complete is True
    assert listing.calls == 3


@pytest.mark.anyio
async def test_analytics_daily_two_arguments_single_day_at_the_cap_is_incomplete() -> None:
    """Without a creative count, a one-day range at the cap keeps its rows and is incomplete."""
    day = date(2026, 9, 1)
    server = Server(_sequence(_json(_capped_daily_body())))
    async with _linkedin(server) as client:
        listing = await client.analytics_daily(ACCOUNT_ID, (day, day))

    assert [_params(request)["dateRange"] for request in server.requests] == [
        _restli_range(day, day)
    ]
    assert [(row.creative_urn, row.date) for row in listing.rows] == [(DISCARDED_CREATIVE, day)]
    assert listing.complete is False
    assert listing.reason == "result limit reached at finest partition (one day)"
    assert listing.interrupted_by is None


@pytest.mark.anyio
async def test_analytics_daily_two_arguments_size_chunks_with_the_listed_creative_count() -> None:
    """The account's creative count from ``list_creatives`` sizes a two-argument call's chunks.

    ``list_creatives`` returns 101 distinct creatives (a repeat across pages counts once), so a
    180-day range reads as 14,999 // 101 = 148 days, then 32 days. Another account has no count
    and reads one chunk; an explicit ``expected_creatives`` still overrides the learned count.
    """
    start, end = date(2026, 4, 5), date(2026, 10, 1)
    first_page = [_creative_element(990200000 + index) for index in range(100)]
    second_page = [_creative_element(990200000), _creative_element(990200100)]

    def handle(request: httpx.Request) -> httpx.Response:
        """Serve two creatives pages, then an empty daily response for every analytics chunk."""
        if request.url.path.endswith("/creatives"):
            if "pageToken" in _params(request):
                return _json(_creatives_page(second_page))
            return _json(_creatives_page(first_page, "creatives-page-2"))
        return _json(_daily_body([]))

    server = Server(handle)
    async with _linkedin(server) as client:
        creatives = await client.list_creatives(ACCOUNT_ID, [CAMPAIGN_LOW])
        learned = await client.analytics_daily(ACCOUNT_ID, (start, end))
        other = await client.analytics_daily("990000002", (start, end))
        explicit = await client.analytics_daily(ACCOUNT_ID, (start, end), expected_creatives=1000)

    assert len(creatives.rows) == 101
    analytics = [request for request in server.requests if request.url.path == "/rest/adAnalytics"]
    for request in analytics:
        _assert_contract_request(request, "ad_analytics")
    ranges = [_params(request)["dateRange"] for request in analytics]
    assert ranges[:2] == [
        _restli_range(start, date(2026, 8, 30)),
        _restli_range(date(2026, 8, 31), end),
    ]
    assert ranges[2] == _restli_range(start, end)
    assert ranges[3] == _restli_range(start, date(2026, 4, 18))
    assert (learned.calls, other.calls, explicit.calls) == (2, 1, 13)
    assert len(ranges) == 2 + 1 + 13
    assert [listing.complete for listing in (learned, other, explicit)] == [True, True, True]


# ---------------------------------------------------------------------------------------------
# analytics_segments: the Statistics Finder request
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("pivot", SEGMENT_PIVOTS)
async def test_analytics_segments_statistics_request_matches_its_contract(pivot: str) -> None:
    """One q=statistics request per pivot and window; rows key campaign, facet and value."""
    value, facet = SEGMENT_VALUES[pivot]
    server = Server(
        _sequence(
            _json(
                _statistics_body(
                    [
                        _segment(
                            [CAMPAIGN_LOW, value],
                            costInLocalCurrency="12.5",
                            oneClickLeads=3,
                        ),
                        _segment([value, CAMPAIGN_HIGH]),
                        _segment([CAMPAIGN_LOW, value], impressions=1),
                    ]
                )
            )
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, pivot, WINDOW_28)

    [request] = server.requests
    _assert_contract_request(request, "ad_analytics_statistics")
    assert _query(request) == (
        f"q=statistics&pivots=List(CAMPAIGN,{pivot})&timeGranularity=ALL"
        f"&dateRange={_restli_range(*WINDOW_28)}&accounts={ACCOUNTS}&fields={SEGMENT_FIELDS}"
    )
    assert [(row.campaign_urn, row.facet, row.value) for row in listing.rows] == [
        (CAMPAIGN_LOW, facet, value),
        (CAMPAIGN_HIGH, facet, value),
    ]
    first, second = listing.rows
    assert first.cost == Decimal("12.5")
    assert first.leads == 3
    assert first.impressions == 54494
    assert second.leads is None
    assert second.cost is None
    assert listing.complete is True
    assert listing.calls == 1


@pytest.mark.anyio
async def test_analytics_segments_statistics_response_at_the_cap_is_incomplete() -> None:
    """A segment response at the cap cannot be split by date, so the listing is incomplete."""
    value, _ = SEGMENT_VALUES["MEMBER_SENIORITY"]
    element = _segment([CAMPAIGN_LOW, value])
    _statistics_body([element])
    server = Server(
        _sequence(
            _json(_statistics_body([element] * MAX_ANALYTICS_ELEMENTS, validate=False)),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)

    assert len(server.requests) == 1
    assert len(listing.rows) == 1
    assert listing.complete is False
    assert listing.reason == "result limit reached"
    assert listing.interrupted_by is None


@pytest.mark.anyio
async def test_analytics_segments_statistics_transient_failure_is_incomplete() -> None:
    """A transient failure of the statistics request yields an interrupted, empty listing."""
    server = Server(_sequence(_unavailable(), _unavailable()))
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, "MEMBER_INDUSTRY", WINDOW_14)

    assert len(server.requests) == 2
    assert listing.rows == ()
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.complete is False
    assert listing.reason == listing.interrupted_by.reason
    assert listing.calls == 1


@pytest.mark.anyio
async def test_analytics_segments_statistics_next_link_continues_past_an_empty_page() -> None:
    """An empty statistics page with a next link is followed; no row behind it is lost."""
    value, facet = SEGMENT_VALUES["MEMBER_SENIORITY"]
    surface = "ad_analytics_statistics"
    server = Server(
        _sequence(
            _json(
                _analytics_body(
                    surface,
                    [_segment([CAMPAIGN_LOW, value])],
                    next_href="/rest/adAnalytics?q=statistics&start=1",
                )
            ),
            _json(_analytics_body(surface, [], next_href="/rest/adAnalytics?q=statistics&start=2")),
            _json(_statistics_body([_segment([CAMPAIGN_HIGH, value])])),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)

    assert server.queries()[1:] == ["q=statistics&start=1", "q=statistics&start=2"]
    assert [(row.campaign_urn, row.facet, row.value) for row in listing.rows] == [
        (CAMPAIGN_LOW, facet, value),
        (CAMPAIGN_HIGH, facet, value),
    ]
    assert listing.complete is True
    assert listing.reason is None
    assert listing.interrupted_by is None
    assert listing.calls == 3


@pytest.mark.anyio
async def test_analytics_segments_statistics_transient_failure_after_an_empty_page() -> None:
    """A transient failure reading past an empty statistics page leaves it incomplete."""
    value, _ = SEGMENT_VALUES["MEMBER_SENIORITY"]
    surface = "ad_analytics_statistics"
    server = Server(
        _sequence(
            _json(
                _analytics_body(
                    surface,
                    [_segment([CAMPAIGN_LOW, value])],
                    next_href="/rest/adAnalytics?q=statistics&start=1",
                )
            ),
            _json(_analytics_body(surface, [], next_href="/rest/adAnalytics?q=statistics&start=2")),
            _unavailable(),
            _unavailable(),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)

    assert len(server.requests) == 4
    assert [row.campaign_urn for row in listing.rows] == [CAMPAIGN_LOW]
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.complete is False
    assert listing.reason == listing.interrupted_by.reason
    assert listing.calls == 3


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error"),
    [
        pytest.param(404, QueryDefect, id="404-defect"),
        pytest.param(403, AccessDenied, id="403-access-denied"),
    ],
)
async def test_analytics_segments_other_errors_never_switch_to_the_fallback(
    status: int, error: type[Exception]
) -> None:
    """Only an HTTP 400 switches finders: a 404 defect or a 403 propagates, and so does the next."""
    server = Server(lambda request: _error(status, "Rejected"))
    async with _linkedin(server) as client:
        for pivot in ("MEMBER_SENIORITY", "MEMBER_INDUSTRY"):
            with pytest.raises(error):
                await client.analytics_segments(ACCOUNT_ID, pivot, WINDOW_28)

    assert [_params(request)["q"] for request in server.requests] == ["statistics", "statistics"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("pivot", "window", "message"),
    [
        pytest.param("MEMBER_COMPANY", WINDOW_28, "pivot must be one of", id="unknown-pivot"),
        pytest.param(
            "MEMBER_SENIORITY",
            (WINDOW_28[1], WINDOW_28[0]),
            "must not start after it ends",
            id="reversed-window",
        ),
    ],
)
async def test_analytics_segments_rejects_invalid_arguments_without_a_call(
    pivot: str, window: tuple[date, date], message: str
) -> None:
    """A pivot outside SEGMENT_PIVOTS or a reversed window is a ValueError before any call."""
    server = Server(_sequence())
    async with _linkedin(server) as client:
        with pytest.raises(ValueError, match=message):
            await client.analytics_segments(ACCOUNT_ID, pivot, window)

    assert server.requests == []


# ---------------------------------------------------------------------------------------------
# analytics_segments: the per-campaign Analytics Finder fallback
# ---------------------------------------------------------------------------------------------


def _fallback_handler(
    discovery: Sequence[Mapping[str, Any]] | Callable[[], httpx.Response],
    *,
    campaign_replies: Mapping[str, Callable[[], httpx.Response]] | None = None,
) -> Handler:
    """Reject q=statistics with HTTP 400 and answer discovery and per-campaign requests.

    ``discovery`` is the discovery elements, or a factory of the discovery response. A
    per-campaign request is answered by ``campaign_replies`` when it names the campaign, else
    with one segment row of the requested pivot's value.
    """
    replies = dict(campaign_replies or {})

    def handle(request: httpx.Request) -> httpx.Response:
        """Route one adAnalytics request by its finder and pivot."""
        params = _params(request)
        if params["q"] == "statistics":
            return _error(400, '"MEMBER_SENIORITY" is not an enum symbol')
        if params["pivot"] == "CAMPAIGN":
            if callable(discovery):
                return discovery()
            return _json(_discovery_body(discovery))
        campaign = unquote(params["campaigns"].removeprefix("List(").removesuffix(")"))
        if campaign in replies:
            return replies[campaign]()
        value, _ = SEGMENT_VALUES[params["pivot"]]
        element = _segment([value], impressions=30, clicks=2, costInLocalCurrency="4.25")
        return _json(_member_body([element]))

    return handle


@pytest.mark.anyio
async def test_analytics_segments_fall_back_to_one_analytics_call_per_campaign(
    captured: list[EventDict],
) -> None:
    """After the Statistics Finder's 400, segments are read per delivering campaign and pivot.

    The discovery call runs once per account and window and is shared by the pivots; campaigns
    with zero impressions are skipped, repeats are read once, and campaigns are read in
    ascending numeric id order. Later calls never retry the rejected Statistics Finder.
    """
    discovery = [
        _discovery(CAMPAIGN_HIGH, 50),
        _discovery(CAMPAIGN_IDLE, 0),
        _discovery(CAMPAIGN_LOW, 7),
        _discovery(CAMPAIGN_HIGH, 50),
    ]
    server = Server(_fallback_handler(discovery))
    async with _linkedin(server) as client:
        seniority = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)
        function = await client.analytics_segments(ACCOUNT_ID, "MEMBER_JOB_FUNCTION", WINDOW_28)
        industry = await client.analytics_segments(ACCOUNT_ID, "MEMBER_INDUSTRY", WINDOW_14)

    range_28, range_14 = _restli_range(*WINDOW_28), _restli_range(*WINDOW_14)

    def per_campaign(pivot: str, date_range: str, campaign_id: str) -> str:
        """The fallback's per-campaign query."""
        return (
            f"q=analytics&pivot={pivot}&timeGranularity=ALL&dateRange={date_range}"
            f"&campaigns=List(urn%3Ali%3AsponsoredCampaign%3A{campaign_id})"
            f"&fields={SEGMENT_FIELDS}"
        )

    def discovery_query(date_range: str) -> str:
        """The fallback's campaign-discovery query."""
        return (
            f"q=analytics&pivot=CAMPAIGN&timeGranularity=ALL&dateRange={date_range}"
            f"&accounts={ACCOUNTS}&fields={SEGMENT_DISCOVERY_FIELDS}"
        )

    assert server.queries() == [
        f"q=statistics&pivots=List(CAMPAIGN,MEMBER_SENIORITY)&timeGranularity=ALL"
        f"&dateRange={range_28}&accounts={ACCOUNTS}&fields={SEGMENT_FIELDS}",
        discovery_query(range_28),
        per_campaign("MEMBER_SENIORITY", range_28, "99000105"),
        per_campaign("MEMBER_SENIORITY", range_28, "990000101"),
        per_campaign("MEMBER_JOB_FUNCTION", range_28, "99000105"),
        per_campaign("MEMBER_JOB_FUNCTION", range_28, "990000101"),
        discovery_query(range_14),
        per_campaign("MEMBER_INDUSTRY", range_14, "99000105"),
        per_campaign("MEMBER_INDUSTRY", range_14, "990000101"),
    ]
    assert [(row.campaign_urn, row.facet, row.value) for row in seniority.rows] == [
        (CAMPAIGN_LOW, "seniority", "urn:li:seniority:5"),
        (CAMPAIGN_HIGH, "seniority", "urn:li:seniority:5"),
    ]
    assert seniority.rows[0].cost == Decimal("4.25")
    assert seniority.rows[0].leads is None
    assert [row.facet for row in function.rows] == ["job_function", "job_function"]
    assert [row.value for row in industry.rows] == ["urn:li:industry:4"] * 2
    assert [seniority.calls, function.calls, industry.calls] == [4, 2, 3]
    assert all(listing.complete for listing in (seniority, function, industry))
    [rejected] = _events(captured, "linkedin.segments.statistics_finder_rejected")
    assert rejected["log_level"] == "warning"
    assert rejected["http_status"] == 400


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("discovery", "message"),
    [
        pytest.param(
            lambda: _error(400, "Invalid campaign facet"),
            "Invalid campaign facet",
            id="discovery-400",
        ),
        pytest.param(
            [{"pivotValues": [CREATIVE_A], "impressions": 3}],
            "urn:li:sponsoredCampaign:",
            id="not-a-campaign-urn",
        ),
        pytest.param(
            [{"pivotValues": [CAMPAIGN_LOW, CAMPAIGN_HIGH], "impressions": 3}],
            "exactly one campaign URN",
            id="two-pivot-values",
        ),
    ],
)
async def test_analytics_segments_fallback_defect_is_chained_to_the_400(
    discovery: Sequence[Mapping[str, Any]] | Callable[[], httpx.Response], message: str
) -> None:
    """A defect of the fallback itself propagates, chained to the Statistics Finder's 400."""
    server = Server(_fallback_handler(discovery))
    async with _linkedin(server) as client:
        with pytest.raises(QueryDefect, match=message) as caught:
            await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)

    cause = caught.value.__cause__
    assert isinstance(cause, QueryDefect)
    assert cause is not caught.value
    assert cause.http_status == 400
    assert cause.endpoint == "ad_analytics_statistics"
    assert len(server.requests) == 2


@pytest.mark.anyio
async def test_analytics_segments_interrupted_discovery_is_not_cached() -> None:
    """A transient discovery failure ends that listing early and is retried by the next pivot."""
    attempts = {"discovery": 0}
    healthy = _fallback_handler([_discovery(CAMPAIGN_LOW, 5)])

    def handle(request: httpx.Request) -> httpx.Response:
        """Fail the first discovery sequence transiently, then answer normally."""
        if _params(request).get("pivot") == "CAMPAIGN":
            attempts["discovery"] += 1
            if attempts["discovery"] <= 2:
                return _unavailable()
        return healthy(request)

    server = Server(handle)
    async with _linkedin(server) as client:
        interrupted = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)
        recovered = await client.analytics_segments(ACCOUNT_ID, "MEMBER_INDUSTRY", WINDOW_28)

    assert interrupted.rows == ()
    assert isinstance(interrupted.interrupted_by, TransientError)
    assert interrupted.complete is False
    assert interrupted.reason == interrupted.interrupted_by.reason
    assert interrupted.calls == 2
    pivots = [_params(request).get("pivot") for request in server.requests]
    assert pivots == [None, "CAMPAIGN", "CAMPAIGN", "CAMPAIGN", "MEMBER_INDUSTRY"]
    assert [row.campaign_urn for row in recovered.rows] == [CAMPAIGN_LOW]
    assert recovered.complete is True


@pytest.mark.anyio
async def test_analytics_segments_discovery_at_the_cap_is_incomplete() -> None:
    """A capped discovery response reads every campaign found but marks the listing incomplete."""
    idle = _discovery(CAMPAIGN_IDLE, 0)
    elements = [idle] * (MAX_ANALYTICS_ELEMENTS - 2)
    elements += [_discovery(CAMPAIGN_HIGH, 9), _discovery(CAMPAIGN_LOW, 5)]
    _discovery_body(elements[-3:])
    capped = _discovery_body(elements, validate=False)
    server = Server(_fallback_handler(lambda: _json(capped)))
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)

    assert [row.campaign_urn for row in listing.rows] == [CAMPAIGN_LOW, CAMPAIGN_HIGH]
    assert listing.complete is False
    assert listing.reason == "result limit reached"
    assert listing.interrupted_by is None
    assert listing.calls == 4


@pytest.mark.anyio
async def test_analytics_segments_campaign_response_at_the_cap_is_incomplete() -> None:
    """A per-campaign response at the cap keeps its rows and marks the listing incomplete."""
    value, _ = SEGMENT_VALUES["MEMBER_SENIORITY"]
    element = _segment([value], impressions=11)
    _member_body([element])
    capped = _member_body([element] * MAX_ANALYTICS_ELEMENTS, validate=False)
    server = Server(
        _fallback_handler(
            [_discovery(CAMPAIGN_LOW, 5), _discovery(CAMPAIGN_HIGH, 9)],
            campaign_replies={CAMPAIGN_LOW: lambda: _json(capped)},
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)

    assert [(row.campaign_urn, row.impressions) for row in listing.rows] == [
        (CAMPAIGN_LOW, 11),
        (CAMPAIGN_HIGH, 30),
    ]
    assert listing.complete is False
    assert listing.reason == "result limit reached"
    assert listing.calls == 4


@pytest.mark.anyio
async def test_analytics_segments_campaign_transient_failure_stops_the_fallback() -> None:
    """A transient per-campaign failure keeps earlier campaigns' rows and reads no later one."""
    server = Server(
        _fallback_handler(
            [
                _discovery(CAMPAIGN_LOW, 5),
                _discovery(CAMPAIGN_HIGH, 9),
                _discovery(CAMPAIGN_THIRD, 2),
            ],
            campaign_replies={CAMPAIGN_HIGH: _unavailable},
        )
    )
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)

    campaigns = [_params(request).get("campaigns") for request in server.requests]
    assert "List(urn%3Ali%3AsponsoredCampaign%3A990000102)" not in campaigns
    assert len(server.requests) == 5
    assert [row.campaign_urn for row in listing.rows] == [CAMPAIGN_LOW]
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.complete is False
    assert listing.reason == listing.interrupted_by.reason


@pytest.mark.anyio
async def test_analytics_segments_fallback_follows_next_links_past_empty_pages() -> None:
    """Discovery and per-campaign chains read the pages behind an empty page."""
    value, facet = SEGMENT_VALUES["MEMBER_SENIORITY"]
    link = "/rest/adAnalytics?q=analytics&chain="
    continuations = {
        "discovery-2": _discovery_body([], next_href=f"{link}discovery-3"),
        "discovery-3": _discovery_body([_discovery(CAMPAIGN_HIGH, 9)]),
        "low-2": _member_body([_segment([value], impressions=11)]),
    }

    def handle(request: httpx.Request) -> httpx.Response:
        """Reject q=statistics; give discovery and one campaign an empty middle page."""
        params = _params(request)
        if params["q"] == "statistics":
            return _error(400, '"MEMBER_SENIORITY" is not an enum symbol')
        if "chain" in params:
            return _json(continuations[params["chain"]])
        if params["pivot"] == "CAMPAIGN":
            first = [_discovery(CAMPAIGN_LOW, 5)]
            return _json(_discovery_body(first, next_href=f"{link}discovery-2"))
        campaign = unquote(params["campaigns"].removeprefix("List(").removesuffix(")"))
        if campaign == CAMPAIGN_LOW:
            return _json(_member_body([], next_href=f"{link}low-2"))
        return _json(_member_body([_segment([value], impressions=30)]))

    server = Server(handle)
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)

    assert [_params(request).get("chain") for request in server.requests] == [
        None,
        None,
        "discovery-2",
        "discovery-3",
        None,
        "low-2",
        None,
    ]
    assert [(row.campaign_urn, row.facet, row.impressions) for row in listing.rows] == [
        (CAMPAIGN_LOW, facet, 11),
        (CAMPAIGN_HIGH, facet, 30),
    ]
    assert listing.complete is True
    assert listing.reason is None
    assert listing.interrupted_by is None
    assert listing.calls == 7


# ---------------------------------------------------------------------------------------------
# analytics_segments: capture-only bounds (max_pages, max_campaigns)
# ---------------------------------------------------------------------------------------------


def _bounded_fallback_handler() -> Handler:
    """Reject q=statistics; serve a paged discovery and paged per-campaign responses.

    Discovery page 1 names ``CAMPAIGN_LOW`` and ``CAMPAIGN_HIGH`` and links to page 2, which
    names ``CAMPAIGN_THIRD``; every campaign's first page carries one row of the requested
    pivot's value and links to an empty second page.
    """
    link = "/rest/adAnalytics?q=analytics&chain="

    def handle(request: httpx.Request) -> httpx.Response:
        """Route one adAnalytics request by finder, pivot and continuation."""
        params = _params(request)
        if params["q"] == "statistics":
            return _error(400, '"MEMBER_SENIORITY" is not an enum symbol')
        if params.get("chain") == "discovery-2":
            return _json(_discovery_body([_discovery(CAMPAIGN_THIRD, 4)]))
        if "chain" in params:
            return _json(_member_body([]))
        if params["pivot"] == "CAMPAIGN":
            first = [_discovery(CAMPAIGN_LOW, 5), _discovery(CAMPAIGN_HIGH, 9)]
            return _json(_discovery_body(first, next_href=f"{link}discovery-2"))
        value, _facet = SEGMENT_VALUES[params["pivot"]]
        return _json(
            _member_body([_segment([value], impressions=30)], next_href=f"{link}campaign-2")
        )

    return handle


@pytest.mark.anyio
async def test_analytics_segments_capture_bounds_send_one_discovery_and_one_campaign() -> None:
    """``max_pages=1, max_campaigns=1``: one discovery and one per-campaign request.

    After the rejected Statistics Finder, the discovery's ``next`` link and the campaign's
    ``next`` link stay unread and only the lowest-id delivering campaign is read, so the
    capture costs exactly one request per fallback surface. The rows read are kept and the
    listing is incomplete with reason "page limit".
    """
    server = Server(_bounded_fallback_handler())
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(
            ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28, max_pages=1, max_campaigns=1
        )

    statistics, discovery, campaign = server.requests
    assert _params(statistics)["q"] == "statistics"
    assert _assert_contract_request(discovery, "ad_analytics_campaign_delivery")["pivot"] == (
        "CAMPAIGN"
    )
    params = _assert_contract_request(campaign, "ad_analytics_member_segments")
    assert params["campaigns"] == "List(urn%3Ali%3AsponsoredCampaign%3A99000105)"
    assert [(row.campaign_urn, row.impressions) for row in listing.rows] == [(CAMPAIGN_LOW, 30)]
    assert (listing.complete, listing.reason, listing.calls) == (False, "page limit", 3)
    assert listing.interrupted_by is None


@pytest.mark.anyio
async def test_analytics_segments_max_campaigns_alone_leaves_later_campaigns_unread() -> None:
    """Every discovery page is read, then only the first two campaigns in id order."""
    server = Server(_bounded_fallback_handler())
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(
            ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28, max_campaigns=2
        )

    assert [_params(request).get("chain") for request in server.requests] == [
        None,
        None,
        "discovery-2",
        None,
        "campaign-2",
        None,
        "campaign-2",
    ]
    assert [row.campaign_urn for row in listing.rows] == [CAMPAIGN_LOW, CAMPAIGN_HIGH]
    assert (listing.complete, listing.reason) == (False, "page limit")


@pytest.mark.anyio
async def test_analytics_segments_without_bounds_read_every_campaign_and_page() -> None:
    """The default (every sync) reads the whole discovery and every campaign's chain."""
    server = Server(_bounded_fallback_handler())
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28)

    assert len(server.requests) == 1 + 2 + 3 * 2
    assert [row.campaign_urn for row in listing.rows] == [
        CAMPAIGN_LOW,
        CAMPAIGN_HIGH,
        CAMPAIGN_THIRD,
    ]
    assert (listing.complete, listing.reason) == (True, None)


@pytest.mark.anyio
async def test_a_bounded_discovery_never_stands_in_for_a_full_one() -> None:
    """A later unbounded call on the same client discovers again instead of reusing the cut."""
    server = Server(_bounded_fallback_handler())
    async with _linkedin(server) as client:
        bounded = await client.analytics_segments(
            ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28, max_pages=1
        )
        full = await client.analytics_segments(ACCOUNT_ID, "MEMBER_JOB_FUNCTION", WINDOW_28)

    discoveries = [
        request
        for request in server.requests
        if _params(request).get("pivot") == "CAMPAIGN" or "discovery" in _query(request)
    ]
    assert len(discoveries) == 3
    assert (bounded.complete, bounded.reason) == (False, "page limit")
    assert [row.campaign_urn for row in bounded.rows] == [CAMPAIGN_LOW, CAMPAIGN_HIGH]
    assert full.complete
    assert [row.campaign_urn for row in full.rows] == [CAMPAIGN_LOW, CAMPAIGN_HIGH, CAMPAIGN_THIRD]


@pytest.mark.anyio
async def test_analytics_segments_statistics_form_reads_one_response_under_max_pages() -> None:
    """The contracted request reads one response; an unread ``next`` link is "page limit"."""
    value, _facet = SEGMENT_VALUES["MEMBER_SENIORITY"]
    link = "/rest/adAnalytics?q=statistics&chain=2"
    body = _analytics_body(
        "ad_analytics_statistics", [_segment([CAMPAIGN_LOW, value])], next_href=link
    )
    server = Server(_sequence(_json(body)))
    async with _linkedin(server) as client:
        listing = await client.analytics_segments(
            ACCOUNT_ID, "MEMBER_SENIORITY", WINDOW_28, max_pages=1, max_campaigns=1
        )

    (request,) = server.requests
    assert _assert_contract_request(request, "ad_analytics_statistics")["q"] == "statistics"
    assert [row.campaign_urn for row in listing.rows] == [CAMPAIGN_LOW]
    assert (listing.complete, listing.reason, listing.calls) == (False, "page limit", 1)


# ---------------------------------------------------------------------------------------------
# list_lead_forms
# ---------------------------------------------------------------------------------------------


def _forms_query(*, count: int, start: int) -> str:
    """The leadForms owner-finder query for one page."""
    return f"q=owner&owner={OWNER}&count={count}&start={start}"


@pytest.mark.anyio
async def test_list_lead_forms_reads_the_documented_sample() -> None:
    """The forms request carries the owner selector and count/start; the sample parses verbatim.

    The sample answers the reference's own request, ``count=1&start=0``, and its ``paging``
    echoes that count, so it is served for that request. Its one form fills the page, so the
    listing reads on to an empty page at ``start=1`` and ends there.
    """
    server = Server(
        _sequence(
            _json(_sample("lead_forms")),
            _json(_page("lead_forms", [], count=1, start=1)),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.list_lead_forms(ACCOUNT_ID, page_size=1)

    for request in server.requests:
        _assert_contract_request(request, "lead_forms")
    assert server.queries() == [_forms_query(count=1, start=0), _forms_query(count=1, start=1)]
    [form] = listing.rows
    assert form.canonical_urn == "urn:li:leadGenForm:6755260984438374400"
    assert form.external_id == "6755260984438374400"
    assert form.name == "LeadGen form for Nimbus 2000"
    assert form.current_version == 1
    assert form.version_map.canonical_urn == form.canonical_urn
    assert form.version_map.version == 1
    assert form.version_map.email_question_ids == ()
    assert form.version_map.question_ids == frozenset({10548, 10540})
    assert listing.complete is True
    assert listing.calls == 2


@pytest.mark.anyio
async def test_list_lead_forms_pages_by_count_and_start() -> None:
    """Pages advance start by count until a page comes back short without a next link."""
    forms = [_form(OTHER_FORM_ID + index, 1, WORK_EMAIL_QUESTIONS) for index in range(5)]
    server = Server(
        _sequence(
            _json(_page("lead_forms", forms[0:2], count=2, start=0)),
            _json(_page("lead_forms", forms[2:4], count=2, start=2)),
            _json(_page("lead_forms", forms[4:5], count=2, start=4)),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.list_lead_forms(ACCOUNT_ID, page_size=2)

    assert server.queries() == [
        _forms_query(count=2, start=0),
        _forms_query(count=2, start=2),
        _forms_query(count=2, start=4),
    ]
    for request in server.requests:
        _assert_contract_request(request, "lead_forms")
    assert [form.external_id for form in listing.rows] == [
        str(OTHER_FORM_ID + index) for index in range(5)
    ]
    assert listing.complete is True
    assert listing.calls == 3


@pytest.mark.anyio
async def test_list_lead_forms_short_page_with_a_next_link_reads_on_from_the_template() -> None:
    """A next link on a short page means more pages; the request is rebuilt, never the href."""
    href = f"/rest/leadForms?count=1&start=1&q=owner&owner={OWNER}"
    server = Server(
        _sequence(
            _json(
                _page(
                    "lead_forms",
                    [_form(FORM_ID, 1, WORK_EMAIL_QUESTIONS)],
                    count=2,
                    next_href=href,
                )
            ),
            _json(_page("lead_forms", [], count=2, start=2)),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.list_lead_forms(ACCOUNT_ID, page_size=2)

    assert server.queries() == [_forms_query(count=2, start=0), _forms_query(count=2, start=2)]
    assert [form.canonical_urn for form in listing.rows] == [FORM_URN]
    assert listing.complete is True


@pytest.mark.anyio
async def test_list_lead_forms_short_page_without_links_ends_the_listing() -> None:
    """A short page with no ``paging.links`` has no next page; the contract makes it optional."""
    body = _page("lead_forms", [_form(FORM_ID, 1, WORK_EMAIL_QUESTIONS)], count=2)
    del body["paging"]["links"]
    _conforming("lead_forms", body)
    server = Server(_sequence(_json(body)))
    async with _linkedin(server) as client:
        listing = await client.list_lead_forms(ACCOUNT_ID, page_size=2)

    assert len(server.requests) == 1
    assert [form.canonical_urn for form in listing.rows] == [FORM_URN]
    assert listing.complete is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("paging", "message"),
    [
        pytest.param(None, "the response has no 'paging' object", id="paging-absent"),
        pytest.param([], "field 'paging' is not a JSON object", id="paging-not-an-object"),
        pytest.param(
            {"count": 2, "start": 0, "links": {}},
            "field 'paging.links' is not a list",
            id="links-not-a-list",
        ),
    ],
)
async def test_list_lead_forms_malformed_paging_on_a_short_page_is_a_defect(
    paging: object, message: str
) -> None:
    """A short page whose paging block breaks the contract is a defect, never an end of data.

    ``paging`` is required by the contract, so a page without it (``None`` here) is a defect too.
    """
    body: dict[str, Any] = {"elements": [_form(FORM_ID, 1, WORK_EMAIL_QUESTIONS)]}
    if paging is not None:
        body["paging"] = paging
    server = Server(_sequence(_json(body)))
    async with _linkedin(server) as client:
        with pytest.raises(QueryDefect, match=message) as caught:
            await client.list_lead_forms(ACCOUNT_ID, page_size=2)

    assert caught.value.endpoint == "lead_forms"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("page_size", "max_pages", "pages", "complete", "reason"),
    [
        pytest.param(1, 1, [1], False, "page limit", id="probe-full-page"),
        pytest.param(1, 1, [0], True, None, id="probe-empty-page"),
        pytest.param(2, 2, [2, 1], True, None, id="last-page-short"),
        pytest.param(2, 2, [2, 2], False, "page limit", id="limit-on-a-full-page"),
    ],
)
async def test_list_lead_forms_max_pages_stops_with_a_page_limit(
    page_size: int, max_pages: int, pages: list[int], complete: bool, reason: str | None
) -> None:
    """max_pages stops paging; the listing is incomplete only when more pages may exist."""
    replies: list[httpx.Response] = []
    form_id = OTHER_FORM_ID
    for index, size in enumerate(pages):
        forms = [_form(form_id + offset, 1, WORK_EMAIL_QUESTIONS) for offset in range(size)]
        form_id += size
        replies.append(_json(_page("lead_forms", forms, count=page_size, start=index * page_size)))
    server = Server(_sequence(*replies))
    async with _linkedin(server) as client:
        listing = await client.list_lead_forms(ACCOUNT_ID, page_size=page_size, max_pages=max_pages)

    assert len(server.requests) == len(pages)
    assert len(listing.rows) == sum(pages)
    assert listing.complete is complete
    assert listing.reason == reason
    assert listing.interrupted_by is None


@pytest.mark.anyio
async def test_list_lead_forms_endless_full_pages_hit_the_defensive_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without max_pages, more full pages than the defensive bound are a defect."""
    monkeypatch.setattr(client_module, "_MAX_OFFSET_PAGES", 3)
    forms = iter(range(OTHER_FORM_ID, OTHER_FORM_ID + 10))

    def handle(request: httpx.Request) -> httpx.Response:
        """Answer every page with one new form."""
        start = int(_params(request)["start"])
        form = _form(next(forms), 1, WORK_EMAIL_QUESTIONS)
        return _json(_page("lead_forms", [form], count=1, start=start))

    server = Server(handle)
    async with _linkedin(server) as client:
        with pytest.raises(QueryDefect, match="more than 3 pages in one listing"):
            await client.list_lead_forms(ACCOUNT_ID, page_size=1)

    assert len(server.requests) == 3


@pytest.mark.anyio
async def test_list_lead_forms_keeps_each_form_once_with_its_current_version() -> None:
    """Forms are keyed by canonical URN; each carries its current version's email question map."""
    edited = _form(
        FORM_ID,
        4,
        [
            _question(WORK_EMAIL_QID, "WORK_EMAIL"),
            _question(EMAIL_QID, "EMAIL"),
            _question(FIRST_NAME_QID, "FIRST_NAME"),
        ],
    )
    other = _form(OTHER_FORM_ID, 1, WORK_EMAIL_QUESTIONS)
    server = Server(
        _sequence(
            _json(_page("lead_forms", [edited, other], count=2, start=0)),
            _json(_page("lead_forms", [_form(FORM_ID, 4, WORK_EMAIL_QUESTIONS)], count=2, start=2)),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.list_lead_forms(ACCOUNT_ID, page_size=2)

    assert [form.canonical_urn for form in listing.rows] == [
        FORM_URN,
        f"urn:li:leadGenForm:{OTHER_FORM_ID}",
    ]
    first = listing.rows[0]
    assert first.current_version == 4
    assert first.version_map == FormVersionMap(
        canonical_urn=FORM_URN,
        version=4,
        email_question_ids=(EMAIL_QID, WORK_EMAIL_QID),
        question_ids=frozenset({EMAIL_QID, WORK_EMAIL_QID, FIRST_NAME_QID}),
    )
    assert listing.rows[1].version_map.email_question_ids == (WORK_EMAIL_QID,)
    assert listing.complete is True


@pytest.mark.anyio
async def test_list_lead_forms_transient_failure_keeps_the_forms_read() -> None:
    """A transient page failure ends the listing early with the forms already read."""
    server = Server(
        _sequence(
            _json(_page("lead_forms", [_form(FORM_ID, 1, WORK_EMAIL_QUESTIONS)], count=1)),
            _unavailable(),
            _unavailable(),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.list_lead_forms(ACCOUNT_ID, page_size=1)

    assert server.queries()[1:] == [_forms_query(count=1, start=1)] * 2
    assert [form.canonical_urn for form in listing.rows] == [FORM_URN]
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.complete is False
    assert listing.reason == listing.interrupted_by.reason
    assert listing.calls == 2


@pytest.mark.anyio
async def test_list_lead_forms_access_denied_propagates() -> None:
    """A 403 on Lead Sync (no r_marketing_leadgen_automation) propagates as access denied."""
    server = Server(_sequence(_error(403, "Not enough permissions to access: leadForms")))
    async with _linkedin(server) as client:
        with pytest.raises(AccessDenied) as caught:
            await client.list_lead_forms(ACCOUNT_ID)

    assert caught.value.http_status == 403
    assert caught.value.endpoint == "lead_forms"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("page_size", "max_pages", "message"),
    [
        pytest.param(0, None, "page_size must be between 1 and 100", id="page-size-zero"),
        pytest.param(101, None, "page_size must be between 1 and 100", id="page-size-101"),
        pytest.param(True, None, "page_size must be between 1 and 100", id="page-size-bool"),
        pytest.param(10, 0, "max_pages must be at least 1", id="max-pages-zero"),
        pytest.param(10, True, "max_pages must be at least 1", id="max-pages-bool"),
    ],
)
async def test_list_lead_forms_rejects_invalid_paging_without_a_call(
    page_size: int, max_pages: int | None, message: str
) -> None:
    """Paging arguments outside 1..100 pages and at least one page are a ValueError."""
    server = Server(_sequence())
    async with _linkedin(server) as client:
        with pytest.raises(ValueError, match=message):
            await client.list_lead_forms(ACCOUNT_ID, page_size=page_size, max_pages=max_pages)

    assert server.requests == []


# ---------------------------------------------------------------------------------------------
# list_lead_responses
# ---------------------------------------------------------------------------------------------

RANGE_START = datetime(2026, 9, 1, 0, 0, tzinfo=EASTERN)
RANGE_END = datetime(2026, 9, 2, 12, 0, 0, 999_999, tzinfo=UTC)
RANGE_START_MS = int(RANGE_START.timestamp()) * 1000
RANGE_END_MS = int(RANGE_END.replace(microsecond=0).timestamp()) * 1000 + 999


def _responses_query(*, count: int = LEAD_PAGE_SIZE, start: int = 0) -> str:
    """The leadFormResponses owner-finder query for one page of the test range."""
    return (
        f"q=owner&owner={OWNER}&leadType=(leadType:SPONSORED)&limitedToTestLeads=false"
        f"&submittedAtTimeRange=(start:{RANGE_START_MS},end:{RANGE_END_MS})"
        f"&count={count}&start={start}&fields={LEAD_FIELDS}"
    )


@pytest.mark.anyio
async def test_list_lead_responses_request_matches_its_contract() -> None:
    """The responses request carries owner, lead type, test-lead flag, range, paging and fields.

    The projection is sent raw: its single URL decoding is LEAD_FIELDS exactly. A WORK_EMAIL
    answer is decoded with the response's own decorated version and stored only as its hash.
    """
    server = Server(_sequence(_json(_lead_page([_lead(LEAD_A, RAW_EMAIL)]))))
    async with _linkedin(server) as client:
        listing = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}
        )

    [request] = server.requests
    params = _assert_contract_request(request, "lead_form_responses")
    assert _query(request) == _responses_query()
    assert params["fields"] == LEAD_FIELDS
    assert unquote(params["fields"]) == LEAD_FIELDS
    assert "%" not in params["fields"]
    assert (RANGE_START - EPOCH) // timedelta(milliseconds=1) == RANGE_START_MS
    [lead] = listing.leads
    assert lead.external_id == LEAD_A
    assert lead.submitted_at == EPOCH + timedelta(milliseconds=SUBMITTED_MS)
    assert lead.account_urn == ACCOUNT_URN
    assert lead.campaign_urn == CAMPAIGN_LOW
    assert lead.creative_urn == CREATIVE_A
    assert lead.lead_form_urn == FORM_URN
    assert lead.lead_form_version == FORM_VERSION
    assert lead.email_status == "ok"
    assert lead.email_sha256 == _sha256(EMAIL)
    assert listing.versions == (
        FormVersionMap(
            canonical_urn=FORM_URN,
            version=FORM_VERSION,
            email_question_ids=(WORK_EMAIL_QID,),
            question_ids=frozenset({FIRST_NAME_QID, WORK_EMAIL_QID}),
        ),
    )
    assert listing.test_leads_skipped == 0
    assert listing.complete is True
    assert listing.reason is None
    assert listing.calls == 1


@pytest.mark.anyio
async def test_list_lead_responses_drops_test_leads(captured: list[EventDict]) -> None:
    """Responses flagged testLead are counted and never returned."""
    page = _lead_page([_lead(TEST_LEAD, TEST_LEAD_EMAIL, test_lead=True), _lead(LEAD_A, EMAIL)])
    server = Server(_sequence(_json(page)))
    async with _linkedin(server) as client:
        listing = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}
        )

    assert [lead.external_id for lead in listing.leads] == [LEAD_A]
    assert listing.test_leads_skipped == 1
    assert _sha256(TEST_LEAD_EMAIL) not in repr(listing)
    [outcome] = _events(captured, "linkedin.listing")
    assert outcome["test_leads_skipped"] == 1
    assert outcome["rows"] == 1


@pytest.mark.anyio
async def test_list_lead_responses_learns_versions_and_dedupes_across_pages(
    captured: list[EventDict],
) -> None:
    """A decoration teaches its version to later responses; a repeated response is kept once.

    A response without a decoration decodes with a map learned earlier in the listing or with a
    stored map; only maps learned from decorations are returned for persisting.
    """
    stored = FormVersionMap.from_storage(FORM_URN, STORED_VERSION, [str(WORK_EMAIL_QID)])
    server = Server(
        _sequence(
            _json(
                _lead_page(
                    [_lead(LEAD_A, EMAIL), _lead(LEAD_B, OTHER_EMAIL, decorated=False)], count=2
                )
            ),
            _json(
                _lead_page(
                    [
                        _lead(LEAD_C, THIRD_EMAIL, version=STORED_VERSION, decorated=False),
                        _lead(LEAD_A, EMAIL),
                    ],
                    count=2,
                    start=2,
                )
            ),
            _json(_lead_page([], count=2, start=4)),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.list_lead_responses(
            ACCOUNT_ID,
            (RANGE_START, RANGE_END),
            known_versions={(FORM_URN, STORED_VERSION): stored},
            page_size=2,
        )

    assert server.queries() == [
        _responses_query(count=2, start=0),
        _responses_query(count=2, start=2),
        _responses_query(count=2, start=4),
    ]
    assert [(lead.external_id, lead.lead_form_version) for lead in listing.leads] == [
        (LEAD_A, FORM_VERSION),
        (LEAD_B, FORM_VERSION),
        (LEAD_C, STORED_VERSION),
    ]
    assert [lead.email_sha256 for lead in listing.leads] == [
        _sha256(EMAIL),
        _sha256(OTHER_EMAIL),
        _sha256(THIRD_EMAIL),
    ]
    assert [(version.canonical_urn, version.version) for version in listing.versions] == [
        (FORM_URN, FORM_VERSION)
    ]
    assert listing.complete is True
    assert listing.calls == 3
    [outcome] = _events(captured, "linkedin.listing")
    assert outcome["repeats_dropped"] == 1
    assert outcome["versions_learned"] == 1
    assert outcome["log_level"] == "info"


@pytest.mark.anyio
async def test_list_lead_responses_never_exposes_a_plaintext_email(
    captured: list[EventDict],
) -> None:
    """Neither the listing, its leads nor any log event carries an email address."""
    server = Server(
        _sequence(_json(_lead_page([_lead(LEAD_A, RAW_EMAIL), _lead(LEAD_B, OTHER_EMAIL)])))
    )
    async with _linkedin(server) as client:
        listing = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}
        )

    rendered = [repr(listing), *(repr(lead) for lead in listing.leads), repr(captured)]
    for text in rendered:
        for email in (EMAIL, OTHER_EMAIL, RAW_EMAIL.strip()):
            assert email.lower() not in text.lower()
    assert all(lead.email_sha256 is not None for lead in listing.leads)


@pytest.mark.anyio
async def test_list_lead_responses_documented_sample_without_a_known_version() -> None:
    """The unprojected documented sample parses verbatim: its version has no map, so no email.

    The sample answers a reference request without ``count``, so its ``paging.count`` is
    LinkedIn's default of 10; it is served for a request of that page size.
    """
    server = Server(_sequence(_json(_sample("lead_form_responses"))))
    async with _linkedin(server) as client:
        listing = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}, page_size=10
        )

    assert server.queries() == [_responses_query(count=10, start=0)]
    [lead] = listing.leads
    assert lead.external_id == SAMPLE_LEAD_ID
    assert lead.lead_form_urn == "urn:li:leadGenForm:3162"
    assert lead.lead_form_version == 1
    assert lead.campaign_urn == "urn:li:sponsoredCampaign:367378525"
    assert lead.creative_urn == "urn:li:sponsoredCreative:121608865"
    assert lead.email_status == "no_email"
    assert lead.email_reason == REASON_UNKNOWN_FORM_VERSION
    assert lead.email_sha256 is None
    assert listing.versions == ()
    assert listing.complete is True


@pytest.mark.anyio
async def test_list_lead_responses_probe_stops_after_one_page() -> None:
    """The recovery probe's one-lead page ends with reason "page limit" when it came back full."""
    server = Server(_sequence(_json(_lead_page([_lead(LEAD_A, EMAIL)], count=1))))
    async with _linkedin(server) as client:
        listing = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}, page_size=1, max_pages=1
        )

    assert server.queries() == [_responses_query(count=1, start=0)]
    assert len(listing.leads) == 1
    assert listing.complete is False
    assert listing.reason == "page limit"
    assert listing.interrupted_by is None


@pytest.mark.anyio
async def test_list_lead_responses_transient_failure_keeps_the_parsed_leads() -> None:
    """A transient page failure ends the listing early with the leads already parsed."""
    server = Server(
        _sequence(
            _json(_lead_page([_lead(LEAD_A, EMAIL)], count=1)),
            _unavailable(),
            _unavailable(),
        )
    )
    async with _linkedin(server) as client:
        listing = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}, page_size=1
        )

    assert [lead.external_id for lead in listing.leads] == [LEAD_A]
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.complete is False
    assert listing.reason == listing.interrupted_by.reason
    assert listing.calls == 2


@pytest.mark.anyio
async def test_list_lead_responses_refreshes_the_token_once_after_a_401() -> None:
    """A 401 from a refreshable token source invalidates the token and repeats the call once."""
    tokens = RefreshingTokens()
    server = Server(
        _sequence(
            _error(401, "The member doesn't have lead access permission."),
            _json(_lead_page([_lead(LEAD_A, EMAIL)])),
        )
    )
    async with _linkedin(server, tokens) as client:
        listing = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}
        )

    assert [request.headers["Authorization"] for request in server.requests] == [
        "Bearer test-linkedin-token-1",
        "Bearer test-linkedin-token-2",
    ]
    assert tokens.invalidations == 1
    assert [lead.external_id for lead in listing.leads] == [LEAD_A]
    assert listing.calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("refreshable", "requests"),
    [pytest.param(False, 1, id="static-token"), pytest.param(True, 2, id="after-one-refresh")],
)
async def test_list_lead_responses_unrefreshable_401_is_access_denied(
    refreshable: bool, requests: int
) -> None:
    """A 401 with a static token, or a second 401 after a refresh, propagates as access denied."""
    tokens: TokenSource = RefreshingTokens() if refreshable else StaticTokenProvider(TOKEN)
    server = Server(lambda request: _error(401, "The member doesn't have lead access permission."))
    async with _linkedin(server, tokens) as client:
        with pytest.raises(AccessDenied) as caught:
            await client.list_lead_responses(
                ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}
            )

    assert caught.value.is_unauthorized is True
    assert len(server.requests) == requests


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("submitted_range", "page_size", "message"),
    [
        pytest.param(
            (datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None), RANGE_END),
            LEAD_PAGE_SIZE,
            "the range start must be timezone-aware",
            id="naive-start",
        ),
        pytest.param(
            (RANGE_START, RANGE_END.replace(tzinfo=None)),
            LEAD_PAGE_SIZE,
            "the range end must be timezone-aware",
            id="naive-end",
        ),
        pytest.param(
            (RANGE_END, RANGE_START),
            LEAD_PAGE_SIZE,
            "a time range must not start after it ends",
            id="reversed",
        ),
        pytest.param(
            (RANGE_START, RANGE_END), 0, "page_size must be between 1 and 100", id="page-size"
        ),
    ],
)
async def test_list_lead_responses_rejects_invalid_arguments_without_a_call(
    submitted_range: tuple[datetime, datetime], page_size: int, message: str
) -> None:
    """A naive instant, a reversed range or an invalid page size is a ValueError before any call."""
    server = Server(_sequence())
    async with _linkedin(server) as client:
        with pytest.raises(ValueError, match=message):
            await client.list_lead_responses(
                ACCOUNT_ID, submitted_range, known_versions={}, page_size=page_size
            )

    assert server.requests == []


@pytest.mark.anyio
async def test_list_lead_responses_two_arguments_decode_with_versions_from_list_lead_forms() -> (
    None
):
    """``list_lead_responses(acct, range)`` decodes undecorated responses with the forms read.

    The client's ``list_lead_forms`` of the account supplies (form, 3): a response of version 3
    decodes, version 2 of the same form stays "unknown form version", another account has no
    maps, and an explicit ``known_versions={}`` is used as passed.
    """
    forms = _page(
        "lead_forms", [_form(FORM_ID, FORM_VERSION, WORK_EMAIL_QUESTIONS)], count=LEAD_PAGE_SIZE
    )
    responses = _lead_page(
        [
            _lead(LEAD_A, EMAIL, decorated=False),
            _lead(LEAD_B, OTHER_EMAIL, version=STORED_VERSION, decorated=False),
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        """Serve the forms page or the responses page by path."""
        if request.url.path == "/rest/leadForms":
            return _json(forms)
        return _json(responses)

    server = Server(handle)
    async with _linkedin(server) as client:
        await client.list_lead_forms(ACCOUNT_ID)
        listing = await client.list_lead_responses(ACCOUNT_ID, (RANGE_START, RANGE_END))
        explicit = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}
        )
        other = await client.list_lead_responses("990000002", (RANGE_START, RANGE_END))

    _assert_contract_request(server.requests[1], "lead_form_responses")
    assert server.queries()[1] == _responses_query()
    assert [
        (lead.external_id, lead.email_status, lead.email_reason, lead.email_sha256)
        for lead in listing.leads
    ] == [
        (LEAD_A, "ok", None, _sha256(EMAIL)),
        (LEAD_B, "no_email", REASON_UNKNOWN_FORM_VERSION, None),
    ]
    assert listing.versions == ()
    assert listing.complete is True
    for unbound in (explicit, other):
        assert [(lead.email_status, lead.email_reason) for lead in unbound.leads] == [
            ("no_email", REASON_UNKNOWN_FORM_VERSION),
            ("no_email", REASON_UNKNOWN_FORM_VERSION),
        ]


@pytest.mark.anyio
async def test_list_lead_responses_two_arguments_reuse_decorations_learned_earlier() -> None:
    """A decoration learned by one call decodes a later undecorated response of its version.

    Maps learned under an explicit ``known_versions`` are remembered too, while an explicit
    ``known_versions={}`` still starts from no map at all.
    """
    server = Server(
        _sequence(
            _json(_lead_page([_lead(LEAD_A, EMAIL)])),
            _json(_lead_page([_lead(LEAD_B, OTHER_EMAIL, decorated=False)])),
            _json(_lead_page([_lead(LEAD_C, THIRD_EMAIL, decorated=False)])),
        )
    )
    async with _linkedin(server) as client:
        first = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}
        )
        second = await client.list_lead_responses(ACCOUNT_ID, (RANGE_START, RANGE_END))
        third = await client.list_lead_responses(
            ACCOUNT_ID, (RANGE_START, RANGE_END), known_versions={}
        )

    assert [(version.canonical_urn, version.version) for version in first.versions] == [
        (FORM_URN, FORM_VERSION)
    ]
    [lead_b] = second.leads
    assert (lead_b.email_status, lead_b.email_sha256) == ("ok", _sha256(OTHER_EMAIL))
    assert second.versions == ()
    [lead_c] = third.leads
    assert (lead_c.email_status, lead_c.email_reason) == ("no_email", REASON_UNKNOWN_FORM_VERSION)
    assert lead_c.email_sha256 is None


def test_analytics_daily_and_list_lead_responses_take_the_two_aap_arguments() -> None:
    """Both methods bind ``(acct, range)`` alone; their context arguments default to ``None``."""
    daily = inspect.signature(LinkedInClient.analytics_daily)
    daily.bind(object(), ACCOUNT_ID, (date(2026, 9, 1), date(2026, 9, 1)))
    assert daily.parameters["expected_creatives"].default is None
    assert daily.parameters["expected_creatives"].kind is inspect.Parameter.KEYWORD_ONLY
    leads = inspect.signature(LinkedInClient.list_lead_responses)
    leads.bind(object(), ACCOUNT_ID, (RANGE_START, RANGE_END))
    assert leads.parameters["known_versions"].default is None
    assert leads.parameters["known_versions"].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------------------------
# get_lead_responses
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_lead_responses_request_matches_the_batch_contract() -> None:
    """The re-fetch sends unquoted ids, owner, lead type and the raw projection, no method header.

    The documented sample is served verbatim to a re-fetch for its own owner account, so its
    element passes the identity check; an id absent from its results is a failed re-fetch.
    """
    sample = _sample("lead_form_responses_batch")["results"][SAMPLE_LEAD_ID]
    assert sample["owner"] == {"sponsoredAccount": SAMPLE_ACCOUNT_URN}
    server = Server(_sequence(_json(_sample("lead_form_responses_batch"))))
    async with _linkedin(server) as client:
        batch = await client.get_lead_responses(SAMPLE_ACCOUNT_URN, [SAMPLE_LEAD_ID, LEAD_A])

    [request] = server.requests
    params = _assert_contract_request(request, "lead_form_responses_batch")
    assert _query(request) == (
        f"ids=List({SAMPLE_LEAD_ID},{LEAD_A})&owner={SAMPLE_OWNER}"
        f"&leadType=(leadType:SPONSORED)&fields={LEAD_FIELDS}"
    )
    assert '"' not in unquote(params["ids"])
    assert "%22" not in params["ids"]
    assert "X-RestLi-Method" not in request.headers
    assert dict(batch.found) == {SAMPLE_LEAD_ID: sample}
    assert batch.failed == frozenset({LEAD_A})
    assert batch.interrupted_by is None
    assert batch.calls == 1


@pytest.mark.anyio
async def test_get_lead_responses_chunks_ids_at_the_batch_size() -> None:
    """Distinct ids are sent in first-seen order, at most LEAD_BATCH_SIZE per call."""
    ids = [f"ffffeeee-0000-dddd-1111-cccc2222bbbb-{index}" for index in range(250)]
    server = Server(_echo_batch)
    async with _linkedin(server) as client:
        batch = await client.get_lead_responses(ACCOUNT_ID, [*ids, *ids[:10]])

    sent = [_requested_ids(request) for request in server.requests]
    assert [len(chunk) for chunk in sent] == [100, 100, 50]
    assert [lead_id for chunk in sent for lead_id in chunk] == ids
    max_ids = _contract("lead_form_responses_batch")["request"]["limits"]["max_ids"]
    assert all(len(chunk) <= max_ids for chunk in sent)
    for request in server.requests:
        _assert_contract_request(request, "lead_form_responses_batch")
    assert sorted(batch.found) == sorted(ids)
    assert batch.failed == frozenset()
    assert batch.calls == 3


@pytest.mark.anyio
async def test_get_lead_responses_maps_results_statuses_and_errors() -> None:
    """Keys are percent-decoded and unquoted; an error, a non-2xx status or absence fails an id.

    Each result carries its own plain id, as LinkedIn returns it, so only the envelope decides.
    """
    requested = [
        "lead-ok",
        "lead-quoted",
        "lead/slash",
        "lead-top-error",
        "lead-nested-error",
        "lead-status-404",
        "lead-status-200",
        "lead-absent",
    ]
    body = {
        "results": {
            "lead-ok": _lead("lead-ok", EMAIL),
            '"lead-quoted"': _lead("lead-quoted", EMAIL),
            "lead%2Fslash": _lead("lead/slash", EMAIL),
            "lead-top-error": _lead("lead-top-error", EMAIL),
            "lead-nested-error": _lead("lead-nested-error", EMAIL),
            "lead-status-404": _lead("lead-status-404", EMAIL),
            "lead-status-200": _lead("lead-status-200", EMAIL),
            "statuses": {"lead-status-404": 404, "lead-status-200": 200},
            "errors": {"lead-nested-error": {"status": 404, "message": "Not found"}},
        },
        "statuses": {},
        "errors": {"lead-top-error": {"status": 403, "message": "Forbidden"}},
    }
    _conforming("lead_form_responses_batch", body)
    server = Server(_sequence(_json(body)))
    async with _linkedin(server) as client:
        batch = await client.get_lead_responses(ACCOUNT_ID, requested)

    assert _params(server.requests[0])["ids"] == (
        "List(lead-ok,lead-quoted,lead%2Fslash,lead-top-error,lead-nested-error,"
        "lead-status-404,lead-status-200,lead-absent)"
    )
    assert set(batch.found) == {"lead-ok", "lead-quoted", "lead/slash", "lead-status-200"}
    assert all(found == _lead(lead_id, EMAIL) for lead_id, found in batch.found.items())
    assert batch.failed == frozenset(
        {"lead-top-error", "lead-nested-error", "lead-status-404", "lead-absent"}
    )


# Marks an identity field the parametrised element omits.
ABSENT = object()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("key", "value", "conforming"),
    [
        pytest.param("id", LEAD_B, True, id="id-of-another-response"),
        pytest.param("id", f" {LEAD_A} ", True, id="id-padded"),
        pytest.param("id", LEAD_A.upper(), True, id="id-other-case"),
        pytest.param("id", ABSENT, False, id="id-missing"),
        pytest.param("id", 990000001, False, id="id-not-a-string"),
        pytest.param("owner", {"sponsoredAccount": OTHER_ACCOUNT_URN}, True, id="owner-other"),
        pytest.param("owner", {"sponsoredAccount": ACCOUNT_ID}, True, id="owner-not-a-urn"),
        pytest.param("owner", {"organization": OTHER_ORGANIZATION_URN}, True, id="owner-organic"),
        pytest.param("owner", {}, True, id="owner-empty"),
        pytest.param("owner", ABSENT, False, id="owner-missing"),
        pytest.param("owner", ACCOUNT_URN, False, id="owner-not-an-object"),
        pytest.param("testLead", True, True, id="test-lead"),
        pytest.param("testLead", ABSENT, False, id="test-lead-missing"),
        pytest.param("testLead", "false", False, id="test-lead-string"),
        pytest.param("testLead", 0, False, id="test-lead-zero"),
        pytest.param("leadType", "COMPANY", True, id="lead-type-company"),
        pytest.param("leadType", "sponsored", True, id="lead-type-lowercase"),
        pytest.param("leadType", ABSENT, False, id="lead-type-missing"),
    ],
)
async def test_get_lead_responses_fails_an_element_not_bound_to_its_request(
    key: str, value: object, conforming: bool, captured: list[EventDict]
) -> None:
    """An element is found only by its own id, sponsored owner, lead type and testLead false.

    The element under the requested key stays decodable, so only the identity check keeps its
    email from being matched to the requested lead: the id is failed, as an unretrievable
    response, and the outcome event counts the rejection without any id or email. Elements
    missing a required field or holding a mistyped one break the contract on purpose.
    """
    element = _lead(LEAD_A, EMAIL)
    if value is ABSENT:
        del element[key]
    else:
        element[key] = value
    assert decode_email(element, {}).email == EMAIL
    body = {"results": {LEAD_A: element}, "statuses": {}, "errors": {}}
    schema = _contract("lead_form_responses_batch")["response"]["body"]
    errors = _schema_errors(body, schema, "lead_form_responses_batch")
    assert (errors == []) is conforming, errors
    server = Server(_sequence(_json(body)))
    async with _linkedin(server) as client:
        batch = await client.get_lead_responses(ACCOUNT_ID, [LEAD_A])

    assert dict(batch.found) == {}
    assert batch.failed == frozenset({LEAD_A})
    assert batch.interrupted_by is None
    assert batch.calls == 1
    assert "@" not in repr(batch)
    [outcome] = _events(captured, "linkedin.listing")
    assert (outcome["rows"], outcome["failed"], outcome["identity_rejected"]) == (0, 1, 1)
    assert outcome["complete"] is True
    assert "@" not in repr(outcome)
    assert LEAD_A not in repr(outcome)


@pytest.mark.anyio
async def test_get_lead_responses_never_binds_another_response_to_the_requested_id(
    captured: list[EventDict],
) -> None:
    """A result keyed A holding B's response fails A; the correct results are still found.

    Decoding B's email for lead A would match A to B's CRM contact, so A ends as a failed
    re-fetch however well-formed B's response is. In the same batch B and C are found and
    decode to their own emails, a test lead is failed, and an absent id is failed without
    counting as an identity rejection.
    """
    body = {
        "results": {
            LEAD_A: _lead(LEAD_B, OTHER_EMAIL),
            LEAD_B: _lead(LEAD_B, OTHER_EMAIL),
            LEAD_C: _lead(LEAD_C, THIRD_EMAIL),
            TEST_LEAD: _lead(TEST_LEAD, TEST_LEAD_EMAIL, test_lead=True),
        },
        "statuses": {},
        "errors": {},
    }
    _conforming("lead_form_responses_batch", body)
    server = Server(_sequence(_json(body)))
    async with _linkedin(server) as client:
        batch = await client.get_lead_responses(
            ACCOUNT_ID, [LEAD_A, LEAD_B, LEAD_C, TEST_LEAD, "lead-absent"]
        )

    assert set(batch.found) == {LEAD_B, LEAD_C}
    assert batch.failed == frozenset({LEAD_A, TEST_LEAD, "lead-absent"})
    decoded_b = decode_email(batch.found[LEAD_B], {})
    decoded_c = decode_email(batch.found[LEAD_C], {})
    assert (decoded_b.status, decoded_b.email) == ("ok", OTHER_EMAIL)
    assert (decoded_c.status, decoded_c.email, decoded_c.sha256) == (
        "ok",
        THIRD_EMAIL,
        _sha256(THIRD_EMAIL),
    )
    assert "@" not in repr(batch)
    [outcome] = _events(captured, "linkedin.listing")
    assert (outcome["rows"], outcome["failed"], outcome["identity_rejected"]) == (2, 3, 2)
    assert "@" not in repr(outcome)
    assert all(lead_id not in repr(outcome) for lead_id in (LEAD_A, LEAD_B, LEAD_C, TEST_LEAD))


@pytest.mark.anyio
async def test_get_lead_responses_transient_failure_fails_every_unread_id(
    captured: list[EventDict],
) -> None:
    """A transient failure stops the batch: the current and every later chunk's ids are failed."""
    ids = [f"ffffeeee-0000-dddd-1111-cccc2222bbbb-{index}" for index in range(250)]
    replies = iter([None, _unavailable(), _unavailable()])

    def handle(request: httpx.Request) -> httpx.Response:
        """Echo the first chunk, then fail transiently."""
        reply = next(replies)
        return _echo_batch(request) if reply is None else reply

    server = Server(handle)
    async with _linkedin(server) as client:
        batch = await client.get_lead_responses(ACCOUNT_ID, ids)

    assert len(server.requests) == 3
    assert sorted(batch.found) == sorted(ids[:100])
    assert batch.failed == frozenset(ids[100:])
    assert isinstance(batch.interrupted_by, TransientError)
    assert batch.calls == 2
    [outcome] = _events(captured, "linkedin.listing")
    assert outcome["complete"] is False
    assert outcome["failed"] == 150
    assert outcome["identity_rejected"] == 0
    assert outcome["reason"] == batch.interrupted_by.reason


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("lead_id", "message"),
    [
        pytest.param("", "non-blank string", id="empty"),
        pytest.param("   ", "non-blank string", id="blank"),
        pytest.param(42, "non-blank string", id="not-a-string"),
        pytest.param("urn:li:leadFormResponse:1", "plain ids, not URNs", id="urn"),
        pytest.param('lead"1', "double quote", id="quoted"),
    ],
)
async def test_get_lead_responses_rejects_invalid_ids_without_a_call(
    lead_id: Any, message: str
) -> None:
    """A blank or non-string id, a URN or an id holding a quote is a ValueError before any call."""
    server = Server(_sequence())
    async with _linkedin(server) as client:
        with pytest.raises(ValueError, match=message):
            await client.get_lead_responses(ACCOUNT_ID, [LEAD_A, lead_id])

    assert server.requests == []


@pytest.mark.anyio
async def test_get_lead_responses_without_ids_makes_no_call() -> None:
    """An empty id list returns an empty batch without calling LinkedIn."""
    server = Server(_sequence())
    async with _linkedin(server) as client:
        batch = await client.get_lead_responses(ACCOUNT_ID, [])

    assert server.requests == []
    assert dict(batch.found) == {}
    assert batch.failed == frozenset()
    assert batch.interrupted_by is None
    assert batch.calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param([], "not a JSON object", id="body-not-an-object"),
        pytest.param({"statuses": {}, "errors": {}}, "no 'results' object", id="no-results"),
        pytest.param({"results": {LEAD_A: "x"}}, "batch result is not", id="result-not-object"),
        pytest.param({"results": {}, "errors": []}, "'errors' is not", id="errors-not-object"),
        pytest.param({"results": {}, "statuses": []}, "'statuses' is not", id="statuses-list"),
    ],
)
async def test_get_lead_responses_rejects_a_malformed_batch_envelope(
    body: object, message: str
) -> None:
    """A batch response that breaks the {results, statuses, errors} envelope is a defect."""
    server = Server(_sequence(_json(body)))
    async with _linkedin(server) as client:
        with pytest.raises(QueryDefect, match=message) as caught:
            await client.get_lead_responses(ACCOUNT_ID, [LEAD_A])

    assert caught.value.endpoint == "lead_form_responses_batch"
    assert caught.value.code == "validation"


# ---------------------------------------------------------------------------------------------
# LeadListing and LeadBatch
# ---------------------------------------------------------------------------------------------


def test_lead_batch_hides_found_elements_from_repr() -> None:
    """LeadBatch is frozen, defaults to no interruption and zero calls, and never shows found."""
    batch = LeadBatch(
        found={LEAD_A: _lead(LEAD_A, EMAIL)},
        failed=frozenset({LEAD_B}),
    )
    assert batch.interrupted_by is None
    assert batch.calls == 0
    assert EMAIL not in repr(batch)
    assert "found" not in repr(batch)
    assert LEAD_B in repr(batch)
    with pytest.raises(FrozenInstanceError):
        batch.calls = 1  # type: ignore[misc]


def test_lead_listing_is_frozen_and_defaults_to_no_interruption() -> None:
    """LeadListing is an immutable result whose interruption defaults to None."""
    listing = LeadListing(
        leads=(),
        versions=(),
        test_leads_skipped=2,
        complete=True,
        reason=None,
        calls=1,
    )
    assert listing.interrupted_by is None
    assert listing.test_leads_skipped == 2
    with pytest.raises(FrozenInstanceError):
        listing.complete = False  # type: ignore[misc]
