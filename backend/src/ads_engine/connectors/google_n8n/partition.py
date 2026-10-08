"""Completeness partitioning and safe absence for the Google Ads webhook connector.

``googleAds:search`` returns at most :data:`~ads_engine.connectors.google_n8n.queries.PAGE_SIZE`
(10,000) rows per page, and the n8n response the user specified carries no page token. A response
of exactly 10,000 rows with no token therefore has unknown completeness, and a truncated response
cannot reveal the ids it omitted. **No step here relies on an id list read off a capped
response.** Completeness comes only from narrowing forms whose completeness can be proven, using
two GAQL features: numeric comparisons on INT64 ids, and ``ORDER BY ... LIMIT``.

:class:`PartitionedFetcher` binds the webhook client ``sync`` owns, and so its call budget, at
construction. Its ``partitioned_fetch(query_id, scope)`` reads one template of
:data:`~ads_engine.connectors.google_n8n.queries.QUERIES` to completion by its ``strategy``:

* ``single`` (Q-G1, Q-G8): one query. A capped result is kept and reported ``incomplete``
  ("result limit reached"); neither template comes near 10,000 rows in practice.
* ``keyset`` (Q-G2, Q-G4): pages of ``... AND {id} > {last_id} ORDER BY {id} LIMIT 9999``.
  Every page stays below the page size, so no page can be silently truncated. A page of exactly
  :data:`~ads_engine.connectors.google_n8n.queries.KEYSET_LIMIT` (9,999) rows continues from its
  largest id; the first shorter page ends the scan. 25,000 campaigns read as pages of 9,999,
  9,999 and 5,002 rows with strictly increasing ``campaign.id >`` filters.
* ``by_ids`` (Q-G2b): the sorted, de-duplicated ids in ``IN`` lists of at most
  :data:`~ads_engine.connectors.google_n8n.queries.MAX_IDS_PER_IN` (1,000). Q-G2b returns at
  most one row per campaign, so every successful chunk is complete.
* ``ad_group_ranges`` (Q-G7): ``ad_group.id`` ranges from ``[0, INT64_MAX]``, halved while
  capped. A single ad group that is still capped is read by keyset on
  ``ad_group_criterion.criterion_id``, which is unique only within its ad group.
* ``dated`` (Q-G3, Q-G5, Q-G6): a capped partition splits the first dimension that is not yet
  single, in this order: the date range down to single days; at one day, a numeric
  ``campaign.id`` range down to one campaign; at one day and one campaign, a numeric
  ``ad_group.id`` range down to one ad group (the campaign filter stays). At one (day, ad group)
  Q-G5 switches to the criterion keyset, and Q-G6, whose search-term rows have no unique
  sortable id, keeps the 10,000 rows as returned and reports the fetch ``incomplete`` ("result
  limit reached at finest partition"). Q-G3 has one row per campaign per day, so a capped
  campaign-day breaks its contract and is a ``QueryDefect``. Ranges are numeric, never lists of
  known ids, so a campaign removed during the window is still covered.

Partitions are processed from an explicit stack, depth first and deterministically: dates
ascending, then ranges ascending (children are pushed in reverse). A capped response's rows are
discarded when it is split, and its children re-read them. Partitions are disjoint, and every
accepted row is checked against the partition it was read for (its date, id ranges, ad group and
keyset position), so a relay that ignored a filter fails as a ``QueryDefect`` instead of
duplicating rows.

Termination:
    Every step either accepts a partition, replaces it by two partitions that are strictly
    smaller in the order (date span, campaign span, ad-group span), switches one single
    (day, ad group) or single ad group to a keyset once, or continues a keyset from a strictly
    greater ``last_id`` bounded by ``INT64_MAX``. Each of these orders is finite, so every fetch
    ends with every row read, unless the call budget or a transient failure interrupts it.

Cost:
    Descending from ``[0, INT64_MAX]`` costs about one call per halving level plus one per empty
    sibling: isolating one heavy campaign takes up to 63 halvings, so up to about 126 calls, and
    isolating one ad group inside it as many again. Every call, partition calls and retries
    included, counts against the product's own cap of ``GOOGLE_DAILY_CALL_BUDGET`` webhook calls
    per sliding 24 h, and deferral (below) bounds what one sync spends.

Interruption:
    A ``TransientError`` that survives ``send_json``'s retries, ``CallBudgetExhausted`` included,
    stops the fetch at once. Rows already accepted are kept; the interrupted partition, whose
    partial pages are discarded because page tokens expire, and every partition still on the
    stack are returned in :attr:`FetchOutcome.deferred`, in the order they would have been read.
    Each entry is a scope :meth:`PartitionedFetcher.partitioned_fetch` accepts, so ``sync``
    resumes them in a later 24 h window; the dataset stays ``incomplete`` until they finish,
    which ``sync`` records.
    ``AccessDenied`` and ``QueryDefect`` propagate unchanged: ``sync.mode`` switches the source
    to simulated data or fails the job.

Safe absence:
    :func:`enumerate_campaigns` runs the complete Q-G2 keyset scan and re-reads, through Q-G2b
    with no status filter, the tracked campaigns the scan no longer returned. A campaign is
    reported ``removed`` or ``missing`` only when both steps completed; otherwise nothing is
    inferred and the previous snapshot stands. :func:`enumerate_entities` applies the same rule to
    ad groups (Q-G4) and keywords (Q-G7).

Reason strings:
    Every ``reason`` begins with a stable prefix that ``ads_engine.sync.freshness`` recognises by
    prefix (that module cannot be imported here, because connectors never import ``sync``):
    :data:`REASON_BUDGET` and :data:`REASON_INTERRUPTED` are repairable by a refetch;
    :data:`REASON_FINEST_PARTITION` and :data:`REASON_RESULT_LIMIT` are the platform's own limits.
    A repairable reason always comes first, so a deferred shortfall is never masked. Reasons and
    logs never carry GAQL text or id lists; a finest-partition reason names at most three
    (day, ad group) partitions.

Example:
    ``sync`` reads a metrics range and records its completeness::

        fetcher = PartitionedFetcher(client)
        outcome = await fetcher.partitioned_fetch(
            "Q-G6", {"start": date(2026, 9, 1), "end": date(2026, 9, 28)}
        )
        rows = [PARSERS["row_search_term_view"](row) for row in outcome.rows]
        if not outcome.complete:
            ...  # google.search_terms is incomplete with outcome.reason
        for scope in outcome.deferred:
            ...  # resumed later with fetcher.partitioned_fetch("Q-G6", scope)

Only the standard library, :mod:`ads_engine.logging`, :mod:`ads_engine.connectors.errors` and
this package's ``queries``, ``parser`` and ``client`` are imported. Nothing here imports ``sync``,
``simulation``, ``db``, ``analysis`` or ``api``.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal, overload

from ads_engine.connectors.errors import (
    AccessDenied,
    CallBudgetExhausted,
    QueryDefect,
    TransientError,
)
from ads_engine.connectors.google_n8n.client import (
    GoogleAdsRow,
    GoogleAdsWebhookClient,
    QueryResult,
)
from ads_engine.connectors.google_n8n.parser import get_field
from ads_engine.connectors.google_n8n.queries import (
    AD_GROUP_HI,
    AD_GROUP_ID,
    AD_GROUP_LO,
    CAMPAIGN_HI,
    CAMPAIGN_LO,
    END,
    IDS,
    INT64_MAX,
    INT64_MIN_ID,
    KEYSET_LIMIT,
    LAST_ID,
    MAX_IDS_PER_IN,
    PAGE_SIZE,
    QUERIES,
    START,
    QueryTemplate,
)
from ads_engine.logging import get_logger

__all__ = [
    "REASON_BUDGET",
    "REASON_FINEST_PARTITION",
    "REASON_INTERRUPTED",
    "REASON_RESULT_LIMIT",
    "CampaignEnumeration",
    "Completeness",
    "EntityEnumeration",
    "EntityQueryId",
    "FetchOutcome",
    "PartitionedFetcher",
    "enumerate_campaigns",
    "enumerate_entities",
]

log = get_logger(__name__)

# ---------------------------------------------------------------------------------------------
# Reason prefixes
# ---------------------------------------------------------------------------------------------

REASON_BUDGET: Final = "call budget exhausted"
"""Prefix of a fetch the call budget interrupted: ``"call budget exhausted; 3 partitions
deferred"``. Equals ``ads_engine.sync.freshness.REASON_DEFERRED`` (repairable)."""

