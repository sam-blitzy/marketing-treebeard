"""Request, paging and entity tests for ``ads_engine.connectors.linkedin.client``.

Covers the Rest.li 2.0 helpers (:func:`encode_urn`, :func:`restli_list`,
:func:`restli_date`, :func:`restli_date_range`, :func:`restli_query`, the epoch-millisecond
time range, chunking, de-duplication and argument checks), the response helpers (``next``
links, page tokens, batch-get envelopes), the result types, the request core of
:class:`LinkedInClient` (headers, the one token refresh after a 401, transient retries,
defects), its three paging loops (``next`` links, ``pageToken`` cursors and
``count``/``start`` offsets) and the entity calls: ``get_ad_account``,
``list_campaign_groups``, ``list_campaigns``, ``list_creatives`` and ``get_posts``.

Every success body is rendered from ``backend/tests/contracts/linkedin/*.json``: it starts
from the contract's documented sample or uses only fields the contract declares, and is
checked against the contract's response schema before it is served, so a body carrying an
undeclared field fails the test. The exceptions are the deliberately malformed bodies of the
defect tests and LinkedIn's error envelope ``{status, serviceErrorCode, code, message}``.
The requests of each entity call are checked against the contract's request shape (method,
path, required headers, required and optional query keys) and, where the contract URL is
literal, compared with it byte for byte. The assertions follow the module's own documented
live 202609 behaviour where it differs from the plan text.

Every call goes through ``build_client("linkedin", httpx.MockTransport(...))`` and the real
``send_json``; retries record their waits instead of sleeping. No test touches the network,
the database or a real token.
"""

import copy
import functools
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from structlog.typing import EventDict

from ads_engine.connectors.errors import AccessDenied, QueryDefect, TransientError
from ads_engine.connectors.http import JsonResponse, RetryPolicy, build_client
from ads_engine.connectors.linkedin import client as client_module
from ads_engine.connectors.linkedin.auth import (
    _RETIRED_TOKENS_MAX,
    SIMULATED_TOKEN,
    HoldsSecrets,
    LinkedInTokenProvider,
    StaticTokenProvider,
)
from ads_engine.connectors.linkedin.client import (
    _USED_TOKENS_MAX,
    CREATIVE_CAMPAIGN_CHUNK,
    DAILY_FIELDS,
    ENTITY_PAGE_SIZE,
    LEAD_FIELDS,
    LEAD_PAGE_SIZE,
    LINKEDIN_VERSION,
    MAX_ANALYTICS_ELEMENTS,
    POST_BATCH_SIZE,
    RESTLI_PROTOCOL_VERSION,
    SOURCE,
    LinkedInClient,
    Listing,
    PostBatch,
    _batch_key,
    _batch_maps,
    _body_object,
    _check_date_range,
    _check_page_size,
    _checked_lead_id,
    _checked_offset_paging,
    _chunks,
    _cursor_token,
    _distinct,
    _distinct_rows,
    _elements,
    _epoch_ms,
    _is_post_urn,
    _log_outcome,
    _next_href,
    _owner,
    _relative_href,
    _restli_time_range,
    _Tally,
    encode_urn,
    restli_date,
    restli_date_range,
    restli_list,
    restli_query,
)
from ads_engine.connectors.linkedin.parser import (
    CAMPAIGN_GROUP_SEARCH_STATUSES,
    CAMPAIGN_SEARCH_STATUSES,
    CREATIVE_INTENDED_STATUSES,
    Money,
    PostRow,
)
from ads_engine.settings import Settings

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "linkedin"

ACCOUNT_ID = "506223315"
ACCOUNT_URN = f"urn:li:sponsoredAccount:{ACCOUNT_ID}"
TOKEN = "test-linkedin-access-token"
OLD_TOKEN = "test-linkedin-token-before-refresh"
NEW_TOKEN = "test-linkedin-token-after-refresh"
BASE_URL = "https://api.linkedin.com/rest/"

ANALYTICS_URL = (
    "adAnalytics?q=analytics&pivot=CREATIVE&timeGranularity=DAILY"
    "&dateRange=(start:(year:2026,month:9,day:1),end:(year:2026,month:9,day:1))"
    f"&accounts=List(urn%3Ali%3AsponsoredAccount%3A506223315)&fields={DAILY_FIELDS}"
)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture
def captured() -> Iterator[list[EventDict]]:
    """Capture every structlog event as its raw event dict."""
    with capture_logs() as entries:
        yield entries


# ---------------------------------------------------------------------------------------------
# Contract files: loading, the response-schema check and the request-shape check
# ---------------------------------------------------------------------------------------------

# The JSON Schema keywords the LinkedIn contracts use; any other keyword fails the check.
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


def _conforming(surface: str, body: dict[str, Any]) -> dict[str, Any]:
    """Return ``body`` after asserting it satisfies its contract's response schema."""
    errors = _schema_errors(body, _contract(surface)["response"]["body"], surface)
    assert errors == [], errors
    return body


def _query_pairs(request: httpx.Request) -> list[tuple[str, str]]:
    """Split the raw query of ``request`` into ``(key, value)`` pairs, bytes untouched."""
    query = request.url.query.decode("ascii")
    if not query:
        return []
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        key, _, value = part.partition("=")
        pairs.append((key, value))
    return pairs


def _relative(request: httpx.Request) -> str:
    """Return the request URL relative to ``https://api.linkedin.com/rest/``, query raw."""
    url = str(request.url)
    assert url.startswith(BASE_URL), url
    return url[len(BASE_URL) :]


def _contract_url(surface: str, **values: str) -> str:
    """Render the contract's request URL with its ``{placeholders}`` filled in."""
    url: str = _contract(surface)["request"]["url"]
    for name, value in values.items():
        url = url.replace("{" + name + "}", value)
    assert "{" not in url, url
    return url


def _assert_contract_request(surface: str, request: httpx.Request) -> None:
    """Assert ``request`` has its contract's method, path, required headers and query keys."""
    contract = _contract(surface)["request"]
    assert request.method == contract["method"]
    for header in contract["required_headers"]:
        assert header in request.headers, f"{surface}: missing header {header}"
    template = contract["url"].partition("?")[0]
    pattern = re.escape(template).replace(re.escape("{id}"), "[0-9]+")
    assert re.fullmatch("/rest/" + pattern, request.url.path), request.url.path
    keys = [key for key, _ in _query_pairs(request)]
    assert len(keys) == len(set(keys)), keys
    allowed = set(contract["required_query"]) | set(contract["optional_query"])
    assert set(contract["required_query"]) <= set(keys), (surface, keys)
    assert set(keys) <= allowed, (surface, keys)


# ---------------------------------------------------------------------------------------------
# The scripted LinkedIn server and the client under test
# ---------------------------------------------------------------------------------------------

type Outcome = httpx.Response | Exception


class Server:
    """Answers each request with the next scripted outcome and records every request.

    A scripted response is returned; a scripted exception is raised from the transport. A
    request beyond the script fails the test. ``waits`` records the seconds the retry policy
    would sleep.
    """

    def __init__(self, *outcomes: Outcome) -> None:
        self._outcomes: list[Outcome] = list(outcomes)
        self.requests: list[httpx.Request] = []
        self.waits: list[float] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request from the script."""
        self.requests.append(request)
        if not self._outcomes:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sleep(self, seconds: float) -> None:
        """Record a retry wait instead of sleeping."""
        self.waits.append(seconds)


class RefreshingTokens:
    """A refreshable token source handing out the given tokens, one per refresh."""

    def __init__(self, *tokens: str, failure: Exception | None = None) -> None:
        self._pending = list(tokens)
        self._current: str | None = None
        self._failure = failure
        self.invalidations = 0
        self.exchanges = 0

    @property
    def can_refresh(self) -> bool:
        """A refresh-token source can always fetch a new token."""
        return True

    async def get(self) -> str:
        """Return the cached token, exchanging the next one when none is cached."""
        if self._failure is not None:
            raise self._failure
        if self._current is None:
            self._current = self._pending.pop(0)
            self.exchanges += 1
        return self._current

    def invalidate(self) -> None:
        """Drop the cached token."""
        self.invalidations += 1
        self._current = None


@asynccontextmanager
async def _linkedin(server: Server, tokens: Any = None) -> AsyncIterator[LinkedInClient]:
    """Yield a client whose HTTP client sends through ``server``, with no-sleep retries."""
    transport = httpx.MockTransport(server.handle)
    async with build_client("linkedin", transport, settings=Settings()) as http:
        yield LinkedInClient(
            http,
            tokens if tokens is not None else StaticTokenProvider(TOKEN),
            retry=RetryPolicy(sleep=server.sleep),
        )


def _ok(body: object) -> httpx.Response:
    """Return an HTTP 200 JSON response."""
    return httpx.Response(200, json=body)


def _error(status: int, code: str, message: str, **headers: str) -> httpx.Response:
    """Return LinkedIn's documented error body ``{status, serviceErrorCode, code, message}``."""
    body = {"status": status, "serviceErrorCode": 100, "code": code, "message": message}
    return httpx.Response(status, json=body, headers=headers)


def _unavailable() -> httpx.Response:
    """Return a transient HTTP 503."""
    return httpx.Response(503, json={"message": "Service Unavailable", "status": 503})


def _outage() -> tuple[httpx.Response, ...]:
    """Return one 503 per attempt of the default policy, so the call fails transiently."""
    return tuple(_unavailable() for _ in range(RetryPolicy().attempts))


# ---------------------------------------------------------------------------------------------
# Contract-rendered bodies
# ---------------------------------------------------------------------------------------------


def _account(*, account_id: str = ACCOUNT_ID, currency: str = "USD") -> dict[str, Any]:
    """Return the documented ad account, renumbered to ``account_id``."""
    body: dict[str, Any] = _sample("ad_account")
    body["id"] = int(account_id)
    body["currency"] = currency
    return _conforming("ad_account", body)


def _group(group_id: int, **fields: Any) -> dict[str, Any]:
    """Return the documented campaign group renumbered to ``group_id`` with ``fields`` set."""
    element: dict[str, Any] = _sample("ad_campaign_groups")["elements"][0]
    element.update(id=group_id, account=ACCOUNT_URN, **fields)
    return element


def _campaign(campaign_id: int, **fields: Any) -> dict[str, Any]:
    """Return the documented campaign renumbered to ``campaign_id`` with ``fields`` set."""
    element: dict[str, Any] = _sample("ad_campaigns")["elements"][0]
    element.update(id=campaign_id, account=ACCOUNT_URN, **fields)
    return element


def _creative(creative_id: int, campaign_id: int) -> dict[str, Any]:
    """Return the documented creative renumbered, in campaign ``campaign_id``."""
    element: dict[str, Any] = _sample("creatives")["elements"][0]
    element.update(
        id=f"urn:li:sponsoredCreative:{creative_id}",
        campaign=f"urn:li:sponsoredCampaign:{campaign_id}",
        account=ACCOUNT_URN,
    )
    return element


