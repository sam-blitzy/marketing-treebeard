"""Tests for ``ads_engine.sync.freshness``: per-dataset freshness, coverage and completeness.

The pure parts need no database: the dataset names per source, the reason prefixes, the
validation of :class:`FreshnessOutcome`, the merge rules that fold an outcome into a stored
row, and :func:`needs_repair`.

:func:`record_freshness`, :func:`mark_unknown`, :func:`load_freshness` and the checkpoint
helpers (:func:`lock_checkpoint`, :func:`write_checkpoint`) run against the
PostgreSQL 18 test database named by ``TEST_DATABASE_URL`` (falling back to
``DATABASE_URL``). The module
creates a private schema, builds only ``source_dataset`` and ``dataset_freshness`` in it,
points every connection's ``search_path`` at it alone, and drops it with ``CASCADE`` at
teardown, so the ``public`` schema and every other schema stay untouched. An unreachable
database fails the database tests; it never skips them. Each database test creates and
disposes its own ``AsyncEngine`` inside its own event loop.
"""

import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo

import anyio
import pytest
from sqlalchemy import JSON, create_engine, func, inspect, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from ads_engine.db.base import Base
from ads_engine.db.models.ops import COMPLETENESS, DATASETS, DatasetFreshness, SourceDataset
from ads_engine.db.repositories import DatasetNotWritable
from ads_engine.sync import freshness
from ads_engine.sync.freshness import (
    PERMANENT_REASONS,
    REASON_DEFERRED,
    REASON_FINEST_PARTITION,
    REASON_INTERRUPTED,
    REASON_SEARCH_CEILING,
    REPAIRABLE_REASONS,
    SOURCE_DATASETS,
    FreshnessOutcome,
    is_repairable_reason,
    load_freshness,
    lock_checkpoint,
    mark_unknown,
    needs_repair,
    record_freshness,
    recorded_shortfall,
    source_of,
    write_checkpoint,
)

type Span = tuple[int, int] | None
type RowState = tuple[datetime | None, datetime | None, date | None, date | None, str, str | None]
type Sessions = async_sessionmaker[AsyncSession]

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The first attempt; later attempts are whole hours after it."""

DEFERRED = f"{REASON_DEFERRED}; 3 partitions deferred"
INTERRUPTED = f"{REASON_INTERRUPTED}: HTTP 503 after 3 attempts"
FAILURE_MARKER = freshness._FAILURE_MARKER
METRICS = "google.metrics_daily"


def _at(hour: int) -> datetime:
    """Return the instant ``hour`` hours after :data:`T0`."""
    return T0 + timedelta(hours=hour)


def _day(n: int) -> date:
    """Return native day ``n`` of the test calendar: day 1 is 2026-09-01, day 31 2026-10-01."""
    return date(2026, 8, 31) + timedelta(days=n)


def _span(span: Span) -> tuple[date | None, date | None]:
    """Turn a ``(first, last)`` pair of day numbers into native dates."""
    if span is None:
        return None, None
    return _day(span[0]), _day(span[1])


def _success(
    hour: int,
    span: Span,
    *,
    complete: bool = True,
    reason: str | None = None,
    repairable: bool = False,
) -> FreshnessOutcome:
    """Build a successful outcome attempted at ``_at(hour)`` over ``span``."""
    covered_from, covered_through = _span(span)
    return FreshnessOutcome.success(
        _at(hour),
        covered_from=covered_from,
        covered_through=covered_through,
        complete=complete,
        reason=reason,
        repairable=repairable,
    )


def _deferred(hour: int, span: Span) -> FreshnessOutcome:
    """Build an incomplete success whose pages were deferred by the call budget."""
    return _success(hour, span, complete=False, reason=DEFERRED, repairable=True)


def _stored(
    span: Span,
    completeness: str,
    reason: str | None = None,
    *,
    attempt: int = 0,
    success: int | None = 0,
) -> freshness._State:
    """Build a stored state as :func:`record_freshness` reads it back from a row."""
    covered_from, covered_through = _span(span)
    return freshness._State(
        last_attempt_at=_at(attempt),
        last_success_at=None if success is None else _at(success),
        covered_from=covered_from,
        covered_through=covered_through,
        completeness=freshness._as_completeness(completeness),
        reason=reason,
    )


def _row(
    completeness: str,
    reason: str | None = None,
    *,
    dataset: str = METRICS,
) -> DatasetFreshness:
    """Build an unsaved ``dataset_freshness`` row in memory."""
    return DatasetFreshness(
        source=source_of(dataset),
        dataset_id=1,
        dataset=dataset,
        completeness=completeness,
        reason=reason,
    )


def _expected(
    attempt: int,
    success: int | None,
    span: Span,
    completeness: str,
    reason: str | None = None,
) -> RowState:
    """Return the stored columns a row is expected to hold, in :func:`_state` order."""
    covered_from, covered_through = _span(span)
    return (
        _at(attempt),
        None if success is None else _at(success),
        covered_from,
        covered_through,
        completeness,
        reason,
    )


def _state(row: DatasetFreshness) -> RowState:
    """Return the stored columns of ``row`` that :func:`record_freshness` maintains."""
    return (
        row.last_attempt_at,
        row.last_success_at,
        row.covered_from,
        row.covered_through,
        row.completeness,
        row.reason,
    )


# ---------------------------------------------------------------------------------------
# Dataset names per source
# ---------------------------------------------------------------------------------------


def test_source_datasets_group_every_dataset_name_by_source() -> None:
    """Each source maps to its own dataset names, in ``DATASETS`` order, read-only."""
    assert dict(SOURCE_DATASETS) == {
        "google": ("google.entities", "google.metrics_daily", "google.search_terms"),
        "linkedin": (
            "linkedin.entities",
            "linkedin.analytics_daily",
            "linkedin.segments",
            "linkedin.leads",
        ),
        "hubspot": (
            "hubspot.contacts",
            "hubspot.associations",
            "hubspot.deal_search",
            "hubspot.google_contacts",
        ),
    }
    assert [name for names in SOURCE_DATASETS.values() for name in names] == list(DATASETS)
    with pytest.raises(TypeError):
        SOURCE_DATASETS["google"] = ()  # type: ignore[index]


@pytest.mark.parametrize("dataset", DATASETS)
def test_source_of_returns_the_source_prefix(dataset: str) -> None:
    """Every known dataset name belongs to the source before its dot."""
    source = source_of(dataset)
    assert source == dataset.partition(".")[0]
    assert dataset in SOURCE_DATASETS[source]


@pytest.mark.parametrize("dataset", ["google.campaigns", "facebook.ads", "google", ""])
def test_source_of_rejects_an_unknown_dataset_name(dataset: str) -> None:
    """A name outside ``DATASETS`` raises ``ValueError``."""
    with pytest.raises(ValueError, match="unknown freshness dataset"):
        source_of(dataset)


def test_build_source_datasets_rebuilds_the_published_mapping() -> None:
    """Building the mapping again from ``DATASETS`` gives the published mapping."""
    assert dict(freshness._build_source_datasets()) == dict(SOURCE_DATASETS)


def test_build_source_datasets_rejects_a_completeness_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Completeness`` must list exactly the values of ``ops.COMPLETENESS``."""
    monkeypatch.setattr(freshness, "COMPLETENESS", ("complete", "incomplete"))
    with pytest.raises(RuntimeError, match="must equal"):
        freshness._build_source_datasets()


