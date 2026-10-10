"""Per-dataset freshness, coverage and completeness: the ``dataset_freshness`` writes.

AAP 0.3.2 ("Freshness and coverage per dataset"). Every source dataset has one
``dataset_freshness`` row per dataset name in :data:`ads_engine.db.models.ops.DATASETS`
(``google.entities`` … ``hubspot.google_contacts``). Each sync job reports what it did to
one named dataset as a :class:`FreshnessOutcome`, and :func:`record_freshness` folds that
outcome into the stored row:

* ``last_attempt_at`` moves on every later attempt; ``last_success_at`` only on a later
  success that was not cut short. A success cut short by a transient error that outlived its
  retries, or by a Google call-budget deferral (:attr:`FreshnessOutcome.cut_short`), records
  what it read (its coverage, ``incomplete`` and its reason) but is no full refresh, so
  ``last_success_at`` stands and the dataset turns stale once that last full refresh is older
  than ``sources.stale_after_hours`` (AAP 0.3.3: after a transient error or a deferral "the
  affected datasets keep their last good data and turn stale"; AAP 0.3.2 stale rule).
* ``covered_from`` / ``covered_through`` are the native dates the stored data covers. A
  success extends the stored range when the new range overlaps or touches it, and a later
  success replaces it otherwise, so a gap in coverage is never hidden behind one wide range.
* ``completeness`` is ``complete``, ``incomplete`` or ``unknown``. A success writes
  ``complete`` or ``incomplete``, never ``unknown``. An incremental success that re-reads
  only part of the stored range never turns an ``incomplete`` row ``complete``: only an
  attempt that re-covers the whole stored range may do that, unless the row's only
  shortfalls are dated finest-partition days that the attempt re-read. A Q-G6 reason names
  the days of its partitions at the result limit (:func:`shortfall_spans`), so a finished
  read (neither cut short nor repairable) of part of the range removes the days it re-read
  and keeps the rest, and the row turns ``complete`` once no day is left.
* A failure changes only ``last_attempt_at`` and ``reason``; the last good data, its
  coverage and its completeness stand (AAP 0.3.3, transient row of the fallback table).
* :func:`mark_unknown` turns a row ``unknown`` when data was added after the success it
  records (contacts matched into a building CRM dataset after its refresh); the next
  success replaces the mark and a failure keeps it.
* Outcomes are folded in the order their attempts started, whatever order they commit
  in. Both timestamps only move forward. A failure that started before the stored latest
  attempt is obsolete and changes nothing. A success that started before the stored
  latest success, or before a stored cut-short attempt, is merged conservatively: it never
  makes the row look better or newer (it moves ``last_success_at`` only to its own start,
  and only past an earlier one), and a range that leaves a gap to the stored one changes
  nothing. A success that started before a stored failed attempt keeps that newer
  failure's reason.

Consumers (the analysis runner and the current-state overlay) derive the per-item
``stale`` and ``incomplete`` flags from these rows and the item's required datasets. This
module only writes and reads rows; it never decides staleness. Every call touches exactly
one row, so a successful sync of one dataset never hides a failed refresh of another.

:func:`record_freshness` never commits: the caller owns the transaction, so the freshness
row lands atomically with the data it describes.

A Google job also keeps the unread work of an interrupted fetch on the row
(``dataset_freshness.checkpoint``): :func:`lock_checkpoint` reads it under the row lock and
:func:`write_checkpoint` replaces or clears it, in the caller's transaction as well.
:func:`record_freshness` never touches that column.

Example:
    A metrics job that ran out of Google call budget mid-partition records the shortfall and
    keeps the deferred scopes in the same transaction::

        outcome = FreshnessOutcome.success(
            utcnow(),
            covered_from=start,
            covered_through=end,
            complete=False,
            reason=f"{REASON_DEFERRED}; 3 partitions deferred",
            repairable=True,
            cut_short=True,
        )
        await write_checkpoint(session, dataset_id, "google.metrics_daily", checkpoint)
        await record_freshness(session, dataset_id, "google.metrics_daily", outcome)

    The row keeps its ``last_success_at``. Its next run reads the stored scopes first, so the
    deferred pages finish in a later 24 h window, and the row turns ``complete``, with a new
    ``last_success_at``, once nothing is left. A dataset with no usable checkpoint falls back
    to :func:`needs_repair` and re-reads its whole recorded range.
"""

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Literal, Self, get_args

