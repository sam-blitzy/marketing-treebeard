"""Per-dataset freshness, coverage and completeness: the ``dataset_freshness`` writes.

AAP 0.3.2 ("Freshness and coverage per dataset"). Every source dataset has one
``dataset_freshness`` row per dataset name in :data:`ads_engine.db.models.ops.DATASETS`
(``google.entities`` … ``hubspot.google_contacts``). Each sync job reports what it did to
one named dataset as a :class:`FreshnessOutcome`, and :func:`record_freshness` folds that
outcome into the stored row:

* ``last_attempt_at`` moves on every attempt; ``last_success_at`` only on success.
* ``covered_from`` / ``covered_through`` are the native dates the stored data covers. A
  success extends the stored range when the new range overlaps or touches it, and replaces
  it otherwise, so a gap in coverage is never hidden behind one wide range.
* ``completeness`` is ``complete``, ``incomplete`` or ``unknown``. A success writes
  ``complete`` or ``incomplete``, never ``unknown``. An incremental success that re-reads
  only part of the stored range never turns an ``incomplete`` row ``complete``: only an
  attempt that re-covers the whole stored range may do that.
* A failure changes only ``last_attempt_at`` and ``reason``; the last good data, its
  coverage and its completeness stand (AAP 0.3.3, transient row of the fallback table).

Consumers (the analysis runner and the current-state overlay) derive the per-item
``stale`` and ``incomplete`` flags from these rows and the item's required datasets. This
module only writes and reads rows; it never decides staleness. Every call touches exactly
one row, so a successful sync of one dataset never hides a failed refresh of another.

:func:`record_freshness` never commits: the caller owns the transaction, so the freshness
row lands atomically with the data it describes.

Example:
    A metrics job that ran out of Google call budget mid-partition::

        outcome = FreshnessOutcome.success(
            utcnow(),
            covered_from=start,
            covered_through=end,
            complete=False,
            reason=f"{REASON_DEFERRED}; 3 partitions deferred",
            repairable=True,
        )
        await record_freshness(session, dataset_id, "google.metrics_daily", outcome)

    On its next run the job checks :func:`needs_repair` and widens its fetch to the
    whole recorded range, so the deferred pages finish in a later 24 h window.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal, Self, get_args

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.db.models.ops import COMPLETENESS, DATASETS, DatasetFreshness, SourceDataset
from ads_engine.logging import get_logger
from ads_engine.settings import SOURCES, Source

__all__ = [
    "PERMANENT_REASONS",
    "REASON_DEFERRED",
    "REASON_FINEST_PARTITION",
    "REASON_INTERRUPTED",
    "REASON_SEARCH_CEILING",
    "REPAIRABLE_REASONS",
    "SOURCE_DATASETS",
    "Completeness",
    "FreshnessOutcome",
    "is_repairable_reason",
    "load_freshness",
    "needs_repair",
    "record_freshness",
    "source_of",
]

_log = get_logger(__name__)

Completeness = Literal["complete", "incomplete", "unknown"]
"""Completeness of a dataset over its covered range; equals :data:`ops.COMPLETENESS`."""

# ---------------------------------------------------------------------------------------
# Reason prefixes. Jobs start their ``reason`` with one of these stable strings and may
# append detail after it (for example "call budget exhausted; 3 partitions deferred"), so
# the repair logic recognises the cause by prefix whatever detail follows.
# ---------------------------------------------------------------------------------------

REASON_DEFERRED: Final = "call budget exhausted"
"""Repairable: pages or partitions were deferred because the Google call budget ran out."""

REASON_INTERRUPTED: Final = "interrupted"
"""Repairable: a transient error survived its retries part-way through a fetch."""

REASON_FINEST_PARTITION: Final = "result limit reached at finest partition"
"""Permanent: a single (day, ad group) Q-G6 partition still returned 10,000 rows."""

REASON_SEARCH_CEILING: Final = "search result ceiling reached"
"""Permanent: a one-hour HubSpot CRM Search partition still held more than 10,000 results."""

REPAIRABLE_REASONS: Final[tuple[str, ...]] = (REASON_DEFERRED, REASON_INTERRUPTED)
"""Prefixes of shortfalls that refetching the recorded range can make good."""

PERMANENT_REASONS: Final[tuple[str, ...]] = (REASON_FINEST_PARTITION, REASON_SEARCH_CEILING)
"""Prefixes of shortfalls inherent to the platform's limits; refetching cannot remove them."""