def test_build_source_datasets_rejects_a_duplicate_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dataset name listed twice is refused."""
    monkeypatch.setattr(freshness, "DATASETS", (*DATASETS, "google.entities"))
    with pytest.raises(RuntimeError, match="duplicate name"):
        freshness._build_source_datasets()


@pytest.mark.parametrize("stray", ["facebook.ads", "google", "google."])
def test_build_source_datasets_rejects_a_name_outside_source_dot_name(
    monkeypatch: pytest.MonkeyPatch, stray: str
) -> None:
    """Every name must be ``<source>.<name>`` for a known source and a non-empty name."""
    monkeypatch.setattr(freshness, "DATASETS", (*DATASETS, stray))
    with pytest.raises(RuntimeError, match="unmatched"):
        freshness._build_source_datasets()


# ---------------------------------------------------------------------------------------
# Reason prefixes
# ---------------------------------------------------------------------------------------


def test_reason_prefixes_split_into_repairable_and_permanent() -> None:
    """Budget deferral and interruption are repairable; platform result limits are not."""
    assert REASON_DEFERRED == "call budget exhausted"
    assert REASON_FINEST_PARTITION == "result limit reached at finest partition"
    assert REPAIRABLE_REASONS == (REASON_DEFERRED, REASON_INTERRUPTED)
    assert PERMANENT_REASONS == (REASON_FINEST_PARTITION, REASON_SEARCH_CEILING)
    assert not set(REPAIRABLE_REASONS) & set(PERMANENT_REASONS)


@pytest.mark.parametrize(
    ("reason", "repairable"),
    [
        (None, False),
        (REASON_DEFERRED, True),
        (DEFERRED, True),
        (REASON_INTERRUPTED, True),
        (INTERRUPTED, True),
        (f"{DEFERRED}{FAILURE_MARKER}timeout", True),
        (REASON_FINEST_PARTITION, False),
        (REASON_SEARCH_CEILING, False),
        ("HTTP 503 after 3 attempts", False),
        (f"partially {REASON_INTERRUPTED}", False),
    ],
)
def test_is_repairable_reason_matches_repairable_prefixes_only(
    reason: str | None, repairable: bool
) -> None:
    """Only a reason that starts with a repairable prefix is repairable."""
    assert is_repairable_reason(reason) is repairable


# ---------------------------------------------------------------------------------------
# FreshnessOutcome
# ---------------------------------------------------------------------------------------


def test_success_builds_a_complete_outcome() -> None:
    """A complete success carries its range, ``complete`` and no reason."""
    outcome = _success(0, (1, 28))
    assert outcome == FreshnessOutcome(
        attempted_at=T0,
        succeeded=True,
        covered_from=_day(1),
        covered_through=_day(28),
        completeness="complete",
        reason=None,
        repairable=False,
    )


def test_success_builds_an_incomplete_outcome_with_its_reason() -> None:
    """An incomplete success carries ``incomplete``, its reason and its repairability."""
    deferred = _deferred(0, (1, 28))
    assert (deferred.completeness, deferred.reason, deferred.repairable) == (
        "incomplete",
        DEFERRED,
        True,
    )
    permanent = _success(0, (1, 28), complete=False, reason=REASON_FINEST_PARTITION)
    assert (permanent.completeness, permanent.reason, permanent.repairable) == (
        "incomplete",
        REASON_FINEST_PARTITION,
        False,
    )


def test_success_accepts_a_full_state_read_and_any_aware_time_zone() -> None:
    """A full-state read has no range, and any timezone-aware instant is accepted."""
    eastern = datetime(2026, 10, 1, 2, 0, tzinfo=timezone(timedelta(hours=-4)))
    outcome = FreshnessOutcome.success(
        eastern, covered_from=None, covered_through=None, complete=True
    )
    assert (outcome.covered_from, outcome.covered_through) == (None, None)
    assert outcome.attempted_at == T0


def test_failure_builds_an_outcome_with_no_coverage_or_completeness() -> None:
    """A failure carries its reason, ``unknown`` and no range."""
    outcome = FreshnessOutcome.failure(T0, reason="HTTP 503 after 3 attempts")
    assert outcome == FreshnessOutcome(
        attempted_at=T0,
        succeeded=False,
        covered_from=None,
        covered_through=None,
        completeness="unknown",
        reason="HTTP 503 after 3 attempts",
        repairable=False,
    )


def test_outcome_is_immutable() -> None:
    """Outcomes are frozen values."""
    outcome = FreshnessOutcome.failure(T0, reason="timeout")
    with pytest.raises(FrozenInstanceError):
        outcome.reason = "other"  # type: ignore[misc]


class _NoOffset(tzinfo):
    """A tzinfo that is set but yields no UTC offset, so the instant is still naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        """Return no offset."""
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        """Return no DST offset."""
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        """Return no name."""
        return None


@pytest.mark.parametrize(
    ("build", "message"),
    [
        pytest.param(
            lambda: FreshnessOutcome.failure(T0.replace(tzinfo=None), reason="timeout"),
            "must be a timezone-aware datetime",
            id="naive-attempted-at",
        ),
        pytest.param(
            lambda: FreshnessOutcome.failure(T0.replace(tzinfo=_NoOffset()), reason="timeout"),
            "must be a timezone-aware datetime",
            id="tzinfo-without-offset",
        ),
        pytest.param(
            lambda: FreshnessOutcome.success(
                T0, covered_from=T0, covered_through=_day(30), complete=True
            ),
            "covered_from must be a native date",
            id="covered-from-datetime",
        ),
        pytest.param(
            lambda: FreshnessOutcome.success(
                T0, covered_from=_day(1), covered_through=T0, complete=True
            ),
            "covered_through must be a native date",
            id="covered-through-datetime",
        ),
        pytest.param(
            lambda: FreshnessOutcome.success(
                T0, covered_from=_day(1), covered_through=None, complete=True
            ),
            "must be given together",
            id="covered-from-only",
        ),
        pytest.param(
            lambda: FreshnessOutcome.success(
                T0, covered_from=None, covered_through=_day(1), complete=True
            ),
            "must be given together",
            id="covered-through-only",
        ),
        pytest.param(
            lambda: FreshnessOutcome.success(
                T0, covered_from=_day(2), covered_through=_day(1), complete=True
            ),
            "is after covered_through",
            id="reversed-range",
        ),
        pytest.param(
            lambda: FreshnessOutcome(
                attempted_at=T0,
                succeeded=True,
                completeness="partial",  # type: ignore[arg-type]
            ),
            "completeness must be one of",
            id="unknown-completeness-value",
        ),
        pytest.param(
            lambda: _success(0, (1, 2), complete=False, reason="   "),
            "must not be blank",
            id="blank-success-reason",
        ),
        pytest.param(
            lambda: FreshnessOutcome.failure(T0, reason=""),
            "must not be blank",
            id="empty-failure-reason",
        ),
        pytest.param(
            lambda: FreshnessOutcome(attempted_at=T0, succeeded=True),
            "must be complete or incomplete",
            id="success-with-unknown",
        ),
        pytest.param(
            lambda: _success(0, (1, 2), complete=False),
            "an incomplete success needs a reason",
            id="incomplete-without-reason",
        ),
        pytest.param(
            lambda: FreshnessOutcome(attempted_at=T0, succeeded=False),
            "a failed attempt needs a reason",
            id="failure-without-reason",
        ),
        pytest.param(
            lambda: FreshnessOutcome(
                attempted_at=T0,
                succeeded=False,
                covered_from=_day(1),
                covered_through=_day(2),
                reason="timeout",
            ),
            "a failed attempt covers no dates",
            id="failure-with-coverage",
        ),
        pytest.param(
            lambda: FreshnessOutcome(
                attempted_at=T0, succeeded=False, completeness="complete", reason="timeout"
            ),
            "a failed attempt establishes no completeness",
            id="failure-with-completeness",
        ),
        pytest.param(
            lambda: _success(0, (1, 2), reason=REASON_DEFERRED, repairable=True),
            "repairable needs an incomplete success",
            id="repairable-complete",
        ),
        pytest.param(
            lambda: _success(
                0, (1, 2), complete=False, reason=REASON_FINEST_PARTITION, repairable=True
            ),
            "repairable needs an incomplete success",
            id="repairable-permanent-reason",
        ),
        pytest.param(
            lambda: _success(
                0, (1, 2), complete=False, reason="3 partitions deferred", repairable=True
            ),
            "repairable needs an incomplete success",
            id="repairable-without-prefix",
        ),
        pytest.param(
            lambda: FreshnessOutcome(
                attempted_at=T0, succeeded=False, reason=REASON_DEFERRED, repairable=True
            ),
            "repairable needs an incomplete success",
            id="repairable-failure",
        ),
    ],
)
def test_outcome_validation_rejects_inconsistent_outcomes(
    build: Callable[[], FreshnessOutcome], message: str
) -> None:
    """Every documented inconsistency raises ``ValueError`` at construction."""
    with pytest.raises(ValueError, match=message):
        build()


# ---------------------------------------------------------------------------------------
# Stored state
# ---------------------------------------------------------------------------------------


def test_state_of_an_absent_row_is_empty_and_unknown() -> None:
    """No row reads as ``unknown`` with no timestamps, dates or reason."""
    assert freshness._State.of(None) == freshness._State(None, None, None, None, "unknown", None)


def test_state_of_a_row_copies_its_columns() -> None:
    """A stored row reads back column for column."""
    row = _row("incomplete", DEFERRED)
    row.last_attempt_at, row.last_success_at = _at(2), _at(1)
    row.covered_from, row.covered_through = _day(1), _day(28)
    assert freshness._State.of(row) == freshness._State(
        _at(2), _at(1), _day(1), _day(28), "incomplete", DEFERRED
    )


@pytest.mark.parametrize("value", COMPLETENESS)
def test_as_completeness_accepts_every_stored_value(value: str) -> None:
    """Every ``ops.COMPLETENESS`` value narrows to itself."""
    assert freshness._as_completeness(value) == value


def test_as_completeness_rejects_any_other_value() -> None:
    """A value outside ``ops.COMPLETENESS`` raises ``ValueError``."""
    with pytest.raises(ValueError, match="is not one of"):
        freshness._as_completeness("partial")


# ---------------------------------------------------------------------------------------
# Merge rules
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, None),
        (DEFERRED, DEFERRED),
        (f"{DEFERRED}{FAILURE_MARKER}timeout", DEFERRED),
        (f"{FAILURE_MARKER}timeout", None),
    ],
)
def test_without_failure_note_drops_only_the_appended_note(
    reason: str | None, expected: str | None
) -> None:
    """The text before the failure marker survives; an empty remainder becomes ``None``."""
    assert freshness._without_failure_note(reason) == expected


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        pytest.param(
            _stored((1, 28), "incomplete", DEFERRED),
            f"{DEFERRED}{FAILURE_MARKER}timeout",
            id="incomplete-keeps-shortfall-first",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", f"{DEFERRED}{FAILURE_MARKER}HTTP 429"),
            f"{DEFERRED}{FAILURE_MARKER}timeout",
            id="incomplete-replaces-earlier-note",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", None),
            "timeout",
            id="incomplete-without-reason",
        ),
        pytest.param(
            _stored((1, 28), "complete", "earlier failure"),
            "timeout",
            id="complete-replaces",
        ),
        pytest.param(
            _stored(None, "unknown", "earlier failure", success=None),
            "timeout",
            id="unknown-replaces",
        ),
    ],
)
def test_failure_reason_appends_to_an_incomplete_shortfall_only(
    stored: freshness._State, expected: str
) -> None:
    """A failure keeps an incomplete row's shortfall first; otherwise it replaces the reason."""
    assert freshness._failure_reason(stored, "timeout") == expected


