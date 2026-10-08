"""Partitioned HubSpot CRM Search: every record created in a time range, or a stated floor.

CRM Search (``POST /crm/objects/2026-09/{objectType}/search``) pages through ``after`` at
most 200 records at a time and stops at 10,000 results per query: asking for a page past
the ceiling returns HTTP 400. A single query over a busy window can therefore never be read
in full. :func:`search_partitioned` reads a time range as a sequence of narrower queries
whose union is the range:

1. ``[start, end)`` is split into consecutive one-day partitions ``[t, min(t + 1 day, end))``,
   read in ascending order.
2. Each partition is one query with a half-open range filter, ``range_property GTE lo`` and
   ``range_property LT hi``, so adjacent partitions share a boundary without overlapping.
3. When a page reports ``total`` above 10,000 and the partition is longer than one hour,
   the partition's rows are discarded and it is split at a whole hour,
   ``mid = lo + floor(hours / 2) hours`` with ``hours = ceil((hi - lo) / 1 h)``
   (24 → 12 → 6 → 3 → 1 + 2 → 1 + 1), and both halves are read in order.
4. A partition of one hour or less is never split. When its ``total`` still exceeds
   10,000, it is read up to the ceiling (no request ever goes past 10,000 results), the
   partition is listed in :attr:`SearchOutcome.partitions_incomplete` and the outcome is
   ``incomplete`` with the reason ``"result limit reached at one-hour partition"``, whether
   or not its last page carries a cursor.
5. Otherwise a partition is complete only when it returned at least as many distinct record
   ids as the ``total`` of its latest page; duplicated records never count twice. When its
   paging ends (a page without a cursor) short of that, its rows are kept, it is listed as
   incomplete and the outcome is ``incomplete`` with the reason
   ``"search paging ended before its reported total"``. Totals can race deletions, so this
   is an incomplete read rather than a defect. When the ceiling stops the paging short of
   that instead, a partition longer than one hour is split and one of at most one hour is
   incomplete with the ceiling reason above.

Two searches use it (AAP 0.3.3):

* the window deal search (:func:`deal_search_filter`): deals whose ``createdate`` falls from
  window start to run time, sorted by ``createdate``. An ``incomplete`` outcome makes the
  orphan-deal share "unknown" for that range (AAP 0.5.4.7);
* Google-touch contact discovery (:func:`google_contacts_filter`): contacts whose configured
  touch-date property falls in the sync range and whose configured campaign property is set
  (``HAS_PROPERTY``), which finds Google-only contacts that never submitted a LinkedIn form.

Request bodies follow ``backend/tests/contracts/hubspot/crm_search.json``. The pager
(:class:`SearchPager`, satisfied by ``HubSpotClient``) owns HTTP, retries, the 4 req/s
throttle and the 3,000-character and ``limit <= 200`` checks; this module never imports the
client (the client imports this module), ``sync``, ``simulation`` or ``api``.

Errors:
    A :class:`~ads_engine.connectors.errors.TransientError` that survived the pager's retries
    stops the search without raising: the rows already read are kept, the current and every
    remaining partition are listed as incomplete, and the outcome is ``incomplete`` with the
    error's :attr:`~ads_engine.connectors.errors.ConnectorError.reason`, so the dataset turns
    incomplete and keeps its last good data. ``AccessDenied`` and ``QueryDefect`` propagate
    unchanged, for ``ads_engine.sync.mode`` to decide. A response that breaks the paging
    contract is a ``QueryDefect``, raised before another request: a record without a usable
    ``id``, a cursor on an empty page, a cursor that repeats any cursor the partition already
    received (``A → B → A`` included), or a numeric cursor that does not exceed the previous
    cursor when that one is numeric too. Invalid arguments raise ``ValueError`` before any
    call.

Instants (Decision 6):
    Every instant is timezone-aware; ranges are normalised to UTC, and filter values are
    integer epoch milliseconds as strings, the form CRM Search takes for datetime properties.

Logging:
    One ``hubspot.search`` event per :func:`search_partitioned` call, carrying the object type,
    the property names filtered on, the partition, split and call counts, the row count and
    the completeness. Filter values, cursors and record contents are never logged.

Example:
    ``sync/jobs.py`` reads the window's deals and records the dataset's completeness::

        outcome = await search_partitioned(
            client, "deals", deal_search_filter(), (window_start, run_time)
        )
        deals = [parse_deal(record) for record in outcome.rows]
        record_freshness("hubspot.deal_search", outcome.completeness, outcome.reason)
"""

