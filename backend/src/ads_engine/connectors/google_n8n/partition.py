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
    which ``sync`` records. :func:`scope_to_json` and :func:`scope_from_json` turn a scope
    into plain JSON and back, validated as ``partitioned_fetch`` validates it, so ``sync`` can
    keep deferred scopes across worker restarts and CLI runs, and
    :func:`interruption_reason` states the total of what is still deferred.
    ``AccessDenied`` and ``QueryDefect`` propagate unchanged: ``sync.mode`` switches the source
    to simulated data or fails the job.

Safe absence:
    :func:`enumerate_campaigns` runs the complete Q-G2 keyset scan and re-reads, through Q-G2b
    with no status filter, the tracked campaigns the scan no longer returned. A campaign is
    reported ``removed`` or ``missing`` only when both steps completed; otherwise nothing is
    inferred and the previous snapshot stands. :func:`enumerate_entities` applies the same rule to
    ad groups (Q-G4) and keywords (Q-G7).

    An interrupted enumeration returns its :class:`EnumerationProgress`: the scopes still
    unread and the tracked keys already known absent from the scopes read. ``sync`` stores it
    and passes it back as ``resume`` in a later entity sync, which reads only the unread
    scopes, so a scan larger than one day's call budget still finishes. The verdict is never
    drawn from an unfinished scan: absence is reported only by the call in which the
    enumeration finishes, from the keys accumulated over every part, and the second check
    (Q-G2b) starts in the entity sync in which the scan completes, over every accumulated
    candidate. An interrupted re-read resumes the same way: its progress keeps the chunks
    not re-read yet and the rows the others returned, the next sync reads only those
    chunks, and ``removed`` and ``missing`` are reported once every chunk has succeeded,
    from the rows of all of them.