@pytest.mark.parametrize(
    ("stored", "new", "expected"),
    [
        pytest.param(
            _stored((1, 28), "complete"), None, ((1, 28), True), id="full-state-read-keeps"
        ),
        pytest.param(
            _stored(None, "unknown", success=None),
            None,
            (None, True),
            id="full-state-read-on-empty",
        ),
        pytest.param(
            _stored(None, "unknown", success=None),
            (1, 28),
            ((1, 28), True),
            id="nothing-stored",
        ),
        pytest.param(
            _stored((1, 10), "unknown"), (5, 15), ((5, 15), True), id="unknown-stored-range"
        ),
        pytest.param(
            _stored((1, 28), "complete"), (26, 30), ((1, 30), False), id="trailing-overlap"
        ),
        pytest.param(_stored((1, 10), "complete"), (11, 15), ((1, 15), False), id="adjacent-after"),
        pytest.param(_stored((10, 20), "complete"), (1, 9), ((1, 20), False), id="adjacent-before"),
        pytest.param(_stored((1, 10), "complete"), (12, 15), ((12, 15), True), id="gap-after"),
        pytest.param(_stored((10, 20), "complete"), (1, 8), ((1, 8), True), id="gap-before"),
        pytest.param(_stored((5, 10), "complete"), (1, 12), ((1, 12), True), id="superset"),
        pytest.param(_stored((1, 28), "incomplete", DEFERRED), (1, 28), ((1, 28), True), id="same"),
        pytest.param(
            _stored((5, 10), "complete"), (1, 8), ((1, 10), False), id="early-start-short-end"
        ),
        pytest.param(_stored((1, 28), "complete"), (10, 12), ((1, 28), False), id="inside"),
    ],
)
def test_merge_coverage_extends_touching_ranges_and_replaces_disjoint_ones(
    stored: freshness._State, new: Span, expected: tuple[Span, bool]
) -> None:
    """Touching ranges unite; disjoint ones replace; only a full re-read recovers all."""
    outcome = _success(1, new)
    span, recovered_all = expected
    assert freshness._merge_coverage(stored, outcome) == (*_span(span), recovered_all)


@pytest.mark.parametrize(
    ("stored", "outcome", "expected"),
    [
        pytest.param(
            _stored((1, 28), "complete"),
            _deferred(1, (26, 30)),
            ("incomplete", DEFERRED),
            id="complete-downgraded",
        ),
        pytest.param(
            _stored((1, 28), "complete"),
            _success(1, (26, 30)),
            ("complete", None),
            id="complete-stays-complete",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", REASON_FINEST_PARTITION),
            _deferred(1, (26, 30)),
            ("incomplete", DEFERRED),
            id="repairable-beats-permanent",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", DEFERRED),
            _success(1, (26, 30), complete=False, reason=REASON_FINEST_PARTITION),
            ("incomplete", DEFERRED),
            id="permanent-never-masks-repairable",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", DEFERRED),
            _success(1, (26, 30), complete=False, reason=INTERRUPTED, repairable=True),
            ("incomplete", DEFERRED),
            id="older-repairable-stays",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", REASON_FINEST_PARTITION),
            _success(1, (26, 30), complete=False, reason=REASON_SEARCH_CEILING),
            ("incomplete", REASON_FINEST_PARTITION),
            id="older-permanent-stays",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", f"{DEFERRED}{FAILURE_MARKER}timeout"),
            _success(1, (26, 30)),
            ("incomplete", DEFERRED),
            id="failure-note-dropped",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", None),
            _success(1, (26, 30), complete=False, reason=REASON_SEARCH_CEILING),
            ("incomplete", REASON_SEARCH_CEILING),
            id="no-stored-reason-takes-new",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", None),
            _success(1, (26, 30)),
            ("incomplete", freshness._UNSPECIFIED_SHORTFALL),
            id="no-reason-anywhere",
        ),
    ],
)
def test_worse_keeps_the_worse_completeness_and_its_reason(
    stored: freshness._State, outcome: FreshnessOutcome, expected: tuple[str, str | None]
) -> None:
    """``incomplete`` beats ``complete``, and a repairable shortfall is never masked."""
    assert freshness._worse(stored, outcome) == expected


def test_merge_of_a_failure_moves_only_the_attempt_and_reason() -> None:
    """A failure keeps the last success, the coverage and the completeness."""
    stored = _stored((1, 28), "complete")
    merged = freshness._merge(stored, FreshnessOutcome.failure(_at(1), reason="timeout"))
    assert merged == freshness._State(_at(1), _at(0), _day(1), _day(28), "complete", "timeout")


def test_merge_of_a_first_failure_records_unknown() -> None:
    """A failure on an absent row records the attempt, ``unknown`` and the reason only."""
    merged = freshness._merge(
        freshness._State.of(None), FreshnessOutcome.failure(_at(1), reason="timeout")
    )
    assert merged == freshness._State(_at(1), None, None, None, "unknown", "timeout")


def test_merge_of_a_full_re_read_takes_its_completeness() -> None:
    """A success that re-reads the whole stored range replaces completeness and reason."""
    merged = freshness._merge(_stored((1, 28), "incomplete", DEFERRED), _success(1, (1, 30)))
    assert merged == freshness._State(_at(1), _at(1), _day(1), _day(30), "complete", None)


def test_merge_of_a_partial_re_read_keeps_the_worse_completeness() -> None:
    """A trailing-days success never turns an incomplete row complete."""
    merged = freshness._merge(_stored((1, 28), "incomplete", DEFERRED), _success(1, (26, 30)))
    assert merged == freshness._State(_at(1), _at(1), _day(1), _day(30), "incomplete", DEFERRED)


# ---------------------------------------------------------------------------------------
# Merge rules: chronology (outcomes committed out of start order)
# ---------------------------------------------------------------------------------------

NOTED = f"{DEFERRED}{FAILURE_MARKER}timeout"
"""An incomplete row's reason after a ``timeout`` failure."""


@pytest.mark.parametrize(
    ("stored", "attempt", "expected"),
    [
        pytest.param(None, 1, 1, id="none-is-earliest"),
        pytest.param(2, 1, 2, id="stored-later"),
        pytest.param(1, 2, 2, id="attempt-later"),
        pytest.param(1, 1, 1, id="tie"),
    ],
)
def test_later_never_moves_an_instant_back(stored: int | None, attempt: int, expected: int) -> None:
    """The later of the stored instant and the attempt's start wins; ``None`` is earliest."""
    assert freshness._later(None if stored is None else _at(stored), _at(attempt)) == _at(expected)


@pytest.mark.parametrize(
    ("stored", "attempt", "expected"),
    [
        pytest.param(
            _stored((1, 28), "complete", "timeout", attempt=2), 1, "timeout", id="complete-row"
        ),
        pytest.param(_stored((1, 28), "incomplete", NOTED, attempt=2), 1, "timeout", id="noted"),
        pytest.param(
            _stored((1, 28), "incomplete", "timeout", attempt=2),
            1,
            "timeout",
            id="incomplete-row-without-shortfall",
        ),
        pytest.param(
            _stored(None, "unknown", "HTTP 502", attempt=2, success=None),
            1,
            "HTTP 502",
            id="never-succeeded",
        ),
        pytest.param(
            _stored((1, 28), "complete", attempt=2, success=2), 1, None, id="latest-is-a-success"
        ),
        pytest.param(_stored((1, 28), "complete", "timeout", attempt=2), 2, None, id="tie"),
        pytest.param(
            _stored((1, 28), "complete", "timeout", attempt=2), 3, None, id="failure-before-attempt"
        ),
        pytest.param(_stored((1, 28), "complete", attempt=2), 1, None, id="no-reason-recorded"),
        pytest.param(
            _stored((1, 28), "incomplete", f"{DEFERRED}{FAILURE_MARKER}", attempt=2),
            1,
            None,
            id="empty-note",
        ),
    ],
)
def test_newer_failure_reason_reads_a_later_failed_attempt_only(
    stored: freshness._State, attempt: int, expected: str | None
) -> None:
    """Only a stored failure that started after the attempt yields its reason."""
    assert freshness._newer_failure_reason(stored, _at(attempt)) == expected


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(_stored((1, 28), "complete", attempt=2, success=2), id="newer-success"),
        pytest.param(
            _stored((1, 28), "incomplete", DEFERRED, attempt=2, success=2),
            id="newer-incomplete-success",
        ),
        pytest.param(_stored((1, 28), "complete", "HTTP 429", attempt=2), id="newer-failure"),
        pytest.param(_stored((1, 28), "incomplete", NOTED, attempt=2), id="newer-noted-failure"),
        pytest.param(
            _stored(None, "unknown", "HTTP 502", attempt=2, success=None), id="newer-first-failure"
        ),
    ],
)
def test_merge_ignores_a_failure_older_than_the_stored_attempt(stored: freshness._State) -> None:
    """A delayed failure changes nothing: a later attempt already recorded the health."""
    late = FreshnessOutcome.failure(_at(1), reason="HTTP 503 after 3 attempts")
    assert freshness._merge(stored, late) == stored