REASON_INTERRUPTED: Final = "interrupted"
"""Prefix of a fetch another transient failure interrupted after its retries:
``"interrupted: google search Q-G5: HTTP 503: ...; 1 partition deferred"``. Equals
``ads_engine.sync.freshness.REASON_INTERRUPTED`` (repairable)."""

REASON_FINEST_PARTITION: Final = "result limit reached at finest partition"
"""Prefix of a Q-G6 fetch in which a single (day, ad group) still returned 10,000 rows:
``"result limit reached at finest partition (2026-09-14, ad group 123)"``. Equals
``ads_engine.sync.freshness.REASON_FINEST_PARTITION`` (permanent)."""

REASON_RESULT_LIMIT: Final = "result limit reached"
"""Reason of a capped ``single`` query (Q-G1, Q-G8), which has nothing to partition by."""

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

_SOURCE: Final = "google"
_SURFACE: Final = "search"
_FULL_RANGE: Final[tuple[int, int]] = (INT64_MIN_ID, INT64_MAX)
_ONE_DAY: Final = timedelta(days=1)

# GAQL fields read off raw rows: partition keys, keyset ids and the Q-G2b status.
_DATE_FIELD: Final = "segments.date"
_CAMPAIGN_FIELD: Final = "campaign.id"
_AD_GROUP_FIELD: Final = "ad_group.id"
_CRITERION_FIELD: Final = "ad_group_criterion.criterion_id"
_CAMPAIGN_STATUS_FIELD: Final = "campaign.status"
_REMOVED: Final = "REMOVED"

# Q-G2 and Q-G2b select the same fields; Q-G2b's IN list is on campaign.id.
_SCAN_QUERY: Final = "Q-G2"
_BY_IDS_QUERY: Final = "Q-G2b"
_ID_LIST_FIELD: Final = _CAMPAIGN_FIELD

_ENTITY_QUERY_IDS: Final[frozenset[str]] = frozenset({"Q-G4", "Q-G7"})

# A finest-partition reason names at most this many (day, ad group) partitions, then a count.
_MAX_NAMED_PARTITIONS: Final = 3

_ID_TEXT_RE: Final = re.compile(r"[0-9]{1,19}")
_DATE_TEXT_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Same rule as client.py: a query id is echoed in an error only when it looks like one, so GAQL
# text passed by mistake never reaches a message.
_QUERY_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

# ---------------------------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------------------------

Completeness = Literal["complete", "incomplete"]
"""Whether a fetch read every row of its scope. ``sync`` stores it in ``dataset_freshness``."""

EntityQueryId = Literal["Q-G4", "Q-G7"]
"""The entity enumerations :func:`enumerate_entities` runs: ad groups and keywords."""


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """Every row one :meth:`PartitionedFetcher.partitioned_fetch` call read, and its completeness.

    Attributes:
        query_id: The template id, such as ``"Q-G6"``.
        rows: The accepted REST ``GoogleAdsRow`` objects, in partition order. Partitions are
            disjoint, so no row appears twice; rows of capped responses that were split are not
            included, because the narrower partitions re-read them.
        completeness: ``complete`` when every partition was read in full; ``incomplete`` when
            the fetch was interrupted (see :attr:`deferred`), a single (day, ad group) of Q-G6
            or a ``single`` query hit the result limit.
        reason: Why the fetch is incomplete, distinct reasons joined with ``"; "``, a
            repairable one (:data:`REASON_BUDGET`, :data:`REASON_INTERRUPTED`) first; ``None``
            when complete. Never GAQL text or an id list.
        calls: POST attempts made during this fetch, retries and next pages included: the
            difference of :attr:`GoogleAdsWebhookClient.calls_made` before and after. A client
            shared by concurrent fetches counts their calls too; ``sync`` runs one fetch per
            client at a time.
        partitions_read: Partition queries answered, capped responses that were split included.
        deferred: The unread partitions after an interruption, the interrupted one first and
            then in the order they would have been read. Each entry is an immutable scope that
            ``PartitionedFetcher(client).partitioned_fetch(query_id, entry)`` accepts. Empty
            unless interrupted.
        interrupted_by: The ``TransientError`` (``CallBudgetExhausted`` included) that stopped
            the fetch, or ``None``.
    """

    query_id: str
    rows: tuple[GoogleAdsRow, ...]
    completeness: Completeness
    reason: str | None
    calls: int
    partitions_read: int
    deferred: tuple[Mapping[str, object], ...]
    interrupted_by: TransientError | None

    @property
    def complete(self) -> bool:
        """Whether :attr:`completeness` is ``complete``."""
        return self.completeness == "complete"