def _cursor_page(
    surface: str,
    elements: list[dict[str, Any]],
    token: str | None = None,
    *,
    metadata: bool = True,
    paging: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one cursor-search page; the last page carries an empty ``metadata`` object."""
    body: dict[str, Any] = {"elements": elements}
    if metadata:
        body["metadata"] = {} if token is None else {"nextPageToken": token}
    if paging is not None:
        body["paging"] = paging
    return _conforming(surface, body)


def _post(urn: str, *, title: str | None = None, commentary: str = "Intro text") -> dict[str, Any]:
    """Return the documented post renumbered to ``urn``, with a media title when given."""
    element: dict[str, Any] = next(iter(_sample("posts")["results"].values()))
    element["id"] = urn
    element["commentary"] = commentary
    element["content"] = {} if title is None else {"media": {"title": title}}
    return element


def _posts_body(
    results: Mapping[str, dict[str, Any]],
    *,
    statuses: Mapping[str, object] | None = None,
    errors: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Return a posts batch-get envelope ``{results, statuses, errors}``."""
    body = {
        "results": dict(results),
        "statuses": dict(statuses or {}),
        "errors": dict(errors or {}),
    }
    return _conforming("posts", body)


def _daily_element(index: int) -> dict[str, Any]:
    """Return one daily analytics element for creative ``index`` (contract ``ad_analytics``)."""
    day = {"year": 2026, "month": 9, "day": 1}
    return {
        "pivotValues": [f"urn:li:sponsoredCreative:{index}"],
        "dateRange": {"start": day, "end": day},
        "impressions": index,
    }


def _analytics_page(elements: list[dict[str, Any]], next_href: str | None = None) -> dict[str, Any]:
    """Return one ``adAnalytics`` response, with a ``rel=next`` link when given."""
    links = [] if next_href is None else [{"rel": "next", "href": next_href, "type": "GET"}]
    body = {"elements": elements, "paging": {"count": 10, "start": 0, "links": links}}
    return _conforming("ad_analytics", body)


def _lead_form(form_id: int) -> dict[str, Any]:
    """Return the documented lead form renumbered to ``form_id``."""
    element: dict[str, Any] = _sample("lead_forms")["elements"][0]
    element["id"] = form_id
    return element


def _offset_page(
    surface: str, elements: list[dict[str, Any]], *, start: int, count: int, has_next: bool
) -> dict[str, Any]:
    """Return one Lead Sync page, with a ``rel=next`` link when ``has_next``."""
    links = []
    if has_next:
        links = [{"rel": "next", "href": f"/rest/leadForms?start={start + count}", "type": "GET"}]
    paging = {"start": start, "count": count, "total": len(elements), "links": links}
    return _conforming(surface, {"elements": elements, "paging": paging})


def _listing_events(entries: list[EventDict], surface: str) -> list[EventDict]:
    """Return the ``linkedin.listing`` events of ``surface``."""
    return [e for e in entries if e["event"] == "linkedin.listing" and e["surface"] == surface]


# ---------------------------------------------------------------------------------------------
# Rest.li 2.0 helpers
# ---------------------------------------------------------------------------------------------


def test_encode_urn_percent_encodes_every_reserved_character() -> None:
    """A URN is percent-encoded whole, so its colons, commas and parentheses are escaped."""
    assert encode_urn("urn:li:sponsoredAccount:123") == "urn%3Ali%3AsponsoredAccount%3A123"
    assert encode_urn("urn:li:versionedLeadGenForm:(urn:li:leadGenForm:1,2)") == (
        "urn%3Ali%3AversionedLeadGenForm%3A%28urn%3Ali%3AleadGenForm%3A1%2C2%29"
    )


@pytest.mark.parametrize(
    ("urn", "message"),
    [
        ("", "must not be blank"),
        ("   ", "must not be blank"),
        ("urn%3Ali%3Ashare%3A1", "already holds a percent escape"),
    ],
)
def test_encode_urn_rejects_blank_and_pre_encoded_urns(urn: str, message: str) -> None:
    """A blank URN, or one already encoded (which would become ``%253A``), is rejected."""
    with pytest.raises(ValueError, match=message):
        encode_urn(urn)


def test_restli_list_joins_encoded_items_as_given() -> None:
    """``List(...)`` joins its items with raw commas, from any iterable, empty included."""
    assert restli_list(["urn%3Ali%3Ax%3A1", "urn%3Ali%3Ax%3A2"]) == (
        "List(urn%3Ali%3Ax%3A1,urn%3Ali%3Ax%3A2)"
    )
    assert restli_list(item for item in ("A", "B")) == "List(A,B)"
    assert restli_list([]) == "List()"


def test_restli_date_has_no_zero_padding() -> None:
    """A Rest.li date is ``(year:Y,month:M,day:D)`` with unpadded numbers."""
    assert restli_date(date(2026, 9, 4)) == "(year:2026,month:9,day:4)"
    assert restli_date(date(2026, 12, 31)) == "(year:2026,month:12,day:31)"


def test_restli_date_range_is_inclusive_and_ordered() -> None:
    """A date range names both inclusive ends; a one-day range is valid, a reversed one not."""
    assert restli_date_range(date(2026, 9, 1), date(2026, 9, 28)) == (
        "(start:(year:2026,month:9,day:1),end:(year:2026,month:9,day:28))"
    )
    assert restli_date_range(date(2026, 9, 1), date(2026, 9, 1)) == (
        "(start:(year:2026,month:9,day:1),end:(year:2026,month:9,day:1))"
    )
    with pytest.raises(ValueError, match="must not start after it ends"):
        restli_date_range(date(2026, 9, 2), date(2026, 9, 1))


def test_restli_query_keeps_restli_values_byte_for_byte() -> None:
    """Pairs are joined with ``&`` unchanged; an empty pair list is an empty query."""
    pairs = [
        ("q", "search"),
        ("search", "(status:(values:List(ACTIVE,PAUSED)))"),
        ("accounts", "List(urn%3Ali%3AsponsoredAccount%3A1)"),
        ("fields", "a,b:(c,d),*~"),
    ]
    assert restli_query(pairs) == (
        "q=search&search=(status:(values:List(ACTIVE,PAUSED)))"
        "&accounts=List(urn%3Ali%3AsponsoredAccount%3A1)&fields=a,b:(c,d),*~"
    )
    assert restli_query([]) == ""


@pytest.mark.parametrize("value", ["", "a b", 'a"b', "a&b", "a#b", "a+b", "a=b", "a/b", "é"])
def test_restli_query_rejects_raw_characters_without_echoing_the_value(value: str) -> None:
    """A value httpx would re-encode, or that would split the query, never reaches the URL."""
    with pytest.raises(ValueError, match="empty or holds a raw character") as raised:
        restli_query([("pageToken", value)])
    assert "'pageToken'" in str(raised.value)
    if value:
        assert value not in str(raised.value)


def test_restli_query_names_only_a_valid_key() -> None:
    """An invalid key is described generically, never echoed."""
    with pytest.raises(ValueError, match="the key of a query key is empty") as raised:
        restli_query([("bad key", "1")])
    assert "bad key" not in str(raised.value)


@pytest.mark.parametrize(
    ("pair", "what"),
    [
        (("pageToken", "%"), "value of 'pageToken'"),
        (("pageToken", "%2"), "value of 'pageToken'"),
        (("pageToken", "%zz"), "value of 'pageToken'"),
        (("pageToken", "a%2Gb"), "value of 'pageToken'"),
        (("%zz", "1"), "key of '%zz'"),
    ],
)
def test_restli_query_rejects_malformed_percent_escapes(pair: tuple[str, str], what: str) -> None:
    """A ``%`` that does not start a two-digit hex escape is rejected in a key or a value."""
    with pytest.raises(ValueError, match="malformed percent escape") as raised:
        restli_query([pair])
    assert what in str(raised.value)


class _NoOffset(tzinfo):
    """A tzinfo that reports no UTC offset, which makes an instant effectively naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        """Report no offset."""
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        """Report no DST offset."""
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        """Report no name."""
        return None


def test_epoch_ms_uses_integer_milliseconds_of_aware_instants() -> None:
    """Epoch milliseconds are exact integers, floor-divided, whatever the instant's offset."""
    instant = datetime(2026, 9, 1, tzinfo=UTC)
    assert _epoch_ms(instant, "x") == int(instant.timestamp()) * 1000
    assert _epoch_ms(datetime(1970, 1, 1, 0, 0, 0, 1_500, tzinfo=UTC), "x") == 1
    plus_one = timezone(timedelta(hours=1))
    assert _epoch_ms(datetime(1970, 1, 1, 1, tzinfo=plus_one), "x") == 0


@pytest.mark.parametrize(
    "instant",
    [
        datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None),
        datetime(2026, 9, 1, tzinfo=_NoOffset()),
    ],
)
def test_epoch_ms_rejects_naive_instants(instant: datetime) -> None:
    """A naive instant, or one whose tzinfo reports no offset, is rejected and named."""
    with pytest.raises(ValueError, match="the range start must be timezone-aware"):
        _epoch_ms(instant, "the range start")


def test_restli_time_range_is_ordered_epoch_milliseconds() -> None:
    """``submittedAtTimeRange`` is ``(start:{ms},end:{ms})``, and a reversed range is rejected."""
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    start_ms = int(start.timestamp()) * 1000
    assert _restli_time_range(start, end) == f"(start:{start_ms},end:{start_ms + 3_600_000})"
    assert _restli_time_range(start, start) == f"(start:{start_ms},end:{start_ms})"
    with pytest.raises(ValueError, match="must not start after it ends"):
        _restli_time_range(end, start)
    with pytest.raises(ValueError, match="the range end must be timezone-aware"):
        _restli_time_range(start, end.replace(tzinfo=None))


def test_owner_selector_encodes_the_account_urn_once() -> None:
    """The Lead Sync owner selector keeps its structure raw and encodes the URN once."""
    assert _owner(ACCOUNT_URN) == "(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A506223315)"


def test_chunks_split_in_order() -> None:
    """Chunks are consecutive, at most ``size`` long, and cover every item once."""
    assert _chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert _chunks([1, 2], 2) == [[1, 2]]
    assert _chunks([], 3) == []


def test_distinct_keeps_first_seen_order() -> None:
    """Repeats are dropped and the first occurrence keeps its position."""
    assert _distinct(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
    assert _distinct([]) == []


@pytest.mark.parametrize(("page_size", "max_pages"), [(1, None), (LEAD_PAGE_SIZE, 1), (50, 3)])
def test_check_page_size_accepts_the_documented_bounds(
    page_size: int, max_pages: int | None
) -> None:
    """Page sizes 1 to 100 and any positive page limit are accepted."""
    _check_page_size(page_size, max_pages)


@pytest.mark.parametrize(
    ("page_size", "max_pages", "message"),
    [
        (0, None, "page_size must be between 1 and 100"),
        (LEAD_PAGE_SIZE + 1, None, "page_size must be between 1 and 100"),
        (True, None, "page_size must be between 1 and 100"),
        (10, 0, "max_pages must be at least 1"),
        (10, -1, "max_pages must be at least 1"),
        (10, True, "max_pages must be at least 1"),
    ],
)
def test_check_page_size_rejects_out_of_range_arguments(
    page_size: int, max_pages: int | None, message: str
) -> None:
    """A page size outside 1 to 100, a page limit below 1, or a ``bool`` is rejected."""
    with pytest.raises(ValueError, match=message):
        _check_page_size(page_size, max_pages)


def test_check_date_range_returns_an_ordered_pair() -> None:
    """An inclusive range is returned as ``(start, end)``; a reversed range is rejected."""
    day = date(2026, 9, 1)
    assert _check_date_range((day, day)) == (day, day)
    assert _check_date_range((day, day + timedelta(days=6))) == (day, day + timedelta(days=6))
    with pytest.raises(ValueError, match="must not start after it ends"):
        _check_date_range((day, day - timedelta(days=1)))


# ---------------------------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------------------------


def _assert_defect(error: QueryDefect, surface: str, fragment: str) -> None:
    """Assert ``error`` is a LinkedIn ``validation`` defect of ``surface`` naming ``fragment``."""
    assert error.source == SOURCE
    assert error.endpoint == surface
    assert error.code == "validation"
    assert fragment in error.message


def test_body_object_rejects_a_non_object_body() -> None:
    """A body that is not a JSON object is a ``validation`` defect of its surface."""
    response = JsonResponse(status=200, body=[{"id": 1}], headers=httpx.Headers())
    with pytest.raises(QueryDefect) as raised:
        _body_object(response, "ad_campaigns")
    _assert_defect(raised.value, "ad_campaigns", "not a JSON object")
    body = {"elements": []}
    assert _body_object(JsonResponse(200, body, httpx.Headers()), "ad_campaigns") is body


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({}, "no 'elements' list"),
        ({"elements": None}, "no 'elements' list"),
        ({"elements": {}}, "no 'elements' list"),
        ({"elements": [{"id": 1}, 2]}, "an item of 'elements' is not a JSON object"),
    ],
)
def test_elements_rejects_an_unrecognised_collection(body: dict[str, Any], fragment: str) -> None:
    """An unrecognised collection is a defect, never an empty listing."""
    with pytest.raises(QueryDefect) as raised:
        _elements(body, "ad_campaigns")
    _assert_defect(raised.value, "ad_campaigns", fragment)


def test_elements_returns_every_object_in_order() -> None:
    """A valid ``elements`` list is returned in response order; an empty one is empty."""
    assert _elements({"elements": [{"id": 2}, {"id": 1}]}, "creatives") == [{"id": 2}, {"id": 1}]
    assert _elements({"elements": []}, "creatives") == []


@pytest.mark.parametrize(
    ("href", "relative"),
    [
        (
            "/rest/adAnalytics?q=analytics&accounts=List(urn%3Ali%3AsponsoredAccount%3A1)&start=10",
            "adAnalytics?q=analytics&accounts=List(urn%3Ali%3AsponsoredAccount%3A1)&start=10",
        ),
        ("https://api.linkedin.com/rest/adAnalytics?start=2", "adAnalytics?start=2"),
        ("https://API.LinkedIn.com/rest/adAnalytics?start=2", "adAnalytics?start=2"),
        ("adAnalytics?start=2", "adAnalytics?start=2"),
        ("/rest/adAccounts/1/creatives", "adAccounts/1/creatives"),
    ],
)
def test_relative_href_strips_the_rest_base_and_keeps_query_bytes(href: str, relative: str) -> None:
    """A ``next`` link becomes a URL relative to ``/rest/`` with its query bytes untouched."""
    assert _relative_href(href, "ad_analytics") == relative


@pytest.mark.parametrize(
    ("href", "fragment"),
    [
        ("http://api.linkedin.com/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("https://evil.example/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("//api.linkedin.com/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("https://api.linkedin.com:8443/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("https://api.linkedin.com:444/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("https://api.linkedin.com:443/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("https://api.linkedin.com:/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("https://api.linkedin.com:x/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("https://user@api.linkedin.com/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("https://u:p@api.linkedin.com/rest/adAnalytics", "outside https://api.linkedin.com"),
        ("https://api.linkedin.com@evil.example/rest/x", "outside https://api.linkedin.com"),
        ("/v2/adAnalytics?start=2", "outside the versioned REST base"),
        ("/rest/", "malformed path"),
        ("/rest/adAccounts/../adAnalytics", "malformed path"),
        ("../adAnalytics", "malformed path"),
        ("/rest/ad%20Analytics", "malformed path"),
        ("/rest/adAccounts//creatives", "malformed path"),
    ],
)
def test_relative_href_rejects_links_outside_the_versioned_api(href: str, fragment: str) -> None:
    """A link to another host, scheme, port, user or base, or a traversing path, is a defect."""
    with pytest.raises(QueryDefect) as raised:
        _relative_href(href, "ad_analytics")
    _assert_defect(raised.value, "ad_analytics", fragment)


# Links urlsplit cannot parse, then links holding a character that is not visible ASCII: one
# urlsplit would silently drop, a host that lower-cases to api.linkedin.com (U+212A KELVIN
# SIGN), and characters httpx would re-encode or refuse.
_INVALID_HREFS = [
    pytest.param("https://[invalid/rest/leadForms", id="unclosed-ipv6-bracket"),
    pytest.param("https://api.linkedin.com]/rest/leadForms", id="unopened-ipv6-bracket"),
    pytest.param("https://[::1x]/rest/leadForms", id="invalid-ipv6-literal"),
    pytest.param("https://[127.0.0.1]/rest/leadForms", id="bracketed-ipv4"),
    pytest.param("https://[v1]/rest/leadForms", id="invalid-ipvfuture"),
    pytest.param("https://api.linkedin.com\uff03/rest/leadForms", id="nfkc-unsafe-host"),
    pytest.param("https://api.lin\u212aedin.com/rest/leadForms?start=2", id="kelvin-sign-host"),
    pytest.param("/rest/lead\nForms?start=2", id="line-break"),
    pytest.param("/rest/leadForms?start=\t2", id="tab"),
    pytest.param("\x00/rest/leadForms?start=2", id="leading-nul"),
    pytest.param("/rest/leadForms?start=2\x00", id="nul"),
    pytest.param("/rest/leadForms?start=2\x7f", id="del"),
    pytest.param("/rest/leadForms?start=\u00e92", id="non-ascii-query"),
    pytest.param("/rest/leadForms?start= 2", id="raw-space"),
]


@pytest.mark.parametrize("href", _INVALID_HREFS)
def test_relative_href_rejects_an_invalid_url_without_quoting_it(href: str) -> None:
    """A link that is not a valid URL is a ``validation`` defect, never a ``ValueError``.

    The defect names no part of the link and chains no ``urlsplit`` error, whose message
    would quote it.
    """
    with pytest.raises(QueryDefect) as raised:
        _relative_href(href, "lead_forms")
    _assert_defect(raised.value, "lead_forms", "a 'next' link is not a valid URL")
    assert raised.value.message == "a 'next' link is not a valid URL"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None or raised.value.__suppress_context__


def _rejected_next_links(path: str) -> list[Any]:
    """Return ``(href, fragment)`` params of ``next`` links to ``path`` that are defects.

    First links that are not valid URLs (three ``urlsplit`` rejects, then a control
    character), then links naming user information or a port on the API host.
    """
    invalid = "a 'next' link is not a valid URL"
    offsite = "a 'next' link points outside https://api.linkedin.com"
    return [
        pytest.param(f"https://[invalid/rest/{path}", invalid, id="unclosed-ipv6-bracket"),
        pytest.param(f"https://api.linkedin.com]/rest/{path}", invalid, id="unopened-bracket"),
        pytest.param(f"https://[::1x]/rest/{path}", invalid, id="invalid-ipv6-literal"),
        pytest.param(f"/rest/{path}?start=1\x00", invalid, id="control-character"),
        pytest.param(f"https://user@api.linkedin.com/rest/{path}", offsite, id="user-info"),
        pytest.param(f"https://api.linkedin.com:444/rest/{path}", offsite, id="port"),
    ]


def test_next_href_reads_only_the_rel_next_link() -> None:
    """Only ``rel=next`` is read, its href stripped; no paging or no next link gives ``None``."""
    links = [
        {"rel": "prev", "href": "/rest/adAnalytics?start=0"},
        {"rel": "next", "href": " /rest/adAnalytics?start=2 "},
    ]
    assert _next_href({"paging": {"links": links}}, "ad_analytics") == "adAnalytics?start=2"
    assert _next_href({}, "ad_analytics") is None
    assert _next_href({"paging": {"start": 0, "count": 10}}, "ad_analytics") is None
    assert _next_href({"paging": {"links": []}}, "ad_analytics") is None
    assert _next_href({"paging": {"links": links[:1]}}, "ad_analytics") is None


@pytest.mark.parametrize(
    ("paging", "fragment"),
    [
        ([], "field 'paging' is not a JSON object"),
        ({"links": {}}, "field 'paging.links' is not a list"),
        ({"links": ["next"]}, "an item of 'paging.links' is not a JSON object"),
        ({"links": [{"rel": "next"}]}, "a 'next' link has no href"),
        ({"links": [{"rel": "next", "href": "  "}]}, "a 'next' link has no href"),
        ({"links": [{"rel": "next", "href": 5}]}, "a 'next' link has no href"),
    ],
)
def test_next_href_rejects_a_malformed_paging_block(paging: object, fragment: str) -> None:
    """A malformed ``paging`` block is a defect, never a silently ended chain."""
    with pytest.raises(QueryDefect) as raised:
        _next_href({"paging": paging}, "ad_analytics")
    _assert_defect(raised.value, "ad_analytics", fragment)


@pytest.mark.parametrize(
    ("body", "token"),
    [
        ({}, None),
        ({"metadata": None}, None),
        ({"metadata": {}}, None),
        ({"metadata": {"nextPageToken": None}}, None),
        ({"metadata": {"nextPageToken": ""}}, None),
        ({"metadata": {"nextPageToken": "DgGerr1iVQ"}}, "DgGerr1iVQ"),
    ],
)
def test_cursor_token_reads_metadata_next_page_token(
    body: dict[str, Any], token: str | None
) -> None:
    """An absent, null or empty ``nextPageToken`` ends the enumeration; a token continues it."""
    assert _cursor_token(body, "ad_campaigns") == token


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"metadata": []}, "field 'metadata' is not a JSON object"),
        ({"metadata": {"nextPageToken": 5}}, "field 'metadata.nextPageToken' is not a string"),
    ],
)
def test_cursor_token_rejects_malformed_metadata(body: dict[str, Any], fragment: str) -> None:
    """Malformed cursor metadata is a defect."""
    with pytest.raises(QueryDefect) as raised:
        _cursor_token(body, "ad_campaigns")
    _assert_defect(raised.value, "ad_campaigns", fragment)