@pytest.mark.parametrize(
    ("stored", "outcome", "expected"),
    [
        pytest.param(
            _stored((1, 28), "complete", attempt=2, success=2),
            _success(1, (26, 30)),
            freshness._State(_at(2), _at(2), _day(1), _day(30), "complete", None),
            id="overlapping-ranges-unite",
        ),
        pytest.param(
            _stored((10, 20), "complete", attempt=2, success=2),
            _success(1, (1, 9)),
            freshness._State(_at(2), _at(2), _day(1), _day(20), "complete", None),
            id="adjacent-ranges-unite",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", DEFERRED, attempt=2, success=2),
            _success(1, (1, 30)),
            freshness._State(_at(2), _at(2), _day(1), _day(30), "incomplete", DEFERRED),
            id="complete-superset-never-upgrades",
        ),
        pytest.param(
            _stored((1, 28), "complete", attempt=2, success=2),
            _deferred(1, (26, 30)),
            freshness._State(_at(2), _at(2), _day(1), _day(30), "incomplete", DEFERRED),
            id="incomplete-read-downgrades",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", None, attempt=2, success=2),
            _success(1, (20, 30)),
            freshness._State(
                _at(2), _at(2), _day(1), _day(30), "incomplete", freshness._UNSPECIFIED_SHORTFALL
            ),
            id="incomplete-always-has-a-reason",
        ),
        pytest.param(
            _stored((12, 15), "complete", attempt=2, success=2),
            _success(1, (1, 5)),
            _stored((12, 15), "complete", attempt=2, success=2),
            id="disjoint-earlier-range-changes-nothing",
        ),
        pytest.param(
            _stored((1, 5), "incomplete", DEFERRED, attempt=2, success=2),
            _success(1, (12, 15)),
            _stored((1, 5), "incomplete", DEFERRED, attempt=2, success=2),
            id="disjoint-later-range-changes-nothing",
        ),
        pytest.param(
            _stored(None, "incomplete", INTERRUPTED, attempt=2, success=2),
            _success(1, None),
            freshness._State(_at(2), _at(2), None, None, "incomplete", INTERRUPTED),
            id="full-state-read-never-upgrades",
        ),
        pytest.param(
            _stored(None, "complete", attempt=2, success=2),
            _success(1, (1, 28)),
            freshness._State(_at(2), _at(2), _day(1), _day(28), "complete", None),
            id="no-stored-range-takes-the-range",
        ),
        pytest.param(
            _stored((1, 10), "unknown", attempt=2, success=2),
            _success(1, (12, 15)),
            freshness._State(_at(2), _at(2), _day(12), _day(15), "complete", None),
            id="unknown-row-takes-the-range",
        ),
    ],
)
def test_merge_of_a_success_older_than_the_stored_success_is_conservative(
    stored: freshness._State, outcome: FreshnessOutcome, expected: freshness._State
) -> None:
    """A delayed success moves no timestamp back and never makes the row look better."""
    merged = freshness._merge(stored, outcome)
    assert merged == expected
    assert merged.completeness != "unknown"


@pytest.mark.parametrize(
    ("stored", "outcome", "expected"),
    [
        pytest.param(
            _stored((1, 28), "complete", "timeout", attempt=2),
            _success(1, (26, 30)),
            freshness._State(_at(2), _at(1), _day(1), _day(30), "complete", "timeout"),
            id="complete-row",
        ),
        pytest.param(
            _stored((1, 28), "complete", "timeout", attempt=2),
            _success(1, (26, 30), complete=False, reason=REASON_FINEST_PARTITION),
            freshness._State(
                _at(2),
                _at(1),
                _day(1),
                _day(30),
                "incomplete",
                f"{REASON_FINEST_PARTITION}{FAILURE_MARKER}timeout",
            ),
            id="complete-row-incomplete-read",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", NOTED, attempt=2),
            _success(1, (26, 30)),
            freshness._State(_at(2), _at(1), _day(1), _day(30), "incomplete", NOTED),
            id="incomplete-row-partial-re-read",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", NOTED, attempt=2),
            _success(1, (1, 30)),
            freshness._State(_at(2), _at(1), _day(1), _day(30), "complete", "timeout"),
            id="incomplete-row-full-re-read",
        ),
        pytest.param(
            _stored(None, "unknown", "HTTP 502", attempt=2, success=None),
            _success(1, (1, 28)),
            freshness._State(_at(2), _at(1), _day(1), _day(28), "complete", "HTTP 502"),
            id="never-succeeded",
        ),
        pytest.param(
            _stored((1, 28), "complete", "timeout", attempt=2, success=1),
            _success(0, (26, 30)),
            freshness._State(_at(2), _at(1), _day(1), _day(30), "complete", "timeout"),
            id="obsolete-success-on-a-complete-row",
        ),
        pytest.param(
            _stored((1, 28), "incomplete", NOTED, attempt=2, success=1),
            _success(0, (1, 30)),
            freshness._State(_at(2), _at(1), _day(1), _day(30), "incomplete", NOTED),
            id="obsolete-success-on-an-incomplete-row",
        ),
    ],
)
def test_merge_of_a_success_older_than_a_stored_failure_keeps_its_reason(
    stored: freshness._State, outcome: FreshnessOutcome, expected: freshness._State
) -> None:
    """The newer failure's reason survives a success that started before it."""
    assert freshness._merge(stored, outcome) == expected


@pytest.mark.parametrize(
    ("first", "earlier", "later"),
    [
        pytest.param(
            None,
            _success(1, (1, 28)),
            FreshnessOutcome.failure(_at(2), reason="timeout"),
            id="first-success-then-failure",
        ),
        pytest.param(
            _success(0, (1, 28)),
            _success(1, (26, 30)),
            FreshnessOutcome.failure(_at(2), reason="timeout"),
            id="complete-success-then-failure",
        ),
        pytest.param(
            _success(0, (1, 28)),
            _success(1, (26, 30), complete=False, reason=REASON_FINEST_PARTITION),
            FreshnessOutcome.failure(_at(2), reason="timeout"),
            id="incomplete-success-then-failure",
        ),
        pytest.param(
            _deferred(0, (1, 28)),
            _success(1, (26, 30)),
            FreshnessOutcome.failure(_at(2), reason="timeout"),
            id="partial-re-read-then-failure",
        ),
        pytest.param(
            _deferred(0, (1, 28)),
            _success(1, (1, 30)),
            FreshnessOutcome.failure(_at(2), reason="timeout"),
            id="full-re-read-then-failure",
        ),
        pytest.param(
            _deferred(0, (1, 28)),
            FreshnessOutcome.failure(_at(1), reason="timeout"),
            _success(2, (26, 30)),
            id="failure-then-success",
        ),
        pytest.param(
            _deferred(0, (1, 28)),
            FreshnessOutcome.failure(_at(1), reason="timeout"),
            FreshnessOutcome.failure(_at(2), reason="HTTP 429"),
            id="failure-then-failure",
        ),
    ],
)
def test_merge_with_a_failure_is_independent_of_commit_order(
    first: FreshnessOutcome | None, earlier: FreshnessOutcome, later: FreshnessOutcome
) -> None:
    """A failure and another attempt leave the same row whichever commits first."""
    start = freshness._State.of(None)
    if first is not None:
        start = freshness._merge(start, first)
    in_start_order = freshness._merge(freshness._merge(start, earlier), later)
    in_reverse_order = freshness._merge(freshness._merge(start, later), earlier)
    assert in_reverse_order == in_start_order
    assert in_start_order.last_attempt_at == _at(2)


def test_merge_treats_an_attempt_at_the_stored_instant_as_current() -> None:
    """Ties are current: re-recording is idempotent and a tied success is not obsolete."""
    deferred = freshness._merge(freshness._State.of(None), _deferred(1, (1, 28)))
    assert freshness._merge(deferred, _deferred(1, (1, 28))) == deferred

    # A tied full re-read repairs the row, which an obsolete success never does.
    repaired = freshness._merge(deferred, _success(1, (1, 30)))
    assert repaired == freshness._State(_at(1), _at(1), _day(1), _day(30), "complete", None)

    timeout = FreshnessOutcome.failure(_at(2), reason="timeout")
    failed = freshness._merge(repaired, timeout)
    assert failed == freshness._State(_at(2), _at(1), _day(1), _day(30), "complete", "timeout")
    assert freshness._merge(failed, timeout) == failed

    # A success that started with the failure did not start before it, so it clears it.
    cleared = freshness._merge(failed, _success(2, (29, 30)))
    assert cleared == freshness._State(_at(2), _at(2), _day(1), _day(30), "complete", None)


# ---------------------------------------------------------------------------------------
# needs_repair
# ---------------------------------------------------------------------------------------