Reason strings:
    Every ``reason`` begins with a stable prefix that ``ads_engine.sync.freshness`` recognises by
    prefix (that module cannot be imported here, because connectors never import ``sync``):
    :data:`REASON_BUDGET` and :data:`REASON_INTERRUPTED` are repairable by a refetch;
    :data:`REASON_FINEST_PARTITION` and :data:`REASON_RESULT_LIMIT` are the platform's own limits.
    A repairable reason always comes first, so a deferred shortfall is never masked. Reasons and
    logs never carry GAQL text or id lists; a finest-partition reason names at most three
    (day, ad group) partitions and counts the rest with the days they are on, so every day at
    the limit is named.

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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal, overload

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
    SCOPE_KEYS,
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
    "EnumerationProgress",
    "EnumerationQueryId",
    "FetchOutcome",
    "PartitionedFetcher",
    "enumerate_campaigns",
    "enumerate_entities",
    "interruption_reason",
    "scope_from_json",
    "scope_to_json",
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
``"result limit reached at finest partition (2026-09-14, ad group 123)"``; partitions after
the third are counted with their days (``"…; and 2 more on 2026-09-14 to 2026-09-15)"``).
Equals ``ads_engine.sync.freshness.REASON_FINEST_PARTITION`` (permanent)."""

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
_KEYWORD_QUERY: Final = "Q-G7"

# Scope keys whose values are native dates; every other scope key holds an int id, except
# ``ids``, which holds a collection of them.
_DATE_KEYS: Final[frozenset[str]] = frozenset({START, END})

# The fields of EnumerationProgress.to_json, exactly.
_PROGRESS_KEYS: Final[frozenset[str]] = frozenset(
    {"query_id", "pending", "absent", "rereading", "reread_rows"}
)

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

EnumerationQueryId = Literal["Q-G2", "Q-G4", "Q-G7"]
"""The safe-absence enumerations an :class:`EnumerationProgress` resumes."""


@dataclass(frozen=True, slots=True)
class EnumerationProgress:
    """Where an unfinished safe-absence enumeration stands, so a later sync resumes it.

    :func:`enumerate_campaigns` and :func:`enumerate_entities` return one in ``progress``
    when an interruption stops them, and read only :attr:`pending` when it is passed back as
    ``resume``. Construction validates every field and normalises :attr:`pending` to
    immutable scopes, :attr:`absent` to a ``frozenset`` and :attr:`reread_rows` to a tuple.

    Example::

        first = await enumerate_entities(client, "Q-G4", tracked)  # budget runs out
        stored = first.progress.to_json()  # kept by sync until the next entity sync
        later = await enumerate_entities(
            client, "Q-G4", tracked, resume=EnumerationProgress.from_json(stored)
        )

    Attributes:
        query_id: ``"Q-G2"`` (campaigns, with the Q-G2b re-read), ``"Q-G4"`` (ad groups) or
            ``"Q-G7"`` (keywords).
        pending: The scopes not read yet, in reading order; never empty. Each is a scope
            ``partitioned_fetch(query_id, scope)`` accepts, or, while :attr:`rereading`, a
            Q-G2b chunk ``{"ids": ...}`` of at most 1,000 candidate ids not re-read yet.
        absent: Tracked keys that lie inside scopes already read but were not returned:
            campaign ids (Q-G2), ad-group ids (Q-G4) or ``(ad_group_id, criterion_id)``
            pairs (Q-G7). They are the absence candidates of the verdict once the
            enumeration finishes; nothing is inferred from them before. While
            :attr:`rereading`, every candidate of the re-read, re-read or not.
        rereading: Q-G2 only: the scan has finished and the Q-G2b re-read of the
            :attr:`pending` chunks is outstanding.
        reread_rows: While :attr:`rereading`, the raw Q-G2b rows, ``REMOVED`` or not, that
            the chunks already re-read returned; each is a JSON object whose ``campaign.id``
            is a candidate, lies in no pending chunk and appears once. They join the verdict
            once every chunk has succeeded, so it is the one a single pass would draw.
            Empty otherwise.

    Raises:
        ValueError: If the query id is not an enumeration, ``rereading`` is set for Q-G4 or
            Q-G7, nothing is pending, a scope is invalid for its template, a key has the
            wrong shape, a pending Q-G2b chunk is empty, holds more than 1,000 ids, repeats
            an id or names one that is not a candidate, or a re-read row is not a JSON
            object, has no INT64 ``campaign.id``, names no candidate, repeats a campaign or
            names a campaign still pending, or rows are kept outside a re-read.
    """

    query_id: EnumerationQueryId
    pending: tuple[Mapping[str, object], ...]
    absent: frozenset[int] | frozenset[tuple[int, int]]
    rereading: bool = False
    reread_rows: tuple[GoogleAdsRow, ...] = ()

    def __post_init__(self) -> None:
        """Validate the fields and normalise the scopes, keys and rows (class docstring)."""
        _enumeration_query_id(self.query_id)
        if not isinstance(self.rereading, bool):
            raise ValueError("rereading must be a bool")
        if self.rereading and self.query_id != _SCAN_QUERY:
            raise ValueError(f"{self.query_id}: only Q-G2 has a Q-G2b re-read to resume")
        if not self.pending:
            raise ValueError(
                "Q-G2: a Q-G2b re-read has a chunk pending"
                if self.rereading
                else f"{self.query_id}: an unfinished enumeration has a pending scope"
            )
        scope_query = _BY_IDS_QUERY if self.rereading else self.query_id
        pending = tuple(
            scope_from_json(scope_query, scope_to_json(scope)) for scope in self.pending
        )
        absent: frozenset[int] | frozenset[tuple[int, int]] = (
            _tracked_pairs(self.query_id, self.absent)
            if self.query_id == _KEYWORD_QUERY
            else _tracked_ids(self.query_id, self.absent)
        )
        rows = tuple(self.reread_rows)
        if self.rereading:
            _check_reread(pending, _tracked_ids(self.query_id, absent), rows)
        elif rows:
            raise ValueError(f"{self.query_id}: only a Q-G2b re-read keeps re-read rows")
        object.__setattr__(self, "pending", pending)
        object.__setattr__(self, "absent", absent)
        object.__setattr__(self, "reread_rows", rows)

    @property
    def outstanding(self) -> int:
        """Scopes still to read: the pending scan scopes, or the Q-G2b chunks of a re-read."""
        return len(self.pending)

    def to_json(self) -> dict[str, Any]:
        """Return the progress as plain JSON; :meth:`from_json` is its inverse.

        A re-read row keeps its JSON values; a ``Decimal`` the relay sent becomes an int
        when integral and a float otherwise, which the row parsers treat alike.

        Example::

            EnumerationProgress("Q-G4", ({"last_id": 9999},), frozenset({5})).to_json()
            # {"query_id": "Q-G4", "pending": [{"last_id": 9999}], "absent": [5],
            #  "rereading": False, "reread_rows": []}
            EnumerationProgress(
                "Q-G2",
                ({"ids": (7,)},),
                frozenset({5, 7}),
                rereading=True,
                reread_rows=({"campaign": {"id": "5", "status": "REMOVED"}},),
            ).to_json()
            # {"query_id": "Q-G2", "pending": [{"ids": [7]}], "absent": [5, 7],
            #  "rereading": True,
            #  "reread_rows": [{"campaign": {"id": "5", "status": "REMOVED"}}]}
        """
        absent: list[object]
        if self.query_id == _KEYWORD_QUERY:
            # JSON has no tuples: a (ad_group_id, criterion_id) pair is stored as a 2-list.
            absent = [list(pair) for pair in sorted(_tracked_pairs(self.query_id, self.absent))]
        else:
            absent = [*sorted(_tracked_ids(self.query_id, self.absent))]
        return {
            "query_id": self.query_id,
            "pending": [scope_to_json(scope) for scope in self.pending],
            "absent": absent,
            "rereading": self.rereading,
            "reread_rows": [_json_value(row) for row in self.reread_rows],
        }

    @classmethod
    def from_json(cls, data: object) -> EnumerationProgress:
        """Rebuild progress stored by :meth:`to_json`, validated before anything is sent.

        Args:
            data: The decoded JSON object.

        Returns:
            The progress.

        Raises:
            ValueError: When ``data`` is not exactly the object :meth:`to_json` writes, or
                any field fails the class's validation.
        """
        if not isinstance(data, Mapping) or set(data) != _PROGRESS_KEYS:
            raise ValueError(
                f"stored enumeration progress must hold exactly {sorted(_PROGRESS_KEYS)}"
            )
        query_id = _enumeration_query_id(data["query_id"])
        pending, absent, rereading = data["pending"], data["absent"], data["rereading"]
        rows = data["reread_rows"]
        if not isinstance(pending, list | tuple) or not isinstance(absent, list | tuple):
            raise ValueError(f"{query_id}: stored pending scopes and absent keys must be lists")
        if not isinstance(rows, list | tuple):
            raise ValueError(f"{query_id}: stored re-read rows must be a list")
        if not isinstance(rereading, bool):
            raise ValueError(f"{query_id}: stored rereading must be a bool")
        # A re-read's pending scopes are Q-G2b chunks; __post_init__ refuses a re-read of
        # another enumeration.
        scope_query = _BY_IDS_QUERY if rereading and query_id == _SCAN_QUERY else query_id
        keys: frozenset[int] | frozenset[tuple[int, int]]
        if query_id == _KEYWORD_QUERY:
            # JSON has no tuples: a stored (ad_group_id, criterion_id) pair is a 2-list.
            keys = _tracked_pairs(
                query_id, [tuple(key) if isinstance(key, list) else key for key in absent]
            )
        else:
            keys = _tracked_ids(query_id, absent)
        return cls(
            query_id=query_id,
            pending=tuple(scope_from_json(scope_query, scope) for scope in pending),
            absent=keys,
            rereading=rereading,
            reread_rows=tuple(rows),
        )


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
        present: Q-G2 rows of every non-removed campaign this call scanned, followed, once the
            re-read is complete, by the Q-G2b rows of every re-read tracked campaign whose
            status is not ``REMOVED``, from every call of the re-read.
        removed: Q-G2b rows whose raw ``campaign.status`` is ``REMOVED``, from every call of
            the re-read; ``sync`` snapshots them as ``ended``. Empty unless :attr:`complete`.
        missing_ids: Tracked campaign ids that neither the complete Q-G2 scan nor Q-G2b
            returned. Empty unless :attr:`complete`.
        complete: Whether the Q-G2 scan ended on a short page with no failed or deferred page
            and every Q-G2b chunk succeeded, the scan's earlier parts read by earlier calls
            included. When ``False`` no removal or disappearance may be inferred: the previous
            snapshot stands and ``google.entities`` is incomplete.
        reason: The failing step's reason, naming every scope still deferred; ``None`` when
            complete.
        calls: POST attempts made by both steps in this call.
        interrupted_by: The transient error that stopped the failing step, or ``None``.
        progress: What a later call must still read (``resume``): the unread scan scopes, or
            the Q-G2b chunks not re-read yet with the rows of those already re-read; ``None``
            once the enumeration finished.
    """

    present: tuple[GoogleAdsRow, ...]
    removed: tuple[GoogleAdsRow, ...]
    missing_ids: frozenset[int]
    complete: bool
    reason: str | None
    calls: int
    interrupted_by: TransientError | None
    progress: EnumerationProgress | None = None