# A failure on an incomplete row appends its own reason after this marker, so the original
# shortfall (and its repairable prefix) survives the failure, and a later success can drop
# the failure note again without guessing where it starts.
_FAILURE_MARKER: Final = "; last attempt failed: "

# Recorded when an incomplete row is carried forward but no reason was ever stored for it
# (a row written outside this module), so ``incomplete`` is never left without a reason.
_UNSPECIFIED_SHORTFALL: Final = "earlier coverage incomplete"

_ONE_DAY: Final = timedelta(days=1)


def _build_source_datasets() -> Mapping[Source, tuple[str, ...]]:
    """Group :data:`DATASETS` by their source prefix, verifying the names on the way.

    Returns:
        A read-only mapping of every source to its dataset names, in ``DATASETS`` order.

    Raises:
        RuntimeError: If a dataset name is not ``<source>.<name>`` for a known source, a
            name is duplicated, or the grouped names do not add up to ``DATASETS``.
    """
    if get_args(Completeness) != tuple(COMPLETENESS):
        raise RuntimeError(
            f"Completeness {get_args(Completeness)!r} must equal ops.COMPLETENESS {COMPLETENESS!r}"
        )
    if len(set(DATASETS)) != len(DATASETS):
        raise RuntimeError(f"ops.DATASETS holds a duplicate name: {DATASETS!r}")
    grouped: dict[Source, tuple[str, ...]] = {
        source: tuple(name for name in DATASETS if name.partition(".")[0] == source)
        for source in SOURCES
    }
    grouped_names = {name for names in grouped.values() for name in names}
    stray = sorted(
        name for name in DATASETS if name not in grouped_names or not name.partition(".")[2]
    )
    if stray or grouped_names != set(DATASETS):
        raise RuntimeError(
            f"every dataset name must be '<source>.<name>' for a source in {SOURCES!r}; "
            f"unmatched: {stray!r}"
        )
    return MappingProxyType(grouped)


SOURCE_DATASETS: Final[Mapping[Source, tuple[str, ...]]] = _build_source_datasets()
"""The freshness dataset names of each source: google 3, linkedin 4, hubspot 4."""

_DATASET_SOURCE: Final[Mapping[str, Source]] = MappingProxyType(
    {name: source for source, names in SOURCE_DATASETS.items() for name in names}
)


def source_of(dataset: str) -> Source:
    """Return the source a freshness dataset name belongs to.

    Example:
        >>> source_of("linkedin.leads")
        'linkedin'

    Args:
        dataset: One of the names in :data:`ads_engine.db.models.ops.DATASETS`.

    Returns:
        The prefix before the dot, as a :data:`ads_engine.settings.Source`.

    Raises:
        ValueError: If ``dataset`` is not a known dataset name.
    """
    try:
        return _DATASET_SOURCE[dataset]
    except KeyError:
        raise ValueError(
            f"unknown freshness dataset {dataset!r}; expected one of {list(DATASETS)!r}"
        ) from None


def is_repairable_reason(reason: str | None) -> bool:
    """Return whether a recorded reason names a shortfall that refetching can repair.

    Args:
        reason: A ``dataset_freshness.reason`` value, or ``None``.

    Returns:
        ``True`` when ``reason`` starts with :data:`REASON_DEFERRED` or
        :data:`REASON_INTERRUPTED`.
    """
    return reason is not None and reason.startswith(REPAIRABLE_REASONS)