@pytest.mark.parametrize(
    ("key", "normalised"),
    [
        ("urn%3Ali%3Ashare%3A1", "urn:li:share:1"),
        ('"aaaabbbb-5"', "aaaabbbb-5"),
        ("%22aaaabbbb-5%22", "aaaabbbb-5"),
        ('""', ""),
        ('"', '"'),
        ('"aaaabbbb-5', '"aaaabbbb-5'),
        ("urn:li:share:1", "urn:li:share:1"),
    ],
)
def test_batch_key_decodes_then_strips_one_pair_of_quotes(key: str, normalised: str) -> None:
    """A batch-result key is percent-decoded, then loses one surrounding pair of quotes."""
    assert _batch_key(key) == normalised


def test_batch_maps_reads_results_and_failures_at_both_levels() -> None:
    """Results are keyed by normalised key; ``errors`` and non-2xx ``statuses`` mark failures.

    Both maps are read at the top level and nested inside ``results``, where the documented
    Lead Sync sample places them; a ``bool`` or non-integer status is not a status.
    """
    body = {
        "results": {
            "urn%3Ali%3Ashare%3A1": {"id": "urn:li:share:1"},
            '"urn:li:share:2"': {"id": "urn:li:share:2"},
            "statuses": {"urn:li:share:3": 404},
            "errors": {"urn%3Ali%3Ashare%3A4": {"status": 403}},
        },
        "statuses": {
            "urn:li:share:1": 200,
            "urn:li:share:2": True,
            "urn:li:share:5": 500,
            "urn:li:share:6": "403",
        },
        "errors": {'"urn:li:share:7"': {"status": 404}},
    }
    found, failed = _batch_maps(body, "posts")
    assert found == {
        "urn:li:share:1": {"id": "urn:li:share:1"},
        "urn:li:share:2": {"id": "urn:li:share:2"},
    }
    assert failed == frozenset(
        {"urn:li:share:3", "urn:li:share:4", "urn:li:share:5", "urn:li:share:7"}
    )
    assert _batch_maps({"results": {}}, "posts") == ({}, frozenset())


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({}, "the batch response has no 'results' object"),
        ({"results": []}, "the batch response has no 'results' object"),
        ({"results": {"urn:li:share:1": 1}}, "a batch result is not a JSON object"),
        ({"results": {}, "errors": []}, "field 'errors' is not a JSON object"),
        ({"results": {"errors": []}}, "field 'errors' is not a JSON object"),
        ({"results": {}, "statuses": []}, "field 'statuses' is not a JSON object"),
        ({"results": {"statuses": "x"}}, "field 'statuses' is not a JSON object"),
    ],
)
def test_batch_maps_rejects_a_malformed_envelope(body: dict[str, Any], fragment: str) -> None:
    """A malformed batch-get envelope is a defect."""
    with pytest.raises(QueryDefect) as raised:
        _batch_maps(body, "posts")
    _assert_defect(raised.value, "posts", fragment)


def test_distinct_rows_keeps_the_first_row_per_key() -> None:
    """The first row per natural key is kept in order and repeats are counted."""
    rows = [("a", 1), ("b", 2), ("a", 3), ("c", 4), ("b", 5)]
    kept, dropped = _distinct_rows(rows, lambda row: row[0])
    assert kept == [("a", 1), ("b", 2), ("c", 4)]
    assert dropped == 2
    assert _distinct_rows([], lambda row: row) == ([], 0)


_NEXT_LINK = {"rel": "next", "href": "/rest/leadForms?start=20", "type": "application/json"}


@pytest.mark.parametrize(
    ("paging", "expected"),
    [
        pytest.param({"start": 10, "count": 10}, False, id="no-total-no-links"),
        pytest.param({"start": 10, "count": 10, "total": 0, "links": []}, False, id="empty-links"),
        pytest.param(
            {"start": 10, "count": 10, "links": [{"rel": "prev", "href": "/rest/leadForms"}]},
            False,
            id="prev-link-only",
        ),
        pytest.param({"start": 10, "count": 10, "links": [_NEXT_LINK]}, True, id="next-link"),
        pytest.param(
            {
                "start": 10,
                "count": 10,
                "total": 10,
                "links": [{"rel": "next", "href": "https://api.linkedin.com/rest/leadForms?x=1"}],
            },
            True,
            id="absolute-next-link",
        ),
    ],
)
def test_checked_offset_paging_reports_a_next_link_on_a_valid_envelope(
    paging: dict[str, Any], expected: bool
) -> None:
    """A contract envelope echoing the requested ``start`` and ``count`` reports ``rel=next``."""
    assert _checked_offset_paging({"paging": paging}, "lead_forms", start=10, count=10) is expected


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        pytest.param({}, "the response has no 'paging' object", id="paging-absent"),
        pytest.param({"paging": None}, "field 'paging' is not a JSON object", id="paging-null"),
        pytest.param({"paging": []}, "field 'paging' is not a JSON object", id="paging-list"),
        pytest.param({"paging": {"count": 10}}, "field 'paging.start' is missing", id="no-start"),
        pytest.param(
            {"paging": {"start": "10", "count": 10}},
            "field 'paging.start' is not an integer",
            id="start-string",
        ),
        pytest.param(
            {"paging": {"start": True, "count": 10}},
            "field 'paging.start' is not an integer",
            id="start-bool",
        ),
        pytest.param(
            {"paging": {"start": None, "count": 10}},
            "field 'paging.start' is not an integer",
            id="start-null",
        ),
        pytest.param(
            {"paging": {"start": 0, "count": 10}},
            "field 'paging.start' does not match the requested start",
            id="start-mismatch",
        ),
        pytest.param({"paging": {"start": 10}}, "field 'paging.count' is missing", id="no-count"),
        pytest.param(
            {"paging": {"start": 10, "count": 10.0}},
            "field 'paging.count' is not an integer",
            id="count-float",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 100}},
            "field 'paging.count' does not match the requested count",
            id="count-mismatch",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "total": -1}},
            "field 'paging.total' is negative",
            id="total-negative",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "total": "1"}},
            "field 'paging.total' is not an integer",
            id="total-string",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "total": False}},
            "field 'paging.total' is not an integer",
            id="total-bool",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "links": {}}},
            "field 'paging.links' is not a list",
            id="links-object",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "links": None}},
            "field 'paging.links' is not a list",
            id="links-null",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "links": ["next"]}},
            "an item of 'paging.links' is not a JSON object",
            id="link-string",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "links": [{"rel": "next"}]}},
            "an item of 'paging.links' has no string 'href'",
            id="link-without-href",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "links": [{"href": "/rest/leadForms"}]}},
            "an item of 'paging.links' has no string 'rel'",
            id="link-without-rel",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "links": [{"rel": 1, "href": "/rest/x"}]}},
            "an item of 'paging.links' has no string 'rel'",
            id="link-rel-not-a-string",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "links": [{**_NEXT_LINK, "type": 1}]}},
            "field 'type' of an item of 'paging.links' is not a string",
            id="link-type-not-a-string",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "links": [{"rel": "next", "href": " "}]}},
            "a 'next' link has no href",
            id="next-href-blank",
        ),
        pytest.param(
            {
                "paging": {
                    "start": 10,
                    "count": 10,
                    "links": [{"rel": "next", "href": "https://www.example.com/rest/leadForms"}],
                }
            },
            "a 'next' link points outside https://api.linkedin.com",
            id="next-href-off-host",
        ),
        pytest.param(
            {
                "paging": {
                    "start": 10,
                    "count": 10,
                    "links": [{"rel": "next", "href": "http://api.linkedin.com/rest/leadForms"}],
                }
            },
            "a 'next' link points outside https://api.linkedin.com",
            id="next-href-plain-http",
        ),
        pytest.param(
            {"paging": {"start": 10, "count": 10, "links": [{"rel": "next", "href": "/v2/x"}]}},
            "a 'next' link is outside the versioned REST base /rest/",
            id="next-href-outside-rest",
        ),
        pytest.param(
            {
                "paging": {
                    "start": 10,
                    "count": 10,
                    "links": [{"rel": "next", "href": "https://[invalid/rest/leadForms"}],
                }
            },
            "a 'next' link is not a valid URL",
            id="next-href-unclosed-ipv6-bracket",
        ),
        pytest.param(
            {
                "paging": {
                    "start": 10,
                    "count": 10,
                    "links": [{"rel": "next", "href": "/rest/leadForms?start=\x0020"}],
                }
            },
            "a 'next' link is not a valid URL",
            id="next-href-control-character",
        ),
        pytest.param(
            {
                "paging": {
                    "start": 10,
                    "count": 10,
                    "links": [{"rel": "next", "href": "https://u@api.linkedin.com/rest/leadForms"}],
                }
            },
            "a 'next' link points outside https://api.linkedin.com",
            id="next-href-user-info",
        ),
        pytest.param(
            {
                "paging": {
                    "start": 10,
                    "count": 10,
                    "links": [
                        {"rel": "next", "href": "https://api.linkedin.com:444/rest/leadForms"}
                    ],
                }
            },
            "a 'next' link points outside https://api.linkedin.com",
            id="next-href-port",
        ),
    ],
)
def test_checked_offset_paging_rejects_a_broken_envelope(
    body: dict[str, Any], fragment: str
) -> None:
    """A missing, malformed or mismatched ``paging`` envelope is a defect, never an end."""
    with pytest.raises(QueryDefect) as raised:
        _checked_offset_paging(body, "lead_form_responses", start=10, count=10)
    _assert_defect(raised.value, "lead_form_responses", fragment)


@pytest.mark.parametrize(
    ("urn", "expected"),
    [
        ("urn:li:share:6778045555198214144", True),
        ("urn:li:ugcPost:6927151919538794496", True),
        ("urn:li:share:", False),
        ("urn:li:share:12a", False),
        ("urn:li:ugcPost:-1", False),
        ("urn:li:adInMailContent:123", False),
        ("urn:li:sponsoredCreative:1", False),
        ("", False),
    ],
)
def test_is_post_urn_accepts_only_numeric_share_and_ugc_post_urns(urn: str, expected: bool) -> None:
    """Only ``urn:li:share:{id}`` and ``urn:li:ugcPost:{id}`` with a numeric id are posts."""
    assert _is_post_urn(urn) is expected


def test_checked_lead_id_returns_a_plain_id() -> None:
    """A plain response id is returned unchanged."""
    assert _checked_lead_id("aaaabbbb-0000-cccc-1111-dddd2222eeee-5") == (
        "aaaabbbb-0000-cccc-1111-dddd2222eeee-5"
    )


@pytest.mark.parametrize(
    ("lead_id", "message"),
    [
        (5, "must be a non-blank string"),
        ("", "must be a non-blank string"),
        ("   ", "must be a non-blank string"),
        ("urn:li:leadFormResponse:1", "plain ids, not URNs"),
        ('aaaa"bbbb', "must not hold a double quote"),
    ],
)
def test_checked_lead_id_rejects_urns_blanks_and_quotes(lead_id: object, message: str) -> None:
    """A non-string, blank, URN or quote-holding id is rejected before any request."""
    with pytest.raises(ValueError, match=message):
        _checked_lead_id(lead_id)  # type: ignore[arg-type]


def test_log_outcome_logs_counts_at_info_or_warning(captured: list[EventDict]) -> None:
    """A complete listing logs at info and an incomplete one at warning, with counts only."""
    _log_outcome("ad_campaigns", rows=3, calls=2, complete=True, reason=None, repeats_dropped=1)
    _log_outcome("creatives", rows=0, calls=1, complete=False, reason="linkedin creatives: x")
    complete, incomplete = captured
    assert complete == {
        "event": "linkedin.listing",
        "log_level": "info",
        "surface": "ad_campaigns",
        "rows": 3,
        "calls": 2,
        "complete": True,
        "reason": None,
        "repeats_dropped": 1,
    }
    assert incomplete["log_level"] == "warning"
    assert incomplete["complete"] is False
    assert incomplete["reason"] == "linkedin creatives: x"


# ---------------------------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------------------------


def test_listing_is_frozen_and_defaults_to_not_interrupted() -> None:
    """A listing is immutable, and ``interrupted_by`` defaults to ``None``."""
    listing = Listing(rows=(1, 2), complete=True, reason=None, calls=1)
    assert listing.interrupted_by is None
    with pytest.raises(FrozenInstanceError):
        listing.complete = False  # type: ignore[misc]


def test_post_batch_carries_posts_failures_and_completeness() -> None:
    """A post batch keeps posts by URN, the failed URNs and the interruption, immutably."""
    error = TransientError("down", source=SOURCE, endpoint="posts", http_status=503)
    post = PostRow(urn="urn:li:share:1", headline="H", intro_text="I")
    batch = PostBatch(
        posts={"urn:li:share:1": post},
        failed=frozenset({"urn:li:share:2"}),
        complete=False,
        reason=error.reason,
        calls=2,
        interrupted_by=error,
    )
    assert batch.posts["urn:li:share:1"] is post
    assert batch.interrupted_by is error
    with pytest.raises(FrozenInstanceError):
        batch.calls = 3  # type: ignore[misc]
    assert PostBatch({}, frozenset(), True, None, 0).interrupted_by is None


# ---------------------------------------------------------------------------------------------
# Construction, headers and the request core
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_repr_never_shows_the_token_source() -> None:
    """The client's ``repr`` and ``str`` name the class only, never a token."""
    for tokens in (StaticTokenProvider(TOKEN), RefreshingTokens(OLD_TOKEN)):
        async with _linkedin(Server(), tokens) as linkedin:
            assert repr(linkedin) == "LinkedInClient()"
            assert str(linkedin) == "LinkedInClient()"
            assert TOKEN not in repr(linkedin)
            assert OLD_TOKEN not in repr(linkedin)