@dataclass(frozen=True, slots=True)
class EntityEnumeration:
    """Every current ad group (Q-G4) or keyword (Q-G7) and the tracked keys no longer returned.

    Attributes:
        rows: The enumeration's rows. Q-G7 returns every keyword, keywords with no performance
            rows included, so ``sync`` writes keyword rows and quality-score snapshots for them.
        absent_keys: Tracked keys the complete enumeration did not return: ``ad_group.id``
            values for Q-G4, ``(ad_group.id, ad_group_criterion.criterion_id)`` pairs for Q-G7
            (criterion ids are unique only within an ad group). They are accumulated over
            every call that read part of a resumed enumeration. Empty unless
            :attr:`complete`.
        complete: Whether the enumeration read every row; when ``False`` nothing is inferred.
        reason: Why the enumeration is incomplete, naming every scope still deferred; ``None``
            when complete.
        calls: POST attempts made in this call.
        interrupted_by: The transient error that stopped the enumeration, or ``None``.
        progress: The unread scopes and the absence candidates found so far, for ``resume``
            in a later call; ``None`` once the enumeration finished.
    """

    rows: tuple[GoogleAdsRow, ...]
    absent_keys: frozenset[int] | frozenset[tuple[int, int]]
    complete: bool
    reason: str | None
    calls: int
    interrupted_by: TransientError | None
    progress: EnumerationProgress | None = None


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
        reasons.append(interruption_reason(interrupted_by, len(deferred)))
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
# Scopes as JSON, for deferred work kept across syncs
# ---------------------------------------------------------------------------------------------