@dataclass(frozen=True, slots=True)
class CampaignEnumeration:
    """The current campaign list and the safe-absence verdict for tracked campaigns.

    ``sync`` snapshots :attr:`present` and :attr:`removed` and raises a ``missing`` discrepancy
    for :attr:`missing_ids`. It also derives shared-budget presence from :attr:`present` plus
    :attr:`removed` (each row carries its ``campaign_budget``), so :attr:`complete` governs any
    budget ``missing`` inference as well.

    Attributes:
        present: Q-G2 rows of every non-removed campaign, followed by Q-G2b rows of re-read
            tracked campaigns whose status is not ``REMOVED``.
        removed: Q-G2b rows whose raw ``campaign.status`` is ``REMOVED``; ``sync`` snapshots them
            as ``ended``. Empty unless :attr:`complete`.
        missing_ids: Tracked campaign ids that neither the complete Q-G2 scan nor Q-G2b
            returned. Empty unless :attr:`complete`.
        complete: Whether the Q-G2 scan ended on a short page with no failed or deferred page
            and every Q-G2b chunk succeeded. When ``False`` no removal or disappearance may be
            inferred: the previous snapshot stands and ``google.entities`` is incomplete.
        reason: The failing step's reason; ``None`` when complete.
        calls: POST attempts made by both steps.
        interrupted_by: The transient error that stopped the failing step, or ``None``.
    """

    present: tuple[GoogleAdsRow, ...]
    removed: tuple[GoogleAdsRow, ...]
    missing_ids: frozenset[int]
    complete: bool
    reason: str | None
    calls: int
    interrupted_by: TransientError | None


@dataclass(frozen=True, slots=True)
class EntityEnumeration:
    """Every current ad group (Q-G4) or keyword (Q-G7) and the tracked keys no longer returned.

    Attributes:
        rows: The enumeration's rows. Q-G7 returns every keyword, keywords with no performance
            rows included, so ``sync`` writes keyword rows and quality-score snapshots for them.
        absent_keys: Tracked keys the complete enumeration did not return: ``ad_group.id``
            values for Q-G4, ``(ad_group.id, ad_group_criterion.criterion_id)`` pairs for Q-G7
            (criterion ids are unique only within an ad group). Empty unless :attr:`complete`.
        complete: Whether the enumeration read every row; when ``False`` nothing is inferred.
        reason: Why the enumeration is incomplete; ``None`` when complete.
        calls: POST attempts made.
        interrupted_by: The transient error that stopped the enumeration, or ``None``.
    """

    rows: tuple[GoogleAdsRow, ...]
    absent_keys: frozenset[int] | frozenset[tuple[int, int]]
    complete: bool
    reason: str | None
    calls: int
    interrupted_by: TransientError | None


@dataclass(frozen=True, slots=True)
class _Step:
    """What reading one partition decided.

    Attributes:
        rows: Rows accepted from the partition.
        children: Partitions that replace it, in reading order: two halves of a capped range,
            a keyset continuation, or the switch to a criterion keyset.
        finest_overflow: The (day, ad group) of a Q-G6 partition kept at the result limit.
        capped_kept: Whether a capped ``single`` query was kept as returned.
    """

    rows: tuple[GoogleAdsRow, ...] = ()
    children: tuple[dict[str, object], ...] = ()
    finest_overflow: tuple[date, int] | None = None
    capped_kept: bool = False


# ---------------------------------------------------------------------------------------------
# partitioned_fetch
# ---------------------------------------------------------------------------------------------


class PartitionedFetcher:
    """The connector's ``partitioned_fetch(query_id, scope)`` entry point, bound to one client.

    ``sync`` builds one fetcher per Google webhook client it owns. The client, and with it the
    call budget ``sync`` injected into it (``GoogleCallBudget`` for live calls, ``UNMETERED``
    for simulated ones), is bound at construction, so every fetch names only a template and its
    scope. The fetcher holds no other state: it reads no settings and holds no credentials
    beyond the client's, so one instance serves every fetch of a sync, and a deferred scope
    resumes through the same instance.

    Example:
        ``sync`` reads the campaign list and a metrics range through one fetcher::

            fetcher = PartitionedFetcher(client)
            campaigns = await fetcher.partitioned_fetch("Q-G2")
            daily = await fetcher.partitioned_fetch(
                "Q-G3", {"start": date(2026, 9, 1), "end": date(2026, 9, 28)}
            )
    """

    __slots__ = ("_client",)

    def __init__(self, client: GoogleAdsWebhookClient) -> None:
        """Bind the webhook client every fetch sends through; nothing is sent.

        Args:
            client: The webhook client; ``sync`` owns it, its HTTP client and its call budget.
        """
        self._client: GoogleAdsWebhookClient = client

    @property
    def client(self) -> GoogleAdsWebhookClient:
        """The bound webhook client, whose ``calls_made`` each outcome's ``calls`` comes from."""
        return self._client

    def __repr__(self) -> str:
        """Return the class name and the client's repr; never the URL, secret or customer id."""
        return f"{type(self).__name__}(client={self._client!r})"

    async def partitioned_fetch(
        self, query_id: str, scope: Mapping[str, object] | None = None
    ) -> FetchOutcome:
        """Read one GAQL template to completion, partitioning every capped response.

        The template's ``strategy`` decides how the scope is narrowed (see the module
        docstring). Every partition is one :meth:`GoogleAdsWebhookClient.run_query` on the
        bound client, which follows a returned page token at once.

        Example::

            fetcher = PartitionedFetcher(client)
            outcome = await fetcher.partitioned_fetch("Q-G2")  # starts at last_id 0
            outcome = await fetcher.partitioned_fetch("Q-G2b", {"ids": tracked_absent_ids})
            outcome = await fetcher.partitioned_fetch(
                "Q-G3", {"start": date(2026, 9, 1), "end": date(2026, 9, 28)}
            )

        Args:
            query_id: A key of :data:`~ads_engine.connectors.google_n8n.queries.QUERIES`.
            scope: Narrowing values keyed by the scope-key constants of ``queries.py``, or
                ``None``:

                * ``single`` (Q-G1, Q-G8): none.
                * ``keyset`` (Q-G2, Q-G4): optional ``last_id`` to start after; 0 by default.
                * ``by_ids`` (Q-G2b): ``ids``, any number of ids; duplicates are dropped, and an
                  empty collection reads nothing and is complete with no call.
                * ``ad_group_ranges`` (Q-G7): optional ``ad_group_lo``/``ad_group_hi`` (the
                  full range by default), or ``ad_group_id`` with ``last_id`` to resume a
                  keyset.
                * ``dated`` (Q-G3, Q-G5, Q-G6): required ``start`` and ``end``; optional
                  campaign and ad-group ranges; for Q-G5, ``ad_group_id`` with ``last_id`` on a
                  single day.

                Every :attr:`FetchOutcome.deferred` entry is a valid scope.

        Returns:
            The rows read and their completeness. An interruption by a ``TransientError``
            returns an ``incomplete`` outcome rather than raising.

        Raises:
            AccessDenied: The webhook or Google refused the credentials; propagated unchanged.
            QueryDefect: The request was rejected as malformed, or a response broke the
                contract: a keyset that did not advance or exceeded its limit, a capped Q-G2b
                chunk or Q-G3 campaign-day, or a row outside the partition it was read for.
            ValueError: ``query_id`` is unknown or ``scope`` is invalid for its template;
                nothing is sent.
        """
        return await _partitioned_fetch(self._client, query_id, scope)