@dataclass(frozen=True, slots=True)
class FreshnessOutcome:
    """What one sync attempt established about one named dataset.

    Build instances with :meth:`success` or :meth:`failure`; direct construction is
    validated the same way. A success always establishes ``complete`` or ``incomplete``,
    and an ``incomplete`` success must say why, because dataset readiness accepts
    ``incomplete`` only with a recorded reason (AAP 0.3.3). A failure establishes nothing
    about the data, so it carries a reason and no coverage.

    Coverage is a closed range of the source's native dates (Google: the account's time
    zone; LinkedIn: UTC). Give both ends or neither. A success with no range describes a
    full-state read, such as an entity sync that re-reads every campaign: its completeness
    replaces the stored one and the stored coverage is left as it is.

    Attributes:
        attempted_at: When the attempt started; timezone-aware.
        succeeded: Whether the attempt finished, completely or not.
        covered_from: First native date the attempt read, or ``None``.
        covered_through: Last native date the attempt read, or ``None``.
        completeness: ``complete`` or ``incomplete`` for a success; ``unknown`` for a
            failure.
        reason: Why the read is incomplete, or why the attempt failed. Start it with one of
            the ``REASON_*`` prefixes where one applies, so repair logic recognises it.
        repairable: Whether a refetch of the recorded range can make the shortfall good.
            ``True`` requires an ``incomplete`` success whose reason starts with a
            prefix in :data:`REPAIRABLE_REASONS`, which is what :func:`needs_repair` reads
            back from the stored row.
    """

    attempted_at: datetime
    succeeded: bool
    covered_from: date | None = None
    covered_through: date | None = None
    completeness: Completeness = "unknown"
    reason: str | None = None
    repairable: bool = False

    def __post_init__(self) -> None:
        """Validate the outcome.

        Raises:
            ValueError: If ``attempted_at`` is naive, a coverage end is a ``datetime`` or
                is given without the other, the range is reversed, the completeness does
                not fit the success flag, a required reason is missing or blank, or
                ``repairable`` contradicts the completeness or the reason.
        """
        if self.attempted_at.tzinfo is None or self.attempted_at.utcoffset() is None:
            raise ValueError("attempted_at must be a timezone-aware datetime")
        for name, value in (
            ("covered_from", self.covered_from),
            ("covered_through", self.covered_through),
        ):
            if isinstance(value, datetime):
                raise ValueError(f"{name} must be a native date, not a datetime")
        if (self.covered_from is None) != (self.covered_through is None):
            raise ValueError("covered_from and covered_through must be given together")
        if (
            self.covered_from is not None
            and self.covered_through is not None
            and self.covered_from > self.covered_through
        ):
            raise ValueError(
                f"covered_from {self.covered_from} is after covered_through {self.covered_through}"
            )
        if self.completeness not in COMPLETENESS:
            raise ValueError(f"completeness must be one of {COMPLETENESS!r}")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("reason must not be blank; pass None for no reason")
        if self.succeeded:
            if self.completeness == "unknown":
                raise ValueError("a successful attempt must be complete or incomplete")
            if self.completeness == "incomplete" and self.reason is None:
                raise ValueError("an incomplete success needs a reason")
        else:
            if self.reason is None:
                raise ValueError("a failed attempt needs a reason")
            if self.covered_from is not None:
                raise ValueError("a failed attempt covers no dates")
            if self.completeness != "unknown":
                raise ValueError("a failed attempt establishes no completeness")
        if self.repairable and (
            self.completeness != "incomplete" or not is_repairable_reason(self.reason)
        ):
            raise ValueError(
                "repairable needs an incomplete success whose reason starts with one of "
                f"{list(REPAIRABLE_REASONS)!r}"
            )

    @classmethod
    def success(
        cls,
        attempted_at: datetime,
        *,
        covered_from: date | None,
        covered_through: date | None,
        complete: bool,
        reason: str | None = None,
        repairable: bool = False,
    ) -> Self:
        """Build the outcome of an attempt that finished, completely or not.

        Args:
            attempted_at: When the attempt started; timezone-aware.
            covered_from: First native date read, or ``None`` for a full-state read.
            covered_through: Last native date read, or ``None`` for a full-state read.
            complete: Whether everything in the range was read.
            reason: Why the read is incomplete; required when ``complete`` is false.
            repairable: Whether a refetch can repair the shortfall (see the class).

        Returns:
            The validated outcome.

        Raises:
            ValueError: If ``complete`` is false and no reason is given, or any other
                check in :meth:`__post_init__` fails.
        """
        return cls(
            attempted_at=attempted_at,
            succeeded=True,
            covered_from=covered_from,
            covered_through=covered_through,
            completeness="complete" if complete else "incomplete",
            reason=reason,
            repairable=repairable,
        )

    @classmethod
    def failure(cls, attempted_at: datetime, *, reason: str) -> Self:
        """Build the outcome of an attempt that failed before it established anything.

        Args:
            attempted_at: When the attempt started; timezone-aware.
            reason: Why it failed, for example the classified error after its retries.

        Returns:
            The validated outcome.

        Raises:
            ValueError: If ``attempted_at`` is naive or ``reason`` is blank.
        """
        return cls(attempted_at=attempted_at, succeeded=False, reason=reason)


