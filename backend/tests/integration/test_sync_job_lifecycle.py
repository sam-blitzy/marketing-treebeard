"""The lifecycle every sync job shares: analysis requests, rollback-safe completion, terminal logs.

Proves the ``_JobBase`` plumbing of ``sync/jobs.py`` on the PostgreSQL test database (``db`` of
``tests/conftest.py``), with simulated clients answering from the catalogue's clean world. Each
test's docstring names the guarantee it proves:

* **Refresh after change** (AAP 0.3.2; 0.5.1 "analysis requests after each job"): every
  transaction of a job that records a success for an active dataset queues an
  ``analysis_request`` committed with that write, so a worker run serving an earlier request
  between two of the job's transactions still leaves one pending after the job's last write; a
  backfill (inactive) context queues none.
* **Attempts recorded on every attempt** (AAP 0.5.2): a completion whose transaction rolls back
  (a failed enqueue, a failing statement after the completion, a failed commit) leaves no
  ``running`` ``sync_run`` row, publishes none of its freshness, queues no request, and leaves
  an earlier committed completion of the same job as it committed.
* **Logging points** (AAP 0.11.1): ``sync.job.start`` and exactly one ``sync.job.finish`` per
  run, both with source, job, rows, calls and duration, on success, on an unexpected error
  (re-raised unchanged) and on cancellation (re-raised, the open rows closed failed); a failed
  job's finish is at error level with the failure text its ``sync_run`` rows record (a
  defect's names its query id) and the error's class, a persistent outage's included.
* **Job starts** (AAP 0.5.2): each job's ``sync_run`` rows start when that job started, on the
  controller's clock, while the run's ``now`` anchors the freshness attempts.
* **Stand-alone matching** (AAP 0.5.1): a ``LeadMatchJob`` pass that attempts rows in an active
  CRM dataset queues one request; an inactive pass, or one with nothing due, queues none.

Every clock is fixed (:data:`T0` builds the datasets, :data:`T1` runs the job under test),
retries and the HubSpot throttle never wait in real time, and no test sleeps.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Final, Literal

import pytest
import structlog
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import DataError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from ads_engine.connectors.http import RetryPolicy
from ads_engine.connectors.hubspot.client import RateLimiter
from ads_engine.db import repositories
from ads_engine.db.models import (
    AnalysisRequest,
    DatasetFreshness,
    LeadMatch,
    SourceConnection,
    SourceDataset,
    SyncRun,
)
from ads_engine.settings import Source, get_settings
from ads_engine.simulation.generator import Fault, SimulatedWorld
from ads_engine.simulation.scenarios import FIXED_NOW, SCENARIOS, fixed_clock
from ads_engine.sync import jobs
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.freshness import FreshnessOutcome
from ads_engine.sync.mode import SourceModeController

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]

SCENARIO: Final = "fallback.missing_credentials.case"
"""The catalogue's clean every-source world: a Google search campaign, a LinkedIn lead-gen
campaign with leads and a CRM funnel, with no fault."""

T0: Final = FIXED_NOW
"""The instant every dataset is built and activated at."""

T1: Final = FIXED_NOW + timedelta(hours=1)
"""The instant the job under test runs at; its ``sync_run`` rows start here."""

BACKFILL_DAYS: Final = 35
"""``BACKFILL_DAYS`` of every build: the default 28-day window and every lead of the world."""

GOOGLE_METRICS: Final = ("google.metrics_daily", "google.search_terms")
"""The freshness datasets of Google's metrics job, in the order it commits them."""

CRM_DATASETS: Final = ("hubspot.contacts", "hubspot.deal_search", "hubspot.associations")
"""The freshness datasets of a CRM sync without Google-touch discovery, in commit order."""

ENTITIES: Final = "google.entities"
"""The freshness dataset the stand-in jobs of this module write."""

UNEXPECTED: Final = "job failed unexpectedly"
CANCELLED: Final = "job cancelled"
"""The ``sync_run.error`` of rows a raised or cancelled run left open."""


# ---------------------------------------------------------------------------------------------
# Logging isolation
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _uncached_loggers() -> Iterator[None]:
    """Keep every logger lazy, so ``capture_logs`` sees ``sync/jobs.py``'s events.

    A logger cached under an earlier configuration would write past ``capture_logs``; as
    ``test_lead_matching.py`` does, nothing is cached while these tests run, and the
    configuration in force before each test is put back after it.
    """
    was_configured = structlog.is_configured()
    saved = structlog.get_config()
    structlog.configure(cache_logger_on_first_use=False)
    try:
        yield
    finally:
        if was_configured:
            structlog.configure(**saved)
        else:
            structlog.reset_defaults()


