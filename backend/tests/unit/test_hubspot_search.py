"""Behavioural tests for ``ads_engine.connectors.hubspot.search``: partitioned CRM Search.

Covers the filters (:class:`SearchFilter`, :func:`deal_search_filter`,
:func:`google_contacts_filter`), the request body (:func:`search_body`), the instant encoding
(:func:`epoch_ms`) and :func:`search_partitioned`: one-day partitioning of ``[start, end)``,
half-open ``GTE``/``LT`` epoch-millisecond filters, whole-hour halving of a partition whose
``total`` exceeds 10,000 (24 → 12 → 6 → 3 → 1 + 2 → 1 + 1), the one-hour partition read to
exactly 10,000 results and marked incomplete, transient failures that keep the rows already
read, propagation of ``AccessDenied`` and ``QueryDefect``, paging-contract breaches, argument
validation, and the ``hubspot.search`` log event.

Every page is served by an in-memory pager. :class:`ContractPager` answers the way the CRM
Search contract (``backend/tests/contracts/hubspot/crm_search.json``) documents: it checks
each request body against the contract's request schema and limits, filters its records by
the body's own filters, and renders each response from the contract's response schema
before decoding it with the production parser (``parse_search_page``). A request past the
10,000-result ceiling fails the test, because HubSpot rejects it with HTTP 400.
:class:`ScriptedPager` replays fixed pages for the breaches a well-behaved server never
produces. No test touches the network or sleeps.
"""

import functools
import json
from bisect import bisect_left
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from structlog.testing import capture_logs
from structlog.typing import EventDict