async def _partitioned_fetch(
    client: GoogleAdsWebhookClient,
    query_id: str,
    scope: Mapping[str, object] | None = None,
) -> FetchOutcome:
    """Run :meth:`PartitionedFetcher.partitioned_fetch` through an explicitly passed client.

    The safe-absence enumerations call it with the client they were given. Arguments, result,
    logging and errors are those the method documents.
    """
    template = _template(query_id)
    initial = _initial_partitions(template, scope)
    calls_before = client.calls_made
    # The stack's top is its end: children are pushed in reverse, so the first is read first.
    stack: list[dict[str, object]] = list(reversed(initial))
    rows: list[GoogleAdsRow] = []
    finest_overflows: list[tuple[date, int]] = []
    capped_kept = False
    partitions_read = 0
    interrupted_by: TransientError | None = None
    deferred: tuple[Mapping[str, object], ...] = ()
    try:
        while stack:
            partition = stack.pop()
            try:
                result = await client.run_query(template.query_id, partition)
            except TransientError as exc:
                # CallBudgetExhausted included. The partition's partial pages are dropped by
                # run_query and re-read on resume, because page tokens expire.
                interrupted_by = exc
                deferred = tuple(_frozen(item) for item in (partition, *reversed(stack)))
                break
            partitions_read += 1
            step = _read_step(template, partition, result)
            rows.extend(step.rows)
            if step.finest_overflow is not None:
                finest_overflows.append(step.finest_overflow)
            capped_kept = capped_kept or step.capped_kept
            stack.extend(reversed(step.children))
    except (AccessDenied, QueryDefect) as exc:
        log.warning(
            "google.partitioned_fetch_failed",
            **{
                **exc.log_fields,
                "query_id": template.query_id,
                "partitions_read": partitions_read,
                "calls": client.calls_made - calls_before,
                "rows": len(rows),
            },
        )
        raise

    reasons: list[str] = []
    if interrupted_by is not None:
        reasons.append(_interruption_reason(interrupted_by, len(deferred)))
    if capped_kept:
        reasons.append(REASON_RESULT_LIMIT)
    if finest_overflows:
        reasons.append(_finest_partition_reason(finest_overflows))
    completeness: Completeness = "incomplete" if reasons else "complete"
    outcome = FetchOutcome(
        query_id=template.query_id,
        rows=tuple(rows),
        completeness=completeness,
        reason="; ".join(dict.fromkeys(reasons)) if reasons else None,
        calls=client.calls_made - calls_before,
        partitions_read=partitions_read,
        deferred=deferred,
        interrupted_by=interrupted_by,
    )
    _log_outcome(outcome, finest_overflows=len(finest_overflows))
    return outcome


def _template(query_id: str) -> QueryTemplate:
    """Return the template of ``query_id``.

    Raises:
        ValueError: When ``query_id`` is not a key of ``QUERIES``. The id is echoed only when
            it looks like one, so GAQL text passed by mistake never reaches the message.
    """
    template = QUERIES.get(query_id)
    if template is None:
        shown = repr(query_id) if _QUERY_ID_RE.fullmatch(query_id) else "(not an identifier)"
        raise ValueError(
            f"unknown Google Ads query id {shown}; expected one of {', '.join(QUERIES)}"
        )
    return template


def _initial_partitions(
    template: QueryTemplate, scope: Mapping[str, object] | None
) -> list[dict[str, object]]:
    """Validate the caller's scope and turn it into the first partitions, in reading order.

    Args:
        template: The template being fetched.
        scope: The caller's scope, or ``None``.

    Returns:
        One partition, or for Q-G2b one per ``IN`` chunk of at most 1,000 ids (none for an
        empty id collection).

    Raises:
        ValueError: When a key is not accepted by the template, a required key is missing, a
            value fails the template's validation, or a Q-G5 criterion keyset spans more than
            one day (criterion ids repeat across days, so such a keyset could skip rows).
    """
    query_id = template.query_id
    given: dict[str, object] = dict(scope) if scope is not None else {}
    match template.strategy:
        case "by_ids":
            extra = sorted(given.keys() - {IDS})
            if extra:
                raise ValueError(f"{query_id}: scope key {extra[0]!r} is not accepted here")
            if IDS not in given:
                raise ValueError(f"{query_id}: scope key {IDS!r} is required")
            ids = _distinct_ids(query_id, given[IDS])
            return [
                {IDS: ids[index : index + MAX_IDS_PER_IN]}
                for index in range(0, len(ids), MAX_IDS_PER_IN)
            ]
        case "keyset":
            given.setdefault(LAST_ID, INT64_MIN_ID)
        case "ad_group_ranges":
            if not given.keys() & {AD_GROUP_LO, AD_GROUP_HI, AD_GROUP_ID, LAST_ID}:
                given[AD_GROUP_LO], given[AD_GROUP_HI] = _FULL_RANGE
        case "dated":
            if START not in given or END not in given:
                raise ValueError(
                    f"{query_id}: a dated query needs scope keys {START!r} and {END!r}"
                )
        case "single":
            pass
    # render is the single authority on scope keys and values; the text is discarded here and
    # rendered again by run_query.
    template.render(**given)
    if (
        template.strategy == "dated"
        and LAST_ID in given
        and _scope_date(given, START) != _scope_date(given, END)
    ):
        raise ValueError(
            f"{query_id}: a criterion keyset ({AD_GROUP_ID!r} with {LAST_ID!r}) needs a "
            f"single day, {START!r} equal to {END!r}"
        )
    return [given]