def _events(entries: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The captured entries of one event."""
    return [entry for entry in entries if entry.get("event") == name]


def _one(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """The single captured entry of one event; fails when there are none or several."""
    found = _events(entries, name)
    assert len(found) == 1, f"{name}: {len(found)} entries"
    return found[0]


# ---------------------------------------------------------------------------------------------
# The lab: controllers, datasets, contexts and database reads
# ---------------------------------------------------------------------------------------------


async def _no_sleep(_seconds: float) -> None:
    """The retry policy's sleep: returns at once."""


@dataclasses.dataclass(slots=True)
class Lab:
    """Controllers on the test database, simulating every source from one world.

    Attributes:
        sessions: The application's session factory on the emptied test database.
        world: The world every simulated client answers from.
    """

    sessions: Sessions
    world: SimulatedWorld

    def controller(
        self,
        at: datetime,
        *,
        world: SimulatedWorld | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> SourceModeController:
        """A controller whose clock stands at ``at`` and whose clients never wait.

        Args:
            at: The fixed instant of its clock.
            world: The world its simulated clients answer from; :attr:`world` when omitted.
            clock: A clock read instead of the fixed instant (one that moves on).
        """
        settings = get_settings()
        tick = [0.0]

        def fixed() -> datetime:
            return at

        read = clock if clock is not None else fixed

        async def advance(seconds: float) -> None:
            tick[0] += seconds

        return SourceModeController(
            settings,
            sessionmaker=self.sessions,
            world=world if world is not None else self.world,
            clock=read,
            budget=GoogleCallBudget(settings, sessionmaker=self.sessions, clock=read),
            retry=RetryPolicy(sleep=_no_sleep),
            hubspot_limiter=RateLimiter(4.0, clock=lambda: tick[0], sleep=advance),
        )

    async def build(self, source: Source) -> tuple[int, int | None]:
        """Build, backfill and activate a simulated primary of ``source`` at :data:`T0`.

        Returns:
            The dataset and, for LinkedIn, its companion CRM dataset.
        """
        outcome = await self.controller(T0).build_dataset(
            source, "simulated", reason="credentials not configured"
        )
        assert outcome.activated, outcome
        assert outcome.dataset_id is not None
        return outcome.dataset_id, outcome.companion_id

    async def seed(self, source: Source) -> int:
        """Insert an empty active simulated primary of ``source``, with its connection."""
        async with self.sessions() as session, session.begin():
            dataset = SourceDataset(
                source=source,
                mode="simulated",
                state="active",
                role="primary",
                reason="seeded by the job lifecycle tests",
                created_at=T0,
                activated_at=T0,
            )
            session.add(dataset)
            await session.flush()
            session.add(
                SourceConnection(
                    source=source,
                    mode="simulated",
                    status="ok",
                    reason="seeded by the job lifecycle tests",
                    active_dataset_id=dataset.id,
                )
            )
            return dataset.id

    @asynccontextmanager
    async def context(
        self,
        source: Source,
        dataset_id: int,
        *,
        active: bool = True,
        world: SimulatedWorld | None = None,
    ) -> AsyncIterator[jobs.JobContext]:
        """The context of ``dataset_id`` at :data:`T1` on simulated clients of ``source``.

        Args:
            source: The dataset's source.
            dataset_id: The dataset the job writes.
            active: ``False`` gives the backfill's inactive context (``jobs._inactive``).
            world: The world the clients answer from (one with a fault); :attr:`world` when
                omitted.
        """
        controller = self.controller(T1, world=world)
        async with controller.open_clients(source, "simulated") as clients:
            ctx = await jobs.JobContext.for_dataset(controller, dataset_id, clients, now=T1)
            yield ctx if active else jobs._inactive(ctx)

    async def requests(self, reason: str) -> list[AnalysisRequest]:
        """The ``analysis_request`` rows queued with ``reason``, by id."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(AnalysisRequest)
                .where(AnalysisRequest.reason == reason)
                .order_by(AnalysisRequest.id)
            )
            return list(rows)

    async def sync_requests(self) -> list[str]:
        """The reasons of every ``analysis_request`` a sync job queued, by id."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(AnalysisRequest.reason)
                .where(AnalysisRequest.reason.startswith("sync:"))
                .order_by(AnalysisRequest.id)
            )
            return list(rows)

    async def runs(self, job: str) -> dict[str | None, SyncRun]:
        """The ``sync_run`` rows of ``job`` started at :data:`T1`, by freshness dataset."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(SyncRun).where(SyncRun.job == job, SyncRun.started_at == T1)
            )
            found = {row.dataset: row for row in rows}
        assert found, f"no {job} sync_run row started at T1"
        return found

    async def freshness(self, dataset_id: int, name: str) -> DatasetFreshness | None:
        """One ``dataset_freshness`` row."""
        async with self.sessions() as session:
            return await session.scalar(
                select(DatasetFreshness).where(
                    DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == name
                )
            )

    async def committed_with(self, request_id: int, dataset_id: int, name: str) -> bool:
        """Whether a request row was committed by the transaction that last wrote a freshness row.

        PostgreSQL stamps every row version with the id of the transaction that wrote it
        (``xmin``), so equal ids mean one transaction committed both.
        """
        async with self.sessions() as session:
            request = await session.scalar(
                text("SELECT xmin::text FROM analysis_request WHERE id = :id"), {"id": request_id}
            )
            row = await session.scalar(
                text(
                    "SELECT xmin::text FROM dataset_freshness "
                    "WHERE dataset_id = :dataset_id AND dataset = :name"
                ),
                {"dataset_id": dataset_id, "name": name},
            )
        assert request is not None, f"no analysis_request {request_id}"
        assert row is not None, f"no {name} freshness row"
        return bool(request == row)

    async def make_matches_due(self, crm_dataset_id: int) -> int:
        """Reset every ``lead_match`` row of a CRM dataset to ``pending``; return how many."""
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                update(LeadMatch)
                .where(LeadMatch.crm_dataset_id == crm_dataset_id)
                .values(
                    status="pending",
                    contact_id=None,
                    contact_read_status=None,
                    next_attempt_at=None,
                    reason=None,
                )
            )
            return int(getattr(result, "rowcount", 0))


@pytest.fixture
def lab(
    db: Sessions,
    make_world: Callable[..., SimulatedWorld],
    set_env: Callable[..., None],
) -> Lab:
    """The lab on the emptied test database, with ``BACKFILL_DAYS`` set to a short span."""
    set_env(BACKFILL_DAYS=str(BACKFILL_DAYS))
    return Lab(sessions=db, world=make_world(SCENARIO, clock=fixed_clock))


def _faulted_world(*faults: Fault) -> SimulatedWorld:
    """The :data:`SCENARIO` world with ``faults`` added: the same entities, misbehaving."""
    entry = SCENARIOS[SCENARIO]
    return dataclasses.replace(entry, spec=entry.spec.adding(faults=faults)).world(
        42, clock=fixed_clock
    )


DEFECT_Q_G6: Final = Fault(
    "google", "search", "http_status", status=400, match={"query_id": "Q-G6"}
)
"""Q-G6 (search terms) answers HTTP 400, a defect, after Q-G3 and Q-G5 answered."""

OUTAGE_Q_G5: Final = Fault(
    "google", "search", "http_status", status=503, match={"query_id": "Q-G5"}
)
"""Q-G5 (keyword days) answers HTTP 503 on every attempt: a persistent outage."""


# ---------------------------------------------------------------------------------------------
# Stand-in jobs and failure injection
# ---------------------------------------------------------------------------------------------


class _ServedBetween(jobs.MetricsSyncJob):
    """Google metrics whose pending requests a worker run serves before each completion.

    Stamping every pending request picked is what the scheduler's coalescing run does to the
    requests it serves; doing it in a transaction of its own just before each completion puts
    a run between every two of the job's transactions.
    """

    async def _complete(
        self,
        session: AsyncSession,
        outcomes: Mapping[str, FreshnessOutcome],
        *,
        changed: bool = False,
        rows: Mapping[str, int] | None = None,
    ) -> None:
        """Serve every pending request, then complete as the real job does."""
        async with self.ctx.sessionmaker() as worker, worker.begin():
            await worker.execute(
                update(AnalysisRequest)
                .where(AnalysisRequest.picked_at.is_(None))
                .values(picked_at=self.ctx.now)
            )
        await super()._complete(session, outcomes, changed=changed, rows=rows)


def _commit_refused(_session: Session) -> None:
    """A ``before_commit`` listener: the commit fails as a dropped connection would."""
    raise OperationalError("COMMIT", {}, ConnectionError("connection lost at commit"))


class _CompleteThenFail(jobs._JobBase):
    """Completes :data:`ENTITIES`, then fails inside the same transaction.

    ``statement``: a later statement of the transaction fails (division by zero). ``commit``:
    the commit itself fails.
    """

    job = jobs.JOB_ENTITIES

    def __init__(self, ctx: jobs.JobContext, failure: Literal["statement", "commit"]) -> None:
        super().__init__(ctx)
        self.failure = failure

    def freshness_datasets(self) -> tuple[str, ...]:
        """One dataset."""
        return (ENTITIES,)

    async def _execute(self) -> None:
        """Write, complete and fail in one transaction."""
        async with self.ctx.sessionmaker() as session, session.begin():
            self._count(ENTITIES, 3)
            await self._complete(
                session, {ENTITIES: jobs._success(self.ctx.now, covered=None, reasons=())}
            )
            if self.failure == "statement":
                await session.execute(text("SELECT 1 / 0"))
            else:
                event.listen(session.sync_session, "before_commit", _commit_refused)


async def _call_google(ctx: jobs.JobContext, times: int) -> None:
    """Send ``times`` real webhook calls (Q-G1) through the context's simulated Google client."""
    client = ctx.clients.require_google()
    for _ in range(times):
        await client.run_query("Q-G1")


class _Raises(jobs._JobBase):
    """Counts rows, makes two calls, then raises an error no connector raises (a bug)."""

    job = jobs.JOB_ENTITIES

    def __init__(self, ctx: jobs.JobContext, error: Exception) -> None:
        super().__init__(ctx)
        self.error = error

    def freshness_datasets(self) -> tuple[str, ...]:
        """One dataset."""
        return (ENTITIES,)

    async def _execute(self) -> None:
        """Count, call, then raise."""
        self._count(ENTITIES, 4)
        await _call_google(self.ctx, 2)
        raise self.error


class _Hangs(jobs._JobBase):
    """Completes :data:`ENTITIES`, then waits until cancelled with ``google.metrics_daily`` open."""

    job = jobs.JOB_ENTITIES

    def __init__(self, ctx: jobs.JobContext) -> None:
        super().__init__(ctx)
        self.waiting = asyncio.Event()

    def freshness_datasets(self) -> tuple[str, ...]:
        """Two datasets: one completed, one still open when the task is cancelled."""
        return (ENTITIES, "google.metrics_daily")

    async def _execute(self) -> None:
        """Complete the first dataset, then wait forever."""
        async with self.ctx.sessionmaker() as session, session.begin():
            self._count(ENTITIES, 2)
            await self._complete(
                session, {ENTITIES: jobs._success(self.ctx.now, covered=None, reasons=())}
            )
        await _call_google(self.ctx, 1)
        self.waiting.set()
        await asyncio.Event().wait()


# ---------------------------------------------------------------------------------------------
# Refresh after change (AAP 0.3.2; 0.5.1 "analysis requests after each job")
# ---------------------------------------------------------------------------------------------


async def test_request_follows_the_last_write_after_a_run_served_an_earlier_one(lab: Lab) -> None:
    """Refresh after change: a run between two transactions still leaves a request pending.

    Google's metrics job commits ``google.metrics_daily`` and then ``google.search_terms``. A
    worker run serves (stamps) the first transaction's request before the second commits; the
    second transaction queues its own, committed with the ``google.search_terms`` freshness
    write, so the search terms are never left out of every run.
    """
    dataset_id, _ = await lab.build("google")
    reason = f"sync:google.{jobs.JOB_METRICS}"

    async with lab.context("google", dataset_id) as ctx:
        result = await _ServedBetween(ctx).run()

    assert (result.status, set(result.freshness)) == ("succeeded", set(GOOGLE_METRICS))
    first, second = await lab.requests(reason)
    assert first.picked_at == T1, "the first request was served between the transactions"
    assert second.picked_at is None, "a request is pending after the job's last write"
    # Stamping rewrote the first row (a new ``xmin``); the pending one is as committed.
    assert await lab.committed_with(second.id, dataset_id, "google.search_terms")


async def test_every_successful_crm_transaction_queues_its_request(lab: Lab) -> None:
    """Refresh after change: each CRM transaction that records a success queues one request.

    Contacts, the deal search and associations commit in three transactions; each commits its
    own ``sync:hubspot.crm`` request with its freshness write.
    """
    dataset_id, _ = await lab.build("hubspot")

    async with lab.context("hubspot", dataset_id) as ctx:
        result = await jobs.CrmSyncJob(ctx).run()

    assert (result.status, set(result.freshness)) == ("succeeded", set(CRM_DATASETS))
    requests = await lab.requests(f"sync:hubspot.{jobs.JOB_CRM}")
    assert len(requests) == len(CRM_DATASETS)
    for request, name in zip(requests, CRM_DATASETS, strict=True):
        assert request.picked_at is None
        assert await lab.committed_with(request.id, dataset_id, name), name


async def test_inactive_context_queues_no_request(lab: Lab) -> None:
    """Refresh after change: a backfill (inactive) context queues nothing; activation does."""
    dataset_id, _ = await lab.build("google")

    async with lab.context("google", dataset_id, active=False) as ctx:
        result = await jobs.MetricsSyncJob(ctx).run()

    assert result.status == "succeeded"
    assert await lab.sync_requests() == []


# ---------------------------------------------------------------------------------------------
# Rolled-back completions (AAP 0.5.2: sync_run and freshness updated on every attempt)
# ---------------------------------------------------------------------------------------------


async def test_failed_enqueue_rolls_back_only_its_own_completion(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every attempt recorded: a failed enqueue closes its rows failed and keeps the earlier close.

    The enqueue of the second transaction (``google.search_terms``) fails. Its freshness write
    and ``sync_run`` close roll back with it; the run re-raises the error unchanged after
    closing that row ``failed`` in a fresh transaction. The first transaction's committed
    close, freshness and request stand, and the finish log lists only what committed.
    """
    dataset_id, _ = await lab.build("google")
    before = await lab.freshness(dataset_id, "google.search_terms")
    assert before is not None
    reason = f"sync:google.{jobs.JOB_METRICS}"
    failure = OperationalError("INSERT INTO analysis_request", {}, ConnectionError("lost"))
    real = repositories.enqueue_analysis_request
    queued: list[str] = []

    async def enqueue(session: AsyncSession, reason: str) -> None:
        queued.append(reason)
        if len(queued) == 2:
            raise failure
        await real(session, reason)

    monkeypatch.setattr(repositories, "enqueue_analysis_request", enqueue)

    with capture_logs() as entries, pytest.raises(OperationalError) as raised:
        async with lab.context("google", dataset_id) as ctx:
            await jobs.MetricsSyncJob(ctx).run()

    assert raised.value is failure
    assert queued == [reason, reason]
    runs = await lab.runs(jobs.JOB_METRICS)
    assert {name: row.status for name, row in runs.items()} == {
        "google.metrics_daily": "succeeded",
        "google.search_terms": "failed",
    }
    assert all(row.finished_at is not None for row in runs.values())
    assert (runs["google.metrics_daily"].error, runs["google.search_terms"].error) == (
        None,
        UNEXPECTED,
    )
    daily = await lab.freshness(dataset_id, "google.metrics_daily")
    terms = await lab.freshness(dataset_id, "google.search_terms")
    assert daily is not None
    assert terms is not None
    assert daily.last_attempt_at == T1
    assert (terms.last_attempt_at, terms.last_success_at) == (
        before.last_attempt_at,
        before.last_success_at,
    ), "the rolled-back freshness write left the row as it was"
    assert len(await lab.requests(reason)) == 1, "only the committed transaction's request"
    finish = _one(entries, "sync.job.finish")
    assert set(finish["datasets"]) == {"google.metrics_daily"}
    assert (finish["log_level"], finish["status"], finish["error"], finish["error_type"]) == (
        "error",
        "failed",
        UNEXPECTED,
        "OperationalError",
    )


@pytest.mark.parametrize("failure", ["statement", "commit"])
async def test_failure_after_completion_rolls_it_back(
    lab: Lab, failure: Literal["statement", "commit"]
) -> None:
    """Every attempt recorded: a completion rolled back by a later failure is closed as failed.

    The completion's freshness write, ``sync_run`` close and request are in the transaction a
    later statement (``statement``) or the commit (``commit``) fails. The run re-raises the
    error; its row ends ``failed`` with ``finished_at`` and the error, never ``running``; no
    freshness, request or logged outcome of the rolled-back completion remains.
    """
    dataset_id, _ = await lab.build("google")
    before = await lab.freshness(dataset_id, ENTITIES)
    assert before is not None
    expected: type[SQLAlchemyError] = DataError if failure == "statement" else OperationalError

    with capture_logs() as entries, pytest.raises(expected):
        async with lab.context("google", dataset_id) as ctx:
            await _CompleteThenFail(ctx, failure).run()

    runs = await lab.runs(jobs.JOB_ENTITIES)
    assert [(row.status, row.error) for row in runs.values()] == [("failed", UNEXPECTED)]
    assert all(row.finished_at is not None for row in runs.values())
    after = await lab.freshness(dataset_id, ENTITIES)
    assert after is not None
    assert (after.last_attempt_at, after.last_success_at) == (
        before.last_attempt_at,
        before.last_success_at,
    )
    assert await lab.sync_requests() == []
    finish = _one(entries, "sync.job.finish")
    assert (finish["status"], finish["datasets"], finish["error"], finish["error_type"]) == (
        "failed",
        {},
        UNEXPECTED,
        expected.__name__,
    )


# ---------------------------------------------------------------------------------------------
# Logging points (AAP 0.11.1: sync job start and finish with source, job, rows, calls, duration)
# ---------------------------------------------------------------------------------------------


async def test_success_logs_start_and_one_finish(lab: Lab) -> None:
    """Logging: a successful run logs one start with zero counters and one finish with totals."""
    dataset_id, _ = await lab.build("google")

    with capture_logs() as entries:
        async with lab.context("google", dataset_id) as ctx:
            result = await jobs.MetricsSyncJob(ctx).run()

    start = _one(entries, "sync.job.start")
    assert {key: start[key] for key in ("source", "job", "dataset_id", "rows", "calls")} == {
        "source": "google",
        "job": jobs.JOB_METRICS,
        "dataset_id": dataset_id,
        "rows": 0,
        "calls": 0,
    }
    assert start["duration_ms"] == 0
    finish = _one(entries, "sync.job.finish")
    assert result.status == "succeeded"
    assert result.rows > 0
    assert result.calls > 0
    assert {
        key: finish[key]
        for key in ("source", "job", "dataset_id", "status", "rows", "calls", "duration_ms")
    } == {
        "source": "google",
        "job": jobs.JOB_METRICS,
        "dataset_id": dataset_id,
        "status": "succeeded",
        "rows": result.rows,
        "calls": result.calls,
        "duration_ms": result.duration_ms,
    }
    assert (finish["log_level"], finish["error"], finish["error_type"]) == ("info", None, None)
    assert set(finish["datasets"]) == set(GOOGLE_METRICS)


async def test_unexpected_error_logs_one_failed_finish_and_propagates(lab: Lab) -> None:
    """Logging: an unexpected error logs one ``failed`` finish at error level, then propagates.

    The error is re-raised as the same object; the finish carries the run's rows, calls and
    duration, the reason its open row closes with as ``error`` and the error's class name as
    ``error_type``, never its text or a traceback; the open row is closed ``failed``.
    """
    dataset_id = await lab.seed("google")
    error = RuntimeError("a bug whose text stays out of the finish event")

    with capture_logs() as entries, pytest.raises(RuntimeError) as raised:
        async with lab.context("google", dataset_id) as ctx:
            await _Raises(ctx, error).run()

    assert raised.value is error
    assert len(_events(entries, "sync.job.start")) == 1
    finish = _one(entries, "sync.job.finish")
    assert {
        key: finish[key] for key in ("source", "job", "dataset_id", "status", "rows", "calls")
    } == {
        "source": "google",
        "job": jobs.JOB_ENTITIES,
        "dataset_id": dataset_id,
        "status": "failed",
        "rows": 4,
        "calls": 2,
    }
    assert (finish["log_level"], finish["error"], finish["error_type"]) == (
        "error",
        UNEXPECTED,
        "RuntimeError",
    )
    assert isinstance(finish["duration_ms"], int)
    assert finish["duration_ms"] >= 0
    assert "exc_info" not in finish
    assert str(error) not in repr(finish)
    runs = await lab.runs(jobs.JOB_ENTITIES)
    assert [(row.status, row.error, row.rows) for row in runs.values()] == [
        ("failed", UNEXPECTED, 4)
    ]
    assert all(row.finished_at is not None for row in runs.values())


async def test_cancellation_logs_one_cancelled_finish_and_propagates(lab: Lab) -> None:
    """Logging: a cancelled run closes its open rows failed and logs one ``cancelled`` finish.

    The ``CancelledError`` propagates; the dataset completed before the cancellation keeps its
    committed success.
    """
    dataset_id = await lab.seed("google")

    with capture_logs() as entries:
        async with lab.context("google", dataset_id) as ctx:
            job = _Hangs(ctx)
            task = asyncio.create_task(job.run())
            await job.waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    finish = _one(entries, "sync.job.finish")
    assert {
        key: finish[key] for key in ("log_level", "status", "rows", "calls", "error", "error_type")
    } == {
        "log_level": "warning",
        "status": "cancelled",
        "rows": 2,
        "calls": 1,
        "error": CANCELLED,
        "error_type": "CancelledError",
    }
    assert set(finish["datasets"]) == {ENTITIES}
    runs = await lab.runs(jobs.JOB_ENTITIES)
    assert {name: (row.status, row.error) for name, row in runs.items()} == {
        ENTITIES: ("succeeded", None),
        "google.metrics_daily": ("failed", CANCELLED),
    }
    assert all(row.finished_at is not None for row in runs.values())


async def test_defect_logs_one_error_finish_with_its_failure_text(lab: Lab) -> None:
    """Logging: a job failed by a defect logs its finish at error level, with its failure text.

    Q-G6 answers HTTP 400 once ``google.metrics_daily`` is recorded. The finish is the run's
    error-level line: its ``error`` is the sanitized text the failed ``sync_run`` row and the
    result record (naming the query id, AAP 0.3.3 "logged with the query id"), and its
    ``error_type`` the defect's class. The result and the finish count every call the job's
    client sent, Q-G3's, Q-G5's and the defective Q-G6 call included.
    """
    dataset_id, _ = await lab.build("google")

    with capture_logs() as entries:
        async with lab.context("google", dataset_id, world=_faulted_world(DEFECT_Q_G6)) as ctx:
            result = await jobs.MetricsSyncJob(ctx).run()
            sent = ctx.clients.require_google().calls_made

    assert (result.status, result.disposition) == ("failed", "defect")
    assert result.calls == sent >= 3
    runs = await lab.runs(jobs.JOB_METRICS)
    assert runs["google.metrics_daily"].status == "succeeded"
    failed = runs["google.search_terms"]
    assert failed.status == "failed"
    assert failed.error is not None
    assert "Q-G6" in failed.error
    assert result.error == failed.error
    finish = _one(entries, "sync.job.finish")
    assert {
        key: finish[key]
        for key in ("log_level", "status", "disposition", "calls", "error", "error_type")
    } == {
        "log_level": "error",
        "status": "failed",
        "disposition": "defect",
        "calls": sent,
        "error": failed.error,
        "error_type": "QueryDefect",
    }


async def test_persistent_outage_logs_one_error_finish_with_its_failure_text(lab: Lab) -> None:
    """Logging: a transient outage whose retries ran out logs the job's finish at error level.

    Q-G5 answers HTTP 503 on all three attempts, so ``google.metrics_daily`` keeps its last
    good data and turns stale while ``google.search_terms`` succeeds. The job failed without
    raising: its finish is an error-level line whose ``error`` is the reason its failed
    ``sync_run`` row records, with no ``error_type``.
    """
    dataset_id, _ = await lab.build("google")

    with capture_logs() as entries:
        async with lab.context("google", dataset_id, world=_faulted_world(OUTAGE_Q_G5)) as ctx:
            result = await jobs.MetricsSyncJob(ctx).run()

    assert (result.status, result.disposition) == ("failed", "stale")
    runs = await lab.runs(jobs.JOB_METRICS)
    assert runs["google.search_terms"].status == "succeeded"
    failed = runs["google.metrics_daily"]
    assert failed.status == "failed"
    assert failed.error is not None
    assert "503" in failed.error
    finish = _one(entries, "sync.job.finish")
    assert {
        key: finish[key] for key in ("log_level", "status", "disposition", "error", "error_type")
    } == {
        "log_level": "error",
        "status": "failed",
        "disposition": "stale",
        "error": failed.error,
        "error_type": None,
    }


# ---------------------------------------------------------------------------------------------
# Job starts (AAP 0.5.2: sync_run started_at and finished_at)
# ---------------------------------------------------------------------------------------------


async def test_each_job_records_its_own_start(lab: Lab) -> None:
    """``sync_run.started_at``: each job's rows start when that job started, not at ``now``.

    One ``run_source_jobs`` call at ``now`` :data:`T1` runs Google's entity and metrics jobs on
    a controller whose clock moves on a second at every reading. Every row of one job shares
    one start read from that clock, later than ``now``; the metrics job starts after the entity
    job finished; ``now`` stays the anchor of the freshness attempts.
    """
    dataset_id, _ = await lab.build("google")
    readings = itertools.count(1)

    def clock() -> datetime:
        return T1 + timedelta(seconds=next(readings))

    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_ENTITIES, jobs.JOB_METRICS],
        controller=lab.controller(T1, clock=clock),
        now=T1,
    )

    assert [(result.job, result.status) for result in report.results] == [
        (jobs.JOB_ENTITIES, "succeeded"),
        (jobs.JOB_METRICS, "succeeded"),
    ]
    async with lab.sessions() as session:
        rows = list(
            await session.scalars(
                select(SyncRun).where(SyncRun.started_at > T1).order_by(SyncRun.id)
            )
        )
    by_job: dict[str, list[SyncRun]] = {}
    for row in rows:
        by_job.setdefault(row.job, []).append(row)
    assert {job: sorted(str(row.dataset) for row in found) for job, found in by_job.items()} == {
        jobs.JOB_ENTITIES: [ENTITIES],
        jobs.JOB_METRICS: sorted(GOOGLE_METRICS),
    }
    (entities_start,) = {row.started_at for row in by_job[jobs.JOB_ENTITIES]}
    (metrics_start,) = {row.started_at for row in by_job[jobs.JOB_METRICS]}
    entities_finish = max(row.finished_at for row in by_job[jobs.JOB_ENTITIES] if row.finished_at)
    assert T1 < entities_start < entities_finish < metrics_start
    assert all(
        row.finished_at is not None and row.finished_at > metrics_start
        for row in by_job[jobs.JOB_METRICS]
    )
    for name in (ENTITIES, *GOOGLE_METRICS):
        row = await lab.freshness(dataset_id, name)
        assert row is not None
        assert row.last_attempt_at == T1, f"{name}: freshness stays anchored on now"


# ---------------------------------------------------------------------------------------------
# Stand-alone matching (AAP 0.5.1 "analysis requests after each job")
# ---------------------------------------------------------------------------------------------


async def test_standalone_matching_queues_one_request_when_it_attempts_rows(lab: Lab) -> None:
    """Stand-alone matching: an active pass that attempts rows queues one request.

    In the simulated LinkedIn dataset's companion CRM dataset (HubSpot, active), every
    ``lead_match`` row is made due. A pass in the backfill's inactive context attempts them and
    queues nothing; made due again, an active pass queues exactly one
    ``sync:hubspot.lead_match`` request; a pass with nothing due queues none.
    """
    _, companion_id = await lab.build("linkedin")
    assert companion_id is not None
    reason = f"sync:hubspot.{jobs.JOB_LEAD_MATCH}"
    assert await lab.make_matches_due(companion_id) > 0

    async with lab.context("hubspot", companion_id, active=False) as ctx:
        inactive = await jobs.LeadMatchJob(ctx).run()
    assert (inactive.status, inactive.rows > 0) == ("succeeded", True)
    assert await lab.requests(reason) == []

    assert await lab.make_matches_due(companion_id) > 0
    async with lab.context("hubspot", companion_id) as ctx:
        active = await jobs.LeadMatchJob(ctx).run()
    assert (active.status, active.rows > 0) == ("succeeded", True)
    (request,) = await lab.requests(reason)
    assert request.picked_at is None

    async with lab.context("hubspot", companion_id) as ctx:
        idle = await jobs.LeadMatchJob(ctx).run()
    assert (idle.status, idle.rows) == ("succeeded", 0)
    assert len(await lab.requests(reason)) == 1
    assert await lab.sync_requests() == [reason]


async def test_matching_counts_the_calls_of_both_its_clients_once(lab: Lab) -> None:
    """Calls (AAP 0.11.1): a pass counts its HubSpot calls and its LinkedIn re-fetches, once.

    The pass is handed a LinkedIn client that already made a call for earlier work, as a live
    backfill hands its own; the job's calls are every HubSpot call of the pass plus only the
    re-fetches this pass sent, each read from the client's own counter.
    """
    _, companion_id = await lab.build("linkedin")
    assert companion_id is not None
    assert await lab.make_matches_due(companion_id) > 0

    async with lab.controller(T1).open_clients("linkedin", "simulated") as linkedin:
        lead_client = linkedin.require_linkedin()
        await lead_client.get_ad_account(linkedin.linkedin_account_ids[0])
        earlier = lead_client.calls
        assert earlier == 1
        async with lab.context("hubspot", companion_id) as ctx:
            hubspot = ctx.clients.require_hubspot()
            result = await jobs.LeadMatchJob(ctx, lead_client=lead_client).run()
            lookups = hubspot.calls
        refetches = lead_client.calls - earlier

    assert (result.status, result.rows > 0) == ("succeeded", True)
    assert lookups > 0
    assert refetches > 0
    assert result.calls == lookups + refetches