def test_needs_repair_is_false_without_a_row() -> None:
    """A dataset with no row yet never needs a repair."""
    assert needs_repair(None) is False


@pytest.mark.parametrize(
    ("completeness", "reason", "expected"),
    [
        ("incomplete", DEFERRED, True),
        ("incomplete", REASON_DEFERRED, True),
        ("incomplete", INTERRUPTED, True),
        ("incomplete", f"{DEFERRED}{FAILURE_MARKER}timeout", True),
        ("incomplete", REASON_FINEST_PARTITION, False),
        ("incomplete", REASON_SEARCH_CEILING, False),
        ("incomplete", "HTTP 503 after 3 attempts", False),
        ("incomplete", None, False),
        ("complete", None, False),
        ("complete", DEFERRED, False),
        ("unknown", None, False),
        ("unknown", INTERRUPTED, False),
    ],
)
def test_needs_repair_only_for_an_incomplete_row_with_a_repairable_reason(
    completeness: str, reason: str | None, expected: bool
) -> None:
    """Only ``incomplete`` with a deferred or interrupted reason triggers a repair."""
    assert needs_repair(_row(completeness, reason)) is expected


# ---------------------------------------------------------------------------------------
# Database: a private schema per module, an engine per test
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the dataset_freshness tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _connect_args(schema: str) -> dict[str, str]:
    """Pin every connection to ``schema`` alone, in UTC like the application's sessions."""
    return {"options": f"-c search_path={schema} -c TimeZone=UTC"}


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding only the freshness tables; drop it afterwards.

    Yields:
        The database URL and the private schema's name.
    """
    url = _database_url()
    schema = f"freshness_test_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(
                connection, tables=[SourceDataset.__table__, DatasetFreshness.__table__]
            )
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield url, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
async def sessions(database: tuple[URL, str]) -> AsyncIterator[Sessions]:
    """Yield a session factory on an engine created and disposed inside this test's loop."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _create_dataset(sessions: Sessions, source: str = "google") -> int:
    """Commit a new building primary ``source_dataset`` row and return its id."""
    async with sessions() as session, session.begin():
        dataset = SourceDataset(source=source, mode="simulated", state="building", role="primary")
        session.add(dataset)
        await session.flush()
        return dataset.id


async def _record(
    sessions: Sessions, dataset_id: int, dataset: str, outcome: FreshnessOutcome
) -> DatasetFreshness:
    """Record ``outcome`` in its own committed transaction and return the written row."""
    async with sessions() as session, session.begin():
        return await record_freshness(session, dataset_id, dataset, outcome)


async def _load(sessions: Sessions, dataset_id: int) -> dict[str, DatasetFreshness]:
    """Load every freshness row of ``dataset_id`` in a fresh session."""
    async with sessions() as session:
        return await load_freshness(session, dataset_id)


async def _count(sessions: Sessions, dataset_id: int) -> int:
    """Count the freshness rows of ``dataset_id``."""
    async with sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(DatasetFreshness)
            .where(DatasetFreshness.dataset_id == dataset_id)
        )
    return int(count or 0)


@pytest.mark.anyio
@pytest.mark.db
async def test_first_record_inserts_the_row(sessions: Sessions) -> None:
    """The first outcome inserts one row keyed by source dataset and dataset name."""
    dataset_id = await _create_dataset(sessions)
    written = await _record(sessions, dataset_id, METRICS, _success(0, (1, 28)))

    assert (written.source, written.dataset_id, written.dataset) == ("google", dataset_id, METRICS)
    assert _state(written) == _expected(0, 0, (1, 28), "complete")
    loaded = await _load(sessions, dataset_id)
    assert list(loaded) == [METRICS]
    assert loaded[METRICS].id == written.id
    assert _state(loaded[METRICS]) == _expected(0, 0, (1, 28), "complete")
    assert await _count(sessions, dataset_id) == 1


@pytest.mark.anyio
@pytest.mark.db
async def test_rerecording_the_same_outcome_is_idempotent(sessions: Sessions) -> None:
    """Recording one outcome twice leaves the same single row with the same values."""
    dataset_id = await _create_dataset(sessions)
    first = await _record(sessions, dataset_id, METRICS, _deferred(0, (1, 28)))
    second = await _record(sessions, dataset_id, METRICS, _deferred(0, (1, 28)))

    assert second.id == first.id
    assert _state(second) == _state(first) == _expected(0, 0, (1, 28), "incomplete", DEFERRED)
    assert await _count(sessions, dataset_id) == 1