def _distinct_ids(query_id: str, value: object) -> tuple[int, ...]:
    """Validate the ``ids`` of a Q-G2b scope.

    Args:
        query_id: Template id, for the error message.
        value: Candidate collection of campaign ids.

    Returns:
        The distinct ids, sorted ascending; possibly empty.

    Raises:
        ValueError: When the value is not a collection of ``int`` ids in ``0 .. INT64_MAX``
            (strings, mappings and ``bool`` values are rejected).
    """
    if isinstance(value, str | bytes | bytearray | Mapping) or not isinstance(value, Collection):
        raise ValueError(f"{query_id}: scope key {IDS!r} must be a collection of int ids")
    ids: set[int] = set()
    for item in value:
        ids.add(_checked_id(query_id, IDS, item))
    return tuple(sorted(ids))


def _checked_id(label: str, key: str, value: object) -> int:
    """Return ``value`` when it is an ``int`` id in ``0 .. INT64_MAX``.

    Raises:
        ValueError: Otherwise (``bool`` included); the message names ``label`` and ``key``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}: {key} must hold int ids, not {type(value).__name__}")
    if not INT64_MIN_ID <= value <= INT64_MAX:
        raise ValueError(f"{label}: {key} must hold ids between 0 and INT64_MAX")
    return value


def _frozen(scope: Mapping[str, object]) -> Mapping[str, object]:
    """Return an immutable copy of a partition scope, for :attr:`FetchOutcome.deferred`."""
    return MappingProxyType(dict(scope))


# ---------------------------------------------------------------------------------------------
# One partition
# ---------------------------------------------------------------------------------------------


def _read_step(
    template: QueryTemplate, partition: Mapping[str, object], result: QueryResult
) -> _Step:
    """Decide what one answered partition contributes and what replaces it.

    Args:
        template: The template being fetched.
        partition: The scope the partition was read with.
        result: Its answer, read to the last page.

    Returns:
        The accepted rows and the partitions that replace this one.

    Raises:
        QueryDefect: When the answer breaks the template's contract.
    """
    if LAST_ID in partition:
        return _keyset_step(template, partition, result)
    match template.strategy:
        case "single":
            return _Step(rows=result.rows, capped_kept=result.capped)
        case "by_ids":
            return _by_ids_step(template, partition, result)
        case "ad_group_ranges":
            return _ad_group_range_step(template, partition, result)
        case "dated":
            return _dated_step(template, partition, result)
        case "keyset":
            # _initial_partitions always sets last_id and continuations keep it.
            raise ValueError(f"{template.query_id}: a keyset partition needs {LAST_ID!r}")


def _keyset_step(
    template: QueryTemplate, partition: Mapping[str, object], result: QueryResult
) -> _Step:
    """Accept one keyset page and continue from its largest id while the page is full.

    ``LIMIT 9999`` keeps every page below the 10,000-row page size, so a page can never be
    silently truncated: a page of exactly 9,999 rows means "continue", a shorter one ends the
    keyset. Every id must exceed the page's ``last_id`` and appear once, so the next
    ``last_id`` strictly increases and the keyset terminates.

    Raises:
        QueryDefect: ``"keyset page over limit"`` for more than 9,999 rows; ``"keyset did not
            advance"`` when an id does not exceed ``last_id``; ``"keyset page repeated an id"``;
            or a row outside the partition's date or ad group.
    """
    keyset_field = template.keyset_field
    if keyset_field is None:
        raise ValueError(f"{template.query_id}: this template has no keyset form")
    last_id = _scope_id(partition, LAST_ID)
    rows = result.rows
    if len(rows) > KEYSET_LIMIT:
        raise _defect(template, "keyset page over limit")
    _check_rows_in_partition(template, partition, rows)
    ids = [_row_id(template, row, keyset_field) for row in rows]
    if any(row_id <= last_id for row_id in ids):
        raise _defect(template, "keyset did not advance")
    if len(set(ids)) != len(ids):
        raise _defect(template, "keyset page repeated an id")
    if len(rows) < KEYSET_LIMIT:
        return _Step(rows=rows)
    return _Step(rows=rows, children=({**partition, LAST_ID: max(ids)},))


def _by_ids_step(
    template: QueryTemplate, partition: Mapping[str, object], result: QueryResult
) -> _Step:
    """Accept one Q-G2b chunk: at most one row per requested campaign.

    Raises:
        QueryDefect: When the chunk is capped, or returns a campaign it did not request or one
            campaign twice.
    """
    if result.capped:
        raise _defect(
            template, f"by-ids chunk returned a full page of {PAGE_SIZE} rows with no page token"
        )
    requested = frozenset(_scope_ids(partition))
    returned = [_row_id(template, row, _ID_LIST_FIELD) for row in result.rows]
    if len(set(returned)) != len(returned) or not requested.issuperset(returned):
        raise _defect(template, "by-ids chunk returned an unrequested or repeated campaign")
    return _Step(rows=result.rows)


def _ad_group_range_step(
    template: QueryTemplate, partition: Mapping[str, object], result: QueryResult
) -> _Step:
    """Accept a Q-G7 ad-group range, or halve it, or switch one ad group to a criterion keyset.

    A capped range ``[lo, hi]`` with ``lo < hi`` is halved at ``lo + (hi - lo) // 2`` into
    ``[lo, mid]`` and ``[mid + 1, hi]``. A single capped ad group is read by keyset on
    ``ad_group_criterion.criterion_id`` from ``last_id`` 0.
    """
    lo, hi = _scope_range(partition, AD_GROUP_LO, AD_GROUP_HI) or _FULL_RANGE
    if not result.capped:
        _check_rows_in_partition(template, partition, result.rows)
        return _Step(rows=result.rows)
    if lo < hi:
        return _Step(children=_halves(partition, AD_GROUP_LO, AD_GROUP_HI, lo, hi))
    return _Step(children=({AD_GROUP_ID: lo, LAST_ID: INT64_MIN_ID},))


def _dated_step(
    template: QueryTemplate, partition: Mapping[str, object], result: QueryResult
) -> _Step:
    """Accept a dated partition, or split the first dimension of a capped one not yet single.

    Order: date range, then (at one day) ``campaign.id`` range, then (at one day and one
    campaign) ``ad_group.id`` range, each from the partition's own range or ``[0, INT64_MAX]``.
    At one (day, ad group) Q-G5 switches to the criterion keyset and Q-G6 keeps its rows as
    returned, reported at the finest partition. A template without an ad-group dimension (Q-G3)
    has one row per campaign per day, so a capped campaign-day breaks its contract.

    Raises:
        QueryDefect: For a capped Q-G3 campaign-day, or a row outside the partition.
    """
    if not result.capped:
        _check_rows_in_partition(template, partition, result.rows)
        return _Step(rows=result.rows)
    start, end = _scope_date(partition, START), _scope_date(partition, END)
    if start < end:
        mid = start + timedelta(days=(end - start).days // 2)
        return _Step(children=({**partition, END: mid}, {**partition, START: mid + _ONE_DAY}))
    accepted = template.allowed_scope_keys
    for lo_key, hi_key in ((CAMPAIGN_LO, CAMPAIGN_HI), (AD_GROUP_LO, AD_GROUP_HI)):
        if lo_key not in accepted:
            continue
        lo, hi = _scope_range(partition, lo_key, hi_key) or _FULL_RANGE
        if lo < hi:
            return _Step(children=_halves(partition, lo_key, hi_key, lo, hi))
    if AD_GROUP_LO not in accepted:
        raise _defect(
            template,
            f"a single campaign-day returned a full page of {PAGE_SIZE} rows; the query has "
            "one row per campaign per day",
        )
    ad_group_id, _ = _scope_range(partition, AD_GROUP_LO, AD_GROUP_HI) or _FULL_RANGE
    if LAST_ID in accepted:
        # Q-G5: criterion ids are unique within one (day, ad group). The ad-group range keys
        # are dropped because render treats them as exclusive with ad_group_id; the campaign
        # range stays.
        keyset = {
            key: value for key, value in partition.items() if key not in {AD_GROUP_LO, AD_GROUP_HI}
        }
        keyset[AD_GROUP_ID] = ad_group_id
        keyset[LAST_ID] = INT64_MIN_ID
        return _Step(children=(keyset,))
    # Q-G6: search-term rows have no unique sortable id, so this is the finest partition.
    _check_rows_in_partition(template, partition, result.rows)
    return _Step(rows=result.rows, finest_overflow=(start, ad_group_id))


def _halves(
    partition: Mapping[str, object], lo_key: str, hi_key: str, lo: int, hi: int
) -> tuple[dict[str, object], dict[str, object]]:
    """Split ``[lo, hi]`` (``lo < hi``) at ``lo + (hi - lo) // 2`` into two non-empty halves."""
    mid = lo + (hi - lo) // 2
    return (
        {**partition, lo_key: lo, hi_key: mid},
        {**partition, lo_key: mid + 1, hi_key: hi},
    )