import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal, Protocol, runtime_checkable

from ads_engine.connectors.errors import AccessDenied, QueryDefect, TransientError
from ads_engine.connectors.hubspot.parser import DEAL_PROPERTIES, HubSpotRecord, SearchPage
from ads_engine.logging import get_logger

__all__ = [
    "CEILING_REASON",
    "DAY",
    "MIN_PARTITION",
    "PAGE_SIZE",
    "RESULT_CEILING",
    "SHORT_READ_REASON",
    "Completeness",
    "SearchFilter",
    "SearchOutcome",
    "SearchPager",
    "deal_search_filter",
    "epoch_ms",
    "google_contacts_filter",
    "search_body",
    "search_partitioned",
]

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Limits (AAP 0.2.4; backend/tests/contracts/hubspot/crm_search.json "limits")
# ---------------------------------------------------------------------------

PAGE_SIZE: Final = 200
"""Records requested per page, CRM Search's maximum ``limit``."""

RESULT_CEILING: Final = 10_000
"""Results one query can return; a page request past it is rejected with HTTP 400."""

MIN_PARTITION: Final = timedelta(hours=1)
"""The narrowest partition: one at or below this length is never split, only read to the
ceiling and marked incomplete."""

DAY: Final = timedelta(days=1)
"""Length of the initial partitions a range is cut into."""

CEILING_REASON: Final = "result limit reached at one-hour partition"
"""Reason recorded when a partition of at most one hour still holds over 10,000 results."""

SHORT_READ_REASON: Final = "search paging ended before its reported total"
"""Reason recorded when a partition's paging ends with fewer distinct records than its
``total``, as when a last page without a cursor, or an empty one, arrives early."""

_MAX_FILTERS_PER_GROUP: Final = 6
_RANGE_FILTERS: Final = 2
_SOURCE: Final = "hubspot"
_SURFACE: Final = "crm_search"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND: Final = timedelta(milliseconds=1)
_OPERATORS: Final = frozenset(
    {
        "BETWEEN",
        "CONTAINS_TOKEN",
        "EQ",
        "GT",
        "GTE",
        "HAS_PROPERTY",
        "IN",
        "LT",
        "LTE",
        "NEQ",
        "NOT_CONTAINS_TOKEN",
        "NOT_HAS_PROPERTY",
        "NOT_IN",
    }
)
"""Filter operators the CRM Search contract defines."""

_FILTER_KEYS: Final = frozenset({"propertyName", "operator", "value", "highValue"})
"""String-valued filter keys; ``values`` (a list) is not expressible as ``Mapping[str, str]``."""

# A CRM object type as it appears in the request path: `deals`, `contacts`, or a custom
# object's `p_{name}` or `2-{id}` form. Anything else would not be a single path segment.
_OBJECT_TYPE_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]*")

Completeness = Literal["complete", "incomplete"]
"""Completeness of a search over its range, as ``dataset_freshness`` records it."""