from sqlalchemy import null, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.analysis.shortfall import (
    FAILURE_MARKER,
    FINEST_PARTITION,
    DaySpan,
    shortfall_outside,
    shortfall_spans,
    shortfall_within,
)
from ads_engine.db import repositories
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
    "DaySpan",
    "FreshnessOutcome",
    "is_repairable_reason",
    "load_freshness",
    "lock_checkpoint",
    "mark_unknown",
    "needs_repair",
    "record_freshness",
    "recorded_shortfall",
    "shortfall_outside",
    "shortfall_spans",
    "shortfall_within",
    "source_of",
    "write_checkpoint",
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

REASON_FINEST_PARTITION: Final = FINEST_PARTITION
"""Permanent: a single (day, ad group) Q-G6 partition still returned 10,000 rows.

Its detail names the days of those partitions (:func:`shortfall_spans`, re-exported with
:func:`shortfall_outside` and :func:`shortfall_within` from :mod:`ads_engine.analysis.shortfall`),
so a later finished read of those days ends the shortfall for them (:func:`record_freshness`)."""

REASON_SEARCH_CEILING: Final = "search result ceiling reached"
"""Permanent: a one-hour HubSpot CRM Search partition still held more than 10,000 results."""

REPAIRABLE_REASONS: Final[tuple[str, ...]] = (REASON_DEFERRED, REASON_INTERRUPTED)
"""Prefixes of shortfalls that refetching the recorded range can make good."""

PERMANENT_REASONS: Final[tuple[str, ...]] = (REASON_FINEST_PARTITION, REASON_SEARCH_CEILING)
"""Prefixes of shortfalls inherent to the platform's limits; a refetch meets them again, so they
never trigger a repair (:func:`needs_repair`)."""