# ---------------------------------------------------------------------------------------------
# Row and scope checks
# ---------------------------------------------------------------------------------------------


def _check_rows_in_partition(
    template: QueryTemplate, partition: Mapping[str, object], rows: Sequence[GoogleAdsRow]
) -> None:
    """Check that every accepted row lies inside the partition it was read for.

    Partitions are disjoint only if the relay honours their filters; a row outside its
    partition would also be read by a sibling and duplicated. Only fields the template selects
    are checked (Q-G5 does not select ``campaign.id``, for example).

    Raises:
        QueryDefect: Naming the field of the first row outside the partition, never its value.
    """
    selected = template.select
    if START in partition and _DATE_FIELD in selected:
        start, end = _scope_date(partition, START), _scope_date(partition, END)
        for row in rows:
            if not start <= _row_date(template, row) <= end:
                raise _outside(template, _DATE_FIELD)
    id_bounds: list[tuple[str, int, int]] = []
    campaigns = _scope_range(partition, CAMPAIGN_LO, CAMPAIGN_HI)
    if campaigns is not None and _CAMPAIGN_FIELD in selected:
        id_bounds.append((_CAMPAIGN_FIELD, *campaigns))
    ad_groups = _scope_range(partition, AD_GROUP_LO, AD_GROUP_HI)
    if ad_groups is not None and _AD_GROUP_FIELD in selected:
        id_bounds.append((_AD_GROUP_FIELD, *ad_groups))
    if AD_GROUP_ID in partition and _AD_GROUP_FIELD in selected:
        ad_group_id = _scope_id(partition, AD_GROUP_ID)
        id_bounds.append((_AD_GROUP_FIELD, ad_group_id, ad_group_id))
    for gaql_field, lo, hi in id_bounds:
        for row in rows:
            if not lo <= _row_id(template, row, gaql_field) <= hi:
                raise _outside(template, gaql_field)


def _row_id(template: QueryTemplate, row: GoogleAdsRow, gaql_field: str) -> int:
    """Read an INT64 id of a raw row: a decimal string, a JSON integer or an integral number.

    Raises:
        QueryDefect: When the id is absent, null, not an integer or outside ``0 .. INT64_MAX``.
    """
    raw = get_field(row, gaql_field, surface=template.row_surface)
    value: int | None = None
    if isinstance(raw, str) and _ID_TEXT_RE.fullmatch(raw) is not None:
        value = int(raw)
    elif isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, Decimal) and raw.is_finite() and raw == raw.to_integral_value():
        value = int(raw)
    if value is None or not INT64_MIN_ID <= value <= INT64_MAX:
        raise _defect(template, f"{gaql_field}: expected an INT64 id on every row")
    return value


def _row_date(template: QueryTemplate, row: GoogleAdsRow) -> date:
    """Read ``segments.date`` of a raw row as a native date.

    Raises:
        QueryDefect: When the date is absent, null or not a valid ``YYYY-MM-DD`` date.
    """
    raw = get_field(row, _DATE_FIELD, surface=template.row_surface)
    parsed: date | None = None
    if isinstance(raw, str) and _DATE_TEXT_RE.fullmatch(raw) is not None:
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            parsed = None
    if parsed is None:
        raise _defect(template, f"{_DATE_FIELD}: expected a YYYY-MM-DD date on every row")
    return parsed


def _scope_id(scope: Mapping[str, object], key: str) -> int:
    """Return an id-valued scope entry that ``render`` has already validated."""
    value = scope[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"scope key {key!r} must be an int id")
    return value