@dataclass(frozen=True, slots=True)
class _State:
    """The mutable columns of one ``dataset_freshness`` row, as a value."""

    last_attempt_at: datetime | None
    last_success_at: datetime | None
    covered_from: date | None
    covered_through: date | None
    completeness: Completeness
    reason: str | None

    @classmethod
    def of(cls, row: DatasetFreshness | None) -> Self:
        """Read a stored row; an absent row is the empty state (``unknown``, no dates)."""
        if row is None:
            return cls(None, None, None, None, "unknown", None)
        return cls(
            last_attempt_at=row.last_attempt_at,
            last_success_at=row.last_success_at,
            covered_from=row.covered_from,
            covered_through=row.covered_through,
            completeness=_as_completeness(row.completeness),
            reason=row.reason,
        )


def _as_completeness(value: str) -> Completeness:
    """Narrow a stored ``completeness`` value to :data:`Completeness`.

    Raises:
        ValueError: If the value is not one of :data:`ops.COMPLETENESS` (the column's
            ``CHECK`` constraint makes this unreachable for rows read from the database).
    """
    if value == "complete":
        return "complete"
    if value == "incomplete":
        return "incomplete"
    if value == "unknown":
        return "unknown"
    raise ValueError(f"stored completeness {value!r} is not one of {COMPLETENESS!r}")


def _without_failure_note(reason: str | None) -> str | None:
    """Drop the note a failure appended to an incomplete row's reason, if any."""
    if reason is None:
        return None
    return reason.partition(_FAILURE_MARKER)[0] or None


def _failure_reason(stored: _State, failure: str) -> str:
    """Return the reason a failed attempt records.

    On an ``incomplete`` row the stored shortfall stays first and the failure is appended
    after :data:`_FAILURE_MARKER`, so the row still says why its data is incomplete and a
    repairable prefix survives for :func:`needs_repair`. Otherwise the failure reason
    replaces the stored one.
    """
    shortfall = _without_failure_note(stored.reason)
    if stored.completeness == "incomplete" and shortfall is not None:
        return f"{shortfall}{_FAILURE_MARKER}{failure}"
    return failure


def _merge_coverage(
    stored: _State, outcome: FreshnessOutcome
) -> tuple[date | None, date | None, bool]:
    """Combine the stored coverage with a successful attempt's range.

    Returns:
        ``(covered_from, covered_through, recovered_all)``. ``recovered_all`` is true when
        the attempt re-read everything the stored row vouches for, so its own
        completeness may replace the stored one.
    """
    new_from, new_through = outcome.covered_from, outcome.covered_through
    if new_from is None or new_through is None:
        # A full-state read (an entity sync) has no date range: it re-read everything.
        return stored.covered_from, stored.covered_through, True
    old_from, old_through = stored.covered_from, stored.covered_through
    if old_from is None or old_through is None or stored.completeness == "unknown":
        # Nothing verified is stored, so the new range is the coverage.
        return new_from, new_through, True
    touches = new_from <= old_through + _ONE_DAY and old_from <= new_through + _ONE_DAY
    if not touches:
        # Disjoint ranges: one wide range would hide the gap between them, so the new
        # range replaces the stored one.
        return new_from, new_through, True
    # Requires both ends: an attempt that starts early but stops before the stored end
    # has not re-read the tail it would otherwise vouch for.
    recovered_all = new_from <= old_from and new_through >= old_through
    return min(new_from, old_from), max(new_through, old_through), recovered_all