from ads_engine.connectors.errors import AccessDenied, QueryDefect, TransientError
from ads_engine.connectors.hubspot.parser import (
    DEAL_PROPERTIES,
    ContactPropertyMap,
    HubSpotRecord,
    SearchPage,
    parse_search_page,
)
from ads_engine.connectors.hubspot.search import (
    CEILING_REASON,
    DAY,
    MIN_PARTITION,
    PAGE_SIZE,
    RESULT_CEILING,
    SearchFilter,
    SearchOutcome,
    SearchPager,
    deal_search_filter,
    epoch_ms,
    google_contacts_filter,
    search_body,
    search_partitioned,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "hubspot" / "crm_search.json"

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_MS = timedelta(milliseconds=1)
HOUR = timedelta(hours=1)
DAY0 = datetime(2026, 9, 1, tzinfo=UTC)
EASTERN = timezone(timedelta(hours=-4), "EDT")
CAMPAIGN_PROPERTY = "google_campaign_id"
TOUCH_PROPERTY = "google_touch_date"
DEAL_ID_BASE = 41_000_000_000
CONTACT_ID_BASE = 52_000_000_000


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture
def captured() -> Iterator[list[EventDict]]:
    """Capture every structlog event as its raw event dict, before any redaction."""
    with capture_logs() as entries:
        yield entries


# --- contract and the conformance check -------------------------------------------------------


@functools.cache
def _contract() -> Mapping[str, Any]:
    """Load ``contracts/hubspot/crm_search.json``."""
    loaded: Mapping[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return loaded


def _request_schema() -> Mapping[str, Any]:
    """Return the contract's ``request.body`` schema."""
    schema: Mapping[str, Any] = _contract()["request"]["body"]
    return schema


def _response_schema() -> Mapping[str, Any]:
    """Return the contract's ``response.body`` schema."""
    schema: Mapping[str, Any] = _contract()["response"]["body"]
    return schema


def _limit(name: str) -> int:
    """Return one of the contract's ``request.limits``."""
    value: int = _contract()["request"]["limits"][name]
    return value


_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "null": lambda value: value is None,
}
_SCHEMA_KEYWORDS = frozenset(
    {"type", "description", "properties", "required", "additionalProperties", "enum", "items"}
)


def _violations(value: object, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """List every way ``value`` departs from a contract schema, or ``[]`` when it conforms.

    Only the JSON Schema keywords the HubSpot search contract uses are understood; any other
    keyword fails the test, so the check can never pass by ignoring a constraint.
    """
    unknown = set(schema) - _SCHEMA_KEYWORDS
    if unknown:
        pytest.fail(f"{path}: contract keywords {sorted(unknown)} are not checked here")
    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else list(declared)
        if not any(_JSON_TYPES[name](value) for name in names):
            return [f"{path}: expected {' or '.join(names)}"]
    found: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{path}: not one of the contract's enum values")
    if isinstance(value, Mapping):
        properties: Mapping[str, Any] = schema.get("properties", {})
        found.extend(
            f"{path}.{key}: required" for key in schema.get("required", ()) if key not in value
        )
        for key, item in value.items():
            if key in properties:
                found.extend(_violations(item, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is not True:
                found.append(f"{path}.{key}: not defined by the contract")
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            found.extend(_violations(item, schema["items"], f"{path}[{index}]"))
    return found


def _page(records: Sequence[HubSpotRecord], *, total: int, after: str | None = None) -> SearchPage:
    """Render a contract-conformant response body and decode it with the production parser."""
    body: dict[str, Any] = {"total": total, "results": [dict(record) for record in records]}
    if after is not None:
        body["paging"] = {"next": {"after": after}}
    assert _violations(body, _response_schema()) == []
    return parse_search_page(body)


def _raw_page(
    records: Sequence[HubSpotRecord], *, total: int, after: str | None = None
) -> SearchPage:
    """Build a page the parser would reject, to prove ``search.py`` defends on its own."""
    return SearchPage(results=tuple(records), total=total, after=after)


# --- records ------------------------------------------------------------------------------------


def _wire_ms(instant: datetime) -> int:
    """Epoch milliseconds of a whole-millisecond instant, computed independently of the module."""
    return (instant - EPOCH) // ONE_MS


def _iso(instant: datetime) -> str:
    """Render an instant the way HubSpot returns datetime properties."""
    return instant.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _at(hours: float) -> datetime:
    """Return the instant ``hours`` after midnight UTC of the first test day."""
    return DAY0 + timedelta(hours=hours)


def _deal(index: int, created: datetime) -> dict[str, Any]:
    """Build one CRM Search deal result in the contract's response shape."""
    stamp = _iso(created)
    return {
        "id": str(DEAL_ID_BASE + index),
        "properties": {
            "createdate": stamp,
            "dealstage": "appointmentscheduled",
            "pipeline": "default",
            "amount": "1500.00",
            "hs_object_id": str(DEAL_ID_BASE + index),
        },
        "createdAt": stamp,
        "updatedAt": stamp,
        "archived": False,
    }


def _deals_between(lo: datetime, hi: datetime, count: int, *, first: int = 0) -> list[Any]:
    """Build ``count`` deals created at even whole-millisecond steps across ``[lo, hi)``."""
    step = (hi - lo) // count
    step -= step % ONE_MS
    assert step > timedelta(0)
    return [_deal(first + offset, lo + offset * step) for offset in range(count)]


def _contact(index: int, touched: datetime, *, campaign: str | None) -> dict[str, Any]:
    """Build one CRM Search contact result carrying the configured Google properties."""
    stamp = _iso(touched)
    return {
        "id": str(CONTACT_ID_BASE + index),
        "properties": {
            "email": f"contact{index}@example.com",
            "lifecyclestage": "lead",
            TOUCH_PROPERTY: stamp,
            CAMPAIGN_PROPERTY: campaign,
            "hs_object_id": str(CONTACT_ID_BASE + index),
        },
        "createdAt": stamp,
        "updatedAt": stamp,
        "archived": False,
    }


def _ids(outcome: SearchOutcome) -> list[str]:
    """Return the ids of an outcome's rows, in order."""
    return [str(row["id"]) for row in outcome.rows]


def _transient() -> TransientError:
    """A rate-limit failure that survived the client's retries."""
    return TransientError(
        "Too many requests",
        source="hubspot",
        endpoint="crm_search",
        code="RATE_LIMIT",
        http_status=429,
        retry_after=1.0,
    )


# --- pagers -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """A request as :class:`ContractPager` read it, before it was answered."""

    index: int
    lo: datetime
    hi: datetime
    offset: int


@dataclass(frozen=True)
class Served:
    """One request :class:`ContractPager` answered."""

    body: Mapping[str, Any]
    lo: datetime
    hi: datetime
    offset: int
    limit: int
    total: int
    ids: tuple[str, ...]
    next_after: str | None

    @property
    def span(self) -> tuple[datetime, datetime]:
        """The partition the request filtered on."""
        return (self.lo, self.hi)


class ContractPager:
    """An in-memory CRM Search endpoint that enforces ``crm_search.json``.

    Records are matched by the request's own filters (``GTE``/``LT`` on the range property,
    ``HAS_PROPERTY`` on any other), sorted ascending by the range property, and paged with
    numeric ``after`` offsets, as HubSpot pages. ``total`` is the match count unless
    ``total_for`` overrides it; ``fail_on`` may raise instead of answering; ``max_page`` caps
    the records per page below the requested ``limit``; ``trailing_cursor`` keeps a cursor on
    the last page, which HubSpot does when that page ends exactly on the ceiling.
    """

    def __init__(
        self,
        records: Sequence[HubSpotRecord],
        *,
        object_type: str = "deals",
        range_property: str = "createdate",
        max_page: int | None = None,
        trailing_cursor: bool = False,
        total_for: Callable[[Call, int], int] | None = None,
        fail_on: Callable[[Call], BaseException | None] | None = None,
    ) -> None:
        keyed = sorted(
            (
                (_wire_ms(datetime.fromisoformat(record["properties"][range_property])), record)
                for record in records
            ),
            key=lambda item: (item[0], str(item[1]["id"])),
        )
        self._keys = [key for key, _ in keyed]
        self._records = [record for _, record in keyed]
        self._object_type = object_type
        self._range_property = range_property
        self._max_page = max_page
        self._trailing_cursor = trailing_cursor
        self._total_for = total_for
        self._fail_on = fail_on
        self.attempts = 0
        self.served: list[Served] = []

    async def search_page(self, object_type: str, body: Mapping[str, object]) -> SearchPage:
        """Check the request against the contract, then answer it from the records."""
        index = self.attempts
        self.attempts += 1
        serialised = json.dumps(body)
        request: dict[str, Any] = json.loads(serialised)
        if object_type != self._object_type:
            pytest.fail(f"request for {object_type!r}, expected {self._object_type!r}")
        violations = _violations(request, _request_schema())
        if violations:
            pytest.fail(f"request breaks the contract: {violations}")
        if len(serialised) >= _limit("max_body_chars"):
            pytest.fail(f"request body has {len(serialised)} characters")
        groups = request["filterGroups"]
        if not 1 <= len(groups) <= _limit("max_filter_groups"):
            pytest.fail(f"request carries {len(groups)} filter groups")
        filters = groups[0]["filters"]
        if len(filters) > _limit("max_filters_per_group"):
            pytest.fail(f"filter group holds {len(filters)} filters")
        lo_ms, hi_ms, required = self._read_filters(filters)
        if request["sorts"] != [{"propertyName": self._range_property, "direction": "ASCENDING"}]:
            pytest.fail(f"this server sorts only by the range property: {request['sorts']}")
        limit: int = request["limit"]
        if not 1 <= limit <= _limit("page_size"):
            pytest.fail(f"limit {limit} is outside 1 to {_limit('page_size')}")
        after = request.get("after")
        if after is not None and not after.isdigit():
            pytest.fail(f"request carries a cursor this server never issued: {after!r}")
        offset = int(after) if after is not None else 0
        if offset + limit > RESULT_CEILING:
            pytest.fail(f"request pages past the 10,000-result ceiling: {offset} + {limit}")
        lo, hi = EPOCH + lo_ms * ONE_MS, EPOCH + hi_ms * ONE_MS
        call = Call(index=index, lo=lo, hi=hi, offset=offset)
        if self._fail_on is not None:
            error = self._fail_on(call)
            if error is not None:
                raise error
        window = self._records[bisect_left(self._keys, lo_ms) : bisect_left(self._keys, hi_ms)]
        matched = [
            record
            for record in window
            if all(record["properties"].get(name) not in (None, "") for name in required)
        ]
        total = len(matched) if self._total_for is None else self._total_for(call, len(matched))
        size = limit if self._max_page is None else min(limit, self._max_page)
        results = matched[offset : offset + size]
        end = offset + len(results)
        next_after: str | None = None
        if end < len(matched) or (self._trailing_cursor and results):
            next_after = str(end)
        page = _page(results, total=total, after=next_after)
        self.served.append(
            Served(
                body=request,
                lo=lo,
                hi=hi,
                offset=offset,
                limit=limit,
                total=total,
                ids=tuple(str(record["id"]) for record in results),
                next_after=next_after,
            )
        )
        return page

    def _read_filters(self, filters: Sequence[Mapping[str, Any]]) -> tuple[int, int, list[str]]:
        """Return the range bounds and the ``HAS_PROPERTY`` names of one filter group."""
        if len(filters) < 2:
            pytest.fail("a partition request needs its two range filters")
        low, high, *extra = filters
        shape = [(low["operator"], low["propertyName"]), (high["operator"], high["propertyName"])]
        if shape != [("GTE", self._range_property), ("LT", self._range_property)]:
            pytest.fail(f"range filters out of shape: {low}, {high}")
        for bound in (low, high):
            if set(bound) != {"propertyName", "operator", "value"} or not bound["value"].isdigit():
                pytest.fail(f"range filter value is not epoch milliseconds: {bound}")
        required: list[str] = []
        for item in extra:
            if item["operator"] != "HAS_PROPERTY" or set(item) != {"propertyName", "operator"}:
                pytest.fail(f"this server evaluates only HAS_PROPERTY extras, got {item}")
            required.append(item["propertyName"])
        return int(low["value"]), int(high["value"]), required

    def first_pages(self) -> list[tuple[datetime, datetime]]:
        """Return the partition of every first-page request, in request order."""
        return [served.span for served in self.served if served.offset == 0]


Step = SearchPage | BaseException


class ScriptedPager:
    """Replays fixed pages or errors in order, recording each request body."""

    def __init__(self, *steps: Step, object_type: str = "deals") -> None:
        self._steps = list(steps)
        self._object_type = object_type
        self.bodies: list[dict[str, Any]] = []

    async def search_page(self, object_type: str, body: Mapping[str, object]) -> SearchPage:
        """Record the request and return, or raise, the next scripted step."""
        assert object_type == self._object_type
        self.bodies.append(json.loads(json.dumps(body)))
        if not self._steps:
            pytest.fail("the search made a request after the script ended")
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def exhausted(self) -> bool:
        """Whether every scripted step was consumed."""
        return not self._steps


def _range_values(body: Mapping[str, Any]) -> tuple[str, str]:
    """Return the ``GTE`` and ``LT`` values of a recorded request body."""
    filters = body["filterGroups"][0]["filters"]
    return filters[0]["value"], filters[1]["value"]


def _search_events(entries: Sequence[EventDict]) -> list[EventDict]:
    """Return the module's own log events."""
    return [entry for entry in entries if str(entry["event"]).startswith("hubspot.search")]


# --- constants and the protocol ----------------------------------------------------------------


def test_limits_match_the_contract() -> None:
    assert PAGE_SIZE == _limit("page_size") == 200
    assert RESULT_CEILING == _limit("max_results") == 10_000
    assert timedelta(days=1) == DAY
    assert timedelta(hours=1) == MIN_PARTITION
    assert CEILING_REASON == "result limit reached at one-hour partition"


def test_pagers_satisfy_the_runtime_checkable_protocol() -> None:
    assert isinstance(ContractPager([]), SearchPager)
    assert isinstance(ScriptedPager(), SearchPager)
    assert not isinstance(object(), SearchPager)


def test_outcome_is_complete_reflects_its_completeness() -> None:
    complete = SearchOutcome(
        rows=(), completeness="complete", reason=None, partitions_incomplete=(), calls=0
    )
    incomplete = SearchOutcome(
        rows=(),
        completeness="incomplete",
        reason=CEILING_REASON,
        partitions_incomplete=((DAY0, DAY0 + HOUR),),
        calls=50,
    )
    assert complete.is_complete is True
    assert incomplete.is_complete is False


# --- epoch_ms -----------------------------------------------------------------------------------


def test_epoch_ms_renders_the_documented_example() -> None:
    assert epoch_ms(datetime(2026, 9, 1, tzinfo=UTC)) == "1788220800000"
    assert epoch_ms(DAY0) == str(_wire_ms(DAY0))


def test_epoch_ms_is_independent_of_the_time_zone() -> None:
    assert epoch_ms(DAY0.astimezone(EASTERN)) == epoch_ms(DAY0)
    assert epoch_ms(datetime(2026, 8, 31, 20, 0, tzinfo=EASTERN)) == "1788220800000"


@pytest.mark.parametrize(
    ("microseconds", "added_ms"),
    [(0, 0), (1, 1), (999, 1), (1000, 1), (1001, 2), (999_999, 1000)],
)
def test_epoch_ms_rounds_a_sub_millisecond_instant_up(microseconds: int, added_ms: int) -> None:
    instant = DAY0 + timedelta(microseconds=microseconds)
    assert epoch_ms(instant) == str(_wire_ms(DAY0) + added_ms)


def test_epoch_ms_handles_instants_before_the_epoch() -> None:
    assert epoch_ms(EPOCH) == "0"
    assert epoch_ms(EPOCH - timedelta(microseconds=500)) == "0"
    assert epoch_ms(EPOCH - ONE_MS) == "-1"
    assert epoch_ms(EPOCH - timedelta(microseconds=1500)) == "-1"


class _NoOffset(tzinfo):
    """A tzinfo whose offset is unknown, which makes a datetime naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


@pytest.mark.parametrize(
    "instant",
    [DAY0.replace(tzinfo=None), DAY0.replace(tzinfo=_NoOffset())],
    ids=["no-tzinfo", "tzinfo-without-offset"],
)
def test_epoch_ms_rejects_a_naive_instant(instant: datetime) -> None:
    with pytest.raises(ValueError, match="instant must be a timezone-aware datetime"):
        epoch_ms(instant)


# --- SearchFilter -------------------------------------------------------------------------------


def test_search_filter_defaults_sort_to_the_range_property() -> None:
    search_filter = SearchFilter(range_property="createdate", properties=("createdate",))
    assert search_filter.sort_property is None
    assert search_filter.sort_by == "createdate"
    assert search_filter.extra_filters == ()


def test_search_filter_sorts_by_an_explicit_sort_property() -> None:
    search_filter = SearchFilter(
        range_property="createdate", properties=("createdate",), sort_property="hs_object_id"
    )
    assert search_filter.sort_by == "hs_object_id"


def test_search_filter_deduplicates_properties_keeping_first_positions() -> None:
    names: Any = ["amount", "createdate", "amount", "dealstage", "createdate"]
    search_filter = SearchFilter(range_property="createdate", properties=names)
    assert search_filter.properties == ("amount", "createdate", "dealstage")


def test_search_filter_is_frozen() -> None:
    search_filter = SearchFilter(range_property="createdate", properties=("createdate",))
    with pytest.raises(FrozenInstanceError):
        search_filter.range_property = "hs_lastmodifieddate"  # type: ignore[misc]


def test_search_filter_freezes_copies_of_its_extra_filters() -> None:
    raw = {"propertyName": "pipeline", "operator": "EQ", "value": "default"}
    search_filter = SearchFilter(
        range_property="createdate", properties=("createdate",), extra_filters=(raw,)
    )
    raw["value"] = "changed"
    (stored,) = search_filter.extra_filters
    assert isinstance(stored, MappingProxyType)
    assert stored == {"propertyName": "pipeline", "operator": "EQ", "value": "default"}
    with pytest.raises(TypeError):
        stored["value"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize("name", ["", "   ", " createdate", "createdate ", 5, None])
def test_search_filter_rejects_a_bad_range_property(name: Any) -> None:
    with pytest.raises(ValueError, match="range_property must be a non-blank property name"):
        SearchFilter(range_property=name, properties=("createdate",))


@pytest.mark.parametrize("name", ["", " createdate", 7])
def test_search_filter_rejects_a_bad_sort_property(name: Any) -> None:
    with pytest.raises(ValueError, match="sort_property must be a non-blank property name"):
        SearchFilter(range_property="createdate", properties=("createdate",), sort_property=name)


def test_search_filter_rejects_properties_given_as_one_string() -> None:
    with pytest.raises(ValueError, match="properties must be a sequence of property names"):
        SearchFilter(range_property="createdate", properties="createdate")  # type: ignore[arg-type]


def test_search_filter_rejects_empty_properties() -> None:
    with pytest.raises(ValueError, match="properties must name at least one property"):
        SearchFilter(range_property="createdate", properties=())


@pytest.mark.parametrize("bad", ["", " amount", 3])
def test_search_filter_rejects_a_bad_property_name(bad: Any) -> None:
    with pytest.raises(ValueError, match=r"properties\[1\] must be a non-blank property name"):
        SearchFilter(range_property="createdate", properties=("createdate", bad))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["propertyName", "operator"], r"extra_filters\[0\] must be a mapping"),
        (
            {"propertyName": "x", "operator": "IN", "values": "a"},
            r"extra_filters\[0\] has keys the contract does not define: \['values'\]",
        ),
        (
            {"propertyName": "x", "operator": "EQ", "value": "a", "zeta": "", "alpha": ""},
            r"has keys the contract does not define: \['alpha', 'zeta'\]",
        ),
        ({"operator": "HAS_PROPERTY"}, r"extra_filters\[0\]\.propertyName must be a non-blank"),
        ({"propertyName": " x", "operator": "EQ"}, r"extra_filters\[0\]\.propertyName"),
        ({"propertyName": "x"}, r"extra_filters\[0\]\.operator must be one of"),
        ({"propertyName": "x", "operator": "LIKE"}, r"extra_filters\[0\]\.operator must be one"),
        ({"propertyName": "x", "operator": "EQ", "value": 5}, r"\.value must be a string"),
        (
            {"propertyName": "x", "operator": "BETWEEN", "value": "1", "highValue": 2},
            r"extra_filters\[0\]\.highValue must be a string",
        ),
    ],
)
def test_search_filter_rejects_an_extra_filter_that_breaks_the_contract(
    extra: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        SearchFilter(
            range_property="createdate", properties=("createdate",), extra_filters=(extra,)
        )


def test_search_filter_accepts_every_contract_operator() -> None:
    operators = _request_schema()["properties"]["filterGroups"]["items"]["properties"]["filters"][
        "items"
    ]["properties"]["operator"]["enum"]
    for operator in operators:
        search_filter = SearchFilter(
            range_property="createdate",
            properties=("createdate",),
            extra_filters=({"propertyName": "pipeline", "operator": operator},),
        )
        assert search_filter.extra_filters[0]["operator"] == operator


def test_search_filter_fits_at_most_four_extra_filters_beside_the_range() -> None:
    extras = tuple(
        {"propertyName": f"property_{index}", "operator": "HAS_PROPERTY"} for index in range(5)
    )
    fitting = SearchFilter(
        range_property="createdate", properties=("createdate",), extra_filters=extras[:4]
    )
    assert len(fitting.extra_filters) + 2 == _limit("max_filters_per_group")
    with pytest.raises(ValueError, match="at most 4 extra filters fit"):
        SearchFilter(range_property="createdate", properties=("createdate",), extra_filters=extras)


# --- deal and Google-contact filters ---------------------------------------------------------


def test_deal_search_filter_reads_createdate_sorted_by_createdate() -> None:
    search_filter = deal_search_filter()
    assert search_filter.range_property == "createdate"
    assert search_filter.sort_by == "createdate"
    assert search_filter.properties == tuple(DEAL_PROPERTIES)
    assert search_filter.extra_filters == ()


def test_google_contacts_filter_requires_the_campaign_property() -> None:
    requested = ContactPropertyMap(
        ql_property="hs_v2_date_entered_1406943076",
        google_campaign_property=CAMPAIGN_PROPERTY,
        google_touch_property=TOUCH_PROPERTY,
    ).request_properties()
    search_filter = google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, requested)
    assert search_filter.range_property == TOUCH_PROPERTY
    assert search_filter.sort_by == TOUCH_PROPERTY
    assert search_filter.properties == requested
    assert search_filter.extra_filters == (
        {"propertyName": CAMPAIGN_PROPERTY, "operator": "HAS_PROPERTY"},
    )


def test_google_contacts_filter_appends_both_configured_properties_when_absent() -> None:
    search_filter = google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, ["email"])
    assert search_filter.properties == ("email", CAMPAIGN_PROPERTY, TOUCH_PROPERTY)
    search_filter = google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, ())
    assert search_filter.properties == (CAMPAIGN_PROPERTY, TOUCH_PROPERTY)


@pytest.mark.parametrize(
    ("campaign", "touch", "message"),
    [
        ("", TOUCH_PROPERTY, "campaign_property must be a non-blank property name"),
        (" google_campaign_id", TOUCH_PROPERTY, "campaign_property must be a non-blank"),
        (CAMPAIGN_PROPERTY, "  ", "touch_property must be a non-blank property name"),
        (CAMPAIGN_PROPERTY, None, "touch_property must be a non-blank property name"),
    ],
)
def test_google_contacts_filter_rejects_a_bad_configured_property(
    campaign: Any, touch: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        google_contacts_filter(campaign, touch, ["email"])


def test_google_contacts_filter_rejects_properties_given_as_one_string() -> None:
    with pytest.raises(ValueError, match="properties must be a sequence of property names"):
        google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, "email")


def test_google_contacts_filter_rejects_a_blank_returned_property() -> None:
    with pytest.raises(ValueError, match=r"properties\[1\] must be a non-blank property name"):
        google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, ["email", ""])


# --- search_body -------------------------------------------------------------------------------


def test_search_body_for_a_first_deal_page_matches_the_contract() -> None:
    body = search_body(deal_search_filter(), DAY0, DAY0 + DAY)
    assert body == {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "createdate", "operator": "GTE", "value": "1788220800000"},
                    {"propertyName": "createdate", "operator": "LT", "value": "1788307200000"},
                ]
            }
        ],
        "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
        "properties": ["createdate", "dealstage", "pipeline", "amount"],
        "limit": 200,
    }
    assert "after" not in body
    assert _violations(body, _request_schema()) == []
    assert set(_request_schema()["required"]) <= set(body)
    assert len(json.dumps(body)) < _limit("max_body_chars")


def test_search_body_carries_after_only_when_paging() -> None:
    body = search_body(deal_search_filter(), DAY0, DAY0 + DAY, after="200")
    assert body["after"] == "200"
    assert _violations(body, _request_schema()) == []


def test_search_body_appends_extra_filters_after_the_range_as_plain_objects() -> None:
    search_filter = google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, ["email"])
    body = search_body(search_filter, DAY0, DAY0 + HOUR)
    filters: Any = body["filterGroups"]
    assert filters[0]["filters"] == [
        {"propertyName": TOUCH_PROPERTY, "operator": "GTE", "value": epoch_ms(DAY0)},
        {"propertyName": TOUCH_PROPERTY, "operator": "LT", "value": epoch_ms(DAY0 + HOUR)},
        {"propertyName": CAMPAIGN_PROPERTY, "operator": "HAS_PROPERTY"},
    ]
    assert type(filters[0]["filters"][2]) is dict
    filters[0]["filters"][2]["operator"] = "NOT_HAS_PROPERTY"
    assert search_filter.extra_filters[0]["operator"] == "HAS_PROPERTY"
    assert body["sorts"] == [{"propertyName": TOUCH_PROPERTY, "direction": "ASCENDING"}]
    assert _violations(body, _request_schema()) == []


def test_search_body_returns_a_fresh_body_each_call() -> None:
    search_filter = deal_search_filter()
    first = search_body(search_filter, DAY0, DAY0 + DAY)
    second = search_body(search_filter, DAY0, DAY0 + DAY)
    assert first == second
    assert first is not second
    assert first["properties"] is not second["properties"]


def test_search_body_normalises_bounds_from_any_time_zone() -> None:
    utc = search_body(deal_search_filter(), DAY0, DAY0 + DAY)
    eastern = search_body(
        deal_search_filter(), DAY0.astimezone(EASTERN), (DAY0 + DAY).astimezone(EASTERN)
    )
    assert eastern == utc


def test_search_body_of_the_widest_contact_discovery_stays_under_the_body_limit() -> None:
    campaign = "google_ads_campaign_identifier_for_attribution"
    touch = "google_ads_most_recent_touch_date_for_attribution"
    requested = ContactPropertyMap(
        mql_property="date_first_meeting_was_booked",
        ql_property="hs_v2_date_entered_1406943076",
        google_campaign_property=campaign,
        google_touch_property=touch,
    ).request_properties()
    body = search_body(
        google_contacts_filter(campaign, touch, requested),
        DAY0,
        DAY0 + DAY,
        after=str(RESULT_CEILING - PAGE_SIZE),
        limit=PAGE_SIZE,
    )
    assert _violations(body, _request_schema()) == []
    assert len(json.dumps(body)) < _limit("max_body_chars")


@pytest.mark.parametrize("limit", [1, 57, PAGE_SIZE])
def test_search_body_accepts_a_limit_from_one_to_the_page_size(limit: int) -> None:
    assert search_body(deal_search_filter(), DAY0, DAY0 + DAY, limit=limit)["limit"] == limit


@pytest.mark.parametrize("limit", [0, -1, PAGE_SIZE + 1, True, False])
def test_search_body_rejects_a_limit_outside_the_page_size(limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be from 1 to 200"):
        search_body(deal_search_filter(), DAY0, DAY0 + DAY, limit=limit)


@pytest.mark.parametrize("after", ["", "   ", "\t"])
def test_search_body_rejects_a_blank_cursor(after: str) -> None:
    with pytest.raises(ValueError, match="after must be a non-blank paging cursor or None"):
        search_body(deal_search_filter(), DAY0, DAY0 + DAY, after=after)


def test_search_body_rejects_naive_bounds() -> None:
    naive = DAY0.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        search_body(deal_search_filter(), naive, DAY0 + DAY)
    with pytest.raises(ValueError, match="timezone-aware"):
        search_body(deal_search_filter(), DAY0, naive + DAY)


# --- search_partitioned: argument validation ---------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "object_type", ["", "Deals", "deals/search", "../deals", "-deals", "deals ", 5, None]
)
async def test_search_rejects_an_object_type_that_is_not_a_path_segment(
    object_type: Any,
) -> None:
    pager = ScriptedPager()
    with pytest.raises(ValueError, match="object_type must be a CRM object type"):
        await search_partitioned(pager, object_type, deal_search_filter(), (DAY0, DAY0 + DAY))
    assert pager.bodies == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (DAY0.replace(tzinfo=None), DAY0 + DAY, "time_range start must be a timezone-aware"),
        (DAY0, (DAY0 + DAY).replace(tzinfo=None), "time_range end must be a timezone-aware"),
        (DAY0, DAY0 - ONE_MS, "time_range end must not precede its start"),
        (DAY0 + DAY, DAY0, "time_range end must not precede its start"),
    ],
    ids=["naive-start", "naive-end", "end-one-ms-early", "reversed"],
)
async def test_search_rejects_an_invalid_range_before_any_call(
    start: datetime, end: datetime, message: str
) -> None:
    pager = ScriptedPager()
    with pytest.raises(ValueError, match=message):
        await search_partitioned(pager, "deals", deal_search_filter(), (start, end))
    assert pager.bodies == []


@pytest.mark.anyio
async def test_search_compares_bounds_in_different_time_zones_as_instants() -> None:
    pager = ScriptedPager()
    start = DAY0
    # 04:59:59.999 on the same calendar day at +05:00, yet one millisecond before the start.
    end = (DAY0 - ONE_MS).astimezone(timezone(timedelta(hours=5)))
    with pytest.raises(ValueError, match="time_range end must not precede its start"):
        await search_partitioned(pager, "deals", deal_search_filter(), (start, end))
    assert pager.bodies == []


@pytest.mark.anyio
async def test_search_over_an_empty_range_makes_no_call_and_is_complete(
    captured: list[EventDict],
) -> None:
    pager = ScriptedPager()
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0))
    assert outcome == SearchOutcome(
        rows=(), completeness="complete", reason=None, partitions_incomplete=(), calls=0
    )
    assert pager.bodies == []
    (event,) = _search_events(captured)
    assert event["event"] == "hubspot.search"
    assert event["log_level"] == "info"
    assert event["partitions"] == 0
    assert event["calls"] == 0


# --- search_partitioned: day partitions and paging -------------------------------------------


@pytest.mark.anyio
async def test_search_reads_one_day_partitions_with_half_open_range_filters() -> None:
    start, end = DAY0, DAY0 + 3 * DAY + 6 * HOUR
    boundary_deals = [
        _deal(1, DAY0),
        _deal(2, DAY0 + DAY - ONE_MS),
        _deal(3, DAY0 + DAY),
        _deal(4, DAY0 + 2 * DAY + 12 * HOUR),
        _deal(5, DAY0 + 3 * DAY),
        _deal(6, end - ONE_MS),
    ]
    outside = [_deal(7, start - ONE_MS), _deal(8, end)]
    pager = ContractPager(boundary_deals + outside)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (start, end))

    expected_partitions = [
        (DAY0, DAY0 + DAY),
        (DAY0 + DAY, DAY0 + 2 * DAY),
        (DAY0 + 2 * DAY, DAY0 + 3 * DAY),
        (DAY0 + 3 * DAY, end),
    ]
    assert [served.span for served in pager.served] == expected_partitions
    for served, (lo, hi) in zip(pager.served, expected_partitions, strict=True):
        assert _range_values(served.body) == (str(_wire_ms(lo)), str(_wire_ms(hi)))
        assert served.body["sorts"] == [{"propertyName": "createdate", "direction": "ASCENDING"}]
        assert served.body["properties"] == list(DEAL_PROPERTIES)
        assert served.body["limit"] == PAGE_SIZE
        assert "after" not in served.body
    assert [served.ids for served in pager.served] == [
        (str(DEAL_ID_BASE + 1), str(DEAL_ID_BASE + 2)),
        (str(DEAL_ID_BASE + 3),),
        (str(DEAL_ID_BASE + 4),),
        (str(DEAL_ID_BASE + 5), str(DEAL_ID_BASE + 6)),
    ]
    assert _ids(outcome) == [str(DEAL_ID_BASE + index) for index in range(1, 7)]
    assert outcome.completeness == "complete"
    assert outcome.is_complete
    assert outcome.reason is None
    assert outcome.partitions_incomplete == ()
    assert outcome.calls == 4


@pytest.mark.anyio
async def test_search_partitions_from_a_mid_day_start_in_whole_days() -> None:
    start = DAY0 + 15 * HOUR + timedelta(minutes=30)
    end = start + 2 * DAY - HOUR
    pager = ContractPager([_deal(1, start), _deal(2, start + DAY), _deal(3, end - ONE_MS)])
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (start, end))
    assert pager.first_pages() == [(start, start + DAY), (start + DAY, end)]
    assert len(outcome.rows) == 3
    assert outcome.is_complete


@pytest.mark.anyio
async def test_search_normalises_a_range_in_another_time_zone_to_utc() -> None:
    start, end = DAY0.astimezone(EASTERN), (DAY0 + DAY + HOUR).astimezone(EASTERN)
    pager = ScriptedPager(_page([_deal(1, DAY0 + HOUR)], total=1), _transient())
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (start, end))
    assert [_range_values(body) for body in pager.bodies] == [
        (epoch_ms(DAY0), epoch_ms(DAY0 + DAY)),
        (epoch_ms(DAY0 + DAY), epoch_ms(DAY0 + DAY + HOUR)),
    ]
    ((lo, hi),) = outcome.partitions_incomplete
    assert (lo, hi) == (DAY0 + DAY, DAY0 + DAY + HOUR)
    assert lo.tzinfo is UTC
    assert hi.tzinfo is UTC
    assert _ids(outcome) == [str(DEAL_ID_BASE + 1)]


@pytest.mark.anyio
async def test_search_pages_through_after_at_two_hundred_records() -> None:
    deals = _deals_between(DAY0, DAY0 + DAY, 450)
    pager = ContractPager(deals)
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))
    assert [served.body.get("after") for served in pager.served] == [None, "200", "400"]
    assert [served.limit for served in pager.served] == [200, 200, 200]
    assert [len(served.ids) for served in pager.served] == [200, 200, 50]
    assert _ids(outcome) == [deal["id"] for deal in deals]
    assert outcome.is_complete
    assert outcome.calls == 3


@pytest.mark.anyio
async def test_search_follows_an_opaque_cursor() -> None:
    pager = ScriptedPager(
        _page([_deal(1, DAY0)], total=3, after="opaque-cursor-1"),
        _page([_deal(2, DAY0 + HOUR)], total=3, after="opaque-cursor-2"),
        _page([_deal(3, DAY0 + 2 * HOUR)], total=3),
    )
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))
    assert [body.get("after") for body in pager.bodies] == [
        None,
        "opaque-cursor-1",
        "opaque-cursor-2",
    ]
    assert len(outcome.rows) == 3
    assert outcome.is_complete
    assert pager.exhausted


@pytest.mark.anyio
async def test_search_treats_a_cursor_of_non_ascii_digits_as_opaque() -> None:
    # U+0661 U+0660 x4: Arabic-Indic "10000", which str.isdigit accepts and int() would parse.
    cursor = "\u0661\u0660\u0660\u0660\u0660"
    pager = ScriptedPager(
        _page([_deal(1, DAY0)], total=2, after=cursor),
        _page([_deal(2, DAY0 + HOUR)], total=2),
    )
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + HOUR))
    assert pager.bodies[1]["after"] == cursor
    assert len(outcome.rows) == 2
    assert outcome.is_complete


@pytest.mark.anyio
async def test_search_keeps_the_first_occurrence_of_a_repeated_id() -> None:
    first = _deal(1, DAY0 + HOUR)
    repeated = {**_deal(1, DAY0 + DAY + HOUR), "updatedAt": "2026-09-02T02:00:00.000Z"}
    pager = ScriptedPager(
        _page([first, _deal(2, DAY0 + 2 * HOUR)], total=2),
        _page([repeated, _deal(3, DAY0 + DAY + 2 * HOUR)], total=2),
    )
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + 2 * DAY))
    assert _ids(outcome) == [str(DEAL_ID_BASE + 1), str(DEAL_ID_BASE + 2), str(DEAL_ID_BASE + 3)]
    assert outcome.rows[0]["updatedAt"] == first["updatedAt"]


@pytest.mark.anyio
async def test_search_accepts_an_integer_record_id() -> None:
    pager = ScriptedPager(_raw_page([{"id": 12345, "properties": {}}], total=1))
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + HOUR))
    assert outcome.rows == ({"id": 12345, "properties": {}},)
    assert outcome.is_complete


@pytest.mark.anyio
async def test_search_reads_the_documented_contacts_sample() -> None:
    sample = _contract()["response"]["documented_sample"]
    assert _violations(sample, _response_schema()) == []
    pager = ScriptedPager(parse_search_page(sample), object_type="contacts")
    search_filter = SearchFilter(range_property="createdate", properties=("email", "createdate"))
    outcome = await search_partitioned(pager, "contacts", search_filter, (DAY0, DAY0 + DAY))
    assert _ids(outcome) == [record["id"] for record in sample["results"]]
    assert outcome.is_complete
    assert outcome.calls == 1


# --- search_partitioned: overflow splitting ------------------------------------------------


@pytest.mark.anyio
async def test_search_halves_a_day_whose_total_exceeds_the_ceiling(
    captured: list[EventDict],
) -> None:
    deals = _deals_between(DAY0, DAY0 + DAY, 12_000)
    pager = ContractPager(deals)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))

    assert pager.first_pages() == [
        (DAY0, DAY0 + DAY),
        (DAY0, DAY0 + 12 * HOUR),
        (DAY0 + 12 * HOUR, DAY0 + DAY),
    ]
    discarded = pager.served[0]
    assert discarded.total == 12_000
    assert len(discarded.ids) == PAGE_SIZE
    ids = _ids(outcome)
    assert ids == [deal["id"] for deal in deals]
    assert len(set(ids)) == 12_000
    assert set(discarded.ids) <= set(ids)
    assert outcome.is_complete
    assert outcome.calls == 1 + 30 + 30
    assert max(served.offset + served.limit for served in pager.served) <= RESULT_CEILING
    (event,) = _search_events(captured)
    assert event["splits"] == 1
    assert event["partitions"] == 2
    assert event["rows"] == 12_000


@pytest.mark.anyio
async def test_search_splits_at_whole_hours_down_to_complete_hourly_partitions() -> None:
    deals = _deals_between(DAY0, DAY0 + HOUR, 6_000) + _deals_between(
        DAY0 + HOUR, DAY0 + 2 * HOUR, 6_000, first=6_000
    )
    pager = ContractPager(deals)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))

    assert pager.first_pages() == [
        (_at(0), _at(24)),
        (_at(0), _at(12)),
        (_at(0), _at(6)),
        (_at(0), _at(3)),
        (_at(0), _at(1)),
        (_at(1), _at(3)),
        (_at(3), _at(6)),
        (_at(6), _at(12)),
        (_at(12), _at(24)),
    ]
    assert _ids(outcome) == [deal["id"] for deal in deals]
    assert outcome.is_complete
    assert outcome.partitions_incomplete == ()
    assert outcome.calls == 4 + 30 + 30 + 3


@pytest.mark.anyio
async def test_search_halves_24_12_6_3_then_1_plus_2_then_1_plus_1_to_the_ceiling(
    captured: list[EventDict],
) -> None:
    deals = _deals_between(_at(2), _at(3), 10_500)
    pager = ContractPager(deals)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))

    assert pager.first_pages() == [
        (_at(0), _at(24)),
        (_at(0), _at(12)),
        (_at(0), _at(6)),
        (_at(0), _at(3)),
        (_at(0), _at(1)),
        (_at(1), _at(3)),
        (_at(1), _at(2)),
        (_at(2), _at(3)),
        (_at(3), _at(6)),
        (_at(6), _at(12)),
        (_at(12), _at(24)),
    ]
    hour = [served for served in pager.served if served.span == (_at(2), _at(3))]
    assert len(hour) == RESULT_CEILING // PAGE_SIZE
    assert hour[-1].offset + hour[-1].limit == RESULT_CEILING
    assert hour[-1].next_after == str(RESULT_CEILING)
    assert max(served.offset + served.limit for served in pager.served) <= RESULT_CEILING
    assert _ids(outcome) == [deal["id"] for deal in deals[:RESULT_CEILING]]
    assert outcome.completeness == "incomplete"
    assert outcome.reason == CEILING_REASON
    assert outcome.partitions_incomplete == ((_at(2), _at(3)),)
    # One call for each of the ten other first pages, fifty for the ceiling hour.
    assert outcome.calls == 10 + 50
    (event,) = _search_events(captured)
    assert event["event"] == "hubspot.search"
    assert event["log_level"] == "warning"
    assert event["splits"] == 5
    assert event["partitions"] == 6
    assert event["completeness"] == "incomplete"
    assert event["partitions_incomplete"] == 1


@pytest.mark.anyio
async def test_search_splits_a_partial_last_day_at_rounded_up_hours() -> None:
    end = _at(2.5)
    deals = _deals_between(_at(2), end, 10_001)
    pager = ContractPager(deals)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, end))

    # 2.5 h rounds up to 3 h, so the split is at 1 h; the remaining 1.5 h splits at 1 h again,
    # and the final 30 minutes, below one hour, is never split.
    assert pager.first_pages() == [
        (_at(0), end),
        (_at(0), _at(1)),
        (_at(1), end),
        (_at(1), _at(2)),
        (_at(2), end),
    ]
    assert outcome.partitions_incomplete == ((_at(2), end),)
    assert outcome.reason == CEILING_REASON
    assert len(outcome.rows) == RESULT_CEILING


@pytest.mark.anyio
async def test_search_discards_a_partition_whose_total_grows_past_the_ceiling_mid_read() -> None:
    deals = _deals_between(DAY0, DAY0 + DAY, 450)

    def grown_total(call: Call, matched: int) -> int:
        # The full-day partition reports 10,001 from its second page on: records arrived.
        return 10_001 if (call.lo, call.hi) == (DAY0, DAY0 + DAY) and call.offset else matched

    pager = ContractPager(deals, total_for=grown_total)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))

    assert [(served.span, served.offset) for served in pager.served][:2] == [
        ((DAY0, DAY0 + DAY), 0),
        ((DAY0, DAY0 + DAY), 200),
    ]
    assert pager.first_pages()[1:] == [(DAY0, _at(12)), (_at(12), DAY0 + DAY)]
    ids = _ids(outcome)
    assert ids == [deal["id"] for deal in deals]
    assert len(ids) == len(set(ids))
    assert outcome.is_complete


@pytest.mark.anyio
async def test_search_reads_a_one_hour_partition_to_exactly_the_ceiling() -> None:
    deals = _deals_between(DAY0, DAY0 + HOUR, 10_500)
    pager = ContractPager(deals, max_page=150)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + HOUR))

    limits = [served.limit for served in pager.served]
    assert limits == [200] * 66 + [100]
    assert [served.offset for served in pager.served] == [150 * page for page in range(67)]
    assert pager.served[-1].offset + pager.served[-1].limit == RESULT_CEILING
    assert pager.attempts == 67
    assert _ids(outcome) == [deal["id"] for deal in deals[:RESULT_CEILING]]
    assert outcome.completeness == "incomplete"
    assert outcome.reason == CEILING_REASON
    assert outcome.partitions_incomplete == ((DAY0, DAY0 + HOUR),)
    assert outcome.calls == 67


@pytest.mark.anyio
async def test_search_stops_at_the_ceiling_with_opaque_cursors() -> None:
    pages = [
        _page(
            [_deal(page * PAGE_SIZE + row, DAY0) for row in range(PAGE_SIZE)],
            total=RESULT_CEILING + 1,
            after=f"opaque-{page}",
        )
        for page in range(RESULT_CEILING // PAGE_SIZE)
    ]
    pager = ScriptedPager(*pages)
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + HOUR))
    assert pager.exhausted
    assert len(pager.bodies) == 50
    assert len(outcome.rows) == RESULT_CEILING
    assert outcome.partitions_incomplete == ((DAY0, DAY0 + HOUR),)
    assert outcome.reason == CEILING_REASON


@pytest.mark.anyio
async def test_search_completes_when_the_page_at_the_ceiling_returned_every_result() -> None:
    deals = _deals_between(DAY0, DAY0 + DAY, RESULT_CEILING)
    pager = ContractPager(deals, trailing_cursor=True)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))

    assert pager.served[-1].next_after == str(RESULT_CEILING)
    assert pager.served[-1].total == RESULT_CEILING
    assert pager.attempts == 50
    assert len(outcome.rows) == RESULT_CEILING
    assert outcome.is_complete
    assert outcome.reason is None


@pytest.mark.anyio
async def test_search_completes_a_day_of_exactly_ten_thousand_results_without_splitting() -> None:
    deals = _deals_between(DAY0, DAY0 + DAY, RESULT_CEILING)
    pager = ContractPager(deals)
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))
    assert pager.first_pages() == [(DAY0, DAY0 + DAY)]
    assert pager.served[-1].next_after is None
    assert len(outcome.rows) == RESULT_CEILING
    assert outcome.is_complete


@pytest.mark.anyio
async def test_search_never_requests_a_cursor_at_the_ceiling_on_a_one_hour_partition() -> None:
    pager = ScriptedPager(_page([_deal(1, DAY0)], total=2, after=str(RESULT_CEILING)))
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + HOUR))
    assert pager.exhausted
    assert len(pager.bodies) == 1
    assert _ids(outcome) == [str(DEAL_ID_BASE + 1)]
    assert outcome.partitions_incomplete == ((DAY0, DAY0 + HOUR),)
    assert outcome.reason == CEILING_REASON


@pytest.mark.anyio
async def test_search_splits_a_longer_partition_whose_cursor_jumps_to_the_ceiling() -> None:
    pager = ScriptedPager(
        _page([_deal(1, _at(1))], total=2, after=str(RESULT_CEILING + 200)),
        _page([_deal(1, _at(1))], total=1),
        _page([_deal(2, _at(13))], total=1),
    )
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))
    assert [_range_values(body) for body in pager.bodies] == [
        (epoch_ms(_at(0)), epoch_ms(_at(24))),
        (epoch_ms(_at(0)), epoch_ms(_at(12))),
        (epoch_ms(_at(12)), epoch_ms(_at(24))),
    ]
    assert _ids(outcome) == [str(DEAL_ID_BASE + 1), str(DEAL_ID_BASE + 2)]
    assert outcome.is_complete


@pytest.mark.anyio
async def test_search_lists_every_ceiling_partition_and_states_the_reason_once() -> None:
    deals = _deals_between(_at(0), _at(1), 10_001) + _deals_between(
        _at(5), _at(6), 10_001, first=10_001
    )
    pager = ContractPager(deals)
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (_at(0), _at(6)))
    assert outcome.partitions_incomplete == ((_at(0), _at(1)), (_at(5), _at(6)))
    assert outcome.reason == CEILING_REASON
    assert len(outcome.rows) == 2 * RESULT_CEILING


# --- search_partitioned: transient failures --------------------------------------------------


@pytest.mark.anyio
async def test_transient_failure_keeps_rows_and_lists_current_and_remaining_partitions(
    captured: list[EventDict],
) -> None:
    deals = (
        _deals_between(DAY0, DAY0 + DAY, 250)
        + _deals_between(DAY0 + DAY, DAY0 + 2 * DAY, 450, first=250)
        + _deals_between(DAY0 + 2 * DAY, DAY0 + 3 * DAY, 10, first=700)
    )
    error = _transient()

    def fail_on(call: Call) -> BaseException | None:
        return error if (call.lo, call.offset) == (DAY0 + DAY, 200) else None

    pager = ContractPager(deals, fail_on=fail_on)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + 3 * DAY))

    assert _ids(outcome) == [deal["id"] for deal in deals[: 250 + 200]]
    assert outcome.completeness == "incomplete"
    assert outcome.reason == error.reason
    assert outcome.partitions_incomplete == (
        (DAY0 + DAY, DAY0 + 2 * DAY),
        (DAY0 + 2 * DAY, DAY0 + 3 * DAY),
    )
    assert pager.attempts == 4
    assert outcome.calls == 4
    (event,) = _search_events(captured)
    assert event["event"] == "hubspot.search"
    assert event["log_level"] == "warning"
    assert event["partitions"] == 1
    assert event["partitions_incomplete"] == 2
    assert event["calls"] == 4
    assert event["rows"] == 450
    assert event["error_kind"] == "transient"
    assert event["error_code"] == "RATE_LIMIT"
    assert event["http_status"] == 429
    assert event["retry_after"] == 1.0
    assert event["reason"] == error.reason


@pytest.mark.anyio
async def test_transient_failure_after_a_split_lists_the_unread_halves() -> None:
    deals = _deals_between(_at(0), _at(1), 6_000) + _deals_between(
        _at(13), _at(14), 6_000, first=6_000
    )
    error = _transient()

    def fail_on(call: Call) -> BaseException | None:
        return error if (call.lo, call.hi, call.offset) == (_at(0), _at(12), 400) else None

    pager = ContractPager(deals, fail_on=fail_on)

    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + 2 * DAY))

    assert _ids(outcome) == [deal["id"] for deal in deals[:400]]
    assert outcome.partitions_incomplete == (
        (_at(0), _at(12)),
        (_at(12), _at(24)),
        (_at(24), _at(48)),
    )
    assert outcome.reason == error.reason


@pytest.mark.anyio
async def test_transient_failure_on_a_first_page_keeps_earlier_partitions_only() -> None:
    error = _transient()
    pager = ScriptedPager(_page([_deal(1, DAY0)], total=1), error)
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + 2 * DAY))
    assert _ids(outcome) == [str(DEAL_ID_BASE + 1)]
    assert outcome.partitions_incomplete == ((DAY0 + DAY, DAY0 + 2 * DAY),)
    assert outcome.calls == 2


@pytest.mark.anyio
async def test_transient_failure_after_a_ceiling_joins_both_reasons_in_order() -> None:
    deals = _deals_between(_at(0), _at(1), 10_001)
    error = _transient()

    def fail_on(call: Call) -> BaseException | None:
        return error if call.lo == _at(1) else None

    pager = ContractPager(deals, fail_on=fail_on)
    outcome = await search_partitioned(pager, "deals", deal_search_filter(), (_at(0), _at(1.5)))
    assert outcome.reason == f"{CEILING_REASON}; {error.reason}"
    assert outcome.partitions_incomplete == ((_at(0), _at(1)), (_at(1), _at(1.5)))
    assert len(outcome.rows) == RESULT_CEILING


# --- search_partitioned: errors that propagate --------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        AccessDenied(
            "This app hasn't been granted all required scopes",
            source="hubspot",
            endpoint="crm_search",
            code="MISSING_SCOPES",
            http_status=403,
        ),
        QueryDefect(
            "Invalid input JSON",
            source="hubspot",
            endpoint="crm_search",
            code="VALIDATION_ERROR",
            http_status=400,
        ),
    ],
    ids=["access-denied", "query-defect"],
)
async def test_access_denied_and_query_defect_propagate_unchanged(
    error: AccessDenied | QueryDefect, captured: list[EventDict]
) -> None:
    pager = ScriptedPager(_page([_deal(1, DAY0)], total=1), error)
    with pytest.raises(type(error)) as caught:
        await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + 3 * DAY))
    assert caught.value is error
    assert len(pager.bodies) == 2
    (event,) = _search_events(captured)
    assert event["event"] == "hubspot.search_failed"
    assert event["log_level"] == "warning"
    assert event["object_type"] == "deals"
    assert event["range_property"] == "createdate"
    assert event["partitions"] == 1
    assert event["splits"] == 0
    assert event["calls"] == 2
    assert event["rows"] == 1
    assert event["error_kind"] == error.kind
    assert event["http_status"] == error.http_status
    assert event["reason"] == error.reason


# --- search_partitioned: paging-contract breaches ---------------------------------------------


def _assert_paging_defect(error: QueryDefect, message: str) -> None:
    """Assert the defect raised for a response that breaks the paging contract."""
    assert error.message == message
    assert error.source == "hubspot"
    assert error.endpoint == "crm_search"
    assert error.code == "validation"
    assert error.kind == "defect"


@pytest.mark.anyio
async def test_a_cursor_on_an_empty_page_is_a_query_defect(captured: list[EventDict]) -> None:
    pager = ScriptedPager(_page([], total=3, after="200"))
    with pytest.raises(QueryDefect) as caught:
        await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))
    _assert_paging_defect(caught.value, "search page carried a paging cursor but no results")
    (event,) = _search_events(captured)
    assert event["event"] == "hubspot.search_failed"
    assert event["error_kind"] == "defect"


@pytest.mark.anyio
async def test_a_cursor_that_does_not_advance_is_a_query_defect() -> None:
    pager = ScriptedPager(
        _page([_deal(1, DAY0)], total=3, after="200"),
        _page([_deal(2, DAY0 + HOUR)], total=3, after="200"),
    )
    with pytest.raises(QueryDefect) as caught:
        await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))
    _assert_paging_defect(caught.value, "search paging cursor did not advance")
    assert pager.exhausted


@pytest.mark.anyio
@pytest.mark.parametrize(
    "record",
    [
        {"properties": {}},
        {"id": None, "properties": {}},
        {"id": True, "properties": {}},
        {"id": "   ", "properties": {}},
        {"id": "", "properties": {}},
        {"id": 1.5, "properties": {}},
        {"id": ["1"], "properties": {}},
    ],
    ids=["missing", "null", "bool", "blank", "empty", "float", "list"],
)
async def test_a_record_without_a_usable_id_is_a_query_defect(record: HubSpotRecord) -> None:
    pager = ScriptedPager(_raw_page([_deal(1, DAY0), record], total=2))
    with pytest.raises(QueryDefect) as caught:
        await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))
    _assert_paging_defect(caught.value, "search result record has no usable 'id'")


@pytest.mark.anyio
async def test_a_record_without_an_id_kept_after_a_transient_failure_is_a_query_defect() -> None:
    pager = ScriptedPager(_raw_page([{"properties": {}}], total=2, after="200"), _transient())
    with pytest.raises(QueryDefect) as caught:
        await search_partitioned(pager, "deals", deal_search_filter(), (DAY0, DAY0 + DAY))
    _assert_paging_defect(caught.value, "search result record has no usable 'id'")


# --- Google-touch contact discovery ------------------------------------------------------------


@pytest.mark.anyio
async def test_google_contact_discovery_filters_on_touch_date_and_campaign_presence() -> None:
    contacts = [
        _contact(1, _at(1), campaign="2468013579"),
        _contact(2, _at(2), campaign=None),
        _contact(3, _at(3), campaign=""),
        _contact(4, _at(26), campaign="1357924680"),
    ]
    pager = ContractPager(contacts, object_type="contacts", range_property=TOUCH_PROPERTY)
    search_filter = google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, ["email"])

    outcome = await search_partitioned(pager, "contacts", search_filter, (DAY0, DAY0 + 2 * DAY))

    assert _ids(outcome) == [str(CONTACT_ID_BASE + 1), str(CONTACT_ID_BASE + 4)]
    assert outcome.is_complete
    for served in pager.served:
        assert served.body["filterGroups"][0]["filters"][2] == {
            "propertyName": CAMPAIGN_PROPERTY,
            "operator": "HAS_PROPERTY",
        }
        assert served.body["sorts"] == [{"propertyName": TOUCH_PROPERTY, "direction": "ASCENDING"}]
        assert served.body["properties"] == ["email", CAMPAIGN_PROPERTY, TOUCH_PROPERTY]


# --- logging -------------------------------------------------------------------------------------


def _logged_text(entries: Sequence[EventDict]) -> list[str]:
    """Return every string value, at any depth, of the captured events."""
    found: list[str] = []
    stack: list[object] = [dict(entry) for entry in entries]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, Mapping):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list | tuple | set | frozenset):
            stack.extend(value)
    return found


@pytest.mark.anyio
async def test_search_log_never_carries_filter_values_cursors_or_records(
    captured: list[EventDict],
) -> None:
    # 5,200 contacts in the first hour and 5,200 in the fourth: the day splits three times
    # (24 → 12 → 6 → 3) and every partition then completes.
    contacts = [
        _contact(
            index,
            (_at(0) if index < 5_200 else _at(3)) + (index % 5_200) * 600 * ONE_MS,
            campaign=f"97531{index:05d}",
        )
        for index in range(10_400)
    ]
    pager = ContractPager(contacts, object_type="contacts", range_property=TOUCH_PROPERTY)
    search_filter = google_contacts_filter(CAMPAIGN_PROPERTY, TOUCH_PROPERTY, ["email"])

    outcome = await search_partitioned(pager, "contacts", search_filter, (DAY0, DAY0 + DAY))

    assert outcome.is_complete
    assert len(outcome.rows) == 10_400
    events = _search_events(captured)
    assert [entry["event"] for entry in events] == ["hubspot.search"]
    (event,) = events
    assert {key: value for key, value in event.items() if key != "log_level"} == {
        "event": "hubspot.search",
        "object_type": "contacts",
        "range_property": TOUCH_PROPERTY,
        "extra_filter_properties": [CAMPAIGN_PROPERTY],
        "partitions": 4,
        "splits": 3,
        "calls": outcome.calls,
        "rows": 10_400,
        "completeness": "complete",
        "partitions_incomplete": 0,
    }
    assert event["log_level"] == "info"
    forbidden: set[str] = set()
    for served in pager.served:
        forbidden.update(_range_values(served.body))
        if served.next_after is not None:
            forbidden.add(served.next_after)
    for contact in contacts:
        forbidden.add(contact["id"])
        forbidden.update(str(value) for value in contact["properties"].values() if value)
    logged = _logged_text(captured)
    leaked = sorted(text for text in logged if any(secret in text for secret in forbidden))
    assert leaked == []
    assert not any("@" in text for text in logged)