def _scope_date(scope: Mapping[str, object], key: str) -> date:
    """Return a date-valued scope entry that ``render`` has already validated."""
    value = scope[key]
    if not isinstance(value, date):
        raise ValueError(f"scope key {key!r} must be a datetime.date")
    return value


def _scope_range(scope: Mapping[str, object], lo_key: str, hi_key: str) -> tuple[int, int] | None:
    """Return an inclusive id range of a scope, or ``None`` when the scope has none."""
    if lo_key not in scope:
        return None
    return _scope_id(scope, lo_key), _scope_id(scope, hi_key)


def _scope_ids(scope: Mapping[str, object]) -> tuple[int, ...]:
    """Return the ``ids`` of a Q-G2b chunk built by :func:`_initial_partitions`."""
    value = scope[IDS]
    if not isinstance(value, tuple):
        raise ValueError(f"scope key {IDS!r} must be a tuple of int ids")
    return tuple(_checked_id(_BY_IDS_QUERY, IDS, item) for item in value)


def _defect(template: QueryTemplate, message: str) -> QueryDefect:
    """Build the validation ``QueryDefect`` of a response that breaks the template's contract."""
    return QueryDefect(
        message,
        source=_SOURCE,
        endpoint=f"{_SURFACE} {template.query_id}",
        code="validation",
        query_id=template.query_id,
    )


def _outside(template: QueryTemplate, gaql_field: str) -> QueryDefect:
    """Build the defect of a row the relay returned outside its partition's filter."""
    return _defect(template, f"row outside the requested partition ({gaql_field})")


# ---------------------------------------------------------------------------------------------
# Reasons and logging
# ---------------------------------------------------------------------------------------------


def _interruption_reason(exc: TransientError, deferred: int) -> str:
    """Return the repairable reason of an interrupted fetch.

    Args:
        exc: The transient error that stopped the fetch.
        deferred: Partitions deferred, the interrupted one included (at least 1).

    Returns:
        ``"call budget exhausted; N partitions deferred"`` for the call budget, otherwise
        ``"interrupted: {exc.reason}; N partitions deferred"``. Both start with a prefix that
        ``sync.freshness`` treats as repairable.
    """
    count = f"{deferred} partition{'' if deferred == 1 else 's'} deferred"
    if isinstance(exc, CallBudgetExhausted):
        return f"{REASON_BUDGET}; {count}"
    return f"{REASON_INTERRUPTED}: {exc.reason}; {count}"


def _finest_partition_reason(overflows: Sequence[tuple[date, int]]) -> str:
    """Return the reason naming up to three (day, ad group) partitions kept at the limit.

    Example::

        _finest_partition_reason([(date(2026, 9, 14), 123)])
        # "result limit reached at finest partition (2026-09-14, ad group 123)"
    """
    named = [
        f"{day.isoformat()}, ad group {ad_group_id}"
        for day, ad_group_id in overflows[:_MAX_NAMED_PARTITIONS]
    ]
    more = len(overflows) - len(named)
    if more > 0:
        named.append(f"and {more} more")
    return f"{REASON_FINEST_PARTITION} ({'; '.join(named)})"


def _log_outcome(outcome: FetchOutcome, *, finest_overflows: int) -> None:
    """Log one ``google.partitioned_fetch`` event: counts only, never GAQL text or ids."""
    fields: dict[str, object] = {
        "source": _SOURCE,
        "query_id": outcome.query_id,
        "partitions_read": outcome.partitions_read,
        "calls": outcome.calls,
        "rows": len(outcome.rows),
        "completeness": outcome.completeness,
        "deferred": len(outcome.deferred),
        "finest_partition_overflows": finest_overflows,
    }
    if outcome.interrupted_by is not None:
        fields["interrupted_by"] = (
            outcome.interrupted_by.code or type(outcome.interrupted_by).__name__
        )
    if outcome.complete:
        log.info("google.partitioned_fetch", **fields)
    else:
        log.warning("google.partitioned_fetch", **fields)


# ---------------------------------------------------------------------------------------------
# Safe absence
# ---------------------------------------------------------------------------------------------


async def enumerate_campaigns(
    client: GoogleAdsWebhookClient, tracked_ids: Collection[int]
) -> CampaignEnumeration:
    """Enumerate every campaign and decide, from complete reads only, which tracked ones left.

    1. Run the Q-G2 keyset scan from ``last_id`` 0. It is complete only when it ended on a
       short page with no failed or deferred page.
    2. Take the tracked ids the scan did not return. When there are none, the result is
       complete with nothing removed or missing.
    3. Re-read those ids with Q-G2b, at most 1,000 per ``IN`` list and with no status filter;
       every chunk must succeed.
    4. Both steps complete: Q-G2b rows with raw status ``REMOVED`` are :attr:`removed`, its
       other rows join :attr:`present`, and ids neither step returned are :attr:`missing_ids`.
    5. Either step incomplete: ``complete=False`` with nothing removed or missing, so no
       removal or disappearance is inferred and the previous snapshot stands. Q-G2b is not run
       after an incomplete scan, whose absent ids cannot be known.

    Args:
        client: The webhook client.
        tracked_ids: External ids of the dataset's tracked campaigns.

    Returns:
        The campaign rows and the safe-absence verdict.

    Raises:
        AccessDenied: Propagated unchanged.
        QueryDefect: Propagated unchanged.
        ValueError: When a tracked id is not an ``int`` in ``0 .. INT64_MAX``; nothing is sent.
    """
    tracked = _tracked_ids(_SCAN_QUERY, tracked_ids)
    scan_template = QUERIES[_SCAN_QUERY]
    scan = await _partitioned_fetch(client, _SCAN_QUERY, {LAST_ID: INT64_MIN_ID})
    if not scan.complete:
        return _log_campaigns(
            CampaignEnumeration(
                present=scan.rows,
                removed=(),
                missing_ids=frozenset(),
                complete=False,
                reason=scan.reason,
                calls=scan.calls,
                interrupted_by=scan.interrupted_by,
            ),
            tracked=len(tracked),
        )
    seen = {_row_id(scan_template, row, _CAMPAIGN_FIELD) for row in scan.rows}
    absent = sorted(tracked - seen)
    if not absent:
        return _log_campaigns(
            CampaignEnumeration(
                present=scan.rows,
                removed=(),
                missing_ids=frozenset(),
                complete=True,
                reason=None,
                calls=scan.calls,
                interrupted_by=None,
            ),
            tracked=len(tracked),
        )
    reread_template = QUERIES[_BY_IDS_QUERY]
    reread = await _partitioned_fetch(client, _BY_IDS_QUERY, {IDS: absent})
    removed: list[GoogleAdsRow] = []
    still_present: list[GoogleAdsRow] = []
    returned: set[int] = set()
    for row in reread.rows:
        returned.add(_row_id(reread_template, row, _CAMPAIGN_FIELD))
        status = get_field(row, _CAMPAIGN_STATUS_FIELD, surface=reread_template.row_surface)
        (removed if status == _REMOVED else still_present).append(row)
    present = scan.rows + tuple(still_present)
    calls = scan.calls + reread.calls
    if not reread.complete:
        return _log_campaigns(
            CampaignEnumeration(
                present=present,
                removed=(),
                missing_ids=frozenset(),
                complete=False,
                reason=reread.reason,
                calls=calls,
                interrupted_by=reread.interrupted_by,
            ),
            tracked=len(tracked),
        )
    return _log_campaigns(
        CampaignEnumeration(
            present=present,
            removed=tuple(removed),
            missing_ids=frozenset(absent) - returned,
            complete=True,
            reason=None,
            calls=calls,
            interrupted_by=None,
        ),
        tracked=len(tracked),
    )


