"""A sync job cancelled mid-run closes its own ``sync_run`` rows (``ads_engine.sync.jobs``).

The scheduler cancels and awaits every lane task when its lock is lost
(``sync/scheduler.py``, ``scheduler.lanes_cancelled``), and the worker does the same on
SIGTERM. ``/sources`` lists the newest ``sync_run`` rows (AAP 0.5.5), so a cancelled job must
never leave its rows ``running``. On ``asyncio.CancelledError``, ``_JobBase.run``:

* **Closes its rows**: logs ``sync.job.cancelled`` and closes every still-open row of the job
  as ``failed`` with ``job cancelled`` and ``finished_at`` set, writing nothing else (no
  freshness row, no ``analysis_request``).
* **Rolled-back closes**: closes a row whose close the cancellation rolled back too, and
  leaves an outcome that was committed before the cancellation as it was.
* **Bounded bookkeeping**: a close that hangs is abandoned after ``CANCEL_CLOSE_TIMEOUT`` and
  one that fails at once is abandoned too; both are logged as ``sync.job.close_failed``.
* **One finish**: after the close, logs the run's one ``sync.job.finish``, as every run does,
  with status ``cancelled`` and error ``CancelledError``.
* **Cancellation wins**: the cancellation is always re-raised, and a second cancellation while
  the rows are being closed propagates at once.

How. Real PostgreSQL 18 through ``conftest.db``. Each test runs a production Google job class
on a ``building`` simulated dataset (whose jobs never queue an ``analysis_request``), with
``_execute`` (and, where a test says so, ``_close_remaining``) overridden by a subclass that
blocks at a chosen point. The job runs as a task and is cancelled the way a lane is. The
module's logger is replaced by a recorder.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Final

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.db.models.analysis import AnalysisRequest
from ads_engine.db.models.ops import DatasetFreshness, SyncRun
from ads_engine.settings import get_settings
from ads_engine.sync import jobs
from ads_engine.sync.freshness import FreshnessOutcome
from ads_engine.sync.jobs import (
    CANCEL_CLOSE_TIMEOUT,
    EntitySyncJob,
    JobContext,
    JobResult,
    MetricsSyncJob,
)
from ads_engine.sync.mode import SourceModeController

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type LogEvent = tuple[str, str, dict[str, object]]

T0: Final = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
"""The fixed instant of every run; it also stamps ``finished_at``."""

_WAIT_S: Final = 10.0
"""The longest any single wait of a test may take before it fails."""

_CANCELLED: Final = "job cancelled"
_ENTITIES: Final = "google.entities"
_METRICS: Final = "google.metrics_daily"
_SEARCH_TERMS: Final = "google.search_terms"


# ---------------------------------------------------------------------------------------------
# Recorders and blocked jobs
# ---------------------------------------------------------------------------------------------


class LogRecorder:
    """Stand-in for ``jobs._log``: records every ``info`` and ``warning`` call."""

    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def info(self, event_name: str, **fields: object) -> None:
        """Record an ``info`` event."""
        self.events.append(("info", event_name, fields))

    def warning(self, event_name: str, **fields: object) -> None:
        """Record a ``warning`` event."""
        self.events.append(("warning", event_name, fields))

    def named(self, event_name: str) -> list[LogEvent]:
        """Return every recorded ``event_name`` event, in order."""
        return [event for event in self.events if event[1] == event_name]


class Gate:
    """Where a blocked job stands: ``entered`` once it blocks, ``closing`` once it closes."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closing = asyncio.Event()

    async def block(self) -> None:
        """Set ``entered``, then wait until the task is cancelled."""
        self.entered.set()
        await asyncio.Event().wait()


class BlockedEntitySync(EntitySyncJob):
    """Google's entity sync, blocked in ``_execute`` once ``run`` has opened its row."""

    def __init__(self, ctx: JobContext, gate: Gate) -> None:
        super().__init__(ctx)
        self.gate = gate

    async def _execute(self) -> None:
        """Block until cancelled."""
        await self.gate.block()