# ---------------------------------------------------------------------------
# Pager protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SearchPager(Protocol):
    """Sends one CRM Search page request; ``HubSpotClient`` satisfies it.

    The implementation owns HTTP, the retry policy, error classification, the 4 req/s
    throttle and the request checks (serialised body at most 3,000 characters,
    ``limit <= 200``), and decodes the response with
    :func:`ads_engine.connectors.hubspot.parser.parse_search_page`.
    """

    async def search_page(self, object_type: str, body: Mapping[str, object]) -> SearchPage:
        """Run one CRM Search request and return its decoded page.

        Args:
            object_type: The CRM object type in the path, ``deals`` or ``contacts``.
            body: The ``PublicObjectSearchRequest`` body, serialised as JSON unchanged.

        Returns:
            The page's raw records, the query's ``total`` and ``paging.next.after``.

        Raises:
            AccessDenied: When HubSpot refuses the token or its scopes.
            TransientError: When the call still fails after the client's retries.
            QueryDefect: When the request or the response breaks the contract.
        """
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_aware(dt: datetime, what: str) -> None:
    """Raise ``ValueError`` unless ``dt`` is timezone-aware."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{what} must be a timezone-aware datetime, got a naive one")


def epoch_ms(dt: datetime) -> str:
    """Return an instant as integer epoch milliseconds in decimal text.

    CRM Search compares datetime properties against epoch-millisecond strings. HubSpot stores
    instants at millisecond precision, so a sub-millisecond instant is rounded *up* to the
    next whole millisecond: a record at millisecond ``m`` is at or after ``dt`` exactly when
    ``m >= epoch_ms(dt)``, which keeps both ``GTE`` and ``LT`` bounds of a half-open range
    exact and adjacent partitions gap-free.

    Example::

        >>> epoch_ms(datetime(2026, 9, 1, tzinfo=UTC))
        '1788220800000'

    Args:
        dt: A timezone-aware instant, in any time zone.

    Returns:
        The epoch milliseconds, for example ``"1788220800000"``.

    Raises:
        ValueError: When ``dt`` is naive.
    """
    _require_aware(dt, "instant")
    milliseconds, remainder = divmod(dt - _EPOCH, _MILLISECOND)
    if remainder:
        milliseconds += 1
    return str(milliseconds)


def _require_name(value: object, what: str) -> str:
    """Return a non-blank property name, or raise ``ValueError`` naming ``what``."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{what} must be a non-blank property name without surrounding spaces")
    return value