def scope_to_json(scope: Mapping[str, object]) -> dict[str, object]:
    """Return a partition scope as plain JSON values; :func:`scope_from_json` is its inverse.

    ``start`` and ``end`` become ``YYYY-MM-DD`` text, ``ids`` a list of ints in the given
    order, and every other key keeps its int id. ``sync`` stores the result for deferred
    scopes, and two scopes are the same partition exactly when their JSON forms are equal.

    Example::

        scope_to_json(
            {
                "start": date(2026, 9, 1),
                "end": date(2026, 9, 3),
                "campaign_lo": 0,
                "campaign_hi": 4611686018427387903,
            }
        )
        # {"start": "2026-09-01", "end": "2026-09-03", "campaign_lo": 0,
        #  "campaign_hi": 4611686018427387903}

    Args:
        scope: A scope :meth:`PartitionedFetcher.partitioned_fetch` accepts, such as an entry
            of :attr:`FetchOutcome.deferred`.

    Returns:
        A new dict of JSON values.

    Raises:
        ValueError: When a key is not a scope key of ``queries.py``, a date key holds
            anything but a ``datetime.date`` (a ``datetime`` included), ``ids`` is not a
            collection of int ids, or another key holds anything but an int id in
            ``0 .. INT64_MAX`` (``bool`` included).
    """
    data: dict[str, object] = {}
    for key, value in scope.items():
        if key not in SCOPE_KEYS:
            raise ValueError(f"unknown scope key {key!r}")
        if key in _DATE_KEYS:
            if isinstance(value, datetime) or not isinstance(value, date):
                raise ValueError(f"scope key {key!r} must be a datetime.date")
            data[key] = value.isoformat()
        elif key == IDS:
            if isinstance(value, str | bytes | bytearray | Mapping) or not isinstance(
                value, Collection
            ):
                raise ValueError(f"scope key {IDS!r} must be a collection of int ids")
            data[key] = [_checked_id("scope", IDS, item) for item in value]
        else:
            data[key] = _checked_id("scope", key, value)
    return data


def scope_from_json(query_id: str, data: object) -> Mapping[str, object]:
    """Rebuild a scope :func:`scope_to_json` stored for ``query_id``, validated before use.

    Types are rebuilt by key: ``start`` and ``end`` from ``YYYY-MM-DD`` text, ``ids`` from a
    list of ints, every other key from an int. The scope is then checked exactly as
    :meth:`PartitionedFetcher.partitioned_fetch` checks a scope (the template's keys,
    required keys, value ranges and pairings, and a Q-G5 criterion keyset on one day), so a
    stored scope that the template would reject is refused here and nothing is ever sent
    for it.

    Example::

        scope_from_json("Q-G6", {"start": "2026-09-01", "end": "2026-09-03"})
        # mappingproxy({"start": date(2026, 9, 1), "end": date(2026, 9, 3)})

    Args:
        query_id: A key of :data:`~ads_engine.connectors.google_n8n.queries.QUERIES`.
        data: The decoded JSON object.

    Returns:
        The immutable scope.

    Raises:
        ValueError: When ``query_id`` is unknown, ``data`` is not an object of scope keys, a
            value has the wrong JSON type (``bool`` and floats included), a date is not a
            valid ``YYYY-MM-DD`` date, or the template rejects the scope.
    """
    template = _template(query_id)
    if not isinstance(data, Mapping):
        raise ValueError(f"{template.query_id}: a stored scope must be a JSON object")
    scope: dict[str, object] = {}
    for key, value in data.items():
        if not isinstance(key, str) or key not in SCOPE_KEYS:
            raise ValueError(f"{template.query_id}: unknown stored scope key")
        if key in _DATE_KEYS:
            scope[key] = _stored_date(template.query_id, key, value)
        elif key == IDS:
            if not isinstance(value, list | tuple):
                raise ValueError(f"{template.query_id}: stored {IDS!r} must be a list of int ids")
            scope[key] = tuple(_checked_id(template.query_id, IDS, item) for item in value)
        else:
            scope[key] = _checked_id(template.query_id, key, value)
    _initial_partitions(template, scope)
    return _frozen(scope)


def _stored_date(query_id: str, key: str, value: object) -> date:
    """Read one stored ``YYYY-MM-DD`` scope date.

    Raises:
        ValueError: When the value is not text of that exact form or not a valid date.
    """
    if isinstance(value, str) and _DATE_TEXT_RE.fullmatch(value) is not None:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{query_id}: stored scope key {key!r} must be a YYYY-MM-DD date")


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


def interruption_reason(exc: TransientError, deferred: int) -> str:
    """Return the repairable reason of interrupted work with ``deferred`` scopes unread.

    Every interrupted fetch and enumeration states its reason with this function, and
    ``sync`` uses it to state the total still deferred for a dataset when that work spans
    several queries or earlier runs.

    Example::

        interruption_reason(CallBudgetExhausted(), 3)
        # "call budget exhausted; 3 partitions deferred"

    Args:
        exc: The transient error that stopped the work.
        deferred: Partitions or scopes still unread, the interrupted one included.

    Returns:
        ``"call budget exhausted; N partitions deferred"`` for the call budget, otherwise
        ``"interrupted: {exc.reason}; N partitions deferred"``. Both start with a prefix that
        ``sync.freshness`` treats as repairable.

    Raises:
        ValueError: When ``deferred`` is not a positive int: interrupted work always leaves
            the interrupted scope unread.
    """
    if isinstance(deferred, bool) or not isinstance(deferred, int) or deferred < 1:
        raise ValueError("an interruption defers at least one scope")
    count = f"{deferred} partition{'' if deferred == 1 else 's'} deferred"
    if isinstance(exc, CallBudgetExhausted):
        return f"{REASON_BUDGET}; {count}"
    return f"{REASON_INTERRUPTED}: {exc.reason}; {count}"