def _worse(stored: _State, outcome: FreshnessOutcome) -> tuple[Completeness, str | None]:
    """Combine completeness when the attempt re-read only part of the stored range.

    ``incomplete`` beats ``complete``. When the stored row is the worse one its shortfall
    reason is kept, because the attempt did not re-read the part that fell short. When both
    are incomplete, a repairable reason wins over a permanent one, so a deferred shortfall
    is never masked by one refetching cannot remove; otherwise the older shortfall stays.
    """
    shortfall = _without_failure_note(stored.reason)
    if stored.completeness != "incomplete":
        return outcome.completeness, outcome.reason
    if (
        outcome.completeness == "incomplete"
        and is_repairable_reason(outcome.reason)
        and not is_repairable_reason(shortfall)
    ):
        return "incomplete", outcome.reason
    return "incomplete", shortfall or outcome.reason or _UNSPECIFIED_SHORTFALL


def _merge(stored: _State, outcome: FreshnessOutcome) -> _State:
    """Fold one attempt's outcome into the stored state (pure; no I/O).

    A failure moves only ``last_attempt_at`` and ``reason``. A success moves both
    timestamps, merges coverage with :func:`_merge_coverage`, and takes the attempt's
    completeness when it re-read the whole stored range, or :func:`_worse` otherwise.
    """
    if not outcome.succeeded:
        failure = outcome.reason or "attempt failed"
        return replace(
            stored,
            last_attempt_at=outcome.attempted_at,
            reason=_failure_reason(stored, failure),
        )
    covered_from, covered_through, recovered_all = _merge_coverage(stored, outcome)
    if recovered_all:
        completeness, reason = outcome.completeness, outcome.reason
    else:
        completeness, reason = _worse(stored, outcome)
    return _State(
        last_attempt_at=outcome.attempted_at,
        last_success_at=outcome.attempted_at,
        covered_from=covered_from,
        covered_through=covered_through,
        completeness=completeness,
        reason=reason,
    )