def _freeze_filter(raw: Mapping[str, str], index: int) -> Mapping[str, str]:
    """Validate one extra filter against the contract and return an immutable copy."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"extra_filters[{index}] must be a mapping")
    unknown = sorted(str(key) for key in raw if key not in _FILTER_KEYS)
    if unknown:
        raise ValueError(f"extra_filters[{index}] has keys the contract does not define: {unknown}")
    _require_name(raw.get("propertyName"), f"extra_filters[{index}].propertyName")
    operator = raw.get("operator")
    if operator not in _OPERATORS:
        raise ValueError(f"extra_filters[{index}].operator must be one of {sorted(_OPERATORS)}")
    for key in ("value", "highValue"):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError(f"extra_filters[{index}].{key} must be a string")
    return MappingProxyType(dict(raw))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """What a partitioned search filters on, sorts by and returns.

    Every request carries one filter group (its filters are ANDed): the partition's range on
    :attr:`range_property` followed by :attr:`extra_filters`.

    Attributes:
        range_property: The datetime property the range filters on, such as ``createdate``.
        properties: The properties each returned record carries, in request order.
        extra_filters: Further filters ANDed with the range, each a contract filter object
            such as ``{"propertyName": "google_campaign_id", "operator": "HAS_PROPERTY"}``.
            Stored as read-only copies.
        sort_property: The property results are sorted by, ascending; ``None`` sorts by
            :attr:`range_property`.

    Raises:
        ValueError: When a property name is blank, ``properties`` is empty, an extra filter
            breaks the contract, or the group would exceed the contract's six filters.
    """

    range_property: str
    properties: tuple[str, ...]
    extra_filters: tuple[Mapping[str, str], ...] = ()
    sort_property: str | None = None

    def __post_init__(self) -> None:
        """Validate the names and filters and freeze copies of the collections."""
        _require_name(self.range_property, "range_property")
        if self.sort_property is not None:
            _require_name(self.sort_property, "sort_property")
        # Typed as a tuple, but a caller may still pass one bare string, whose characters
        # would otherwise be read as property names.
        given: object = self.properties
        if isinstance(given, str):
            raise ValueError("properties must be a sequence of property names, not one string")
        names = tuple(dict.fromkeys(self.properties))
        if not names:
            raise ValueError("properties must name at least one property")
        for index, name in enumerate(names):
            _require_name(name, f"properties[{index}]")
        filters = tuple(_freeze_filter(raw, index) for index, raw in enumerate(self.extra_filters))
        if _RANGE_FILTERS + len(filters) > _MAX_FILTERS_PER_GROUP:
            raise ValueError(
                f"a filter group holds at most {_MAX_FILTERS_PER_GROUP} filters; the range "
                f"uses {_RANGE_FILTERS}, so at most {_MAX_FILTERS_PER_GROUP - _RANGE_FILTERS} "
                "extra filters fit"
            )
        object.__setattr__(self, "properties", names)
        object.__setattr__(self, "extra_filters", filters)

    @property
    def sort_by(self) -> str:
        """The property results are sorted by: :attr:`sort_property` or the range property."""
        return self.sort_property if self.sort_property is not None else self.range_property


def deal_search_filter() -> SearchFilter:
    """Return the window deal search: ``createdate`` in range, sorted by ``createdate``.

    The search reads every portal deal created from window start to run time with the deal
    properties ``crm_deal`` stores (:data:`~ads_engine.connectors.hubspot.parser.DEAL_PROPERTIES`),
    so the orphan-deal share is measured over all of them (AAP 0.3.3, 0.5.4.7).

    Returns:
        The filter, sorted by ``createdate`` ascending.
    """
    return SearchFilter(
        range_property="createdate",
        properties=tuple(DEAL_PROPERTIES),
        sort_property="createdate",
    )


def google_contacts_filter(
    campaign_property: str, touch_property: str, properties: Sequence[str]
) -> SearchFilter:
    """Return the Google-touch contact discovery: touch date in range, campaign property set.

    Call it only when both ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` and
    ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`` are configured and verified by the properties API;
    without them the Google-to-CRM link is unavailable and no discovery runs (AAP 0.3.3). The
    search finds Google-only contacts that never submitted a LinkedIn form.

    Args:
        campaign_property: The contact property holding the Google campaign id; it must be
            set (``HAS_PROPERTY``) on every contact returned.
        touch_property: The datetime contact property holding the touch instant; the range
            filters and sorts on it.
        properties: The contact properties to return. Both configured properties are appended
            when absent, because discovery reads the campaign id and the touch instant.

    Returns:
        The filter, sorted by the touch-date property ascending.

    Raises:
        ValueError: When a property name is blank or ``properties`` is a single string.
    """
    _require_name(campaign_property, "campaign_property")
    _require_name(touch_property, "touch_property")
    if isinstance(properties, str):
        raise ValueError("properties must be a sequence of property names, not one string")
    names = (*properties, campaign_property, touch_property)
    return SearchFilter(
        range_property=touch_property,
        properties=tuple(dict.fromkeys(names)),
        extra_filters=({"propertyName": campaign_property, "operator": "HAS_PROPERTY"},),
    )


def search_body(
    search_filter: SearchFilter,
    lo: datetime,
    hi: datetime,
    *,
    after: str | None = None,
    limit: int = PAGE_SIZE,
) -> dict[str, object]:
    """Build one ``PublicObjectSearchRequest`` body for the partition ``[lo, hi)``.

    The shape follows ``backend/tests/contracts/hubspot/crm_search.json``::

        {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "createdate",
                            "operator": "GTE",
                            "value": "<epoch ms of lo>",
                        },
                        {
                            "propertyName": "createdate",
                            "operator": "LT",
                            "value": "<epoch ms of hi>",
                        },
                        *extra_filters,
                    ]
                }
            ],
            "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
            "properties": ["createdate", "dealstage", "pipeline", "amount"],
            "limit": 200,
            "after": "<paging.next.after>",
        }

    ``GTE``/``LT`` rather than the inclusive ``BETWEEN`` keeps the range half-open, so a record
    on a shared partition boundary is read by exactly one partition. ``after`` is omitted on a
    partition's first page.

    Args:
        search_filter: The range property, extra filters, sort and properties.
        lo: The inclusive, timezone-aware lower bound.
        hi: The exclusive, timezone-aware upper bound.
        after: ``paging.next.after`` of the previous page, or ``None`` for the first page.
        limit: Records per page, from 1 to :data:`PAGE_SIZE`.

    Returns:
        A fresh body that serialises to JSON as is.

    Raises:
        ValueError: When a bound is naive, ``limit`` is outside 1 to 200, or ``after`` is blank.
    """
    if isinstance(limit, bool) or not 1 <= limit <= PAGE_SIZE:
        raise ValueError(f"limit must be from 1 to {PAGE_SIZE}")
    if after is not None and not after.strip():
        raise ValueError("after must be a non-blank paging cursor or None")
    filters: list[dict[str, str]] = [
        {"propertyName": search_filter.range_property, "operator": "GTE", "value": epoch_ms(lo)},
        {"propertyName": search_filter.range_property, "operator": "LT", "value": epoch_ms(hi)},
    ]
    filters.extend(dict(extra) for extra in search_filter.extra_filters)
    body: dict[str, object] = {
        "filterGroups": [{"filters": filters}],
        "sorts": [{"propertyName": search_filter.sort_by, "direction": "ASCENDING"}],
        "properties": list(search_filter.properties),
        "limit": limit,
    }
    if after is not None:
        body["after"] = after
    return body


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """The records a partitioned search read and how complete they are.

    Attributes:
        rows: The raw records, de-duplicated by ``id`` (first occurrence kept), in partition
            then page order. Callers parse them with ``parser.parse_deal`` or
            ``parser.parse_contact``.
        completeness: ``complete`` when every partition was read to its last page and
            returned at least its reported ``total`` of distinct records; ``incomplete`` when
            any partition hit the ceiling at one hour, ended its paging short of its
            ``total``, or was cut short by a transient failure.
        reason: Why the outcome is incomplete (distinct reasons joined by ``"; "``, each
            :data:`CEILING_REASON`, :data:`SHORT_READ_REASON` or a transient failure's
            reason), or ``None`` when it is complete.
        partitions_incomplete: The UTC ``[lo, hi)`` partitions not read in full, ascending.
        calls: Every ``search_page`` call made, including discarded overflow pages and the
            call that failed.
    """

    rows: tuple[HubSpotRecord, ...]
    completeness: Completeness
    reason: str | None
    partitions_incomplete: tuple[tuple[datetime, datetime], ...]
    calls: int

    @property
    def is_complete(self) -> bool:
        """Whether every partition of the range was read in full."""
        return self.completeness == "complete"


# ---------------------------------------------------------------------------
# Partitioned search
# ---------------------------------------------------------------------------

_Partition = tuple[datetime, datetime]
_ReadResult = Literal["done", "ceiling", "short", "split"]


def _defect(message: str) -> QueryDefect:
    """Build the defect for a CRM Search response that breaks the paging contract."""
    return QueryDefect(message, source=_SOURCE, endpoint=_SURFACE, code="validation")


def _day_partitions(start: datetime, end: datetime) -> list[_Partition]:
    """Cut ``[start, end)`` into consecutive one-day partitions, the last one shorter."""
    partitions: list[_Partition] = []
    lo = start
    while lo < end:
        hi = min(lo + DAY, end)
        partitions.append((lo, hi))
        lo = hi
    return partitions


def _split_point(lo: datetime, hi: datetime) -> datetime:
    """Return the whole-hour split of a partition longer than one hour.

    ``mid = lo + floor(hours / 2) hours`` with ``hours = ceil((hi - lo) / 1 h)``, so a day
    splits 24 → 12 → 6 → 3 → 1 + 2 → 1 + 1. For any span above one hour, ``hours >= 2`` and
    ``floor(hours / 2) <= hours - 1``, so ``lo < mid < hi`` and both halves are shorter.
    """
    whole, remainder = divmod(hi - lo, MIN_PARTITION)
    hours = whole + (1 if remainder else 0)
    return lo + (hours // 2) * MIN_PARTITION


def _cursor_offset(after: str) -> int | None:
    """Return a numeric ``after`` cursor as an int; ``None`` for an opaque cursor.

    CRM Search cursors are result offsets in decimal text (``"200"``), so one at or past the
    ceiling names a page HubSpot would reject with HTTP 400.
    """
    return int(after) if after.isascii() and after.isdigit() else None


def _record_id(record: HubSpotRecord) -> str:
    """Return a record's ``id`` as text, the de-duplication key."""
    value = record.get("id")
    if isinstance(value, bool) or not isinstance(value, str | int) or not str(value).strip():
        raise _defect("search result record has no usable 'id'")
    return str(value)


class _PartitionedSearch:
    """Mutable state of one :func:`search_partitioned` call."""

    __slots__ = ("calls", "object_type", "pager", "rows", "search_filter", "seen")

    def __init__(self, pager: SearchPager, object_type: str, search_filter: SearchFilter) -> None:
        self.pager = pager
        self.object_type = object_type
        self.search_filter = search_filter
        self.calls = 0
        self.rows: list[HubSpotRecord] = []
        self.seen: set[str] = set()

    def keep(self, records: Sequence[HubSpotRecord]) -> None:
        """Append records not seen before, keeping the first occurrence of each id."""
        for record in records:
            key = _record_id(record)
            if key not in self.seen:
                self.seen.add(key)
                self.rows.append(record)

    async def read(self, lo: datetime, hi: datetime, partial: list[HubSpotRecord]) -> _ReadResult:
        """Read one partition into ``partial``, page by page.

        Completeness is judged on the distinct record ids the partition returned, never on raw
        page lengths, against the ``total`` of its latest page; raw page lengths only bound
        each request's ``limit`` so that no request goes past the 10,000-result ceiling.

        Args:
            lo: The partition's inclusive lower bound.
            hi: The partition's exclusive upper bound.
            partial: Receives the partition's records as pages arrive, so the caller keeps
                them when a transient failure interrupts the partition. Cleared on a split.

        Returns:
            ``done`` when paging ended and the partition returned at least ``total`` distinct
            records; ``split`` when the partition is longer than one hour and holds over
            10,000 results; ``ceiling`` when a partition of at most one hour holds over 10,000
            results and was read up to the ceiling, or when the ceiling stopped the read
            before ``total`` distinct records arrived; ``short`` when paging ended (a page
            without a cursor) with fewer than ``total`` distinct records.

        Raises:
            TransientError: When a page still fails after the pager's retries.
            AccessDenied: When HubSpot refuses the token.
            QueryDefect: When a request or response breaks the contract: a record without a
                usable id, a cursor returned with an empty page, a cursor that repeats any
                earlier cursor of the partition, or a numeric cursor that does not exceed a
                numeric previous cursor. Raised before another request is made.
        """
        splittable = hi - lo > MIN_PARTITION
        after: str | None = None
        read = 0
        unique: set[str] = set()
        cursors: set[str] = set()
        while True:
            body = search_body(
                self.search_filter,
                lo,
                hi,
                after=after,
                limit=min(PAGE_SIZE, RESULT_CEILING - read),
            )
            self.calls += 1
            page = await self.pager.search_page(self.object_type, body)
            if page.total > RESULT_CEILING and splittable:
                partial.clear()
                return "split"
            unique.update(_record_id(record) for record in page.results)
            partial.extend(page.results)
            read += len(page.results)
            next_after = page.after
            if next_after is None:
                # Paging ended. A partition still above the ceiling here is at most one hour
                # long (a longer one was split above), so it stays incomplete; otherwise the
                # page must have delivered every match the query counts.
                if page.total > RESULT_CEILING:
                    return "ceiling"
                if len(unique) < page.total:
                    return "short"
                return "done"
            if not page.results:
                raise _defect("search page carried a paging cursor but no results")
            if next_after in cursors:
                raise _defect(
                    "search paging cursor did not advance"
                    if next_after == after
                    else "search paging cursor repeated an earlier cursor"
                )
            offset = _cursor_offset(next_after)
            previous = None if after is None else _cursor_offset(after)
            if offset is not None and previous is not None and offset <= previous:
                raise _defect("search paging cursor did not advance")
            cursors.add(next_after)
            if read >= RESULT_CEILING or (offset is not None and offset >= RESULT_CEILING):
                # The next page would lie past the 10,000-result ceiling, which HubSpot rejects
                # with HTTP 400, so it is never requested.
                if len(unique) >= page.total:
                    return "done"
                if splittable:
                    partial.clear()
                    return "split"
                return "ceiling"
            after = next_after


async def search_partitioned(
    pager: SearchPager,
    object_type: str,
    search_filter: SearchFilter,
    time_range: tuple[datetime, datetime],
) -> SearchOutcome:
    """Read every record whose range property falls in ``time_range``, or state a floor.

    The range is cut into one-day partitions read in ascending order. A partition whose
    ``total`` exceeds 10,000 is halved at a whole hour down to one hour; a one-hour partition
    still above the ceiling is read up to 10,000 results and marked incomplete. Pages are
    followed through ``paging.next.after`` at 200 records each. A partition whose paging ends
    with fewer distinct records than its ``total`` keeps its rows and is marked incomplete
    with :data:`SHORT_READ_REASON`. See the module docstring for the full procedure.

    Args:
        pager: Sends each page request (``HubSpotClient``, or the simulator-backed client).
        object_type: The CRM object type in the path, ``deals`` or ``contacts``.
        search_filter: The range property, extra filters, sort and properties, normally
            :func:`deal_search_filter` or :func:`google_contacts_filter`.
        time_range: ``(start, end)``, timezone-aware, start inclusive and end exclusive. An
            empty range (``start == end``) makes no call and is complete.

    Returns:
        The de-duplicated records, the completeness with its reason, the partitions not read
        in full and the number of calls made.

    Raises:
        ValueError: When ``object_type`` is not a path segment, a bound is naive, or the range
            is reversed. Raised before any call.
        AccessDenied: When HubSpot refuses the token or its scopes; propagated unchanged.
        QueryDefect: When a request or response breaks the contract, propagated unchanged, or
            when a page breaks the paging contract (a record without a usable ``id``, a
            cursor on an empty page, a repeated cursor or a numeric cursor that does not
            advance).
    """
    if not isinstance(object_type, str) or not _OBJECT_TYPE_RE.fullmatch(object_type):
        raise ValueError("object_type must be a CRM object type such as 'deals' or 'contacts'")
    start, end = time_range
    _require_aware(start, "time_range start")
    _require_aware(end, "time_range end")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end < start:
        raise ValueError("time_range end must not precede its start")

    search = _PartitionedSearch(pager, object_type, search_filter)
    queue: deque[_Partition] = deque(_day_partitions(start, end))
    incomplete: list[_Partition] = []
    reasons: list[str] = []
    failure: TransientError | None = None
    partitions_read = 0
    splits = 0

    while queue:
        lo, hi = queue.popleft()
        partial: list[HubSpotRecord] = []
        try:
            result = await search.read(lo, hi, partial)
        except TransientError as error:
            # Retries are exhausted: keep what was read, list the current partition and every
            # partition not yet read, and let the dataset turn incomplete rather than fail.
            search.keep(partial)
            incomplete.append((lo, hi))
            incomplete.extend(queue)
            queue.clear()
            reasons.append(error.reason)
            failure = error
            break
        except (AccessDenied, QueryDefect) as error:
            log.warning(
                "hubspot.search_failed",
                object_type=object_type,
                range_property=search_filter.range_property,
                partitions=partitions_read,
                splits=splits,
                calls=search.calls,
                rows=len(search.rows),
                **error.log_fields,
            )
            raise
        if result == "split":
            mid = _split_point(lo, hi)
            queue.appendleft((mid, hi))
            queue.appendleft((lo, mid))
            splits += 1
            continue
        search.keep(partial)
        partitions_read += 1
        if result == "ceiling":
            incomplete.append((lo, hi))
            reasons.append(CEILING_REASON)
        elif result == "short":
            # Totals can race deletions, so paging that ends early is an incomplete read of
            # the rows it returned, not a defect that would fail the whole search.
            incomplete.append((lo, hi))
            reasons.append(SHORT_READ_REASON)

    completeness: Completeness = "incomplete" if incomplete else "complete"
    outcome = SearchOutcome(
        rows=tuple(search.rows),
        completeness=completeness,
        reason="; ".join(dict.fromkeys(reasons)) if reasons else None,
        partitions_incomplete=tuple(incomplete),
        calls=search.calls,
    )
    fields: dict[str, object] = {
        "object_type": object_type,
        "range_property": search_filter.range_property,
        "extra_filter_properties": [extra["propertyName"] for extra in search_filter.extra_filters],
        "partitions": partitions_read,
        "splits": splits,
        "calls": outcome.calls,
        "rows": len(outcome.rows),
        "completeness": outcome.completeness,
        "partitions_incomplete": len(outcome.partitions_incomplete),
    }
    if failure is not None:
        fields.update(failure.log_fields)
    if outcome.is_complete:
        log.info("hubspot.search", **fields)
    else:
        log.warning("hubspot.search", **fields)
    return outcome