def _day_spans(days: Iterable[date]) -> str:
    """Render distinct days ascending, consecutive ones joined as ``first to last``."""
    spans: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if spans and day - spans[-1][1] == _ONE_DAY:
            spans[-1] = (spans[-1][0], day)
        else:
            spans.append((day, day))
    return ", ".join(
        first.isoformat() if first == last else f"{first.isoformat()} to {last.isoformat()}"
        for first, last in spans
    )


def _finest_partition_reason(overflows: Sequence[tuple[date, int]]) -> str:
    """Return the reason naming up to three (day, ad group) partitions kept at the limit.

    The partitions after the third are counted with the days they are on, so the reason
    names every day at the limit: ``sync.freshness`` ends the shortfall for a day once a later
    read of it finishes, and the analysis flags a window incomplete only when it holds one of
    those days. The format is the dated-shortfall grammar of
    ``ads_engine.analysis.shortfall``, which this package cannot import.

    Example::

        _finest_partition_reason([(date(2026, 9, 14), 123)])
        # "result limit reached at finest partition (2026-09-14, ad group 123)"
        _finest_partition_reason(
            [(date(2026, 9, 14), group) for group in (1, 2, 3, 4)]
            + [(date(2026, 9, 15), 5), (date(2026, 9, 18), 5)]
        )
        # "result limit reached at finest partition (2026-09-14, ad group 1; 2026-09-14,
        # ad group 2; 2026-09-14, ad group 3; and 3 more on 2026-09-14 to 2026-09-15,
        # 2026-09-18)"
    """
    named = [
        f"{day.isoformat()}, ad group {ad_group_id}"
        for day, ad_group_id in overflows[:_MAX_NAMED_PARTITIONS]
    ]
    rest = overflows[_MAX_NAMED_PARTITIONS:]
    if rest:
        named.append(f"and {len(rest)} more on {_day_spans(day for day, _ in rest)}")
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


@dataclass(frozen=True, slots=True)
class _ScopesRead:
    """What reading an enumeration's scopes in order established, up to an interruption.

    Attributes:
        rows: The rows of every scope read, in reading order.
        started: The scopes this call began to read, the interrupted one included.
        pending: The scopes still unread: the interrupted fetch's deferred partitions, then
            every scope not started; empty when every scope was read.
        calls: POST attempts made.
        interrupted_by: The transient error that stopped the reads, or ``None``.
    """

    rows: tuple[GoogleAdsRow, ...]
    started: tuple[Mapping[str, object], ...]
    pending: tuple[Mapping[str, object], ...]
    calls: int
    interrupted_by: TransientError | None


async def _read_in_order(
    client: GoogleAdsWebhookClient, query_id: str, scopes: Sequence[Mapping[str, object]]
) -> _ScopesRead:
    """Read ``scopes`` of one enumeration in order, stopping at the first interruption.

    Keyset (Q-G2, Q-G4) and ad-group range (Q-G7) fetches have no capped form they keep as
    returned, so each one is complete exactly when no interruption stopped it.
    """
    rows: list[GoogleAdsRow] = []
    calls = 0
    for index, scope in enumerate(scopes):
        outcome = await _partitioned_fetch(client, query_id, scope)
        rows.extend(outcome.rows)
        calls += outcome.calls
        if outcome.interrupted_by is not None:
            return _ScopesRead(
                rows=tuple(rows),
                started=tuple(scopes[: index + 1]),
                pending=(*outcome.deferred, *(_frozen(rest) for rest in scopes[index + 1 :])),
                calls=calls,
                interrupted_by=outcome.interrupted_by,
            )
    return _ScopesRead(
        rows=tuple(rows), started=tuple(scopes), pending=(), calls=calls, interrupted_by=None
    )


def _scope_holds(scope: Mapping[str, object], key: int | tuple[int, int]) -> bool:
    """Return whether a tracked key lies inside a scope of a safe-absence enumeration.

    * A keyset scope ``{last_id: L}`` (Q-G2, Q-G4) holds every id above ``L``; a scope with
      no ``last_id`` starts after 0, as :meth:`PartitionedFetcher.partitioned_fetch` does.
    * A Q-G7 range ``{ad_group_lo, ad_group_hi}`` holds every keyword of an ad group in the
      range (the full range when the scope has none); a criterion keyset
      ``{ad_group_id: A, last_id: L}`` holds the keywords of ad group ``A`` above ``L``.
    * A Q-G2b chunk ``{ids}`` holds exactly its ids.
    """
    if isinstance(key, tuple):
        ad_group_id, criterion_id = key
        if AD_GROUP_ID in scope:
            return ad_group_id == _scope_id(scope, AD_GROUP_ID) and criterion_id > _scope_id(
                scope, LAST_ID
            )
        lo, hi = _scope_range(scope, AD_GROUP_LO, AD_GROUP_HI) or _FULL_RANGE
        return lo <= ad_group_id <= hi
    if IDS in scope:
        return key in _scope_ids(scope)
    last_id = _scope_id(scope, LAST_ID) if LAST_ID in scope else INT64_MIN_ID
    return key > last_id