@pytest.mark.anyio
async def test_every_request_carries_bearer_version_and_restli_headers() -> None:
    """Each request is a ``GET`` under ``/rest/`` with Bearer, version and Rest.li headers."""
    server = Server(_ok(_account()))
    async with _linkedin(server) as linkedin:
        await linkedin.get_ad_account(ACCOUNT_ID)
    (request,) = server.requests
    _assert_contract_request("ad_account", request)
    assert request.method == "GET"
    assert str(request.url) == f"{BASE_URL}adAccounts/{ACCOUNT_ID}"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Linkedin-Version"] == LINKEDIN_VERSION == "202609"
    assert request.headers["X-Restli-Protocol-Version"] == RESTLI_PROTOCOL_VERSION == "2.0.0"
    assert request.headers["Accept"] == "application/json"


@pytest.mark.anyio
async def test_extra_headers_never_override_the_authentication_headers() -> None:
    """A call's extra headers are sent, but never replace the Bearer or version headers."""
    server = Server(_ok(_posts_body({})))
    tally = _Tally()
    extra = {
        "X-RestLi-Method": "BATCH_GET",
        "Authorization": "Bearer other",
        "Linkedin-Version": "202401",
    }
    async with _linkedin(server) as linkedin:
        response = await linkedin._send("posts", "posts?ids=List()", TOKEN, extra, tally)
    assert response.status == 200
    assert tally.calls == 1
    (request,) = server.requests
    assert request.headers["X-RestLi-Method"] == "BATCH_GET"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Linkedin-Version"] == LINKEDIN_VERSION


@pytest.mark.anyio
async def test_get_sends_the_built_query_byte_for_byte() -> None:
    """``_get`` sends ``path?restli_query(pairs)`` unchanged, and a bare path with no pairs."""
    pairs = [
        ("q", "search"),
        ("search", "(status:(values:List(ACTIVE,PAUSED)))"),
        ("pageToken", "a%2Fb%2B%3D"),
    ]
    server = Server(_ok({"elements": []}), _ok({"elements": []}))
    async with _linkedin(server) as linkedin:
        await linkedin._get("ad_campaigns", "adAccounts/1/adCampaigns", pairs, tally=_Tally())
        await linkedin._get("ad_account", "adAccounts/1", (), tally=_Tally())
    with_query, bare = server.requests
    assert with_query.url.query.decode("ascii") == restli_query(pairs)
    assert _relative(with_query) == f"adAccounts/1/adCampaigns?{restli_query(pairs)}"
    assert _relative(bare) == "adAccounts/1"
    assert bare.url.query == b""


@pytest.mark.anyio
async def test_get_rejects_a_raw_query_value_before_sending() -> None:
    """A query value holding a raw character fails before any request is sent."""
    server = Server()
    async with _linkedin(server) as linkedin:
        with pytest.raises(ValueError, match="raw character"):
            await linkedin._get(
                "ad_campaigns", "adAccounts/1/adCampaigns", [("q", "a b")], tally=_Tally()
            )
    assert server.requests == []


@pytest.mark.anyio
async def test_a_401_refreshes_the_token_once_and_retries(captured: list[EventDict]) -> None:
    """The first 401 of a refreshable source invalidates the token and repeats the call once."""
    server = Server(_error(401, "INVALID_ACCESS_TOKEN", "Invalid access token"), _ok(_account()))
    tokens = RefreshingTokens(OLD_TOKEN, NEW_TOKEN)
    async with _linkedin(server, tokens) as linkedin:
        account = await linkedin.get_ad_account(ACCOUNT_ID)
    assert account.external_id == ACCOUNT_ID
    first, second = server.requests
    assert first.headers["Authorization"] == f"Bearer {OLD_TOKEN}"
    assert second.headers["Authorization"] == f"Bearer {NEW_TOKEN}"
    assert (tokens.invalidations, tokens.exchanges) == (1, 2)
    retry_events = [e for e in captured if e["event"] == "linkedin.token.retry_after_401"]
    assert retry_events == [
        {"event": "linkedin.token.retry_after_401", "log_level": "info", "surface": "ad_account"}
    ]
    (outcome,) = _listing_events(captured, "ad_account")
    assert outcome["calls"] == 2
    assert all(OLD_TOKEN not in str(e) and NEW_TOKEN not in str(e) for e in captured)


@pytest.mark.anyio
async def test_a_second_401_propagates_as_access_denied() -> None:
    """A 401 after the one refresh is access denied, with ``is_unauthorized`` set."""
    server = Server(
        _error(401, "INVALID_ACCESS_TOKEN", "Invalid access token"),
        _error(401, "INVALID_ACCESS_TOKEN", "Invalid access token"),
    )
    tokens = RefreshingTokens(OLD_TOKEN, NEW_TOKEN)
    async with _linkedin(server, tokens) as linkedin:
        with pytest.raises(AccessDenied) as raised:
            await linkedin.get_ad_account(ACCOUNT_ID)
    assert raised.value.is_unauthorized
    assert raised.value.endpoint == "ad_account"
    assert len(server.requests) == 2
    assert tokens.invalidations == 1


@pytest.mark.anyio
async def test_a_401_with_a_static_token_propagates_at_once() -> None:
    """A static token cannot refresh, so its first 401 is access denied with no retry."""
    server = Server(_error(401, "INVALID_ACCESS_TOKEN", "Invalid access token"))
    async with _linkedin(server, StaticTokenProvider(TOKEN)) as linkedin:
        with pytest.raises(AccessDenied) as raised:
            await linkedin.get_ad_account(ACCOUNT_ID)
    assert raised.value.is_unauthorized
    assert len(server.requests) == 1


@pytest.mark.anyio
async def test_a_403_is_access_denied_without_a_token_refresh() -> None:
    """A 403 propagates at once as access denied; the token is never invalidated for it."""
    server = Server(_error(403, "ACCESS_DENIED", "Not enough permissions to access: adAccounts"))
    tokens = RefreshingTokens(OLD_TOKEN, NEW_TOKEN)
    async with _linkedin(server, tokens) as linkedin:
        with pytest.raises(AccessDenied) as raised:
            await linkedin.get_ad_account(ACCOUNT_ID)
    assert not raised.value.is_unauthorized
    assert raised.value.http_status == 403
    assert raised.value.code == "ACCESS_DENIED"
    assert len(server.requests) == 1
    assert tokens.invalidations == 0


@pytest.mark.anyio
async def test_a_token_source_failure_propagates_before_any_request() -> None:
    """A refresh the token source cannot complete propagates its own error unchanged."""
    denied = AccessDenied("refresh token revoked", source=SOURCE, endpoint="access_token")
    server = Server()
    async with _linkedin(server, RefreshingTokens(failure=denied)) as linkedin:
        with pytest.raises(AccessDenied) as raised:
            await linkedin.get_ad_account(ACCOUNT_ID)
    assert raised.value is denied
    assert server.requests == []


@pytest.mark.anyio
async def test_the_client_keeps_only_the_last_eight_distinct_tokens_it_used() -> None:
    """The client keeps the last eight distinct bearer tokens it obtained, as secrets only.

    Nine calls, each answered 401 and then 200, make a refreshable source hand out ten
    tokens, the second to the ninth twice (served from its cache, then refreshed away). The
    client keeps the last eight, once each, as ``SecretStr``; neither its ``repr`` nor that
    of what it keeps shows a token. A later call's 403 echoing one it still keeps, but that
    request no longer carries, comes back with that token redacted.
    """
    issued = [f"test-linkedin-token-{index:02d}-of-ten" for index in range(10)]
    server = Server(
        *(
            outcome
            for _ in range(9)
            for outcome in (_error(401, "INVALID_ACCESS_TOKEN", "Expired"), _ok(_account()))
        ),
        _error(403, "ACCESS_DENIED", f"{issued[2]} may not read this account"),
    )
    tokens = RefreshingTokens(*issued)
    async with _linkedin(server, tokens) as linkedin:
        for _ in range(9):
            await linkedin.get_ad_account(ACCOUNT_ID)
        with pytest.raises(AccessDenied) as raised:
            await linkedin.get_ad_account(ACCOUNT_ID)
        kept = list(linkedin._used_tokens)
        shown = (repr(linkedin), str(linkedin), repr(linkedin._used_tokens))

    assert (tokens.exchanges, tokens.invalidations) == (10, 9)
    assert _USED_TOKENS_MAX == _RETIRED_TOKENS_MAX == 8
    assert all(isinstance(token, SecretStr) for token in kept)
    assert [token.get_secret_value() for token in kept] == issued[2:]
    assert shown[:2] == ("LinkedInClient()", "LinkedInClient()")
    assert not any(token in text for token in issued for text in shown)
    assert server.requests[-1].headers["Authorization"] == f"Bearer {issued[-1]}"
    assert raised.value.reason == (
        "linkedin ad_account: ACCESS_DENIED: [REDACTED] may not read this account"
    )


def _exchanged(access: str, refresh: str) -> httpx.Response:
    """Return a successful token exchange issuing ``access`` and rotating to ``refresh``."""
    return _ok({"access_token": access, "expires_in": 3600, "refresh_token": refresh})


@pytest.mark.anyio
async def test_held_secrets_cover_current_rotated_and_retired_tokens() -> None:
    """``held_secrets()`` lists every plaintext a provider holds or retired, read when called.

    Refresh mode: the client id, the client secret and the configured refresh token before
    any exchange; then the rotated refresh token, the cached access token and the refresh
    token rotated away; after an invalidation and a second rotation, also the dropped access
    token and the first rotated refresh token. Static mode, of the provider and of
    ``StaticTokenProvider``: the static token alone. No ``repr`` shows any of them.
    """
    client_id = "test-linkedin-held-client-id"
    client_secret = "test-linkedin-held-client-secret"
    configured = "test-linkedin-held-refresh-0"
    first_access, first_rotated = "test-linkedin-held-access-1", "test-linkedin-held-refresh-1"
    second_access, second_rotated = "test-linkedin-held-access-2", "test-linkedin-held-refresh-2"
    settings = Settings(
        linkedin_client_id=client_id,
        linkedin_client_secret=SecretStr(client_secret),
        linkedin_refresh_token=SecretStr(configured),
    )
    server = Server(
        _exchanged(first_access, first_rotated), _exchanged(second_access, second_rotated)
    )
    transport = httpx.MockTransport(server.handle)
    async with build_client("linkedin", transport, settings=Settings()) as http:
        tokens = LinkedInTokenProvider(settings, http, retry=RetryPolicy(sleep=server.sleep))
        before = tokens.held_secrets()
        assert await tokens.get() == first_access
        after_first = tokens.held_secrets()
        tokens.invalidate()
        assert await tokens.get() == second_access
        after_second = tokens.held_secrets()
        shown = (repr(tokens), str(tokens))
        static = LinkedInTokenProvider(Settings(linkedin_access_token=SecretStr(TOKEN)), http)

    assert tokens.mode == "refresh"
    assert len(server.requests) == 2
    assert set(before) == {client_id, client_secret, configured}
    assert set(after_first) == {client_id, client_secret, first_rotated, first_access, configured}
    assert set(after_second) == {
        client_id,
        client_secret,
        second_rotated,
        second_access,
        configured,
        first_access,
        first_rotated,
    }
    assert all(isinstance(secret, str) for secret in after_second)
    assert shown == ("LinkedInTokenProvider(mode='refresh')",) * 2
    assert static.mode == "static"
    assert static.held_secrets() == (TOKEN,)
    assert repr(static) == "LinkedInTokenProvider(mode='static')"
    assert StaticTokenProvider(TOKEN).held_secrets() == (TOKEN,)
    assert StaticTokenProvider(SIMULATED_TOKEN).held_secrets() == (SIMULATED_TOKEN,)
    assert TOKEN not in repr(StaticTokenProvider(TOKEN))


@pytest.mark.anyio
async def test_only_token_sources_offering_held_secrets_are_holds_secrets() -> None:
    """Both providers are :class:`HoldsSecrets`; a source without ``held_secrets`` is not."""
    transport = httpx.MockTransport(Server().handle)
    async with build_client("linkedin", transport, settings=Settings()) as http:
        refreshing = LinkedInTokenProvider(
            Settings(
                linkedin_client_id="test-linkedin-held-client-id",
                linkedin_client_secret=SecretStr("test-linkedin-held-client-secret"),
                linkedin_refresh_token=SecretStr("test-linkedin-held-refresh-0"),
            ),
            http,
        )
        static = LinkedInTokenProvider(Settings(linkedin_access_token=SecretStr(TOKEN)), http)
    assert (refreshing.mode, static.mode) == ("refresh", "static")
    assert isinstance(refreshing, HoldsSecrets)
    assert isinstance(static, HoldsSecrets)
    assert isinstance(StaticTokenProvider(TOKEN), HoldsSecrets)
    assert not isinstance(RefreshingTokens(OLD_TOKEN), HoldsSecrets)


class HoldingTokens(RefreshingTokens):
    """A refreshable token source that also :class:`HoldsSecrets`, held as given."""

    def __init__(self, *tokens: str) -> None:
        super().__init__(*tokens)
        self.held: list[str] = []
        self.reads = 0

    def held_secrets(self) -> tuple[str, ...]:
        """Return the secrets currently set in ``held``, counting each read."""
        self.reads += 1
        return tuple(self.held)


@pytest.mark.anyio
async def test_every_rest_call_holds_what_the_token_source_holds_when_it_is_sent() -> None:
    """A REST error echoing a secret only the token source holds never carries it.

    The source's held secrets are read for each call: a secret it starts holding between
    two calls is stripped from the second call's 403, as is the one it held before. A
    source without ``held_secrets`` still serves the client, which holds its own tokens.
    """
    rotated, rotated_again = "test-linkedin-only-held-1", "test-linkedin-only-held-2"
    server = Server(
        _error(403, "ACCESS_DENIED", f"{rotated} may not read this account"),
        _error(403, "ACCESS_DENIED", f"{rotated} and {rotated_again} may not read it"),
    )
    tokens = HoldingTokens(TOKEN)
    tokens.held = [rotated]
    async with _linkedin(server, tokens) as linkedin:
        with pytest.raises(AccessDenied) as first:
            await linkedin.get_ad_account(ACCOUNT_ID)
        tokens.held = [rotated_again, rotated]
        with pytest.raises(AccessDenied) as second:
            await linkedin.get_ad_account(ACCOUNT_ID)

    assert tokens.reads == 2
    assert first.value.reason == (
        "linkedin ad_account: ACCESS_DENIED: [REDACTED] may not read this account"
    )
    assert second.value.reason == (
        "linkedin ad_account: ACCESS_DENIED: [REDACTED] and [REDACTED] may not read it"
    )
    assert all(request.headers["Authorization"] == f"Bearer {TOKEN}" for request in server.requests)

    plain = Server(_error(403, "ACCESS_DENIED", f"{TOKEN} may not read this account"))
    async with _linkedin(plain, RefreshingTokens(TOKEN)) as linkedin:
        with pytest.raises(AccessDenied) as unheld:
            await linkedin.get_ad_account(ACCOUNT_ID)
    assert unheld.value.reason == (
        "linkedin ad_account: ACCESS_DENIED: [REDACTED] may not read this account"
    )