# A failure on an incomplete row appends its own reason after this marker, so the original
# shortfall (and its repairable prefix) survives the failure, and a later success can drop
# the failure note again without guessing where it starts. The dated-shortfall grammar
# ignores the note through the same marker.
_FAILURE_MARKER: Final = FAILURE_MARKER

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

    A success whose read a transient error (after its retries) or a Google call-budget
    deferral stopped before it finished is ``cut_short``: what it read is recorded like any
    incomplete success, but it is no successful refresh, so :func:`record_freshness` keeps
    the stored ``last_success_at`` and the dataset turns stale once that is too old (AAP
    0.3.3 transient row and "Timeouts and budget"). A shortfall the platform's own limits
    cause (a result limit, a page limit) is not cut short: the read finished.

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
        cut_short: Whether a transient error that outlived its retries, or a call-budget
            deferral, stopped the read before it finished; ``last_success_at`` then stands.
            ``True`` requires an ``incomplete`` success, which always carries its reason. It
            is set by the job that saw the interruption, never inferred from the reason,
            because not every source's interruption reason carries a repairable prefix.
    """

    attempted_at: datetime
    succeeded: bool
    covered_from: date | None = None
    covered_through: date | None = None
    completeness: Completeness = "unknown"
    reason: str | None = None
    repairable: bool = False
    cut_short: bool = False

    def __post_init__(self) -> None:
        """Validate the outcome.

        Raises:
            ValueError: If ``attempted_at`` is naive, a coverage end is a ``datetime`` or
                is given without the other, the range is reversed, the completeness does
                not fit the success flag, a required reason is missing or blank,
                ``repairable`` contradicts the completeness or the reason, or ``cut_short``
                is set on anything but an incomplete success.
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
        if self.cut_short and (
            not self.succeeded or self.completeness != "incomplete" or self.reason is None
        ):
            raise ValueError("cut_short needs an incomplete success with its reason")

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
        cut_short: bool = False,
    ) -> Self:
        """Build the outcome of an attempt that finished, completely or not.

        Args:
            attempted_at: When the attempt started; timezone-aware.
            covered_from: First native date read, or ``None`` for a full-state read.
            covered_through: Last native date read, or ``None`` for a full-state read.
            complete: Whether everything in the range was read.
            reason: Why the read is incomplete; required when ``complete`` is false.
            repairable: Whether a refetch can repair the shortfall (see the class).
            cut_short: Whether a transient error or a call-budget deferral stopped the read
                early, so the attempt is no successful refresh (see the class); requires
                ``complete`` false.

        Returns:
            The validated outcome.

        Raises:
            ValueError: If ``complete`` is false and no reason is given, ``cut_short`` is
                set on a complete read, or any other check in :meth:`__post_init__` fails.
        """
        return cls(
            attempted_at=attempted_at,
            succeeded=True,
            covered_from=covered_from,
            covered_through=covered_through,
            completeness="complete" if complete else "incomplete",
            reason=reason,
            repairable=repairable,
            cut_short=cut_short,
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


def _later(stored: datetime | None, attempted_at: datetime) -> datetime:
    """Return the later of a stored instant and an attempt's start; ``None`` is earliest."""
    return attempted_at if stored is None else max(stored, attempted_at)


def _newer_failure_reason(stored: _State, attempted_at: datetime) -> str | None:
    """Return the reason of a stored failed attempt that started after ``attempted_at``.

    The stored latest attempt is a failure when it is later than the stored latest success,
    or when there is no success at all. Its reason is the text after
    :data:`_FAILURE_MARKER` when a failure note was appended to a shortfall, and the whole
    stored reason otherwise (see :func:`_failure_reason`).

    Returns:
        The failure's reason, or ``None`` when the stored latest attempt is not a failure
        that started after ``attempted_at``, or the row records no reason for it.
    """
    last_attempt, last_success = stored.last_attempt_at, stored.last_success_at
    if last_attempt is None or last_attempt <= attempted_at:
        return None
    if last_success is not None and last_success >= last_attempt:
        return None
    if stored.reason is None:
        return None
    shortfall, marker, note = stored.reason.partition(_FAILURE_MARKER)
    return (note if marker else shortfall) or None


def _newer_cut_short(stored: _State, attempted_at: datetime) -> bool:
    """Return whether the stored latest attempt is a cut-short success after ``attempted_at``.

    A cut-short success moves ``last_attempt_at`` but not ``last_success_at`` and always
    records ``incomplete`` with its reason, while a failure on an incomplete row appends its
    reason after :data:`_FAILURE_MARKER` (:func:`_failure_reason`). So a stored latest attempt
    that started after ``attempted_at`` and after the stored latest success (or with none
    stored), on an ``incomplete`` row whose reason carries no failure note, was cut short. Its
    completeness and reason are then newer than the attempt being merged, which must not
    replace them, and it is no failure whose reason the merge appends.
    """
    last_attempt, last_success = stored.last_attempt_at, stored.last_success_at
    if last_attempt is None or last_attempt <= attempted_at:
        return False
    if last_success is not None and last_success >= last_attempt:
        return False
    return (
        stored.completeness == "incomplete"
        and stored.reason is not None
        and _FAILURE_MARKER not in stored.reason
    )


def _ranges_touch(
    first_from: date, first_through: date, second_from: date, second_through: date
) -> bool:
    """Return whether two closed date ranges overlap or are adjacent (no day between)."""
    return first_from <= second_through + _ONE_DAY and second_from <= first_through + _ONE_DAY


def _disjoint(stored: _State, outcome: FreshnessOutcome) -> bool:
    """Return whether a success's range and the stored verified range leave a gap.

    This is the case in which :func:`_merge_coverage` lets the new range replace the stored
    one. It is false for a full-state read, for a row with no stored range and for an
    ``unknown`` row, where nothing verified stands to be replaced.
    """
    new_from, new_through = outcome.covered_from, outcome.covered_through
    old_from, old_through = stored.covered_from, stored.covered_through
    if (
        new_from is None
        or new_through is None
        or old_from is None
        or old_through is None
        or stored.completeness == "unknown"
    ):
        return False
    return not _ranges_touch(new_from, new_through, old_from, old_through)


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
    if not _ranges_touch(new_from, new_through, old_from, old_through):
        # Disjoint ranges: one wide range would hide the gap between them, so the new
        # range replaces the stored one.
        return new_from, new_through, True
    # Requires both ends: an attempt that starts early but stops before the stored end
    # has not re-read the tail it would otherwise vouch for.
    recovered_all = new_from <= old_from and new_through >= old_through
    return min(new_from, old_from), max(new_through, old_through), recovered_all


def _top_level_parts(reason: str) -> tuple[str, ...]:
    """Split a reason at every ``"; "`` outside parentheses: the reasons a join put together.

    A dated part keeps the ``"; "`` between its entries, because they are inside its
    parentheses.
    """
    parts: list[str] = []
    depth = start = index = 0
    while index < len(reason):
        if reason[index] == "(":
            depth += 1
        elif reason[index] == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and reason.startswith("; ", index):
            parts.append(reason[start:index])
            start = index + 2
            index = start
            continue
        index += 1
    parts.append(reason[start:])
    return tuple(parts)


def _joined(first: str, second: str | None) -> str:
    """Join a shortfall reason after another with ``"; "``, adding only what it does not say.

    What ``first`` already represents is left out of ``second``, so joining the same reason
    again changes nothing and replaying an outcome leaves the row as it was
    (:func:`record_freshness`):

    * both dated (:func:`shortfall_spans`): ``second`` loses every day ``first`` names
      (:func:`shortfall_outside`, per span), and nothing is added once no day is left; a day
      both name stays flagged through ``first``;
    * otherwise ``second`` is left out when its top-level parts already stand, in order and
      together, among those of ``first`` (:func:`_top_level_parts`), ``first`` itself
      included.

    Args:
        first: The reason kept first, with no failure note.
        second: The reason to add, or ``None``.

    Returns:
        ``first``, followed by what ``second`` adds to it.
    """
    if second is None:
        return first
    days = shortfall_spans(first)
    if days is not None and shortfall_spans(second) is not None:
        added: str | None = second
        for start, end in days:
            added = shortfall_outside(added, start=start, end=end)
            if added is None:
                return first
        return f"{first}; {added}"
    held, adding = _top_level_parts(first), _top_level_parts(second)
    width = len(adding)
    if any(held[index : index + width] == adding for index in range(len(held) - width + 1)):
        return first
    return f"{first}; {second}"


def _worse(
    stored: _State, outcome: FreshnessOutcome, *, reread: bool = True
) -> tuple[Completeness, str | None]:
    """Combine completeness when the attempt's own completeness may not replace the stored one.

    That is when the attempt re-read only part of the stored range (``reread``), or when it
    started before the stored latest success (an obsolete attempt: ``reread`` false).
    ``incomplete`` beats ``complete``. When the stored row is the worse one its shortfall
    reason is kept, because the attempt did not re-read the part that fell short. When both
    are incomplete, a repairable reason wins over a permanent one, so a deferred shortfall is
    never masked by one refetching cannot remove; otherwise the stored shortfall stays.

    A stored shortfall that names its days (:func:`shortfall_spans`: the Q-G6
    finest-partition reason) is judged per day instead, when the attempt is a finished read
    of a date range, neither cut short nor repairable. A current attempt removes the days it
    re-read (:func:`shortfall_outside`): with none left its own completeness and reason
    apply, otherwise the row stays ``incomplete`` with the days left followed by the
    attempt's own shortfall. An obsolete attempt removes no day, because a newer read already
    stands, but an incomplete one adds its own shortfall after the stored one. Either way
    the attempt's shortfall adds only what the kept one does not already name
    (:func:`_joined`), so the same outcome merged again changes nothing.
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
    first, last = outcome.covered_from, outcome.covered_through
    if (
        shortfall is not None
        and shortfall_spans(shortfall) is not None
        and first is not None
        and last is not None
        and not outcome.cut_short
        and not outcome.repairable
    ):
        # A dated shortfall names the days it lacks, so it is settled per day: a finished
        # read of those days ends it, and one that met the limit on other days adds to it.
        # The stored shortfall therefore no longer wins a tie with the attempt's: keeping it
        # would flag days that were re-read, and dropping the attempt's would hide days that
        # were not. Undated shortfalls name no day and still yield only to a whole-range read.
        own = outcome.reason if outcome.completeness == "incomplete" else None
        if reread:
            remaining = shortfall_outside(shortfall, start=first, end=last)
            if remaining is None:
                return outcome.completeness, outcome.reason
            return "incomplete", _joined(remaining, own)
        if own is not None:
            return "incomplete", _joined(shortfall, own)
    return "incomplete", shortfall or outcome.reason or _UNSPECIFIED_SHORTFALL


def _merge(stored: _State, outcome: FreshnessOutcome) -> _State:
    """Fold one attempt's outcome into the stored state (pure; no I/O).

    Attempts can commit out of start order (a slow attempt finishes after a later, faster
    one), so the merge is chronological on ``attempted_at`` rather than on commit order:

    * Timestamps never move back: ``last_attempt_at`` becomes the later of the stored value
      and ``attempted_at``, and on a success that was not cut short so does
      ``last_success_at``. A cut-short success keeps the stored ``last_success_at``.
    * A failure that started before the stored latest attempt is obsolete: a later attempt
      already recorded the dataset's health, so the stored state is returned unchanged.
      Otherwise the failure moves only ``last_attempt_at`` and ``reason``.
    * A success that started before the stored latest success, or before a stored cut-short
      attempt (:func:`_newer_cut_short`), is obsolete for the data and never makes the row
      look better or newer. A range that leaves a gap to the stored one changes nothing;
      otherwise the coverage merges with :func:`_merge_coverage` and the completeness and
      reason come from :func:`_worse`, which removes no day of a dated shortfall. One that
      started after the stored latest success still moves ``last_success_at`` to its own
      start, as in start order.
    * Any other success merges coverage with :func:`_merge_coverage` and takes the
      attempt's completeness when it re-read the whole stored range, or :func:`_worse`
      otherwise, which ends the days of a dated shortfall a finished read re-read. A
      cut-short success merges its coverage, completeness and reason the same way, and
      removes no day.
    * A success that started before a stored failed attempt keeps that failure's reason
      (:func:`_failure_reason`), so the row reads as if both had committed in start order.
    """
    attempted_at = outcome.attempted_at
    last_attempt_at = _later(stored.last_attempt_at, attempted_at)
    if not outcome.succeeded:
        if stored.last_attempt_at is not None and attempted_at < stored.last_attempt_at:
            return stored
        failure = outcome.reason or "attempt failed"
        return replace(
            stored,
            last_attempt_at=last_attempt_at,
            reason=_failure_reason(stored, failure),
        )
    newer_cut_short = _newer_cut_short(stored, attempted_at)
    if newer_cut_short or (
        stored.last_success_at is not None and attempted_at < stored.last_success_at
    ):
        if _disjoint(stored, outcome):
            # The older range must neither replace the newer one nor be united with it
            # across the gap, so the stored coverage, completeness and reason stand.
            return replace(stored, last_attempt_at=last_attempt_at)
        covered_from, covered_through, _ = _merge_coverage(stored, outcome)
        completeness, reason = _worse(stored, outcome, reread=False)
    else:
        covered_from, covered_through, recovered_all = _merge_coverage(stored, outcome)
        if recovered_all:
            completeness, reason = outcome.completeness, outcome.reason
        else:
            completeness, reason = _worse(stored, outcome, reread=True)
    merged = _State(
        last_attempt_at=last_attempt_at,
        last_success_at=(
            stored.last_success_at
            if outcome.cut_short
            else _later(stored.last_success_at, attempted_at)
        ),
        covered_from=covered_from,
        covered_through=covered_through,
        completeness=completeness,
        reason=reason,
    )
    if newer_cut_short:
        # The newer attempt was a cut-short success, not a failure: no failure note to keep.
        return merged
    newer_failure = _newer_failure_reason(stored, attempted_at)
    if newer_failure is not None:
        return replace(merged, reason=_failure_reason(merged, newer_failure))
    return merged


async def _checked_source(session: AsyncSession, dataset_id: int, dataset: str) -> Source:
    """Return the source of a freshness dataset name that ``dataset_id`` may record.

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
    return source


async def record_freshness(
    session: AsyncSession,
    dataset_id: int,
    dataset: str,
    outcome: FreshnessOutcome,
) -> DatasetFreshness:
    """Fold one attempt's outcome into the ``dataset_freshness`` row it describes.

    The row is keyed (``dataset_id``, ``dataset``) and only that row is touched, so
    recording ``google.entities`` never changes ``google.metrics_daily``.

    * **Failure** started no earlier than the stored ``last_attempt_at``:
      ``last_attempt_at`` and ``reason`` change. ``last_success_at``, the coverage and the
      completeness stand, so the last good data keeps its standing until consumers judge
      it stale; a first-ever failure records ``unknown``. On an ``incomplete`` row the
      failure is appended to the stored shortfall reason.
    * **Success** started no earlier than the stored ``last_success_at``:
      ``last_success_at`` becomes ``attempted_at``, and so does ``last_attempt_at``
      unless a later attempt is already stored. The coverage becomes the union of the
      stored and new ranges when they overlap or are adjacent, and the new range
      otherwise. The attempt's completeness and reason apply when it re-read the whole
      stored range; otherwise the worse of stored and new is kept, so a trailing-days
      refresh never turns a backfill-incomplete row complete, unless the row's only
      shortfalls are dated finest-partition days the refresh re-read: a finished read
      (neither cut short nor repairable) removes those days from the reason, and the row
      turns complete once none is left. ``unknown`` is never written on success.
    * **Cut-short success** (:attr:`FreshnessOutcome.cut_short`: a transient error or a
      call-budget deferral stopped the read): merged like any success, so its coverage,
      ``incomplete`` and reason are recorded and a repairable reason still drives
      :func:`needs_repair`, except that ``last_success_at`` stands. The dataset therefore
      keeps its last good data and turns stale once its last full refresh is older than
      ``sources.stale_after_hours`` (AAP 0.3.3, 0.3.2); the next success that is not cut
      short moves ``last_success_at`` again.
    * **Chronology**: outcomes fold in the order their attempts started, not the order
      they commit in, and ``last_attempt_at`` and ``last_success_at`` never move back. A
      failure that started before the stored ``last_attempt_at`` is obsolete and leaves
      the row unchanged. A success that started before the stored ``last_success_at``, or
      before a stored cut-short attempt, is merged conservatively and never makes the row
      look better or newer: a range that leaves a gap to the stored one changes nothing,
      and otherwise the coverage merges as above while the worse completeness and its
      reason are kept, no dated day is removed, and an incomplete attempt's shortfall is
      added after a dated stored one, without the days that one already names (one that
      started after the stored ``last_success_at`` still moves it to its own start). A
      success that started before a stored failed attempt keeps that failure's reason, as
      if the two had committed in start order. Recording the same outcome again changes
      nothing.

    Concurrent writers of the same row serialize: an empty row is first inserted with
    ``ON CONFLICT DO NOTHING`` so that the following ``SELECT ... FOR UPDATE`` always has
    a row to lock, then the merged state is written with ``INSERT ... ON CONFLICT DO
    UPDATE``. The lock orders the writes but not the attempts, which is why the merge
    compares start times. The function flushes but never commits; the caller's
    transaction makes the freshness row atomic with the data it describes.

    Retired datasets are read-only (AAP 0.3.3), so the source dataset is then locked with
    :func:`~ads_engine.db.repositories.ensure_writable`: a retired one is refused, and the
    lifecycle lock held until the caller's transaction ends keeps an activation from
    retiring it before this row commits.

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
        ads_engine.db.repositories.DatasetNotWritable: If the source dataset is retired,
            including retired by an activation that committed while this call waited.
    """
    source = await _checked_source(session, dataset_id, dataset)
    await repositories.ensure_writable(session, dataset_id)

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
        "cut_short": outcome.cut_short,
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


async def mark_unknown(
    session: AsyncSession, dataset_id: int, dataset: str, *, reason: str
) -> DatasetFreshness | None:
    """Withdraw what a stored ``dataset_freshness`` row vouches for: make it ``unknown``.

    For data a recorded success no longer covers, for example contacts matched into a
    building CRM dataset after its contact refresh and associations ran. Only
    ``completeness`` and ``reason`` change; ``last_attempt_at``, ``last_success_at`` and
    the coverage stand, so the row keeps its history while consumers and the readiness test
    read it as unverified. The row is locked ``FOR UPDATE`` first, so the change serialises
    with :func:`record_freshness` writers of the same row.

    A later success replaces the mark: :func:`record_freshness` treats an ``unknown`` row as
    vouching for nothing, so the success's own coverage, completeness and reason apply. A
    later failure keeps it, because a failure never changes completeness. The function
    flushes but never commits; the caller's transaction owns the change.

    Retired datasets are read-only (AAP 0.3.3), so before the freshness row is locked the
    source dataset is locked with :func:`~ads_engine.db.repositories.ensure_writable`, in
    the code base's lock order (``source_dataset`` before ``dataset_freshness``): a retired
    one is refused, and the lifecycle lock held until the caller's transaction ends keeps an
    activation from retiring it before this change commits.

    Args:
        session: The caller's session.
        dataset_id: The ``source_dataset`` the row belongs to.
        dataset: The dataset name, one of :data:`ops.DATASETS`.
        reason: Why the row's data is no longer verified; must not be blank.

    Returns:
        The updated row, or ``None`` when no row exists yet: an absent row already reads as
        ``unknown``, so nothing is written.

    Raises:
        ValueError: If ``reason`` is blank, ``dataset`` is unknown, ``dataset_id`` does not
            exist, or the source dataset belongs to a different source than ``dataset``.
        ads_engine.db.repositories.DatasetNotWritable: If the source dataset is retired,
            including retired by an activation that committed while this call waited.
    """
    if not reason.strip():
        raise ValueError("a reason is required to mark freshness unknown")
    await _checked_source(session, dataset_id, dataset)
    await repositories.ensure_writable(session, dataset_id)
    row = await session.scalar(
        select(DatasetFreshness)
        .where(DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == dataset)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    previous = row.completeness
    row.completeness = "unknown"
    row.reason = reason
    await session.flush()
    _log.warning(
        "sync.freshness.unknown",
        dataset_id=dataset_id,
        dataset=dataset,
        previous_completeness=previous,
        reason=reason,
    )
    return row


async def _locked_row(session: AsyncSession, dataset_id: int, dataset: str) -> DatasetFreshness:
    """Lock one ``dataset_freshness`` row ``FOR UPDATE``, seeding it first when it is missing.

    The order is :func:`record_freshness`'s: the dataset name and source are checked, the
    source dataset is locked with :func:`~ads_engine.db.repositories.ensure_writable` (a
    retired one is refused), an absent row is inserted ``unknown`` with ``ON CONFLICT DO
    NOTHING`` so the lock always has a row, and the row is read back under the lock.

    Raises:
        ValueError: If ``dataset`` is unknown, ``dataset_id`` does not exist, or the source
            dataset belongs to a different source than ``dataset``.
        ads_engine.db.repositories.DatasetNotWritable: If the source dataset is retired.
    """
    source = await _checked_source(session, dataset_id, dataset)
    await repositories.ensure_writable(session, dataset_id)
    await session.execute(
        insert(DatasetFreshness)
        .values(source=source, dataset_id=dataset_id, dataset=dataset, completeness="unknown")
        .on_conflict_do_nothing(index_elements=["dataset_id", "dataset"])
    )
    locked = await session.scalars(
        select(DatasetFreshness)
        .where(DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == dataset)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return locked.one()


def _plain_checkpoint(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    """Return a checkpoint as a plain JSON object, rejecting anything JSON cannot store.

    Raises:
        ValueError: If ``checkpoint`` is not a mapping with string keys, or holds a value
            JSON cannot represent (a ``date``, a set, a non-finite float).
    """
    if not isinstance(checkpoint, Mapping) or not all(isinstance(key, str) for key in checkpoint):
        raise ValueError("a checkpoint must be a JSON object with string keys")
    try:
        text = json.dumps(dict(checkpoint), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"a checkpoint must hold JSON values only: {exc}") from None
    plain: dict[str, Any] = json.loads(text)
    return plain


async def lock_checkpoint(
    session: AsyncSession, dataset_id: int, dataset: str
) -> dict[str, Any] | None:
    """Lock one ``dataset_freshness`` row and return the checkpoint it currently stores.

    For a job that merges its own unread work with what another run stored meanwhile: the
    row stays locked ``FOR UPDATE`` until the caller's transaction ends, so no other writer
    can change the checkpoint between this read and :func:`write_checkpoint`. A missing row
    is seeded ``unknown`` first, exactly as :func:`record_freshness` seeds it. Nothing is
    committed.

    Args:
        session: The caller's session, inside its transaction.
        dataset_id: The ``source_dataset`` the row belongs to.
        dataset: The dataset name, one of :data:`ops.DATASETS`.

    Returns:
        A copy of the stored checkpoint, or ``None`` when none is stored.

    Raises:
        ValueError: If ``dataset`` is unknown, ``dataset_id`` does not exist, or the source
            dataset belongs to a different source than ``dataset``.
        ads_engine.db.repositories.DatasetNotWritable: If the source dataset is retired.
    """
    row = await _locked_row(session, dataset_id, dataset)
    return None if row.checkpoint is None else copy.deepcopy(row.checkpoint)


async def write_checkpoint(
    session: AsyncSession,
    dataset_id: int,
    dataset: str,
    checkpoint: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Store, replace or clear the checkpoint of one ``dataset_freshness`` row.

    The row is locked as :func:`lock_checkpoint` locks it (seeded ``unknown`` when missing,
    the source dataset checked writable first), the previously stored value is read under
    that lock, and only the ``checkpoint`` column is updated: the attempt timestamps,
    coverage, completeness and reason stand, because :func:`record_freshness` owns them. The
    function flushes but never commits, so the checkpoint lands in the caller's transaction,
    atomically with the freshness outcome and the data it describes.

    Example::

        previous = await write_checkpoint(
            session, dataset_id, "google.search_terms", {"version": 1, "pending": {...}}
        )
        await write_checkpoint(session, dataset_id, "google.search_terms", None)  # drained

    Args:
        session: The caller's session, inside its transaction.
        dataset_id: The ``source_dataset`` the row belongs to.
        dataset: The dataset name, one of :data:`ops.DATASETS`.
        checkpoint: The JSON object to store, or ``None`` to clear it: the column is then
            SQL ``NULL`` (never the JSON value ``null``) and the row's attribute reads ``None``.

    Returns:
        A copy of the checkpoint stored before this call, or ``None`` when none was.

    Raises:
        ValueError: If ``checkpoint`` is not a JSON object, ``dataset`` is unknown,
            ``dataset_id`` does not exist, or the source dataset belongs to a different
            source than ``dataset``; nothing is written.
        ads_engine.db.repositories.DatasetNotWritable: If the source dataset is retired.
    """
    value = None if checkpoint is None else _plain_checkpoint(checkpoint)
    row = await _locked_row(session, dataset_id, dataset)
    previous = None if row.checkpoint is None else copy.deepcopy(row.checkpoint)
    if value is None:
        # A ``jsonb`` column binds Python ``None`` as the JSON value ``null``, so clearing
        # assigns SQL ``NULL`` explicitly: "nothing outstanding" is ``checkpoint IS NULL``.
        # The ORM expires an attribute assigned a SQL expression, so it is read back here.
        row.checkpoint = null()
        await session.flush()
        await session.refresh(row, ["checkpoint"])
    else:
        row.checkpoint = value
        await session.flush()
    _log.info(
        "sync.freshness.checkpoint",
        dataset_id=dataset_id,
        dataset=dataset,
        stored=value is not None,
        replaced=previous is not None,
    )
    return previous


def recorded_shortfall(row: DatasetFreshness | None) -> str | None:
    """Return the shortfall an ``incomplete`` row records, without a failed attempt's note.

    A failure on an incomplete row appends its own reason after the shortfall (see
    :func:`record_freshness`); this returns the shortfall alone, for example
    ``"result limit reached at finest partition (2026-09-14, ad group 123)"``.

    Args:
        row: The stored freshness row, or ``None``.

    Returns:
        The shortfall reason, or ``None`` when the row is absent, not ``incomplete``, or
        records no reason.
    """
    if row is None or row.completeness != "incomplete":
        return None
    return _without_failure_note(row.reason)


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
    pages finish (AAP 0.3.3). A Google dataset whose row holds a usable ``checkpoint``
    resumes the stored scopes instead and widens nothing; the widening is its fallback.

    Permanent shortfalls (:data:`REASON_FINEST_PARTITION`,
    :data:`REASON_SEARCH_CEILING`) come from the platform's own result limits, so a
    refetch meets them again and they never trigger a repair; a dated Q-G6 shortfall ends
    for the days a later finished read re-reads (:func:`record_freshness`). Neither does a
    ``complete`` or ``unknown`` row, or an absent one.

    Args:
        row: The stored freshness row, or ``None`` when the dataset has none yet.

    Returns:
        ``True`` when the next fetch should cover the full recorded range.
    """
    return row is not None and row.completeness == "incomplete" and is_repairable_reason(row.reason)