class StuckCloseEntitySync(BlockedEntitySync):
    """The blocked entity sync whose row bookkeeping hangs, or fails with ``failure``."""

    def __init__(self, ctx: JobContext, gate: Gate, *, failure: Exception | None = None) -> None:
        super().__init__(ctx, gate)
        self.failure = failure

    async def _close_remaining(self, error: str | None, *, quiet: bool = False) -> None:
        """Set ``closing``, then raise ``failure`` or wait until cancelled."""
        del error, quiet
        self.gate.closing.set()
        if self.failure is not None:
            raise self.failure
        await asyncio.Event().wait()


class PartlyRecordedMetricsSync(MetricsSyncJob):
    """Google's incremental metrics, cancelled between its two freshness records.

    ``google.metrics_daily`` is recorded and committed; ``google.search_terms`` is recorded
    (which closes its row in that transaction) and the job blocks before the commit, so the
    cancellation rolls that close back.
    """

    def __init__(self, ctx: JobContext, gate: Gate) -> None:
        super().__init__(ctx, "incremental")
        self.gate = gate

    async def _execute(self) -> None:
        """Commit one outcome, then block inside the transaction recording the other."""
        ctx = self.ctx
        async with ctx.sessionmaker() as session, session.begin():
            await self._complete(session, {_METRICS: _read(ctx.now)})
        async with ctx.sessionmaker() as session, session.begin():
            await self._complete(session, {_SEARCH_TERMS: _read(ctx.now)})
            await self.gate.block()


def _read(now: datetime) -> FreshnessOutcome:
    """A complete read of the three native days before ``now``."""
    return FreshnessOutcome.success(
        now, covered_from=date(2026, 10, 5), covered_through=date(2026, 10, 7), complete=True
    )


# ---------------------------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    """Replace the jobs module's logger with a recorder for the test."""
    recorder = LogRecorder()
    monkeypatch.setattr(jobs, "_log", recorder)
    return recorder


@pytest.fixture
async def context(db: Sessions) -> AsyncIterator[JobContext]:
    """The job context of a ``building`` simulated Google dataset, its clients open."""
    controller = SourceModeController(get_settings(), sessionmaker=db, clock=lambda: T0)
    dataset_id = await controller.datasets.begin(
        "google", "simulated", reason="credentials not configured"
    )
    async with controller.open_clients("google", "simulated") as clients:
        yield await JobContext.for_dataset(controller, dataset_id, clients)


async def cancel_at(task: asyncio.Task[JobResult], reached: asyncio.Event) -> float:
    """Cancel ``task`` once ``reached`` is set; return the seconds until it ended cancelled."""
    await asyncio.wait_for(reached.wait(), _WAIT_S)
    loop = asyncio.get_running_loop()
    began = loop.time()
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=_WAIT_S)
    assert task in done, "the cancelled job never ended"
    assert task.cancelled(), "the job swallowed its cancellation"
    return loop.time() - began


async def sync_runs(db: Sessions) -> dict[str | None, SyncRun]:
    """Every ``sync_run`` row, by freshness dataset name."""
    async with db() as session:
        return {row.dataset: row for row in await session.scalars(select(SyncRun))}


async def count(db: Sessions, model: type[AnalysisRequest] | type[DatasetFreshness]) -> int:
    """The number of rows of ``model``."""
    async with db() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def assert_cancelled_row(row: SyncRun) -> None:
    """Assert ``row`` was closed by the cancellation."""
    assert row.status == "failed"
    assert row.finished_at == T0
    assert row.error == _CANCELLED
    assert row.completeness is None


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