@pytest.mark.anyio
async def test_transient_failures_are_retried_with_backoff_and_retry_after() -> None:
    """A 503 waits the exponential backoff, a 429 its ``Retry-After``; it is one call."""
    server = Server(
        _unavailable(),
        _error(429, "TOO_MANY_REQUESTS", "Resource level throttle limit", **{"Retry-After": "7"}),
        _ok(_cursor_page("ad_campaigns", [_campaign(1)])),
    )
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_campaigns(ACCOUNT_ID)
    assert len(server.requests) == 3
    assert listing.complete
    assert listing.calls == 1
    backoff, retry_after = server.waits
    assert 2.0 <= backoff <= 3.0
    assert retry_after == 7.0


@pytest.mark.anyio
async def test_a_timeout_is_transient_and_retried() -> None:
    """A read timeout is transient and retried, so one slow response does not fail the call."""
    server = Server(httpx.ReadTimeout("read timed out"), _ok(_account()))
    async with _linkedin(server) as linkedin:
        account = await linkedin.get_ad_account(ACCOUNT_ID)
    assert account.currency == "USD"
    assert len(server.requests) == 2
    assert len(server.waits) == 1


@pytest.mark.anyio
async def test_a_transient_failure_on_every_attempt_raises_transient_error() -> None:
    """After the policy's three attempts a single read raises the last ``TransientError``."""
    server = Server(*_outage())
    async with _linkedin(server) as linkedin:
        with pytest.raises(TransientError) as raised:
            await linkedin.get_ad_account(ACCOUNT_ID)
    assert raised.value.http_status == 503
    assert len(server.requests) == 3


@pytest.mark.anyio
async def test_a_400_is_a_defect_and_never_retried() -> None:
    """HTTP 400 is a ``QueryDefect`` raised on its first occurrence, with no retry."""
    server = Server(_error(400, "PARAM_INVALID", "Invalid param"))
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect) as raised:
            await linkedin.list_campaigns(ACCOUNT_ID)
    assert raised.value.http_status == 400
    assert raised.value.endpoint == "ad_campaigns"
    assert len(server.requests) == 1
    assert server.waits == []


# ---------------------------------------------------------------------------------------------
# Next-link chains (_follow)
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_follow_reads_one_response_without_a_next_link() -> None:
    """A response without a ``next`` link is the whole chain and is not capped."""
    server = Server(_ok(_analytics_page([_daily_element(1), _daily_element(2)])))
    tally = _Tally()
    async with _linkedin(server) as linkedin:
        pages = await linkedin._follow("ad_analytics", ANALYTICS_URL, tally)
    assert [e["impressions"] for e in pages.elements] == [1, 2]
    assert (pages.capped, pages.interrupted_by, tally.calls) == (False, None, 1)
    assert _relative(server.requests[0]) == ANALYTICS_URL
    _assert_contract_request("ad_analytics", server.requests[0])


@pytest.mark.anyio
async def test_follow_follows_relative_and_absolute_next_links_at_once() -> None:
    """Each ``next`` link is followed in order with its query bytes kept, until none remains."""
    second = f"{ANALYTICS_URL}&start=10&count=10"
    third = f"{ANALYTICS_URL}&start=20&count=10"
    server = Server(
        _ok(_analytics_page([_daily_element(1)], f"/rest/{second}")),
        _ok(_analytics_page([_daily_element(2)], f"https://api.linkedin.com/rest/{third}")),
        _ok(_analytics_page([_daily_element(3)])),
    )
    tally = _Tally()
    async with _linkedin(server) as linkedin:
        pages = await linkedin._follow("ad_analytics", ANALYTICS_URL, tally)
    assert [_relative(request) for request in server.requests] == [ANALYTICS_URL, second, third]
    assert [e["impressions"] for e in pages.elements] == [1, 2, 3]
    assert tally.calls == 3
    assert not pages.capped


@pytest.mark.anyio
async def test_follow_follows_the_next_link_of_an_empty_page() -> None:
    """An empty page with a ``next`` link is not the end: the rows behind it are read."""
    second = f"{ANALYTICS_URL}&start=10"
    third = f"{ANALYTICS_URL}&start=20"
    server = Server(
        _ok(_analytics_page([_daily_element(1)], f"/rest/{second}")),
        _ok(_analytics_page([], f"/rest/{third}")),
        _ok(_analytics_page([_daily_element(2), _daily_element(3)])),
    )
    tally = _Tally()
    async with _linkedin(server) as linkedin:
        pages = await linkedin._follow("ad_analytics", ANALYTICS_URL, tally)
    assert [_relative(request) for request in server.requests] == [ANALYTICS_URL, second, third]
    assert [e["impressions"] for e in pages.elements] == [1, 2, 3]
    assert (pages.capped, pages.interrupted_by, tally.calls) == (False, None, 3)


@pytest.mark.anyio
async def test_follow_rejects_an_empty_page_whose_next_link_repeats_a_request() -> None:
    """An empty page linking back to a request already read is a defect, never the end."""
    second = f"{ANALYTICS_URL}&start=10"
    server = Server(
        _ok(_analytics_page([_daily_element(1)], f"/rest/{second}")),
        _ok(_analytics_page([], f"/rest/{second}")),
    )
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect) as raised:
            await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally())
    _assert_defect(raised.value, "ad_analytics", "repeats an earlier request")
    assert len(server.requests) == 2


@pytest.mark.anyio
async def test_follow_bounds_a_chain_of_empty_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty pages that keep returning fresh ``next`` links are cut off at the bound."""
    monkeypatch.setattr(client_module, "_MAX_NEXT_LINKS", 2)
    server = Server(
        *(_ok(_analytics_page([], f"/rest/{ANALYTICS_URL}&start={i}")) for i in range(1, 4))
    )
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect) as raised:
            await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally())
    _assert_defect(raised.value, "ad_analytics", "more than 2 'next' links in one chain")
    assert len(server.requests) == 3


@pytest.mark.anyio
async def test_follow_marks_a_transient_failure_after_an_empty_page_as_interrupted() -> None:
    """A continuation of an empty page that fails transiently interrupts the chain."""
    server = Server(
        _ok(_analytics_page([_daily_element(1)], f"/rest/{ANALYTICS_URL}&start=10")),
        _ok(_analytics_page([], f"/rest/{ANALYTICS_URL}&start=20")),
        *_outage(),
    )
    tally = _Tally()
    async with _linkedin(server) as linkedin:
        pages = await linkedin._follow("ad_analytics", ANALYTICS_URL, tally)
    assert [e["impressions"] for e in pages.elements] == [1]
    assert isinstance(pages.interrupted_by, TransientError)
    assert not pages.capped
    assert tally.calls == 3


@pytest.mark.anyio
@pytest.mark.parametrize("repeated", [f"/rest/{ANALYTICS_URL}", f"/rest/{ANALYTICS_URL}&start=10"])
async def test_follow_rejects_a_next_link_that_repeats_a_request(repeated: str) -> None:
    """A ``next`` link to a URL already requested is a defect, so the chain terminates."""
    server = Server(
        _ok(_analytics_page([_daily_element(1)], f"/rest/{ANALYTICS_URL}&start=10")),
        _ok(_analytics_page([_daily_element(2)], repeated)),
    )
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect) as raised:
            await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally())
    _assert_defect(raised.value, "ad_analytics", "repeats an earlier request")
    assert len(server.requests) == 2


@pytest.mark.anyio
async def test_follow_marks_a_response_at_the_cap_without_next_link_as_capped() -> None:
    """A response with 15,000 elements and no ``next`` link may be truncated: it is capped."""
    elements = [_daily_element(index) for index in range(MAX_ANALYTICS_ELEMENTS)]
    server = Server(_ok(_analytics_page(elements)))
    async with _linkedin(server) as linkedin:
        pages = await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally())
    assert pages.capped
    assert len(pages.elements) == MAX_ANALYTICS_ELEMENTS


@pytest.mark.anyio
async def test_follow_a_full_page_with_a_next_link_is_not_capped() -> None:
    """A full page that links to a next page is followed, and only the last page decides."""
    elements = [_daily_element(index) for index in range(MAX_ANALYTICS_ELEMENTS)]
    server = Server(
        _ok(_analytics_page(elements, f"/rest/{ANALYTICS_URL}&start=15000")),
        _ok(_analytics_page([_daily_element(1)])),
    )
    async with _linkedin(server) as linkedin:
        pages = await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally())
    assert not pages.capped
    assert len(pages.elements) == MAX_ANALYTICS_ELEMENTS + 1


@pytest.mark.anyio
async def test_follow_keeps_elements_read_before_a_transient_failure() -> None:
    """A transient failure stops the chain; the elements already read are kept."""
    server = Server(
        _ok(_analytics_page([_daily_element(1)], f"/rest/{ANALYTICS_URL}&start=10")),
        *_outage(),
    )
    tally = _Tally()
    async with _linkedin(server) as linkedin:
        pages = await linkedin._follow("ad_analytics", ANALYTICS_URL, tally)
    assert [e["impressions"] for e in pages.elements] == [1]
    assert isinstance(pages.interrupted_by, TransientError)
    assert not pages.capped
    assert tally.calls == 2


@pytest.mark.anyio
async def test_follow_propagates_access_denied_mid_chain() -> None:
    """Access denied on a next page propagates; it never ends the chain quietly."""
    server = Server(
        _ok(_analytics_page([_daily_element(1)], f"/rest/{ANALYTICS_URL}&start=10")),
        _error(403, "ACCESS_DENIED", "Not enough permissions"),
    )
    async with _linkedin(server) as linkedin:
        with pytest.raises(AccessDenied):
            await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally())


@pytest.mark.anyio
async def test_follow_rejects_an_offsite_next_link_before_following_it() -> None:
    """A ``next`` link outside ``https://api.linkedin.com/rest/`` is a defect; it is not sent."""
    server = Server(_ok(_analytics_page([_daily_element(1)], "https://evil.example/rest/x")))
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect, match=re.escape("outside https://api.linkedin.com")):
            await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally())
    assert len(server.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(("href", "fragment"), _rejected_next_links("adAnalytics"))
async def test_analytics_daily_rejects_an_unusable_next_link_before_following_it(
    href: str, fragment: str
) -> None:
    """An invalid, user-info or ported ``next`` link is a defect through ``_follow``.

    The page is empty, so the link is the only continuation; it is never sent, and no
    ``ValueError`` escapes the connector.
    """
    server = Server(_ok(_analytics_page([], href)))
    day = date(2026, 9, 1)
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect) as raised:
            await linkedin.analytics_daily(ACCOUNT_ID, (day, day), expected_creatives=1)
    _assert_defect(raised.value, "ad_analytics", fragment)
    assert len(server.requests) == 1
    assert _relative(server.requests[0]) == ANALYTICS_URL


@pytest.mark.anyio
async def test_follow_bounds_the_next_link_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that keeps returning fresh ``next`` links is cut off with a defect."""
    monkeypatch.setattr(client_module, "_MAX_NEXT_LINKS", 2)
    server = Server(
        *(
            _ok(_analytics_page([_daily_element(i)], f"/rest/{ANALYTICS_URL}&start={i}"))
            for i in range(1, 4)
        )
    )
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect, match="more than 2 'next' links in one chain"):
            await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally())
    assert len(server.requests) == 3


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "fragment"),
    [([], "not a JSON object"), ({"paging": {"count": 0, "start": 0}}, "no 'elements' list")],
)
async def test_follow_rejects_an_unrecognised_response(body: object, fragment: str) -> None:
    """A body that is not an ``elements`` collection is a defect, never an empty chain."""
    server = Server(_ok(body))
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect, match=fragment):
            await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally())


# ---------------------------------------------------------------------------------------------
# Cursor paging (_cursor_pages) through the campaign search
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_campaigns_sends_the_all_status_search_and_pages_by_token() -> None:
    """Every status is requested; each later page sends the last token, encoded once.

    The first request equals the ``ad_campaigns`` contract URL byte for byte; the token's
    reserved characters are percent-encoded exactly once as ``pageToken``.
    """
    cursor = "page/2+next=="
    server = Server(
        _ok(_cursor_page("ad_campaigns", [_campaign(1), _campaign(2)], cursor)),
        _ok(_cursor_page("ad_campaigns", [_campaign(3)])),
    )
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_campaigns(ACCOUNT_ID)
    first, second = server.requests
    expected = _contract_url("ad_campaigns", id=ACCOUNT_ID)
    assert _relative(first) == expected
    assert _relative(second) == f"{expected}&pageToken=page%2F2%2Bnext%3D%3D"
    assert f"List({','.join(CAMPAIGN_SEARCH_STATUSES)})" in expected
    assert ("pageSize", str(ENTITY_PAGE_SIZE)) in _query_pairs(first)
    for request in server.requests:
        _assert_contract_request("ad_campaigns", request)
    assert [row.external_id for row in listing.rows] == ["1", "2", "3"]
    assert (listing.complete, listing.reason, listing.calls) == (True, None, 2)
    assert listing.interrupted_by is None
    assert listing.rows[0].daily_budget == Money(amount=Decimal(18), currency="USD")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "page",
    [
        _cursor_page("ad_campaigns", [_campaign(1)], metadata=False),
        _cursor_page("ad_campaigns", [_campaign(1)]),
        {"elements": [_campaign(1)], "metadata": {"nextPageToken": None}},
        {"elements": [_campaign(1)], "metadata": {"nextPageToken": ""}},
    ],
)
async def test_cursor_paging_ends_on_an_absent_null_or_empty_token(page: dict[str, Any]) -> None:
    """No ``metadata``, an empty one, or a null or empty token ends the search after one page."""
    server = Server(_ok(_conforming("ad_campaigns", page)))
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_campaigns(ACCOUNT_ID)
    assert len(server.requests) == 1
    assert listing.complete
    assert len(listing.rows) == 1


@pytest.mark.anyio
async def test_cursor_paging_ignores_the_legacy_paging_block() -> None:
    """A stale ``rel=next`` href in the legacy ``paging`` block is never followed."""
    stale = {
        "start": 0,
        "count": 10,
        "links": [{"rel": "next", "href": "/rest/adAccounts/1/creatives?start=10", "type": "GET"}],
    }
    server = Server(_ok(_cursor_page("creatives", [_creative(9, 1)], paging=stale)))
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_creatives(ACCOUNT_ID, ["1"])
    assert len(server.requests) == 1
    assert listing.complete
    assert [row.urn for row in listing.rows] == ["urn:li:sponsoredCreative:9"]


@pytest.mark.anyio
async def test_cursor_paging_rejects_a_token_that_does_not_advance() -> None:
    """A page token seen before is a defect, so a looping cursor cannot run forever."""
    server = Server(
        _ok(_cursor_page("ad_campaigns", [_campaign(1)], "token-a")),
        _ok(_cursor_page("ad_campaigns", [_campaign(2)], "token-b")),
        _ok(_cursor_page("ad_campaigns", [_campaign(3)], "token-a")),
    )
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect) as raised:
            await linkedin.list_campaigns(ACCOUNT_ID)
    _assert_defect(raised.value, "ad_campaigns", "page token did not advance")
    assert len(server.requests) == 3


@pytest.mark.anyio
async def test_a_search_cut_short_by_a_transient_failure_is_incomplete(
    captured: list[EventDict],
) -> None:
    """A page that fails on every attempt ends the search: rows kept, ``complete`` false.

    Only a complete listing may drive ``missing`` discrepancies, so the reason and the
    interruption are both recorded and the listing is logged at warning.
    """
    server = Server(_ok(_cursor_page("ad_campaigns", [_campaign(1)], "token-a")), *_outage())
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_campaigns(ACCOUNT_ID)
    assert [row.external_id for row in listing.rows] == ["1"]
    assert listing.complete is False
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.reason == listing.interrupted_by.reason
    assert "ad_campaigns" in listing.reason
    assert listing.calls == 2
    (event,) = _listing_events(captured, "ad_campaigns")
    assert (event["log_level"], event["complete"], event["rows"]) == ("warning", False, 1)


@pytest.mark.anyio
async def test_cursor_paging_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cursor that never ends is cut off at the page bound with a defect."""
    monkeypatch.setattr(client_module, "_MAX_CURSOR_PAGES", 2)
    server = Server(
        _ok(_cursor_page("ad_campaigns", [_campaign(1)], "token-a")),
        _ok(_cursor_page("ad_campaigns", [_campaign(2)], "token-b")),
    )
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect, match="more than 2 pages in one search"):
            await linkedin.list_campaigns(ACCOUNT_ID)
    assert len(server.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"elements": [], "metadata": []}, "field 'metadata' is not a JSON object"),
        ({"metadata": {}}, "no 'elements' list"),
        ([], "not a JSON object"),
    ],
)
async def test_cursor_paging_rejects_a_malformed_page(body: object, fragment: str) -> None:
    """A page that breaks the cursor contract is a defect, never an empty or ended search."""
    server = Server(_ok(body))
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect, match=fragment):
            await linkedin.list_campaigns(ACCOUNT_ID)