@overload
async def enumerate_entities(
    client: GoogleAdsWebhookClient,
    query_id: Literal["Q-G4"],
    tracked_keys: Collection[int],
) -> EntityEnumeration: ...


@overload
async def enumerate_entities(
    client: GoogleAdsWebhookClient,
    query_id: Literal["Q-G7"],
    tracked_keys: Collection[tuple[int, int]],
) -> EntityEnumeration: ...


async def enumerate_entities(
    client: GoogleAdsWebhookClient,
    query_id: EntityQueryId,
    tracked_keys: Collection[int] | Collection[tuple[int, int]],
) -> EntityEnumeration:
    """Enumerate every ad group (Q-G4) or keyword (Q-G7) and the tracked keys no longer returned.

    Q-G4 runs the full ``ad_group.id`` keyset scan and keys rows by ``ad_group.id``. Q-G7 runs
    the full ``ad_group.id`` range partitioning, with a criterion keyset for any single capped
    ad group, and keys rows by ``(ad_group.id, ad_group_criterion.criterion_id)``, because
    criterion ids are unique only within an ad group. :attr:`EntityEnumeration.absent_keys` is
    computed only from a complete enumeration; otherwise it is empty and nothing is inferred.

    Args:
        client: The webhook client.
        query_id: ``"Q-G4"`` or ``"Q-G7"``.
        tracked_keys: The dataset's tracked keys: ``int`` ad-group ids for Q-G4,
            ``(ad_group_id, criterion_id)`` tuples for Q-G7.

    Returns:
        The rows and the absent tracked keys.

    Raises:
        AccessDenied: Propagated unchanged.
        QueryDefect: Propagated unchanged.
        ValueError: When ``query_id`` is neither ``"Q-G4"`` nor ``"Q-G7"`` or a tracked key has
            the wrong shape; nothing is sent.
    """
    if query_id not in _ENTITY_QUERY_IDS:
        raise ValueError("enumerate_entities reads 'Q-G4' or 'Q-G7' only")
    template = QUERIES[query_id]
    absent_keys: frozenset[int] | frozenset[tuple[int, int]]
    if query_id == "Q-G4":
        tracked_ad_groups = _tracked_ids(query_id, tracked_keys)
        outcome = await _partitioned_fetch(client, query_id, {LAST_ID: INT64_MIN_ID})
        ad_groups = {_row_id(template, row, _AD_GROUP_FIELD) for row in outcome.rows}
        absent_keys = frozenset(tracked_ad_groups - ad_groups if outcome.complete else ())
    else:
        tracked_keywords = _tracked_pairs(query_id, tracked_keys)
        outcome = await _partitioned_fetch(client, query_id)
        keywords = {
            (_row_id(template, row, _AD_GROUP_FIELD), _row_id(template, row, _CRITERION_FIELD))
            for row in outcome.rows
        }
        absent_keys = frozenset(tracked_keywords - keywords if outcome.complete else ())
    enumeration = EntityEnumeration(
        rows=outcome.rows,
        absent_keys=absent_keys,
        complete=outcome.complete,
        reason=outcome.reason,
        calls=outcome.calls,
        interrupted_by=outcome.interrupted_by,
    )
    log.info(
        "google.enumerate_entities",
        source=_SOURCE,
        query_id=query_id,
        tracked=len(tracked_keys),
        rows=len(enumeration.rows),
        absent=len(enumeration.absent_keys),
        complete=enumeration.complete,
        calls=enumeration.calls,
    )
    return enumeration


def _tracked_ids(query_id: str, values: Collection[object]) -> frozenset[int]:
    """Validate tracked ids before any call.

    A tracked id of the wrong type (a ``str`` external id, say) would never match a returned
    row and would be reported absent, so it is rejected instead.

    Raises:
        ValueError: When an element is not an ``int`` in ``0 .. INT64_MAX``.
    """
    return frozenset(_checked_id(query_id, "tracked keys", value) for value in values)


def _tracked_pairs(query_id: str, values: Collection[object]) -> frozenset[tuple[int, int]]:
    """Validate tracked ``(ad_group_id, criterion_id)`` keys before any call.

    Raises:
        ValueError: When an element is not a 2-tuple of ``int`` ids in ``0 .. INT64_MAX``.
    """
    pairs: set[tuple[int, int]] = set()
    for value in values:
        if not isinstance(value, tuple) or len(value) != 2:
            raise ValueError(f"{query_id}: tracked keys must be (ad_group_id, criterion_id) tuples")
        ad_group_id, criterion_id = value
        pairs.add(
            (
                _checked_id(query_id, "tracked keys", ad_group_id),
                _checked_id(query_id, "tracked keys", criterion_id),
            )
        )
    return frozenset(pairs)


def _log_campaigns(enumeration: CampaignEnumeration, *, tracked: int) -> CampaignEnumeration:
    """Log one ``google.enumerate_campaigns`` event (counts only) and return the enumeration."""
    log.info(
        "google.enumerate_campaigns",
        source=_SOURCE,
        tracked=tracked,
        present=len(enumeration.present),
        removed=len(enumeration.removed),
        missing=len(enumeration.missing_ids),
        complete=enumeration.complete,
        calls=enumeration.calls,
    )
    return enumeration