async def test_cancelled_job_closes_its_rows_as_failed(
    db: Sessions, context: JobContext, log: LogRecorder
) -> None:
    gate = Gate()
    task = asyncio.create_task(BlockedEntitySync(context, gate).run())
    await asyncio.wait_for(gate.entered.wait(), _WAIT_S)
    opened = (await sync_runs(db))[_ENTITIES]
    assert (opened.job, opened.status, opened.finished_at) == ("entities", "running", None)

    await cancel_at(task, gate.entered)

    runs = await sync_runs(db)
    assert set(runs) == {_ENTITIES}
    assert_cancelled_row(runs[_ENTITIES])
    # Only the job's own row was written.
    assert await count(db, DatasetFreshness) == 0
    assert await count(db, AnalysisRequest) == 0
    [(level, _, fields)] = log.named("sync.job.cancelled")
    assert level == "warning"
    assert fields["source"] == "google"
    assert fields["job"] == "entities"
    assert fields["dataset_id"] == context.dataset_id
    # One start, the cancellation, then the run's one finish; no close failed.
    assert [name for _, name, _ in log.events] == [
        "sync.job.start",
        "sync.job.cancelled",
        "sync.job.finish",
    ]
    [(level, _, finish)] = log.named("sync.job.finish")
    assert level == "warning"
    assert {key: value for key, value in finish.items() if key != "duration_ms"} == {
        "source": "google",
        "job": "entities",
        "dataset_id": context.dataset_id,
        "status": "cancelled",
        "rows": 0,
        "calls": 0,
        "completeness": "unknown",
        "datasets": {},
        "disposition": None,
        "error": "CancelledError",
    }
    duration_ms = finish["duration_ms"]
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0


async def test_cancel_closes_a_rolled_back_close_and_keeps_a_committed_one(
    db: Sessions, context: JobContext, log: LogRecorder
) -> None:
    gate = Gate()
    task = asyncio.create_task(PartlyRecordedMetricsSync(context, gate).run())

    await cancel_at(task, gate.entered)

    runs = await sync_runs(db)
    assert set(runs) == {_METRICS, _SEARCH_TERMS}
    committed = runs[_METRICS]
    assert (committed.status, committed.completeness, committed.error) == (
        "succeeded",
        "complete",
        None,
    )
    assert_cancelled_row(runs[_SEARCH_TERMS])
    async with db() as session:
        recorded = set(await session.scalars(select(DatasetFreshness.dataset)))
    assert recorded == {_METRICS}, "the rolled-back freshness record stays unwritten"
    assert len(log.named("sync.job.cancelled")) == 1
    assert log.named("sync.job.close_failed") == []


async def test_hung_close_is_abandoned_after_the_bound(
    db: Sessions, context: JobContext, log: LogRecorder
) -> None:
    gate = Gate()
    task = asyncio.create_task(StuckCloseEntitySync(context, gate).run())

    elapsed = await cancel_at(task, gate.entered)

    bound = CANCEL_CLOSE_TIMEOUT.total_seconds()
    assert bound - 0.05 <= elapsed < bound + 1.0
    assert gate.closing.is_set()
    [(level, _, fields)] = log.named("sync.job.close_failed")
    assert level == "warning"
    assert fields["error"] == "TimeoutError"
    assert fields["job"] == "entities"
    # Abandoned bookkeeping leaves the row as it was.
    assert (await sync_runs(db))[_ENTITIES].status == "running"


async def test_failing_close_is_logged_and_the_cancellation_propagates(
    db: Sessions, context: JobContext, log: LogRecorder
) -> None:
    gate = Gate()
    failure = OperationalError("UPDATE sync_run", None, Exception("connection lost"))
    task = asyncio.create_task(StuckCloseEntitySync(context, gate, failure=failure).run())

    elapsed = await cancel_at(task, gate.entered)

    assert elapsed < CANCEL_CLOSE_TIMEOUT.total_seconds()
    [(_, _, fields)] = log.named("sync.job.close_failed")
    assert fields["error"] == "OperationalError"
    assert (await sync_runs(db))[_ENTITIES].status == "running"


async def test_second_cancellation_during_the_close_propagates_at_once(
    db: Sessions, context: JobContext, log: LogRecorder
) -> None:
    gate = Gate()
    task = asyncio.create_task(StuckCloseEntitySync(context, gate).run())
    await asyncio.wait_for(gate.entered.wait(), _WAIT_S)
    task.cancel()

    elapsed = await cancel_at(task, gate.closing)

    assert elapsed < CANCEL_CLOSE_TIMEOUT.total_seconds() / 2
    assert log.named("sync.job.close_failed") == []
    assert len(log.named("sync.job.cancelled")) == 1
    assert (await sync_runs(db))[_ENTITIES].status == "running"