# ---------------------------------------------------------------------------------------------
# Offset paging (_offset_pages)
# ---------------------------------------------------------------------------------------------


async def _offset_run(
    server: Server,
    *,
    page_size: int,
    max_pages: int | None,
) -> tuple[tuple[bool, str | None, TransientError | None], list[list[Any]], _Tally]:
    """Run ``_offset_pages`` over the ``lead_forms`` finder and collect each consumed page."""
    consumed: list[list[Any]] = []
    tally = _Tally()
    async with _linkedin(server) as linkedin:
        outcome = await linkedin._offset_pages(
            "lead_forms",
            "leadForms",
            [("q", "owner"), ("owner", _owner(ACCOUNT_URN))],
            (),
            page_size=page_size,
            max_pages=max_pages,
            tally=tally,
            consume=consumed.append,
        )
    return outcome, consumed, tally


@pytest.mark.anyio
async def test_offset_pages_rebuild_each_page_from_the_template() -> None:
    """Pages send ``head``, ``count``, ``start``, ``tail`` in order, ``start`` += page size.

    Paging stops on a page shorter than the page size with no ``next`` link.
    """
    window = "(start:1788220800000,end:1788224400000)"
    head = [
        ("q", "owner"),
        ("owner", _owner(ACCOUNT_URN)),
        ("leadType", "(leadType:SPONSORED)"),
        ("limitedToTestLeads", "false"),
        ("submittedAtTimeRange", window),
    ]
    lead: dict[str, Any] = _sample("lead_form_responses")["elements"][0]
    surface = "lead_form_responses"
    server = Server(
        _ok(_offset_page(surface, [lead, lead], start=0, count=2, has_next=False)),
        _ok(_offset_page(surface, [lead, lead], start=2, count=2, has_next=False)),
        _ok(_offset_page(surface, [lead], start=4, count=2, has_next=False)),
    )
    consumed: list[list[Any]] = []
    tally = _Tally()
    async with _linkedin(server) as linkedin:
        outcome = await linkedin._offset_pages(
            surface,
            "leadFormResponses",
            head,
            [("fields", LEAD_FIELDS)],
            page_size=2,
            max_pages=None,
            tally=tally,
            consume=consumed.append,
        )
    assert outcome == (True, None, None)
    assert [len(page) for page in consumed] == [2, 2, 1]
    assert tally.calls == 3
    for start, request in zip((0, 2, 4), server.requests, strict=True):
        _assert_contract_request(surface, request)
        assert _relative(request) == _contract_url(
            surface,
            acct=encode_urn(ACCOUNT_URN),
            start_ms="1788220800000",
            end_ms="1788224400000",
            count="2",
            start=str(start),
            fields=LEAD_FIELDS,
        )


@pytest.mark.anyio
async def test_offset_pages_continue_a_short_page_that_links_to_a_next_page() -> None:
    """A short page with a ``next`` link is not the last; an empty page always is."""
    server = Server(
        _ok(
            _offset_page(
                "lead_forms", [_lead_form(1), _lead_form(2)], start=0, count=3, has_next=True
            )
        ),
        _ok(_offset_page("lead_forms", [], start=3, count=3, has_next=True)),
    )
    outcome, consumed, tally = await _offset_run(server, page_size=3, max_pages=None)
    assert outcome == (True, None, None)
    assert [len(page) for page in consumed] == [2, 0]
    assert tally.calls == 2
    assert [dict(_query_pairs(r))["start"] for r in server.requests] == ["0", "3"]
    for request in server.requests:
        _assert_contract_request("lead_forms", request)


@pytest.mark.anyio
async def test_offset_pages_stop_at_max_pages_with_reason_page_limit() -> None:
    """A full page at ``max_pages`` stops the listing as incomplete with "page limit"."""
    server = Server(
        _ok(_offset_page("lead_forms", [_lead_form(1)], start=0, count=1, has_next=False))
    )
    outcome, consumed, tally = await _offset_run(server, page_size=1, max_pages=1)
    assert outcome == (False, "page limit", None)
    assert len(consumed) == 1
    assert tally.calls == 1


@pytest.mark.anyio
async def test_offset_pages_a_last_page_at_max_pages_is_complete() -> None:
    """When the page at ``max_pages`` is also the last, the listing is complete."""
    server = Server(
        _ok(_offset_page("lead_forms", [_lead_form(1)], start=0, count=2, has_next=False))
    )
    outcome, _, _ = await _offset_run(server, page_size=2, max_pages=1)
    assert outcome == (True, None, None)


@pytest.mark.anyio
async def test_offset_pages_keep_consumed_pages_before_a_transient_failure() -> None:
    """A transient failure ends the listing incomplete; earlier pages were already consumed."""
    server = Server(
        _ok(_offset_page("lead_forms", [_lead_form(1)], start=0, count=1, has_next=False)),
        *_outage(),
    )
    (complete, reason, interrupted), consumed, tally = await _offset_run(
        server, page_size=1, max_pages=None
    )
    assert complete is False
    assert isinstance(interrupted, TransientError)
    assert reason == interrupted.reason
    assert len(consumed) == 1
    assert tally.calls == 2


@pytest.mark.anyio
async def test_offset_pages_without_max_pages_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finder that keeps returning full pages is cut off at the page bound with a defect."""
    monkeypatch.setattr(client_module, "_MAX_OFFSET_PAGES", 2)
    server = Server(
        _ok(_offset_page("lead_forms", [_lead_form(1)], start=0, count=1, has_next=False)),
        _ok(_offset_page("lead_forms", [_lead_form(2)], start=1, count=1, has_next=False)),
    )
    with pytest.raises(QueryDefect, match="more than 2 pages in one listing"):
        await _offset_run(server, page_size=1, max_pages=None)
    assert len(server.requests) == 2


@pytest.mark.anyio
async def test_offset_pages_reject_a_malformed_paging_block() -> None:
    """A short page whose ``paging`` block is malformed is a defect."""
    server = Server(_ok({"elements": [_lead_form(1)], "paging": []}))
    with pytest.raises(QueryDefect, match="field 'paging' is not a JSON object"):
        await _offset_run(server, page_size=2, max_pages=None)


@pytest.mark.anyio
async def test_offset_pages_validate_the_envelope_before_consuming_a_page() -> None:
    """A second page answering offset 0 again is a defect, and its elements are never consumed."""
    forms = [_lead_form(1), _lead_form(2)]
    server = Server(
        _ok(_offset_page("lead_forms", forms, start=0, count=2, has_next=False)),
        _ok(_offset_page("lead_forms", forms, start=0, count=2, has_next=False)),
    )
    consumed: list[list[Any]] = []
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect) as raised:
            await linkedin._offset_pages(
                "lead_forms",
                "leadForms",
                [("q", "owner"), ("owner", _owner(ACCOUNT_URN))],
                (),
                page_size=2,
                max_pages=None,
                tally=_Tally(),
                consume=consumed.append,
            )
    _assert_defect(raised.value, "lead_forms", "field 'paging.start' does not match")
    assert len(consumed) == 1
    assert [dict(_query_pairs(r))["start"] for r in server.requests] == ["0", "2"]


_LEAD_RANGE = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC))
_OFFSET_SURFACES = ("lead_forms", "lead_form_responses")


def _lead_response(index: int) -> dict[str, Any]:
    """Return the documented lead response renumbered to ``index``."""
    element: dict[str, Any] = _sample("lead_form_responses")["elements"][0]
    element["id"] = f"aaaabbbb-0000-cccc-1111-dddd2222eeee-{index}"
    return element


def _offset_element(surface: str, index: int) -> dict[str, Any]:
    """Return element ``index`` of an offset listing on ``surface``."""
    return _lead_form(index) if surface == "lead_forms" else _lead_response(index)


async def _list_offset(
    surface: str, server: Server, *, page_size: int
) -> tuple[bool, str | None, int, int]:
    """Run ``list_lead_forms`` or ``list_lead_responses``; return completeness, rows and calls."""
    async with _linkedin(server) as linkedin:
        if surface == "lead_forms":
            forms = await linkedin.list_lead_forms(ACCOUNT_ID, page_size=page_size)
            return forms.complete, forms.reason, len(forms.rows), forms.calls
        leads = await linkedin.list_lead_responses(
            ACCOUNT_ID, _LEAD_RANGE, known_versions={}, page_size=page_size
        )
        return leads.complete, leads.reason, len(leads.leads), leads.calls


@pytest.mark.anyio
@pytest.mark.parametrize("surface", _OFFSET_SURFACES)
async def test_lead_listings_reject_an_empty_page_without_paging(surface: str) -> None:
    """``{"elements": []}`` breaks the required ``paging`` block: a defect, not an empty listing."""
    server = Server(_ok({"elements": []}))
    with pytest.raises(QueryDefect) as raised:
        await _list_offset(surface, server, page_size=LEAD_PAGE_SIZE)
    _assert_defect(raised.value, surface, "the response has no 'paging' object")
    assert len(server.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("surface", _OFFSET_SURFACES)
@pytest.mark.parametrize(
    ("paging", "fragment"),
    [
        pytest.param(None, "the response has no 'paging' object", id="paging-absent"),
        pytest.param([], "field 'paging' is not a JSON object", id="paging-list"),
        pytest.param("0", "field 'paging' is not a JSON object", id="paging-string"),
        pytest.param({"count": 2}, "field 'paging.start' is missing", id="start-missing"),
        pytest.param(
            {"start": "0", "count": 2}, "field 'paging.start' is not an integer", id="start-str"
        ),
        pytest.param(
            {"start": False, "count": 2}, "field 'paging.start' is not an integer", id="start-bool"
        ),
        pytest.param({"start": 0}, "field 'paging.count' is missing", id="count-missing"),
        pytest.param(
            {"start": 0, "count": 100},
            "field 'paging.count' does not match the requested count",
            id="count-mismatch",
        ),
        pytest.param(
            {"start": 0, "count": 2, "total": -1},
            "field 'paging.total' is negative",
            id="total-negative",
        ),
        pytest.param(
            {"start": 0, "count": 2, "total": "1"},
            "field 'paging.total' is not an integer",
            id="total-str",
        ),
        pytest.param(
            {"start": 0, "count": 2, "links": {}},
            "field 'paging.links' is not a list",
            id="links-not-a-list",
        ),
        pytest.param(
            {"start": 0, "count": 2, "links": [{"rel": "next"}]},
            "an item of 'paging.links' has no string 'href'",
            id="link-without-href",
        ),
        pytest.param(
            {"start": 0, "count": 2, "links": [{"href": "/rest/leadForms?start=2"}]},
            "an item of 'paging.links' has no string 'rel'",
            id="link-without-rel",
        ),
        pytest.param(
            {"start": 0, "count": 2, "links": [{"rel": ["next"], "href": "/rest/leadForms"}]},
            "an item of 'paging.links' has no string 'rel'",
            id="link-rel-not-a-string",
        ),
        pytest.param(
            {
                "start": 0,
                "count": 2,
                "links": [{"rel": "next", "href": "https://evil.example/rest/leadForms?start=2"}],
            },
            "a 'next' link points outside https://api.linkedin.com",
            id="next-href-off-host",
        ),
    ],
)
async def test_lead_listings_reject_a_broken_paging_envelope_on_a_short_page(
    surface: str, paging: object, fragment: str
) -> None:
    """A short page whose envelope breaks its contract is a defect, never the listing's end."""
    body: dict[str, Any] = {"elements": [_offset_element(surface, 1)]}
    if paging is not None:
        body["paging"] = paging
    server = Server(_ok(body))
    with pytest.raises(QueryDefect) as raised:
        await _list_offset(surface, server, page_size=2)
    _assert_defect(raised.value, surface, fragment)
    assert len(server.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("surface", _OFFSET_SURFACES)
@pytest.mark.parametrize(("href", "fragment"), _rejected_next_links("leadForms"))
async def test_lead_listings_reject_an_unusable_next_link(
    surface: str, href: str, fragment: str
) -> None:
    """An HTTP 200 page whose only flaw is its ``next`` href is a defect, never a ``ValueError``.

    The body is an empty first page with a contract envelope, the shape of the review's
    reproduction, so the listing is never reported complete.
    """
    link = {"rel": "next", "href": href}
    paging = {"start": 0, "count": LEAD_PAGE_SIZE, "links": [link]}
    server = Server(_ok(_conforming(surface, {"elements": [], "paging": paging})))
    with pytest.raises(QueryDefect) as raised:
        await _list_offset(surface, server, page_size=LEAD_PAGE_SIZE)
    _assert_defect(raised.value, surface, fragment)
    assert len(server.requests) == 1
    _assert_contract_request(surface, server.requests[0])


@pytest.mark.anyio
@pytest.mark.parametrize("surface", _OFFSET_SURFACES)
async def test_lead_listings_reject_a_second_page_answering_the_first_offset(surface: str) -> None:
    """Page 2 answered with ``start=0`` is a defect: the listing is not reported complete."""
    first = [_offset_element(surface, 1), _offset_element(surface, 2)]
    server = Server(
        _ok(_offset_page(surface, first, start=0, count=2, has_next=False)),
        _ok(_offset_page(surface, [_offset_element(surface, 3)], start=0, count=2, has_next=False)),
    )
    with pytest.raises(QueryDefect) as raised:
        await _list_offset(surface, server, page_size=2)
    _assert_defect(raised.value, surface, "field 'paging.start' does not match the requested start")
    assert [dict(_query_pairs(r))["start"] for r in server.requests] == ["0", "2"]


@pytest.mark.anyio
@pytest.mark.parametrize("surface", _OFFSET_SURFACES)
@pytest.mark.parametrize(
    "paging",
    [
        pytest.param({"start": 0, "count": 2}, id="no-total-no-links"),
        pytest.param({"start": 0, "count": 2, "total": 1, "links": []}, id="total-and-links"),
    ],
)
async def test_lead_listings_end_on_a_valid_short_page(
    surface: str, paging: dict[str, Any]
) -> None:
    """A contract envelope on a short page without ``next`` ends the listing complete."""
    body = _conforming(surface, {"elements": [_offset_element(surface, 1)], "paging": paging})
    server = Server(_ok(body))
    assert await _list_offset(surface, server, page_size=2) == (True, None, 1, 1)
    _assert_contract_request(surface, server.requests[0])


@pytest.mark.anyio
@pytest.mark.parametrize("surface", _OFFSET_SURFACES)
@pytest.mark.parametrize(
    "href",
    [
        pytest.param("/rest/leadForms?count=2&start=2", id="relative"),
        pytest.param("https://api.linkedin.com/rest/leadFormResponses?start=2", id="absolute"),
    ],
)
async def test_lead_listings_read_on_after_a_short_page_with_a_valid_next_link(
    surface: str, href: str
) -> None:
    """A short page with a valid ``next`` link continues from the template; an empty page ends."""
    link = {"rel": "next", "href": href, "type": "application/json"}
    paging = {"start": 0, "count": 2, "total": 1, "links": [link]}
    first = _conforming(surface, {"elements": [_offset_element(surface, 1)], "paging": paging})
    server = Server(
        _ok(first),
        _ok(_offset_page(surface, [], start=2, count=2, has_next=False)),
    )
    assert await _list_offset(surface, server, page_size=2) == (True, None, 1, 2)
    assert [dict(_query_pairs(r))["start"] for r in server.requests] == ["0", "2"]
    for request in server.requests:
        _assert_contract_request(surface, request)


# ---------------------------------------------------------------------------------------------
# Ad account
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_ad_account_reads_the_account_and_remembers_its_currency(
    captured: list[EventDict],
) -> None:
    """The account probe parses the account; its currency serves later group listings.

    A bare campaign-group ``dailyBudget`` is then read in that currency with no second
    ``adAccounts`` call.
    """
    server = Server(
        _ok(_account(currency="EUR")),
        _ok(_cursor_page("ad_campaign_groups", [_group(7, dailyBudget="25.50")])),
    )
    async with _linkedin(server) as linkedin:
        account = await linkedin.get_ad_account(ACCOUNT_ID)
        groups = await linkedin.list_campaign_groups(ACCOUNT_ID)
    assert (account.external_id, account.urn, account.currency) == (ACCOUNT_ID, ACCOUNT_URN, "EUR")
    assert (account.name, account.raw_status, account.test) == ("Company A", "ACTIVE", False)
    assert [request.url.path for request in server.requests] == [
        f"/rest/adAccounts/{ACCOUNT_ID}",
        f"/rest/adAccounts/{ACCOUNT_ID}/adCampaignGroups",
    ]
    assert groups.rows[0].daily_budget == Money(amount=Decimal("25.50"), currency="EUR")
    assert groups.calls == 1
    (event,) = _listing_events(captured, "ad_account")
    assert (event["rows"], event["calls"], event["complete"]) == (1, 1, True)


@pytest.mark.anyio
async def test_get_ad_account_accepts_the_account_urn() -> None:
    """An account URN reads the same ``adAccounts/{id}`` path as its numeric id."""
    server = Server(_ok(_account()))
    async with _linkedin(server) as linkedin:
        account = await linkedin.get_ad_account(ACCOUNT_URN)
    assert account.urn == ACCOUNT_URN
    assert _relative(server.requests[0]) == f"adAccounts/{ACCOUNT_ID}"


@pytest.mark.anyio
async def test_get_ad_account_rejects_a_response_naming_another_account() -> None:
    """A response for another account is a defect, and its currency is not remembered."""
    server = Server(
        _ok(_account(account_id="123456")),
        _ok(_account(currency="USD")),
        _ok(_cursor_page("ad_campaign_groups", [_group(7)])),
    )
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect) as raised:
            await linkedin.get_ad_account(ACCOUNT_ID)
        groups = await linkedin.list_campaign_groups(ACCOUNT_ID)
    _assert_defect(raised.value, "ad_account", "names another account")
    assert [request.url.path for request in server.requests] == [
        f"/rest/adAccounts/{ACCOUNT_ID}",
        f"/rest/adAccounts/{ACCOUNT_ID}",
        f"/rest/adAccounts/{ACCOUNT_ID}/adCampaignGroups",
    ]
    assert groups.calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize("account_id", ["", "abc", "12a", "urn:li:sponsoredCampaign:1", "-1"])