@pytest.mark.anyio
@pytest.mark.db
async def test_trailing_refresh_extends_coverage_and_stays_complete(sessions: Sessions) -> None:
    """A complete trailing-days refresh that overlaps the stored range extends it."""
    dataset_id = await _create_dataset(sessions)
    await _record(sessions, dataset_id, METRICS, _success(0, (1, 28)))
    written = await _record(sessions, dataset_id, METRICS, _success(1, (26, 30)))

    assert _state(written) == _expected(1, 1, (1, 30), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_only_a_full_re_read_turns_an_incomplete_row_complete(sessions: Sessions) -> None:
    """A partial refresh keeps a deferred row incomplete; re-covering the range repairs it."""
    dataset_id = await _create_dataset(sessions)
    deferred = await _record(sessions, dataset_id, METRICS, _deferred(0, (1, 28)))
    assert needs_repair(deferred) is True

    trailing = await _record(sessions, dataset_id, METRICS, _success(1, (26, 30)))
    assert _state(trailing) == _expected(1, 1, (1, 30), "incomplete", DEFERRED)
    assert needs_repair(trailing) is True

    repaired = await _record(sessions, dataset_id, METRICS, _success(2, (1, 31)))
    assert _state(repaired) == _expected(2, 2, (1, 31), "complete")
    assert needs_repair(repaired) is False


@pytest.mark.anyio
@pytest.mark.db
async def test_completeness_downgrades_and_recovers(sessions: Sessions) -> None:
    """Complete turns incomplete on a short read, then complete again on a full one."""
    dataset_id = await _create_dataset(sessions)
    await _record(sessions, dataset_id, METRICS, _success(0, (1, 28)))

    interrupted = await _record(
        sessions,
        dataset_id,
        METRICS,
        _success(1, (26, 30), complete=False, reason=INTERRUPTED, repairable=True),
    )
    assert _state(interrupted) == _expected(1, 1, (1, 30), "incomplete", INTERRUPTED)
    assert needs_repair(interrupted) is True

    capped = await _record(
        sessions,
        dataset_id,
        METRICS,
        _success(2, (1, 30), complete=False, reason=REASON_FINEST_PARTITION),
    )
    assert _state(capped) == _expected(2, 2, (1, 30), "incomplete", REASON_FINEST_PARTITION)
    assert needs_repair(capped) is False

    recovered = await _record(sessions, dataset_id, METRICS, _success(3, (1, 30)))
    assert _state(recovered) == _expected(3, 3, (1, 30), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_a_repairable_shortfall_is_never_masked_by_a_permanent_one(
    sessions: Sessions,
) -> None:
    """Between two shortfalls, the repairable one is the reason kept on the row."""
    dataset_id = await _create_dataset(sessions)
    await _record(
        sessions,
        dataset_id,
        METRICS,
        _success(0, (1, 28), complete=False, reason=REASON_FINEST_PARTITION),
    )

    deferred = await _record(sessions, dataset_id, METRICS, _deferred(1, (26, 30)))
    assert _state(deferred) == _expected(1, 1, (1, 30), "incomplete", DEFERRED)
    assert needs_repair(deferred) is True

    capped = await _record(
        sessions,
        dataset_id,
        METRICS,
        _success(2, (29, 31), complete=False, reason=REASON_FINEST_PARTITION),
    )
    assert _state(capped) == _expected(2, 2, (1, 31), "incomplete", DEFERRED)
    assert needs_repair(capped) is True


@pytest.mark.anyio
@pytest.mark.db
async def test_disjoint_windows_replace_the_coverage(sessions: Sessions) -> None:
    """A range that neither overlaps nor touches the stored one replaces it."""
    dataset_id = await _create_dataset(sessions)
    await _record(sessions, dataset_id, METRICS, _success(0, (1, 10)))

    newer = await _record(sessions, dataset_id, METRICS, _success(1, (12, 15)))
    assert _state(newer) == _expected(1, 1, (12, 15), "complete")

    older = await _record(sessions, dataset_id, METRICS, _success(2, (1, 5)))
    assert _state(older) == _expected(2, 2, (1, 5), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_restated_older_window_touching_the_stored_range_extends_it(
    sessions: Sessions,
) -> None:
    """An older adjacent window extends the range but cannot vouch for the stored tail."""
    dataset_id = await _create_dataset(sessions)
    search_terms = "google.search_terms"
    await _record(sessions, dataset_id, METRICS, _success(0, (10, 28)))
    await _record(sessions, dataset_id, search_terms, _deferred(0, (10, 28)))

    metrics = await _record(sessions, dataset_id, METRICS, _success(1, (1, 9)))
    terms = await _record(sessions, dataset_id, search_terms, _success(1, (1, 9)))

    assert _state(metrics) == _expected(1, 1, (1, 28), "complete")
    assert _state(terms) == _expected(1, 1, (1, 28), "incomplete", DEFERRED)


@pytest.mark.anyio
@pytest.mark.db
async def test_failure_keeps_the_last_good_state(sessions: Sessions) -> None:
    """A failure moves only the attempt and reason; a later success clears the reason."""
    dataset_id = await _create_dataset(sessions)
    await _record(sessions, dataset_id, METRICS, _success(0, (1, 28)))

    failed = await _record(
        sessions,
        dataset_id,
        METRICS,
        FreshnessOutcome.failure(_at(1), reason="HTTP 503 after 3 attempts"),
    )
    assert _state(failed) == _expected(1, 0, (1, 28), "complete", "HTTP 503 after 3 attempts")
    assert needs_repair(failed) is False

    succeeded = await _record(sessions, dataset_id, METRICS, _success(2, (1, 28)))
    assert _state(succeeded) == _expected(2, 2, (1, 28), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_failure_on_an_incomplete_row_appends_its_note(sessions: Sessions) -> None:
    """The shortfall stays first, one failure note follows it, and a success drops the note."""
    dataset_id = await _create_dataset(sessions)
    await _record(sessions, dataset_id, METRICS, _deferred(0, (1, 28)))

    timeout = FreshnessOutcome.failure(_at(1), reason="timeout")
    failed = await _record(sessions, dataset_id, METRICS, timeout)
    noted = f"{DEFERRED}{FAILURE_MARKER}timeout"
    assert _state(failed) == _expected(1, 0, (1, 28), "incomplete", noted)
    assert needs_repair(failed) is True

    again = await _record(sessions, dataset_id, METRICS, timeout)
    assert _state(again) == _state(failed)

    throttled = await _record(
        sessions, dataset_id, METRICS, FreshnessOutcome.failure(_at(2), reason="HTTP 429")
    )
    assert throttled.reason == f"{DEFERRED}{FAILURE_MARKER}HTTP 429"

    trailing = await _record(sessions, dataset_id, METRICS, _success(3, (26, 30)))
    assert _state(trailing) == _expected(3, 3, (1, 30), "incomplete", DEFERRED)

    repaired = await _record(sessions, dataset_id, METRICS, _success(4, (1, 30)))
    assert _state(repaired) == _expected(4, 4, (1, 30), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_first_ever_failure_records_unknown(sessions: Sessions) -> None:
    """A failure before any success leaves the row ``unknown`` with no coverage."""
    dataset_id = await _create_dataset(sessions, "hubspot")
    contacts = "hubspot.contacts"

    failed = await _record(
        sessions, dataset_id, contacts, FreshnessOutcome.failure(_at(0), reason="HTTP 502")
    )
    assert (failed.source, failed.dataset) == ("hubspot", contacts)
    assert _state(failed) == _expected(0, None, None, "unknown", "HTTP 502")
    assert needs_repair(failed) is False

    succeeded = await _record(sessions, dataset_id, contacts, _success(1, (1, 28)))
    assert _state(succeeded) == _expected(1, 1, (1, 28), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_full_state_read_replaces_completeness_and_keeps_coverage(
    sessions: Sessions,
) -> None:
    """A success with no range sets completeness and leaves any stored coverage as it is."""
    dataset_id = await _create_dataset(sessions, "linkedin")
    entities, leads = "linkedin.entities", "linkedin.leads"

    first = await _record(sessions, dataset_id, entities, _success(0, None))
    assert _state(first) == _expected(0, 0, None, "complete")
    cut_short = await _record(
        sessions,
        dataset_id,
        entities,
        _success(1, None, complete=False, reason=INTERRUPTED, repairable=True),
    )
    assert _state(cut_short) == _expected(1, 1, None, "incomplete", INTERRUPTED)
    assert needs_repair(cut_short) is True

    await _record(sessions, dataset_id, leads, _deferred(0, (1, 28)))
    reread = await _record(sessions, dataset_id, leads, _success(1, None))
    assert _state(reread) == _expected(1, 1, (1, 28), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_each_call_touches_only_its_own_row(sessions: Sessions) -> None:
    """A success of one dataset never hides a failed refresh of another, in any dataset."""
    first_id = await _create_dataset(sessions)
    second_id = await _create_dataset(sessions)
    entities = "google.entities"

    await _record(sessions, first_id, entities, _success(0, None))
    await _record(sessions, first_id, METRICS, _success(0, (1, 28)))
    await _record(sessions, first_id, METRICS, FreshnessOutcome.failure(_at(1), reason="timeout"))
    await _record(sessions, first_id, entities, _success(2, None))
    await _record(sessions, second_id, entities, _success(3, None))

    first = await _load(sessions, first_id)
    assert _state(first[entities]) == _expected(2, 2, None, "complete")
    assert _state(first[METRICS]) == _expected(1, 0, (1, 28), "complete", "timeout")
    second = await _load(sessions, second_id)
    assert list(second) == [entities]
    assert _state(second[entities]) == _expected(3, 3, None, "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_load_freshness_returns_rows_keyed_by_dataset_in_name_order(
    sessions: Sessions,
) -> None:
    """Rows come back keyed by name in name order; names without a row are absent."""
    dataset_id = await _create_dataset(sessions, "hubspot")
    assert await _load(sessions, dataset_id) == {}

    names = SOURCE_DATASETS["hubspot"]
    for hour, name in enumerate(reversed(names[1:])):
        await _record(sessions, dataset_id, name, _success(hour, (1, 28)))

    loaded = await _load(sessions, dataset_id)
    assert list(loaded) == sorted(names[1:])
    assert names[0] not in loaded
    for name, row in loaded.items():
        assert (row.source, row.dataset_id, row.dataset) == ("hubspot", dataset_id, name)


@pytest.mark.anyio
@pytest.mark.db
async def test_load_freshness_refreshes_rows_already_in_the_session(sessions: Sessions) -> None:
    """Loading again in one session returns what another transaction committed since."""
    dataset_id = await _create_dataset(sessions)
    await _record(sessions, dataset_id, METRICS, _success(0, (1, 28)))

    async with sessions() as session:
        before = await load_freshness(session, dataset_id)
        assert before[METRICS].completeness == "complete"
        await _record(sessions, dataset_id, METRICS, _deferred(1, (1, 30)))
        after = await load_freshness(session, dataset_id)

    assert after[METRICS] is before[METRICS]
    assert _state(after[METRICS]) == _expected(1, 1, (1, 30), "incomplete", DEFERRED)


@pytest.mark.anyio
@pytest.mark.db
async def test_record_freshness_rejects_unknown_missing_and_foreign_datasets(
    sessions: Sessions,
) -> None:
    """An unknown name, a missing source dataset or another source's dataset is refused."""
    linkedin_id = await _create_dataset(sessions, "linkedin")
    outcome = _success(0, (1, 28))

    async with sessions() as session:
        with pytest.raises(ValueError, match="unknown freshness dataset"):
            await record_freshness(session, linkedin_id, "linkedin.campaigns", outcome)
        missing_id = int(await session.scalar(select(func.max(SourceDataset.id))) or 0) + 1000
        with pytest.raises(ValueError, match=f"source dataset {missing_id} does not exist"):
            await record_freshness(session, missing_id, METRICS, outcome)
        with pytest.raises(ValueError, match="belongs to google"):
            await record_freshness(session, linkedin_id, METRICS, outcome)

    assert await _load(sessions, linkedin_id) == {}


@pytest.mark.anyio
@pytest.mark.db
async def test_record_freshness_refuses_a_retired_dataset(sessions: Sessions) -> None:
    """A retired dataset is read-only: its freshness row is never written again."""
    dataset_id = await _create_dataset(sessions)
    await _record(sessions, dataset_id, METRICS, _success(0, (1, 28)))
    async with sessions() as session, session.begin():
        await session.execute(
            update(SourceDataset)
            .where(SourceDataset.id == dataset_id)
            .values(state="retired", retired_at=_at(1))
        )

    with pytest.raises(DatasetNotWritable) as refused:
        await _record(sessions, dataset_id, METRICS, _success(2, (1, 30)))

    assert (refused.value.dataset_id, refused.value.state) == (dataset_id, "retired")
    loaded = await _load(sessions, dataset_id)
    assert _state(loaded[METRICS]) == _expected(0, 0, (1, 28), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_mark_unknown_refuses_a_retired_dataset(sessions: Sessions) -> None:
    """A retired dataset is read-only: its freshness row is never marked unknown either."""
    dataset_id = await _create_dataset(sessions)
    await _record(sessions, dataset_id, METRICS, _success(0, (1, 28)))
    async with sessions() as session, session.begin():
        await session.execute(
            update(SourceDataset)
            .where(SourceDataset.id == dataset_id)
            .values(state="retired", retired_at=_at(1))
        )

    with pytest.raises(DatasetNotWritable) as refused:
        async with sessions() as session, session.begin():
            await mark_unknown(
                session, dataset_id, METRICS, reason="days restated after the recorded success"
            )

    assert (refused.value.dataset_id, refused.value.state) == (dataset_id, "retired")
    loaded = await _load(sessions, dataset_id)
    assert _state(loaded[METRICS]) == _expected(0, 0, (1, 28), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_record_freshness_never_commits(sessions: Sessions) -> None:
    """The caller's transaction owns the row: rolling it back leaves no row behind."""
    dataset_id = await _create_dataset(sessions)

    async with sessions() as session:
        written = await record_freshness(session, dataset_id, METRICS, _success(0, (1, 28)))
        assert _state(written) == _expected(0, 0, (1, 28), "complete")
        await session.rollback()

    assert await _load(sessions, dataset_id) == {}


@pytest.mark.anyio
@pytest.mark.db
async def test_concurrent_writers_of_one_row_serialize(sessions: Sessions) -> None:
    """A second writer waits for the first and merges into its committed state."""
    dataset_id = await _create_dataset(sessions)
    await _record(sessions, dataset_id, METRICS, _success(0, (1, 28)))
    second_done = anyio.Event()

    async def second_writer() -> None:
        """Record a complete trailing refresh while the first writer holds the row."""
        await _record(sessions, dataset_id, METRICS, _success(2, (29, 31)))
        second_done.set()

    with anyio.fail_after(60):
        async with sessions() as first:
            await record_freshness(first, dataset_id, METRICS, _deferred(1, (26, 30)))
            async with anyio.create_task_group() as group:
                group.start_soon(second_writer)
                await anyio.sleep(0.5)
                second_waited = not second_done.is_set()
                await first.commit()

    assert second_waited
    loaded = await _load(sessions, dataset_id)
    assert _state(loaded[METRICS]) == _expected(2, 2, (1, 31), "incomplete", DEFERRED)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_delayed_older_success_never_regresses_a_newer_one(sessions: Sessions) -> None:
    """Committed after a newer success, an older one moves no timestamp back or upgrades."""
    dataset_id = await _create_dataset(sessions)
    search_terms = "google.search_terms"
    await _record(sessions, dataset_id, METRICS, _success(2, (12, 15)))
    await _record(sessions, dataset_id, search_terms, _deferred(2, (1, 28)))

    metrics = await _record(sessions, dataset_id, METRICS, _success(1, (1, 5)))
    terms = await _record(sessions, dataset_id, search_terms, _success(1, (1, 30)))

    assert _state(metrics) == _expected(2, 2, (12, 15), "complete")
    assert _state(terms) == _expected(2, 2, (1, 30), "incomplete", DEFERRED)
    assert needs_repair(terms) is True


@pytest.mark.anyio
@pytest.mark.db
async def test_a_delayed_older_failure_leaves_a_newer_success_standing(
    sessions: Sessions,
) -> None:
    """Committed after a newer success, an older failure changes nothing on the row."""
    dataset_id = await _create_dataset(sessions)
    search_terms = "google.search_terms"
    await _record(sessions, dataset_id, METRICS, _success(2, (1, 28)))
    await _record(sessions, dataset_id, search_terms, _deferred(2, (1, 28)))

    timeout = FreshnessOutcome.failure(_at(1), reason="timeout")
    metrics = await _record(sessions, dataset_id, METRICS, timeout)
    terms = await _record(sessions, dataset_id, search_terms, timeout)

    assert _state(metrics) == _expected(2, 2, (1, 28), "complete")
    assert _state(terms) == _expected(2, 2, (1, 28), "incomplete", DEFERRED)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_delayed_older_success_keeps_a_newer_failure(sessions: Sessions) -> None:
    """Committed after a newer failure, an older success lands as in start order."""
    commit_order = await _create_dataset(sessions)
    start_order = await _create_dataset(sessions)
    search_terms = "google.search_terms"
    failure = FreshnessOutcome.failure(_at(2), reason="HTTP 503 after 3 attempts")
    trailing = _success(1, (26, 30))
    for dataset, first in ((METRICS, _success(0, (1, 28))), (search_terms, _deferred(0, (1, 28)))):
        for dataset_id, outcomes in (
            (commit_order, (first, failure, trailing)),
            (start_order, (first, trailing, failure)),
        ):
            for outcome in outcomes:
                await _record(sessions, dataset_id, dataset, outcome)

    reversed_rows = await _load(sessions, commit_order)
    ordered_rows = await _load(sessions, start_order)
    assert _state(reversed_rows[METRICS]) == _expected(
        2, 1, (1, 30), "complete", "HTTP 503 after 3 attempts"
    )
    noted = f"{DEFERRED}{FAILURE_MARKER}HTTP 503 after 3 attempts"
    assert _state(reversed_rows[search_terms]) == _expected(2, 1, (1, 30), "incomplete", noted)
    assert needs_repair(reversed_rows[search_terms]) is True
    for dataset in (METRICS, search_terms):
        assert _state(reversed_rows[dataset]) == _state(ordered_rows[dataset])

    later = await _record(sessions, commit_order, METRICS, _success(3, (29, 31)))
    assert _state(later) == _expected(3, 3, (1, 31), "complete")


# ---------------------------------------------------------------------------------------
# mark_unknown: withdrawing what a row vouches for
# ---------------------------------------------------------------------------------------

CONTACTS = "hubspot.contacts"
RESTAGED = "3 lead matches staged after the CRM backfill"


async def _mark(sessions: Sessions, dataset_id: int, dataset: str) -> DatasetFreshness | None:
    """Mark one row unknown in its own committed transaction and return it."""
    async with sessions() as session, session.begin():
        return await mark_unknown(session, dataset_id, dataset, reason=RESTAGED)


@pytest.mark.anyio
@pytest.mark.db
async def test_mark_unknown_keeps_the_history_and_coverage(sessions: Sessions) -> None:
    """Only completeness and reason change; timestamps and coverage stand, and it persists."""
    dataset_id = await _create_dataset(sessions, "hubspot")
    await _record(sessions, dataset_id, CONTACTS, _success(0, (1, 28)))
    await _record(sessions, dataset_id, CONTACTS, FreshnessOutcome.failure(_at(1), reason="x"))
    marked = await _mark(sessions, dataset_id, CONTACTS)

    assert marked is not None
    assert _state(marked) == (_at(1), _at(0), _day(1), _day(28), "unknown", RESTAGED)
    loaded = await _load(sessions, dataset_id)
    assert _state(loaded[CONTACTS]) == _state(marked)
    assert needs_repair(loaded[CONTACTS]) is False


@pytest.mark.anyio
@pytest.mark.db
async def test_a_later_success_replaces_the_mark_and_a_failure_keeps_it(
    sessions: Sessions,
) -> None:
    """A failure after the mark leaves the row unknown; the next success verifies it again."""
    dataset_id = await _create_dataset(sessions, "hubspot")
    await _record(sessions, dataset_id, CONTACTS, _success(0, (1, 28)))
    await _mark(sessions, dataset_id, CONTACTS)

    failed = await _record(
        sessions, dataset_id, CONTACTS, FreshnessOutcome.failure(_at(1), reason="HTTP 503")
    )
    assert _state(failed) == _expected(1, 0, (1, 28), "unknown", "HTTP 503")

    rerun = await _record(sessions, dataset_id, CONTACTS, _success(2, (1, 30)))
    assert _state(rerun) == _expected(2, 2, (1, 30), "complete")

    await _mark(sessions, dataset_id, CONTACTS)
    # A success at the stored instant (a fixed clock) is current, so it replaces it too.
    same_instant = await _record(
        sessions,
        dataset_id,
        CONTACTS,
        _success(2, (1, 30), complete=False, reason=INTERRUPTED, repairable=True),
    )
    assert _state(same_instant) == _expected(2, 2, (1, 30), "incomplete", INTERRUPTED)


@pytest.mark.anyio
@pytest.mark.db
async def test_mark_unknown_writes_nothing_for_an_absent_row(sessions: Sessions) -> None:
    """An absent row already reads as unknown: nothing is inserted."""
    dataset_id = await _create_dataset(sessions, "hubspot")
    assert await _mark(sessions, dataset_id, CONTACTS) is None
    assert await _count(sessions, dataset_id) == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_mark_unknown_rejects_a_blank_reason_and_foreign_datasets(
    sessions: Sessions,
) -> None:
    """A blank reason, an unknown name, a missing or another source's dataset is refused."""
    hubspot_id = await _create_dataset(sessions, "hubspot")
    await _record(sessions, hubspot_id, CONTACTS, _success(0, (1, 28)))

    async with sessions() as session:
        with pytest.raises(ValueError, match="a reason is required"):
            await mark_unknown(session, hubspot_id, CONTACTS, reason="  ")
        with pytest.raises(ValueError, match="unknown freshness dataset"):
            await mark_unknown(session, hubspot_id, "hubspot.meetings", reason=RESTAGED)
        missing_id = int(await session.scalar(select(func.max(SourceDataset.id))) or 0) + 1000
        with pytest.raises(ValueError, match=f"source dataset {missing_id} does not exist"):
            await mark_unknown(session, missing_id, CONTACTS, reason=RESTAGED)
        with pytest.raises(ValueError, match="belongs to google"):
            await mark_unknown(session, hubspot_id, METRICS, reason=RESTAGED)

    loaded = await _load(sessions, hubspot_id)
    assert _state(loaded[CONTACTS]) == _expected(0, 0, (1, 28), "complete")


@pytest.mark.anyio
@pytest.mark.db
async def test_mark_unknown_never_commits(sessions: Sessions) -> None:
    """The caller's transaction owns the change: rolling it back keeps the row verified."""
    dataset_id = await _create_dataset(sessions, "hubspot")
    await _record(sessions, dataset_id, CONTACTS, _success(0, (1, 28)))

    async with sessions() as session:
        marked = await mark_unknown(session, dataset_id, CONTACTS, reason=RESTAGED)
        assert marked is not None
        assert marked.completeness == "unknown"
        await session.rollback()

    loaded = await _load(sessions, dataset_id)
    assert _state(loaded[CONTACTS]) == _expected(0, 0, (1, 28), "complete")


SEARCH_TERMS = "google.search_terms"
CHECKPOINT = {
    "version": 1,
    "pending": {"Q-G6": [{"start": "2026-09-01", "end": "2026-09-14"}]},
    "covered_from": "2026-09-01",
    "reasons": [],
}


async def _stored_checkpoint(
    sessions: Sessions, dataset_id: int, dataset: str
) -> tuple[bool, str | None]:
    """Read in SQL whether the stored checkpoint ``IS NULL``, and its ``jsonb_typeof``."""
    async with sessions() as session:
        is_null, json_type = (
            await session.execute(
                select(
                    DatasetFreshness.checkpoint.is_(None),
                    func.jsonb_typeof(DatasetFreshness.checkpoint),
                ).where(
                    DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == dataset
                )
            )
        ).one()
    return bool(is_null), json_type


@pytest.mark.anyio
@pytest.mark.db
async def test_a_checkpoint_is_stored_replaced_and_cleared_in_the_callers_transaction(
    sessions: Sessions,
) -> None:
    """``write_checkpoint`` returns what it replaces, commits nothing, and touches only its column.

    The first write seeds the missing row ``unknown`` (as ``record_freshness`` would), the
    second returns the first value under the row lock, a rolled-back write leaves the stored
    one, and clearing returns it once more. The coverage, completeness and reason that
    ``record_freshness`` wrote stand throughout, and ``record_freshness`` keeps the checkpoint.
    """
    dataset_id = await _create_dataset(sessions)
    async with sessions() as session, session.begin():
        assert await lock_checkpoint(session, dataset_id, SEARCH_TERMS) is None
        assert await write_checkpoint(session, dataset_id, SEARCH_TERMS, CHECKPOINT) is None
    seeded = (await _load(sessions, dataset_id))[SEARCH_TERMS]
    assert (seeded.completeness, seeded.last_attempt_at, seeded.checkpoint) == (
        "unknown",
        None,
        CHECKPOINT,
    )
    assert await _stored_checkpoint(sessions, dataset_id, SEARCH_TERMS) == (False, "object")

    await _record(sessions, dataset_id, SEARCH_TERMS, _deferred(0, (1, 14)))
    recorded = (await _load(sessions, dataset_id))[SEARCH_TERMS]
    assert recorded.checkpoint == CHECKPOINT, "record_freshness never touches the checkpoint"
    assert _state(recorded) == _expected(0, 0, (1, 14), "incomplete", DEFERRED)

    later = {**CHECKPOINT, "pending": {"Q-G6": [{"start": "2026-09-10", "end": "2026-09-14"}]}}
    async with sessions() as session, session.begin():
        current = await lock_checkpoint(session, dataset_id, SEARCH_TERMS)
        assert current == CHECKPOINT
        assert current is not None
        current["reasons"].append("edited copy")
        assert await write_checkpoint(session, dataset_id, SEARCH_TERMS, later) == CHECKPOINT

    async with sessions() as session:
        assert await write_checkpoint(session, dataset_id, SEARCH_TERMS, None) == later
        await session.rollback()
    assert (await _load(sessions, dataset_id))[SEARCH_TERMS].checkpoint == later
    assert await _stored_checkpoint(sessions, dataset_id, SEARCH_TERMS) == (False, "object")

    async with sessions() as session, session.begin():
        assert await write_checkpoint(session, dataset_id, SEARCH_TERMS, None) == later
    cleared = (await _load(sessions, dataset_id))[SEARCH_TERMS]
    assert cleared.checkpoint is None
    assert await _stored_checkpoint(sessions, dataset_id, SEARCH_TERMS) == (True, None), (
        "a cleared checkpoint is SQL NULL, not the JSON value null"
    )
    assert _state(cleared) == _expected(0, 0, (1, 14), "incomplete", DEFERRED)


@pytest.mark.anyio
@pytest.mark.db
async def test_clearing_a_checkpoint_stores_sql_null_and_reads_back_none_in_the_session(
    sessions: Sessions,
) -> None:
    """Clearing writes SQL ``NULL``, even over a JSON ``null``, and the row reads ``None`` at once.

    The cleared attribute is loaded again in the writer's session, so reading it needs no
    lazy load; a row seeded by a clear, or one holding the JSON value ``null`` (as a clear
    stored before), ends ``checkpoint IS NULL``, and a later write stores a JSON object again.
    """
    dataset_id = await _create_dataset(sessions)
    async with sessions() as session, session.begin():
        assert await write_checkpoint(session, dataset_id, METRICS, None) is None
    assert await _stored_checkpoint(sessions, dataset_id, METRICS) == (True, None)

    async with sessions() as session, session.begin():
        await session.execute(
            update(DatasetFreshness)
            .where(DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == METRICS)
            .values(checkpoint=JSON.NULL)
        )
    assert await _stored_checkpoint(sessions, dataset_id, METRICS) == (False, "null")

    async with sessions() as session, session.begin():
        assert await write_checkpoint(session, dataset_id, METRICS, CHECKPOINT) is None
        assert await write_checkpoint(session, dataset_id, METRICS, None) == CHECKPOINT
        row = await session.scalar(
            select(DatasetFreshness).where(
                DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == METRICS
            )
        )
        assert row is not None
        assert "checkpoint" not in inspect(row).unloaded, "no lazy load is left pending"
        assert row.checkpoint is None
        assert not session.dirty
    assert await _stored_checkpoint(sessions, dataset_id, METRICS) == (True, None)

    async with sessions() as session, session.begin():
        await session.execute(
            update(DatasetFreshness)
            .where(DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == METRICS)
            .values(checkpoint=JSON.NULL)
        )
    async with sessions() as session, session.begin():
        assert await write_checkpoint(session, dataset_id, METRICS, None) is None
    assert await _stored_checkpoint(sessions, dataset_id, METRICS) == (True, None), (
        "a JSON null left by an earlier clear is turned into SQL NULL"
    )

    async with sessions() as session, session.begin():
        assert await write_checkpoint(session, dataset_id, METRICS, CHECKPOINT) is None
    assert await _stored_checkpoint(sessions, dataset_id, METRICS) == (False, "object")
    assert (await _load(sessions, dataset_id))[METRICS].checkpoint == CHECKPOINT


@pytest.mark.anyio
@pytest.mark.db
async def test_write_checkpoint_refuses_what_json_cannot_store_and_a_retired_dataset(
    sessions: Sessions,
) -> None:
    """A value JSON cannot hold, a foreign dataset name or a retired dataset writes nothing."""
    dataset_id = await _create_dataset(sessions)
    async with sessions() as session:
        with pytest.raises(ValueError, match="JSON values only"):
            await write_checkpoint(session, dataset_id, METRICS, {"covered_from": date(2026, 9, 1)})
        with pytest.raises(ValueError, match="JSON values only"):
            await write_checkpoint(session, dataset_id, METRICS, {"ratio": float("nan")})
        with pytest.raises(ValueError, match="string keys"):
            await write_checkpoint(session, dataset_id, METRICS, {1: "x"})  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="belongs to hubspot"):
            await write_checkpoint(session, dataset_id, "hubspot.google_contacts", CHECKPOINT)
    assert await _count(sessions, dataset_id) == 0, "nothing was seeded for a refused write"

    async with sessions() as session, session.begin():
        await session.execute(
            update(SourceDataset)
            .where(SourceDataset.id == dataset_id)
            .values(state="retired", retired_at=_at(1))
        )
    async with sessions() as session:
        with pytest.raises(DatasetNotWritable):
            await write_checkpoint(session, dataset_id, METRICS, CHECKPOINT)
        with pytest.raises(DatasetNotWritable):
            await lock_checkpoint(session, dataset_id, METRICS)


@pytest.mark.anyio
@pytest.mark.db
async def test_recorded_shortfall_is_the_incomplete_reason_without_a_failure_note(
    sessions: Sessions,
) -> None:
    """The stored shortfall a checkpointed run carries forward, with no failure note appended."""
    dataset_id = await _create_dataset(sessions)
    assert recorded_shortfall(None) is None
    complete = await _record(sessions, dataset_id, METRICS, _success(0, (1, 28)))
    assert recorded_shortfall(complete) is None

    capped = await _record(
        sessions,
        dataset_id,
        METRICS,
        _success(1, (1, 28), complete=False, reason=REASON_FINEST_PARTITION),
    )
    assert recorded_shortfall(capped) == REASON_FINEST_PARTITION
    failed = await _record(
        sessions, dataset_id, METRICS, FreshnessOutcome.failure(_at(2), reason="timeout")
    )
    assert failed.reason == f"{REASON_FINEST_PARTITION}{FAILURE_MARKER}timeout"
    assert recorded_shortfall(failed) == REASON_FINEST_PARTITION