def _absent_after[K: (int, tuple[int, int])](
    tracked: frozenset[K],
    returned: Collection[K],
    read: _ScopesRead,
    prior: frozenset[K],
) -> frozenset[K]:
    """Return the absence candidates known after one call of a (resumed) enumeration.

    A tracked key is a new candidate when it lies inside a scope this call began to read,
    inside none of the scopes still pending, and was not returned. Candidates of earlier
    calls (``prior``) stay candidates while they are still tracked: the scopes they lie in
    were read in full and are disjoint from everything read later.
    """
    found = {
        key
        for key in tracked
        if key not in returned
        and any(_scope_holds(scope, key) for scope in read.started)
        and not any(_scope_holds(scope, key) for scope in read.pending)
    }
    return frozenset(found) | ((prior & tracked) - frozenset(returned))


def _enumeration_query_id(value: object) -> EnumerationQueryId:
    """Narrow a query id to :data:`EnumerationQueryId`.

    Raises:
        ValueError: When it is not ``"Q-G2"``, ``"Q-G4"`` or ``"Q-G7"``.
    """
    if value == "Q-G2":
        return "Q-G2"
    if value == "Q-G4":
        return "Q-G4"
    if value == "Q-G7":
        return "Q-G7"
    raise ValueError("enumeration progress is for 'Q-G2', 'Q-G4' or 'Q-G7' only")


def _check_resume(query_id: str, resume: EnumerationProgress | None) -> None:
    """Refuse progress of another enumeration before anything is sent.

    Raises:
        ValueError: When ``resume`` belongs to another query id.
    """
    if resume is not None and resume.query_id != query_id:
        raise ValueError(f"progress of {resume.query_id} cannot resume {query_id}")