async def test_entity_calls_reject_an_invalid_account_id_before_any_request(
    account_id: str,
) -> None:
    """An id that is neither numeric nor an account URN fails before anything is sent."""
    server = Server()
    async with _linkedin(server) as linkedin:
        for call in (
            linkedin.get_ad_account,
            linkedin.list_campaign_groups,
            linkedin.list_campaigns,
        ):
            with pytest.raises(ValueError, match="account_id must be a numeric id"):
                await call(account_id)
        with pytest.raises(ValueError, match="account_id must be a numeric id"):
            await linkedin.list_creatives(account_id, ["1"])
    assert server.requests == []


# ---------------------------------------------------------------------------------------------
# Campaign groups
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_campaign_groups_sends_the_contract_search_and_reads_every_page() -> None:
    """The seven-status search equals the contract URL; the documented token is sent back.

    The first page is the documented sample verbatim; the last carries the live ``paging``
    block and an empty ``metadata`` object.
    """
    sample = _sample("ad_campaign_groups")
    token = sample["metadata"]["nextPageToken"]
    last = _group(
        7,
        dailyBudget="25.50",
        totalBudget={"amount": "1000", "currencyCode": "USD"},
    )
    server = Server(
        _ok(_conforming("ad_campaign_groups", sample)),
        _ok(
            _cursor_page(
                "ad_campaign_groups", [last], paging={"start": 0, "count": 100, "links": []}
            )
        ),
    )
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_campaign_groups(ACCOUNT_ID, account_currency="USD")
    first, second = server.requests
    expected = _contract_url("ad_campaign_groups", id=ACCOUNT_ID)
    assert f"List({','.join(CAMPAIGN_GROUP_SEARCH_STATUSES)})" in expected
    assert _relative(first) == expected
    assert _relative(second) == f"{expected}&pageToken={token}"
    for request in server.requests:
        _assert_contract_request("ad_campaign_groups", request)
    assert [row.external_id for row in listing.rows] == ["603407684", "504293601", "7"]
    assert (listing.complete, listing.reason, listing.calls) == (True, None, 2)
    assert listing.rows[2].daily_budget == Money(amount=Decimal("25.50"), currency="USD")
    assert listing.rows[2].total_budget == Money(amount=Decimal(1000), currency="USD")


@pytest.mark.anyio
async def test_list_campaign_groups_reads_the_account_currency_when_unknown() -> None:
    """Without a known currency, one ``adAccounts`` call precedes the search."""
    server = Server(
        _ok(_account(currency="GBP")),
        _ok(_cursor_page("ad_campaign_groups", [_group(7, dailyBudget="10")])),
    )
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_campaign_groups(ACCOUNT_URN)
    account_request, search_request = server.requests
    _assert_contract_request("ad_account", account_request)
    _assert_contract_request("ad_campaign_groups", search_request)
    assert listing.rows[0].daily_budget == Money(amount=Decimal(10), currency="GBP")
    assert listing.calls == 2
    assert listing.complete


@pytest.mark.anyio
async def test_list_campaign_groups_cut_short_by_the_currency_lookup_is_incomplete() -> None:
    """A transient failure of the currency lookup returns an empty, incomplete listing."""
    server = Server(*_outage())
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_campaign_groups(ACCOUNT_ID)
    assert listing.rows == ()
    assert listing.complete is False
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.reason == listing.interrupted_by.reason
    assert listing.calls == 1
    assert {request.url.path for request in server.requests} == {f"/rest/adAccounts/{ACCOUNT_ID}"}


@pytest.mark.anyio
async def test_list_campaign_groups_keeps_the_first_of_repeated_groups(
    captured: list[EventDict],
) -> None:
    """A group repeated across pages is returned once, as first read, and the repeat counted."""
    server = Server(
        _ok(_cursor_page("ad_campaign_groups", [_group(1), _group(2, name="first")], "t1")),
        _ok(_cursor_page("ad_campaign_groups", [_group(2, name="second"), _group(3)])),
    )
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_campaign_groups(ACCOUNT_ID, account_currency="USD")
    assert [(row.external_id, row.name) for row in listing.rows] == [
        ("1", "Refresh group"),
        ("2", "first"),
        ("3", "Refresh group"),
    ]
    (event,) = _listing_events(captured, "ad_campaign_groups")
    assert (event["rows"], event["repeats_dropped"], event["calls"]) == (3, 1, 2)


@pytest.mark.anyio
async def test_list_campaign_groups_propagates_access_denied() -> None:
    """A 403 on the search propagates as access denied, never an empty listing."""
    server = Server(_error(403, "ACCESS_DENIED", "Not enough permissions"))
    async with _linkedin(server) as linkedin:
        with pytest.raises(AccessDenied):
            await linkedin.list_campaign_groups(ACCOUNT_ID, account_currency="USD")


# ---------------------------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_campaigns_checks_budgets_against_the_learned_currency() -> None:
    """A campaign budget in another currency than the probed account's is a defect."""
    server = Server(
        _ok(_account(currency="EUR")), _ok(_cursor_page("ad_campaigns", [_campaign(1)]))
    )
    async with _linkedin(server) as linkedin:
        await linkedin.get_ad_account(ACCOUNT_ID)
        with pytest.raises(QueryDefect, match="not in the ad account currency"):
            await linkedin.list_campaigns(ACCOUNT_ID)
    assert len(server.requests) == 2


@pytest.mark.anyio
async def test_list_campaigns_given_currency_is_checked_without_an_account_call() -> None:
    """A currency passed in is used directly: no ``adAccounts`` call and the budget checked."""
    server = Server(_ok(_cursor_page("ad_campaigns", [_campaign(1)])))
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect, match="not in the ad account currency"):
            await linkedin.list_campaigns(ACCOUNT_ID, account_currency="EUR")
    assert [request.url.path for request in server.requests] == [
        f"/rest/adAccounts/{ACCOUNT_ID}/adCampaigns"
    ]


# ---------------------------------------------------------------------------------------------
# Creatives
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_creatives_without_campaigns_makes_no_call() -> None:
    """An empty campaign list is an empty, complete listing: ``List()`` would mean all."""
    server = Server()
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_creatives(ACCOUNT_ID, [])
    assert (listing.rows, listing.complete, listing.reason, listing.calls) == ((), True, None, 0)
    assert server.requests == []


def _creatives_url(campaign_ids: Sequence[int]) -> str:
    """Render the ``creatives`` contract URL for the given campaign ids."""
    urns = ",".join(encode_urn(f"urn:li:sponsoredCampaign:{cid}") for cid in campaign_ids)
    return _contract_url("creatives", id=ACCOUNT_ID, campaign_urns=urns)


@pytest.mark.anyio
async def test_list_creatives_sends_distinct_campaigns_in_chunks_of_100() -> None:
    """Campaigns are de-duplicated, encoded once and sent 100 per request, each chunk paged."""
    ids = list(range(1, CREATIVE_CAMPAIGN_CHUNK + 2))
    campaigns = [str(cid) for cid in ids] + ["urn:li:sponsoredCampaign:1", "2"]
    server = Server(
        _ok(_cursor_page("creatives", [_creative(11, 1)], "chunk-1-token")),
        _ok(_cursor_page("creatives", [_creative(12, 2)])),
        _ok(_cursor_page("creatives", [_creative(13, ids[-1])])),
    )
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_creatives(ACCOUNT_URN, campaigns)
    first, second, third = server.requests
    assert _relative(first) == _creatives_url(ids[:CREATIVE_CAMPAIGN_CHUNK])
    assert _relative(second) == (
        f"{_creatives_url(ids[:CREATIVE_CAMPAIGN_CHUNK])}&pageToken=chunk-1-token"
    )
    assert _relative(third) == _creatives_url(ids[CREATIVE_CAMPAIGN_CHUNK:])
    assert f"List({','.join(CREATIVE_INTENDED_STATUSES)})" in _relative(first)
    for request in server.requests:
        _assert_contract_request("creatives", request)
    assert [row.urn for row in listing.rows] == [
        "urn:li:sponsoredCreative:11",
        "urn:li:sponsoredCreative:12",
        "urn:li:sponsoredCreative:13",
    ]
    assert listing.rows[0].post_urn == "urn:li:share:6778045555198214144"
    assert (listing.complete, listing.calls) == (True, 3)


@pytest.mark.anyio
async def test_list_creatives_stops_at_the_first_interrupted_chunk() -> None:
    """A chunk cut short ends the listing: later chunks are not read and it is incomplete."""
    ids = list(range(1, CREATIVE_CAMPAIGN_CHUNK + 2))
    server = Server(_ok(_cursor_page("creatives", [_creative(11, 1)], "chunk-1-token")), *_outage())
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_creatives(ACCOUNT_ID, [str(cid) for cid in ids])
    assert [row.urn for row in listing.rows] == ["urn:li:sponsoredCreative:11"]
    assert listing.complete is False
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.reason == listing.interrupted_by.reason
    assert listing.calls == 2
    assert len(server.requests) == 1 + RetryPolicy().attempts
    assert all("pageToken=chunk-1-token" in _relative(r) for r in server.requests[1:])


@pytest.mark.anyio
async def test_list_creatives_keeps_one_row_per_creative_across_chunks() -> None:
    """A creative returned by two chunks is listed once."""
    ids = list(range(1, CREATIVE_CAMPAIGN_CHUNK + 2))
    server = Server(
        _ok(_cursor_page("creatives", [_creative(11, 1)])),
        _ok(_cursor_page("creatives", [_creative(11, 1), _creative(12, ids[-1])])),
    )
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_creatives(ACCOUNT_ID, [str(cid) for cid in ids])
    assert [row.urn for row in listing.rows] == [
        "urn:li:sponsoredCreative:11",
        "urn:li:sponsoredCreative:12",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("campaign", ["abc", "urn:li:sponsoredCreative:1", ""])
async def test_list_creatives_rejects_an_invalid_campaign_id(campaign: str) -> None:
    """A campaign that is neither numeric nor a campaign URN fails before any request."""
    server = Server()
    async with _linkedin(server) as linkedin:
        with pytest.raises(ValueError, match="campaign_id must be a numeric id"):
            await linkedin.list_creatives(ACCOUNT_ID, ["1", campaign])
    assert server.requests == []


# ---------------------------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_posts_batch_gets_distinct_post_urns_with_the_method_header(
    captured: list[EventDict],
) -> None:
    """Only share and ugcPost URNs are requested, once each, with ``X-RestLi-Method``.

    Result keys are decoded whether LinkedIn returns them raw or percent-encoded.
    """
    share = "urn:li:share:6844785523593134080"
    ugc = "urn:li:ugcPost:6927151919538794496"
    urns = [share, ugc, share, "urn:li:adInMailContent:3", "urn:li:share:abc"]
    body = _posts_body(
        {
            share: _post(share, title="  Download the guide  "),
            encode_urn(ugc): _post(ugc, commentary="  "),
        }
    )
    server = Server(_ok(body))
    async with _linkedin(server) as linkedin:
        batch = await linkedin.get_posts(urns)
    (request,) = server.requests
    _assert_contract_request("posts", request)
    assert _relative(request) == _contract_url(
        "posts", post_urns=f"{encode_urn(share)},{encode_urn(ugc)}"
    )
    assert request.headers["X-RestLi-Method"] == "BATCH_GET"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert batch.posts == {
        share: PostRow(urn=share, headline="Download the guide", intro_text="Intro text"),
        ugc: PostRow(urn=ugc, headline=None, intro_text=None),
    }
    assert (batch.failed, batch.complete, batch.reason, batch.calls) == (
        frozenset(),
        True,
        None,
        1,
    )
    assert batch.interrupted_by is None
    (event,) = _listing_events(captured, "posts")
    assert (event["rows"], event["failed"], event["skipped"]) == (2, 0, 2)