async def record_freshness(
    session: AsyncSession,
    dataset_id: int,
    dataset: str,
    outcome: FreshnessOutcome,
) -> DatasetFreshness:
    """Fold one attempt's outcome into the ``dataset_freshness`` row it describes.

    The row is keyed (``dataset_id``, ``dataset``) and only that row is touched, so
    recording ``google.entities`` never changes ``google.metrics_daily``.

    * **Failure**: ``last_attempt_at`` and ``reason`` change. ``last_success_at``, the
      coverage and the completeness stand, so the last good data keeps its standing until
      consumers judge it stale; a first-ever failure records ``unknown``. On an
      ``incomplete`` row the failure is appended to the stored shortfall reason.
    * **Success**: ``last_attempt_at`` and ``last_success_at`` become ``attempted_at``.
      The coverage becomes the union of the stored and new ranges when they overlap or
      are adjacent, and the new range otherwise. The attempt's completeness and reason
      apply when it re-read the whole stored range; otherwise the worse of stored and new
      is kept, so a trailing-days refresh never turns a backfill-incomplete row complete.
      ``unknown`` is never written on success.

    Concurrent writers of the same row serialize: an empty row is first inserted with
    ``ON CONFLICT DO NOTHING`` so that the following ``SELECT ... FOR UPDATE`` always has
    a row to lock, then the merged state is written with ``INSERT ... ON CONFLICT DO
    UPDATE``. The function flushes but never commits; the caller's transaction makes the
    freshness row atomic with the data it describes.

    Args:
        session: The caller's session, inside the transaction that wrote the data.
        dataset_id: The ``source_dataset`` the data was written to.
        dataset: The dataset name, one of :data:`ops.DATASETS`.
        outcome: What the attempt established.

    Returns:
        The written row, refreshed from the database.

    Raises:
        ValueError: If ``dataset`` is unknown, ``dataset_id`` does not exist, or the source
            dataset belongs to a different source than ``dataset``.
    """
    source = source_of(dataset)
    dataset_source = await session.scalar(
        select(SourceDataset.source).where(SourceDataset.id == dataset_id)
    )
    if dataset_source is None:
        raise ValueError(f"source dataset {dataset_id} does not exist")
    if dataset_source != source:
        raise ValueError(
            f"freshness dataset {dataset!r} belongs to {source}, "
            f"but source dataset {dataset_id} is {dataset_source}"
        )

    key = {"source": source, "dataset_id": dataset_id, "dataset": dataset}
    # FOR UPDATE locks nothing when the row is missing, so make sure it exists first; the
    # empty row is overwritten below before this transaction can commit.
    await session.execute(
        insert(DatasetFreshness)
        .values(**key, completeness="unknown")
        .on_conflict_do_nothing(index_elements=["dataset_id", "dataset"])
    )
    stored = await session.scalar(
        select(DatasetFreshness)
        .where(DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == dataset)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    merged = _merge(_State.of(stored), outcome)

    values: dict[str, object] = {
        "last_attempt_at": merged.last_attempt_at,
        "last_success_at": merged.last_success_at,
        "covered_from": merged.covered_from,
        "covered_through": merged.covered_through,
        "completeness": merged.completeness,
        "reason": merged.reason,
    }
    # The upsert is written here, on the natural key, rather than through a generic
    # repository helper: the merge above needs the locked row, so read, merge and write
    # must happen together in this one function.
    upsert = insert(DatasetFreshness).values(**key, **values)
    upsert = upsert.on_conflict_do_update(
        index_elements=["dataset_id", "dataset"],
        set_={name: upsert.excluded[name] for name in ("source", *values)},
    )
    written = (
        await session.scalars(
            upsert.returning(DatasetFreshness),
            execution_options={"populate_existing": True},
        )
    ).one()

    event = {
        "dataset_id": dataset_id,
        "dataset": dataset,
        "succeeded": outcome.succeeded,
        "completeness": written.completeness,
        "covered_from": written.covered_from,
        "covered_through": written.covered_through,
        "reason": written.reason,
    }
    if outcome.succeeded:
        _log.info("sync.freshness", **event)
    else:
        _log.warning("sync.freshness", **event)
    return written


async def load_freshness(session: AsyncSession, dataset_id: int) -> dict[str, DatasetFreshness]:
    """Return every freshness row of one source dataset, keyed by dataset name.

    Names with no row yet are absent from the result; consumers treat an absent row as
    never refreshed (``unknown``).

    Args:
        session: An open session.
        dataset_id: The ``source_dataset`` whose rows to read.

    Returns:
        The rows, refreshed from the database, keyed by ``dataset`` in name order.
    """
    rows = await session.scalars(
        select(DatasetFreshness)
        .where(DatasetFreshness.dataset_id == dataset_id)
        .order_by(DatasetFreshness.dataset)
        .execution_options(populate_existing=True)
    )
    return {row.dataset: row for row in rows}


def needs_repair(row: DatasetFreshness | None) -> bool:
    """Return whether a dataset's next fetch must re-read its whole recorded range.

    A row needs repair when it is ``incomplete`` for a reason that a refetch can make
    good: its reason starts with :data:`REASON_DEFERRED` (Google call budget exhausted,
    pages deferred to a later 24 h window) or :data:`REASON_INTERRUPTED` (a transient error
    outlived its retries mid-fetch). Jobs that see ``True`` widen their next fetch from
    the usual trailing days to ``[covered_from, today]``. That fetch re-covers the whole
    stored range, so its own completeness replaces the stored one in
    :func:`record_freshness`, and the dataset stays ``incomplete`` until the deferred
    pages finish (AAP 0.3.3).

    Permanent shortfalls (:data:`REASON_FINEST_PARTITION`,
    :data:`REASON_SEARCH_CEILING`) come from the platform's own result limits, so
    refetching cannot remove them and they never trigger a repair. Neither does a
    ``complete`` or ``unknown`` row, or an absent one.

    Args:
        row: The stored freshness row, or ``None`` when the dataset has none yet.

    Returns:
        ``True`` when the next fetch should cover the full recorded range.
    """
    return row is not None and row.completeness == "incomplete" and is_repairable_reason(row.reason)