async def enumerate_campaigns(
    client: GoogleAdsWebhookClient,
    tracked_ids: Collection[int],
    *,
    resume: EnumerationProgress | None = None,
) -> CampaignEnumeration:
    """Enumerate every campaign and decide, from complete reads only, which tracked ones left.

    1. Run the Q-G2 keyset scan: from ``last_id`` 0, or with ``resume`` only over its
       pending scopes, in order. An interruption stops it at once: the unread scopes and the
       candidates found so far are returned in :attr:`CampaignEnumeration.progress`, and
       nothing is inferred.
    2. Once the scan has finished, the candidates are the tracked ids that lie inside a scope
       read by this call or an earlier one and were never returned. When there are none, the
       result is complete with nothing removed or missing.
    3. Re-read every candidate with Q-G2b, at most 1,000 per ``IN`` list and with no status
       filter; every chunk must succeed. An interrupted chunk returns progress
       (``rereading``) holding the chunks not re-read yet, the rows the chunks already
       re-read returned and every candidate, and nothing is inferred. With that progress as
       ``resume`` only the pending chunks are read, so a re-read larger than one day's call
       budget still finishes.
    4. Both steps complete, every chunk of the re-read succeeded, in this call or an earlier
       one: the Q-G2b rows of every chunk with raw status ``REMOVED`` are :attr:`removed`,
       its other rows join :attr:`present`, and candidates no chunk returned are
       :attr:`missing_ids`: the verdict of one uninterrupted pass.
    5. Either step incomplete: ``complete=False`` with nothing removed or missing, so no
       removal or disappearance is inferred and the previous snapshot stands; the rows of
       an unfinished re-read wait in the progress. Q-G2b never runs after an unfinished
       scan, whose absent ids cannot be known yet.

    Args:
        client: The webhook client.
        tracked_ids: External ids of the dataset's tracked campaigns.
        resume: Progress an earlier call returned for this dataset, or ``None`` to start a
            new scan.

    Returns:
        The campaign rows read by this call and the safe-absence verdict.

    Raises:
        AccessDenied: Propagated unchanged.
        QueryDefect: Propagated unchanged.
        ValueError: When a tracked id is not an ``int`` in ``0 .. INT64_MAX`` or ``resume``
            belongs to another enumeration; nothing is sent.
    """
    tracked = _tracked_ids(_SCAN_QUERY, tracked_ids)
    _check_resume(_SCAN_QUERY, resume)
    scan_template = QUERIES[_SCAN_QUERY]
    reread_template = QUERIES[_BY_IDS_QUERY]
    present: tuple[GoogleAdsRow, ...] = ()
    calls = 0
    reread_earlier: tuple[GoogleAdsRow, ...] = ()
    if resume is not None and resume.rereading:
        # Candidates count only while still tracked, with the rows and chunks they lie in.
        candidates = _tracked_ids(_SCAN_QUERY, resume.absent) & tracked
        unread = sorted(
            campaign_id
            for chunk in resume.pending
            for campaign_id in _scope_ids(chunk)
            if campaign_id in candidates
        )
        reread_earlier = tuple(
            row
            for row in resume.reread_rows
            if _row_id(reread_template, row, _CAMPAIGN_FIELD) in candidates
        )
    else:
        prior = frozenset() if resume is None else _tracked_ids(_SCAN_QUERY, resume.absent)
        scopes = (_frozen({LAST_ID: INT64_MIN_ID}),) if resume is None else resume.pending
        scan = await _read_in_order(client, _SCAN_QUERY, scopes)
        present, calls = scan.rows, scan.calls
        seen = {_row_id(scan_template, row, _CAMPAIGN_FIELD) for row in scan.rows}
        candidates = _absent_after(tracked, seen, scan, prior)
        if scan.interrupted_by is not None:
            progress = EnumerationProgress(_SCAN_QUERY, scan.pending, candidates)
            return _log_campaigns(
                CampaignEnumeration(
                    present=present,
                    removed=(),
                    missing_ids=frozenset(),
                    complete=False,
                    reason=interruption_reason(scan.interrupted_by, progress.outstanding),
                    calls=calls,
                    interrupted_by=scan.interrupted_by,
                    progress=progress,
                ),
                tracked=len(tracked),
            )
        unread = sorted(candidates)
    if not candidates:
        return _log_campaigns(
            CampaignEnumeration(
                present=present,
                removed=(),
                missing_ids=frozenset(),
                complete=True,
                reason=None,
                calls=calls,
                interrupted_by=None,
            ),
            tracked=len(tracked),
        )
    # An empty id list reads nothing and sends no call: every chunk was re-read earlier.
    reread = await _partitioned_fetch(client, _BY_IDS_QUERY, {IDS: unread})
    reread_rows = (*reread_earlier, *reread.rows)
    calls += reread.calls
    if reread.interrupted_by is not None:
        # A capped Q-G2b chunk is a QueryDefect, so a chunk falls short only when interrupted.
        # The chunks read so far keep their rows, and the next call reads only the chunks
        # still deferred, so the verdict rests on one complete pass over every candidate.
        progress = EnumerationProgress(
            _SCAN_QUERY, reread.deferred, candidates, rereading=True, reread_rows=reread_rows
        )
        return _log_campaigns(
            CampaignEnumeration(
                present=present,
                removed=(),
                missing_ids=frozenset(),
                complete=False,
                reason=interruption_reason(reread.interrupted_by, progress.outstanding),
                calls=calls,
                interrupted_by=reread.interrupted_by,
                progress=progress,
            ),
            tracked=len(tracked),
        )
    removed: list[GoogleAdsRow] = []
    still_present: list[GoogleAdsRow] = []
    returned: set[int] = set()
    for row in reread_rows:
        returned.add(_row_id(reread_template, row, _CAMPAIGN_FIELD))
        status = get_field(row, _CAMPAIGN_STATUS_FIELD, surface=reread_template.row_surface)
        (removed if status == _REMOVED else still_present).append(row)
    return _log_campaigns(
        CampaignEnumeration(
            present=(*present, *still_present),
            removed=tuple(removed),
            missing_ids=candidates - returned,
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
    *,
    resume: EnumerationProgress | None = None,
) -> EntityEnumeration: ...


@overload
async def enumerate_entities(
    client: GoogleAdsWebhookClient,
    query_id: Literal["Q-G7"],
    tracked_keys: Collection[tuple[int, int]],
    *,
    resume: EnumerationProgress | None = None,
) -> EntityEnumeration: ...


async def enumerate_entities(
    client: GoogleAdsWebhookClient,
    query_id: EntityQueryId,
    tracked_keys: Collection[int] | Collection[tuple[int, int]],
    *,
    resume: EnumerationProgress | None = None,
) -> EntityEnumeration:
    """Enumerate every ad group (Q-G4) or keyword (Q-G7) and the tracked keys no longer returned.

    Q-G4 runs the ``ad_group.id`` keyset scan and keys rows by ``ad_group.id``. Q-G7 runs the
    ``ad_group.id`` range partitioning from the full range, with a criterion keyset for any
    single capped ad group, and keys rows by ``(ad_group.id, ad_group_criterion.criterion_id)``,
    because criterion ids are unique only within an ad group. With ``resume`` only its pending
    scopes are read, in order. An interruption stops the reads at once and returns
    :attr:`EntityEnumeration.progress`. :attr:`EntityEnumeration.absent_keys` is reported only
    by the call in which the enumeration finishes, from the candidates of every call; until
    then it is empty and nothing is inferred.

    Args:
        client: The webhook client.
        query_id: ``"Q-G4"`` or ``"Q-G7"``.
        tracked_keys: The dataset's tracked keys: ``int`` ad-group ids for Q-G4,
            ``(ad_group_id, criterion_id)`` tuples for Q-G7.
        resume: Progress an earlier call returned for this enumeration, or ``None`` to start
            a new one.

    Returns:
        The rows read by this call and the absent tracked keys.

    Raises:
        AccessDenied: Propagated unchanged.
        QueryDefect: Propagated unchanged.
        ValueError: When ``query_id`` is neither ``"Q-G4"`` nor ``"Q-G7"``, a tracked key has
            the wrong shape, or ``resume`` belongs to another enumeration; nothing is sent.
    """
    if query_id not in _ENTITY_QUERY_IDS:
        raise ValueError("enumerate_entities reads 'Q-G4' or 'Q-G7' only")
    _check_resume(query_id, resume)
    template = QUERIES[query_id]
    absent: frozenset[int] | frozenset[tuple[int, int]]
    if query_id == "Q-G4":
        tracked_ad_groups = _tracked_ids(query_id, tracked_keys)
        scopes = (_frozen({LAST_ID: INT64_MIN_ID}),) if resume is None else resume.pending
        read = await _read_in_order(client, query_id, scopes)
        ad_groups = {_row_id(template, row, _AD_GROUP_FIELD) for row in read.rows}
        prior_ids = frozenset() if resume is None else _tracked_ids(query_id, resume.absent)
        absent = _absent_after(tracked_ad_groups, ad_groups, read, prior_ids)
    else:
        tracked_keywords = _tracked_pairs(query_id, tracked_keys)
        full = {AD_GROUP_LO: _FULL_RANGE[0], AD_GROUP_HI: _FULL_RANGE[1]}
        scopes = (_frozen(full),) if resume is None else resume.pending
        read = await _read_in_order(client, query_id, scopes)
        keywords = {
            (_row_id(template, row, _AD_GROUP_FIELD), _row_id(template, row, _CRITERION_FIELD))
            for row in read.rows
        }
        prior_pairs = frozenset() if resume is None else _tracked_pairs(query_id, resume.absent)
        absent = _absent_after(tracked_keywords, keywords, read, prior_pairs)
    progress: EnumerationProgress | None = None
    reason: str | None = None
    if read.interrupted_by is not None:
        progress = EnumerationProgress(query_id, read.pending, absent)
        reason = interruption_reason(read.interrupted_by, progress.outstanding)
    enumeration = EntityEnumeration(
        rows=read.rows,
        absent_keys=absent if progress is None else frozenset(),
        complete=progress is None,
        reason=reason,
        calls=read.calls,
        interrupted_by=read.interrupted_by,
        progress=progress,
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
        resumed=resume is not None,
        pending=0 if progress is None else progress.outstanding,
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


def _check_reread(
    chunks: Sequence[Mapping[str, object]], candidates: frozenset[int], rows: Sequence[object]
) -> None:
    """Validate the state of an unfinished Q-G2b re-read (:class:`EnumerationProgress`).

    Args:
        chunks: The pending Q-G2b scopes, already checked against the template.
        candidates: Every candidate id of the re-read.
        rows: The raw rows the chunks already re-read returned.

    Raises:
        ValueError: When a chunk is empty, holds more than ``MAX_IDS_PER_IN`` ids, repeats
            an id (within it or across chunks) or names a non-candidate; or a row is not a
            JSON object, has no INT64 ``campaign.id`` or no readable ``campaign.status``,
            names a non-candidate, repeats a campaign or names a campaign still pending.
            Messages never carry ids.
    """
    unread: set[int] = set()
    for chunk in chunks:
        ids = _scope_ids(chunk)
        if not ids or len(ids) > MAX_IDS_PER_IN:
            raise ValueError(f"Q-G2: a pending Q-G2b chunk holds 1 to {MAX_IDS_PER_IN} ids")
        distinct = set(ids)
        if len(distinct) != len(ids) or unread & distinct:
            raise ValueError("Q-G2: a candidate id is pending twice")
        if not candidates.issuperset(distinct):
            raise ValueError("Q-G2: a pending Q-G2b chunk names an id that is not a candidate")
        unread |= distinct
    template = QUERIES[_BY_IDS_QUERY]
    reread: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Q-G2: a re-read row must be a JSON object")
        try:
            campaign_id = _row_id(template, row, _CAMPAIGN_FIELD)
            get_field(row, _CAMPAIGN_STATUS_FIELD, surface=template.row_surface)
        except QueryDefect:
            raise ValueError(
                "Q-G2: a re-read row needs an INT64 campaign.id and a readable campaign.status"
            ) from None
        if campaign_id not in candidates:
            raise ValueError("Q-G2: a re-read row names a campaign that is not a candidate")
        if campaign_id in reread:
            raise ValueError("Q-G2: a re-read row repeats a campaign")
        if campaign_id in unread:
            raise ValueError("Q-G2: a re-read row names a campaign whose chunk is still pending")
        reread.add(campaign_id)


def _json_value(value: object) -> object:
    """Return a raw row value as plain JSON, for a stored re-read row.

    Objects and arrays are copied. A ``Decimal`` (how the HTTP layer decodes a JSON number
    with a fraction or exponent) becomes an ``int`` when integral and a ``float`` otherwise.
    """
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


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
        pending=0 if enumeration.progress is None else enumeration.progress.outstanding,
        rereading=enumeration.progress is not None and enumeration.progress.rereading,
    )
    return enumeration