@pytest.mark.anyio
async def test_get_posts_fails_keys_in_errors_with_bad_statuses_or_absent() -> None:
    """A per-key 403 inside HTTP 200, an ``errors`` entry or an absent key is a failed read."""
    urns = [f"urn:li:share:{n}" for n in range(1, 5)]
    body = _posts_body(
        {urns[0]: _post(urns[0]), urns[1]: _post(urns[1])},
        statuses={urns[0]: 200, urns[1]: 403},
        errors={urns[2]: {"status": 403, "message": "Not enough permissions"}},
    )
    server = Server(_ok(body))
    async with _linkedin(server) as linkedin:
        batch = await linkedin.get_posts(urns)
    assert set(batch.posts) == {urns[0]}
    assert batch.failed == frozenset(urns[1:])
    assert batch.complete


@pytest.mark.anyio
async def test_get_posts_sends_at_most_50_urns_and_fails_every_unread_chunk() -> None:
    """URNs go 50 per call; a transient failure fails that chunk and every later one."""
    urns = [f"urn:li:share:{n}" for n in range(1, 2 * POST_BATCH_SIZE + 21)]
    first_chunk = urns[:POST_BATCH_SIZE]
    server = Server(_ok(_posts_body({urn: _post(urn) for urn in first_chunk})), *_outage())
    async with _linkedin(server) as linkedin:
        batch = await linkedin.get_posts(urns)
    assert set(batch.posts) == set(first_chunk)
    assert batch.failed == frozenset(urns[POST_BATCH_SIZE:])
    assert batch.complete is False
    assert isinstance(batch.interrupted_by, TransientError)
    assert batch.reason == batch.interrupted_by.reason
    assert batch.calls == 2
    assert len(server.requests) == 1 + RetryPolicy().attempts
    first_request = server.requests[0]
    assert _relative(first_request) == _contract_url(
        "posts", post_urns=",".join(encode_urn(urn) for urn in first_chunk)
    )
    second_ids = dict(_query_pairs(server.requests[1]))["ids"]
    assert second_ids.count(",") == POST_BATCH_SIZE - 1


@pytest.mark.anyio
@pytest.mark.parametrize("urns", [[], ["urn:li:adInMailContent:1", "urn:li:share:x"]])
async def test_get_posts_without_post_urns_makes_no_call(urns: list[str]) -> None:
    """With no share or ugcPost URN to read, nothing is sent and the batch is complete."""
    server = Server()
    async with _linkedin(server) as linkedin:
        batch = await linkedin.get_posts(urns)
    assert (batch.posts, batch.failed, batch.complete, batch.calls) == ({}, frozenset(), True, 0)
    assert server.requests == []


@pytest.mark.anyio
async def test_get_posts_propagates_an_http_403_on_the_whole_call() -> None:
    """An HTTP-level 403 (no ``r_organization_social``) is access denied, never swallowed."""
    server = Server(_error(403, "ACCESS_DENIED", "Not enough permissions to access: posts"))
    async with _linkedin(server) as linkedin:
        with pytest.raises(AccessDenied) as raised:
            await linkedin.get_posts(["urn:li:share:1"])
    assert raised.value.endpoint == "posts"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"results": []}, "no 'results' object"),
        ([], "not a JSON object"),
        ({"results": {}, "errors": []}, "field 'errors' is not a JSON object"),
    ],
)
async def test_get_posts_rejects_a_malformed_envelope(body: object, fragment: str) -> None:
    """A batch response that breaks the envelope is a defect, never an all-failed batch."""
    server = Server(_ok(body))
    async with _linkedin(server) as linkedin:
        with pytest.raises(QueryDefect, match=fragment):
            await linkedin.get_posts(["urn:li:share:1"])


# ---------------------------------------------------------------------------------------------
# Listing assembly (_listing)
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_listing_assembly_lets_an_interruption_reason_win() -> None:
    """An interruption's reason wins over a result-limit reason; either makes it incomplete."""
    error = TransientError("down", source=SOURCE, endpoint="ad_analytics", http_status=503)
    async with _linkedin(Server()) as linkedin:
        interrupted = linkedin._listing(
            "ad_analytics", [1, 2, 2], _Tally(calls=3), error, key=lambda r: r, reason="limit"
        )
        capped = linkedin._listing(
            "ad_analytics", [1], _Tally(calls=1), None, key=lambda r: r, reason="limit"
        )
        complete = linkedin._listing("ad_analytics", [], _Tally(), None, key=lambda r: r)
    assert (interrupted.rows, interrupted.complete, interrupted.reason) == (
        (1, 2),
        False,
        error.reason,
    )
    assert (interrupted.calls, interrupted.interrupted_by) == (3, error)
    assert (capped.complete, capped.reason, capped.interrupted_by) == (False, "limit", None)
    assert (complete.rows, complete.complete, complete.reason, complete.calls) == (
        (),
        True,
        None,
        0,
    )


# ---------------------------------------------------------------------------------------------
# Capture-only bounds: every sync passes none, ``capture-fixtures`` passes 1
# ---------------------------------------------------------------------------------------------


def _analytics_chain() -> tuple[httpx.Response, ...]:
    """A fresh three-response ``next``-link chain over :data:`ANALYTICS_URL`."""
    return (
        _ok(_analytics_page([_daily_element(1)], f"/rest/{ANALYTICS_URL}&start=10")),
        _ok(_analytics_page([_daily_element(2)], f"/rest/{ANALYTICS_URL}&start=20")),
        _ok(_analytics_page([_daily_element(3)])),
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("max_pages", "requests", "limited"),
    [(1, 1, True), (2, 2, True), (3, 3, False), (None, 3, False)],
)
async def test_follow_reads_at_most_max_pages_responses(
    max_pages: int | None, requests: int, limited: bool
) -> None:
    """A bounded chain stops with its ``next`` link unread and says so; ``None`` reads on."""
    server = Server(*_analytics_chain()[:requests])
    tally = _Tally()
    async with _linkedin(server) as linkedin:
        pages = await linkedin._follow("ad_analytics", ANALYTICS_URL, tally, max_pages=max_pages)
    assert len(server.requests) == requests == tally.calls
    assert [e["impressions"] for e in pages.elements] == list(range(1, requests + 1))
    assert (pages.limited, pages.capped, pages.interrupted_by) == (limited, False, None)


@pytest.mark.anyio
async def test_follow_bound_on_a_response_without_a_next_link_is_not_limited() -> None:
    """A single response that ends the chain is complete under any bound."""
    server = Server(_ok(_analytics_page([_daily_element(1)])))
    async with _linkedin(server) as linkedin:
        pages = await linkedin._follow("ad_analytics", ANALYTICS_URL, _Tally(), max_pages=1)
    assert (len(server.requests), pages.limited) == (1, False)


def _entity_listing(
    surface: str, linkedin: LinkedInClient, max_pages: int | None
) -> Awaitable[Listing[Any]]:
    """Call the cursor listing of ``surface`` with ``max_pages``."""
    if surface == "ad_campaign_groups":
        return linkedin.list_campaign_groups(
            ACCOUNT_ID, account_currency="USD", max_pages=max_pages
        )
    return linkedin.list_campaigns(ACCOUNT_ID, max_pages=max_pages)


def _entity(surface: str, entity_id: int) -> dict[str, Any]:
    """One element of the cursor listing of ``surface``."""
    return _group(entity_id) if surface == "ad_campaign_groups" else _campaign(entity_id)


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["ad_campaign_groups", "ad_campaigns"])
async def test_entity_listings_read_at_most_max_pages(
    surface: str, captured: list[EventDict]
) -> None:
    """``max_pages=1`` sends one request; the unread page makes the listing "page limit".

    The request is the contract's first page, the rows read are kept, and the listing is
    incomplete without an interruption, so ``sync`` would infer nothing from it.
    """
    server = Server(_ok(_cursor_page(surface, [_entity(surface, 1), _entity(surface, 2)], "t-2")))
    async with _linkedin(server) as linkedin:
        listing = await _entity_listing(surface, linkedin, 1)
    (request,) = server.requests
    assert _relative(request) == _contract_url(surface, id=ACCOUNT_ID)
    _assert_contract_request(surface, request)
    assert [row.external_id for row in listing.rows] == ["1", "2"]
    assert (listing.complete, listing.reason, listing.calls) == (False, "page limit", 1)
    assert listing.interrupted_by is None
    (event,) = _listing_events(captured, surface)
    assert (event["complete"], event["reason"], event["calls"]) == (False, "page limit", 1)


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["ad_campaign_groups", "ad_campaigns"])
@pytest.mark.parametrize("max_pages", [2, 3, None])
async def test_entity_listings_within_their_bound_are_complete(
    surface: str, max_pages: int | None
) -> None:
    """A search that ends within the bound, or with no bound, reads every page and is complete."""
    server = Server(
        _ok(_cursor_page(surface, [_entity(surface, 1)], "t-2")),
        _ok(_cursor_page(surface, [_entity(surface, 2)])),
    )
    async with _linkedin(server) as linkedin:
        listing = await _entity_listing(surface, linkedin, max_pages)
    assert len(server.requests) == 2
    assert [row.external_id for row in listing.rows] == ["1", "2"]
    assert (listing.complete, listing.reason, listing.calls) == (True, None, 2)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("max_pages", "requests", "creatives", "reason"),
    [
        pytest.param(1, 1, ["11"], "page limit", id="first-page-only"),
        pytest.param(2, 2, ["11", "12"], "page limit", id="second-chunk-unread"),
        pytest.param(3, 3, ["11", "12", "13"], None, id="bound-not-reached"),
        pytest.param(None, 3, ["11", "12", "13"], None, id="unbounded"),
    ],
)
async def test_list_creatives_max_pages_counts_pages_across_chunks(
    max_pages: int | None, requests: int, creatives: list[str], reason: str | None
) -> None:
    """The bound is the total number of pages over every campaign chunk.

    Chunk 1 (100 campaigns) has two pages and chunk 2 one; a chunk or page left unread by
    the bound makes the listing incomplete with reason "page limit".
    """
    ids = list(range(1, CREATIVE_CAMPAIGN_CHUNK + 2))
    server = Server(
        *(
            _ok(_cursor_page("creatives", [_creative(11, 1)], "chunk-1-token")),
            _ok(_cursor_page("creatives", [_creative(12, 2)])),
            _ok(_cursor_page("creatives", [_creative(13, ids[-1])])),
        )[:requests]
    )
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_creatives(
            ACCOUNT_ID, [str(cid) for cid in ids], max_pages=max_pages
        )
    assert len(server.requests) == requests
    assert [row.urn.rpartition(":")[2] for row in listing.rows] == creatives
    assert (listing.complete, listing.reason, listing.calls) == (reason is None, reason, requests)


@pytest.mark.anyio
async def test_list_creatives_of_one_campaign_reads_one_page_under_the_capture_bound() -> None:
    """The capture's call (one campaign, ``max_pages=1``) is the contract's one request."""
    server = Server(_ok(_cursor_page("creatives", [_creative(11, 1)], "more")))
    async with _linkedin(server) as linkedin:
        listing = await linkedin.list_creatives(ACCOUNT_ID, ["1"], max_pages=1)
    (request,) = server.requests
    assert _relative(request) == _creatives_url([1])
    assert (listing.complete, listing.reason, len(listing.rows)) == (False, "page limit", 1)


@pytest.mark.anyio
async def test_analytics_daily_max_pages_leaves_the_next_link_unread() -> None:
    """One response is read; its rows are kept and the unread link makes it "page limit"."""
    day = date(2026, 9, 1)
    server = Server(*_analytics_chain()[:1])
    async with _linkedin(server) as linkedin:
        listing = await linkedin.analytics_daily(
            ACCOUNT_ID, (day, day), expected_creatives=1, max_pages=1
        )
    (request,) = server.requests
    assert _relative(request) == ANALYTICS_URL
    _assert_contract_request("ad_analytics", request)
    assert [row.impressions for row in listing.rows] == [1]
    assert (listing.complete, listing.reason, listing.calls) == (False, "page limit", 1)
    assert listing.interrupted_by is None


@pytest.mark.anyio
async def test_analytics_daily_without_a_bound_follows_every_next_link() -> None:
    """The default reads the whole chain, as every sync does."""
    day = date(2026, 9, 1)
    server = Server(*_analytics_chain())
    async with _linkedin(server) as linkedin:
        listing = await linkedin.analytics_daily(ACCOUNT_ID, (day, day), expected_creatives=1)
    assert len(server.requests) == 3
    assert [row.impressions for row in listing.rows] == [1, 2, 3]
    assert (listing.complete, listing.reason) == (True, None)


_BOUNDED_CALLS: list[tuple[str, Callable[[LinkedInClient, Any], Awaitable[object]]]] = [
    (
        "max_pages",
        lambda linkedin, bound: linkedin.list_campaign_groups(
            ACCOUNT_ID, account_currency="USD", max_pages=bound
        ),
    ),
    ("max_pages", lambda linkedin, bound: linkedin.list_campaigns(ACCOUNT_ID, max_pages=bound)),
    (
        "max_pages",
        lambda linkedin, bound: linkedin.list_creatives(ACCOUNT_ID, ["1"], max_pages=bound),
    ),
    (
        "max_pages",
        lambda linkedin, bound: linkedin.analytics_daily(
            ACCOUNT_ID, (date(2026, 9, 1), date(2026, 9, 1)), max_pages=bound
        ),
    ),
    (
        "max_pages",
        lambda linkedin, bound: linkedin.analytics_segments(
            ACCOUNT_ID, "MEMBER_SENIORITY", (date(2026, 9, 1), date(2026, 9, 1)), max_pages=bound
        ),
    ),
    (
        "max_campaigns",
        lambda linkedin, bound: linkedin.analytics_segments(
            ACCOUNT_ID,
            "MEMBER_SENIORITY",
            (date(2026, 9, 1), date(2026, 9, 1)),
            max_campaigns=bound,
        ),
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("name", "call"), _BOUNDED_CALLS)
@pytest.mark.parametrize("bound", [0, -1, True])
async def test_capture_bounds_reject_invalid_values_before_any_request(
    name: str, call: Callable[[LinkedInClient, Any], Awaitable[object]], bound: object
) -> None:
    """A bound below 1, or a ``bool``, is a ``ValueError`` and nothing is sent."""
    server = Server()
    async with _linkedin(server) as linkedin:
        with pytest.raises(ValueError, match=f"{name} must be at least 1 when given"):
            await call(linkedin, bound)
    assert server.requests == []
