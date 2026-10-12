"""The scheduler's due-job and manual dispatch and its nightly purge, on PostgreSQL.

AAP 0.3.2 ("Continuous analysis: sync schedule": wall-clock slots in ``DISPLAY_TIMEZONE``; the
worker wakes every 30 s to pick up due jobs and pending ``sync_request`` rows), AAP 0.3.3
("Active-dataset filter"; "Retirement and purge": retired datasets are kept 30 days, then
purged by the nightly cleanup, one ``DELETE`` per dataset, a companion with its parent) and
AAP 0.5.2 (``sync_run``: source, job, started_at, status, rows, error; ``sync_request``:
source, requested_at, picked_at). ``sync/scheduler.py`` is driven through its real
``due_jobs``, ``tick`` and lane passes; proved here:

* **Active sources**: ``due_jobs`` leaves out a source with no dataset, only a building or
  only a retired one, and a HubSpot companion does not make HubSpot active; sources with an
  active primary and no history get every scheduled job in schedule order, in ``SOURCES``
  order.
* **History**: a ``sync_run`` started at the latest slot satisfies it whatever its status; one
  started 1 s earlier, another source's run, other job names (``crm_companion``,
  ``lead_match``, ``probe``, ``purge``) and runs older than the two-day horizon do not; after
  downtime each job is due once; in America/New_York the 03:00 restatement is judged on the
  New York wall clock.
* **Scheduled dispatch**: ``tick`` runs each due source once through ``jobs.run_source_jobs``
  with trigger ``scheduled``, the due job names, the scheduler's controller and the tick's
  instant, in the order google, linkedin, hubspot; the report lists the jobs per source and
  the failed ones apart; one source's failure does not stop the others.
* **Feedback loop** (the real simulated stack): after the start-up backfills, the 15:00 UTC
  tick runs exactly the 3-hourly jobs, records them in ``sync_run``, and the next wake runs
  nothing; the 18:00 tick adds the 6-hourly metrics.
* **Manual dispatch**: one tick claims every pending ``sync_request`` (``picked_at`` = the
  tick) and runs each source's jobs with trigger ``manual`` and no job list, in request order,
  before the due jobs and even when nothing is due; a claimed row is never claimed again; a
  source lane claims only its own sources' rows.
* **Purge**: at 05:00 New York time the purge deletes only primaries retired more than 30 days
  ago (ascending ids, a LinkedIn parent's companion with it) and records one ``purge`` row per
  source purged with its count, or one google row with 0; a run today of any source
  satisfies the slot until 05:00 the next day; the first purge ever runs at once; a failed
  purge is recorded ``failed`` and not retried before the next slot.
* **Recovery probes** (AAP 0.3.2: every 30 min while a source is in automatic fallback; AAP
  0.3.3 "Recovery readiness"): with no ``probe`` history the first tick probes Google once,
  under a ``probe`` row started at the tick and closed at the clock's reading once ``recover``
  returned; the next probe waits until 30 min after the latest one started (29 min 59 s: none;
  exactly 30 min: one), whatever that row's status; a source not in fallback is never probed,
  and neither another source's ``probe`` row nor another Google job spaces Google's probe; the
  row ends ``succeeded`` only when a live dataset was activated, ``failed`` otherwise with the
  outcome's reason (for a probe that did not pass, its deciding check's), ``live dataset was
  not activated`` without one, or ``source is no longer in automatic fallback; nothing was
  probed`` without an outcome; each probe logs one ``sync.job.start`` and one
  ``sync.job.finish`` (AAP 0.11.1) with its source, job, dataset, status, rows, checks,
  calls, duration and error.

How. Real PostgreSQL through ``conftest.db`` (emptied per test); rows of ``source_dataset``,
``sync_run`` and ``sync_request`` are inserted directly. Every scheduler runs on a fixed clock
(:class:`_Clock`), built after ``set_env`` so its ``display_tz`` is the one set. Except in the
feedback-loop test, ``ensure_dataset`` builds nothing, no source is in automatic fallback (in
the recovery-probe tests Google alone is, and ``recover`` is a recording fake,
:class:`Recovery`) and ``jobs.run_source_jobs`` (which the scheduler calls through the
``jobs`` module) is a recording fake (:class:`SourceJobs`). The feedback-loop test runs on
its own working directory (a ``config/`` copy and a ``tests`` link for the simulator's
contract validation), with the HubSpot throttle and the retry policy on fake time, so no test
sleeps.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from ads_engine.connectors.http import RetryPolicy
from ads_engine.connectors.hubspot.client import RateLimiter
from ads_engine.db.models.ops import SourceDataset, SyncRequest, SyncRun
from ads_engine.settings import SOURCES, Source, get_settings
from ads_engine.sync import jobs
from ads_engine.sync.datasets import DatasetManager
from ads_engine.sync.mode import (
    REASON_NO_CREDENTIALS,
    REASON_RECOVERED,
    BuildOutcome,
    ProbeCheck,
    ProbeReport,
    ProbeRunLog,
    SourceModeController,
    bootstrap_sources,
)
from ads_engine.sync.scheduler import JOB_PROBE, JOB_PURGE, Scheduler, TickReport

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]

NEW_YORK: Final = "America/New_York"

GOOGLE_JOBS: Final = ("entities", "metrics", "restatement")
LINKEDIN_JOBS: Final = ("entities", "metrics", "restatement", "leads")
HUBSPOT_JOBS: Final = ("crm",)
"""The scheduled jobs of each source in schedule order (AAP 0.3.2; segments run apart)."""

ALL_DUE: Final[Mapping[Source, tuple[str, ...]]] = {
    "google": GOOGLE_JOBS,
    "linkedin": LINKEDIN_JOBS,
    "hubspot": HUBSPOT_JOBS,
}

NOW: Final = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
"""A UTC-zone instant: the 3- and 6-hourly slots are 12:00 UTC, the restatement 03:00 UTC."""

SLOT_12: Final = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SLOT_03: Final = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)

NY_PURGE: Final = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
"""05:00 EDT on 2026-10-05, the New York purge slot."""

ONE_SECOND: Final = timedelta(seconds=1)
RETENTION: Final = timedelta(days=30)
"""AAP 0.3.3: retired datasets are kept 30 days."""

T0: Final = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
"""The simulated worker's start: every dataset is built and backfilled at this instant."""

BACKFILL_DAYS: Final = "35"
"""Covers the default 28-day window while keeping every simulated backfill small."""

TESTS_DIR: Final = Path(__file__).resolve().parents[1]
"""The suite whose ``contracts/`` the simulator validates every request and payload against."""


# ---------------------------------------------------------------------------------------------
# Clocks, fakes and fixtures
# ---------------------------------------------------------------------------------------------


class _Clock:
    """A clock standing at one instant until the test moves it."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        """Return the current fixed instant."""
        return self.at


class _FakeTime:
    """Monotonic seconds that advance only when slept through: throttles and retries never wait."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        """Return the fake monotonic reading."""
        return self.seconds

    async def sleep(self, seconds: float) -> None:
        """Advance the fake time by ``seconds`` instead of sleeping."""
        self.seconds += max(seconds, 0.0)


@dataclass(frozen=True, slots=True)
class Call:
    """One call of the fake ``run_source_jobs``.

    Attributes:
        source: The source asked for.
        trigger: The trigger passed.
        options: Every other keyword passed (``jobs``, ``controller``, ``now``, ...).
    """

    source: Source
    trigger: jobs.Trigger
    options: Mapping[str, object] = field(default_factory=dict)


class SourceJobs:
    """Stands in for ``jobs.run_source_jobs``: records each call and runs nothing.

    Each call returns a report with one result per job asked for (``jobs.SOURCE_JOBS`` of the
    source when no names are passed): ``failed`` for the (source, job) pairs in
    :attr:`failing`, ``succeeded`` otherwise. A source in :attr:`raising` raises
    :attr:`error` instead, as a database outage inside a job would.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.failing: set[tuple[Source, str]] = set()
        self.raising: set[Source] = set()
        self.error: Exception = OperationalError("SELECT", {}, ConnectionError("db restarting"))

    async def __call__(
        self, source: Source, *, trigger: jobs.Trigger, **options: object
    ) -> jobs.SourceSyncReport:
        """Record the call; return the requested jobs' results, or raise for a raising source."""
        self.calls.append(Call(source, trigger, dict(options)))
        if source in self.raising:
            raise self.error
        names = options.get("jobs")
        requested = jobs.SOURCE_JOBS[source] if names is None else _names(names)
        results = tuple(
            _result(source, name, failed=(source, name) in self.failing) for name in requested
        )
        return jobs.SourceSyncReport(source=source, trigger=trigger, results=results)

    def of(self, trigger: jobs.Trigger) -> list[Call]:
        """The calls made with ``trigger``, in order."""
        return [call for call in self.calls if call.trigger == trigger]


def _names(value: object) -> tuple[str, ...]:
    """The job names the scheduler passed; they must be a tuple of strings."""
    assert isinstance(value, tuple), value
    names = tuple(str(name) for name in value)
    assert names == value
    return names


def _result(source: Source, job: str, *, failed: bool) -> jobs.JobResult:
    """A finished job's result, ``failed`` or ``succeeded``, with nothing written."""
    return jobs.JobResult(
        job=job,
        source=source,
        dataset_id=0,
        status="failed" if failed else "succeeded",
        rows=0,
        calls=0,
        freshness={},
        disposition=None,
        error="fake failure" if failed else None,
        duration_ms=0,
    )


@pytest.fixture
def quiet_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ensure_dataset`` builds nothing and no source is in automatic fallback."""

    async def ensure_dataset(
        self: SourceModeController, source: Source, *, force_simulated: bool = False
    ) -> BuildOutcome | None:
        del self, source, force_simulated  # Nothing is ever built here.
        return None

    async def in_fallback(self: SourceModeController, source: Source) -> bool:
        del self, source  # No source is in automatic fallback.
        return False

    monkeypatch.setattr(SourceModeController, "ensure_dataset", ensure_dataset)
    monkeypatch.setattr(SourceModeController, "in_fallback", in_fallback)


@pytest.fixture
def source_jobs(monkeypatch: pytest.MonkeyPatch, quiet_sources: None) -> SourceJobs:
    """``jobs.run_source_jobs`` replaced by a recording fake (the scheduler calls it there)."""
    del quiet_sources  # Requested for its patches.
    fake = SourceJobs()
    monkeypatch.setattr(jobs, "run_source_jobs", fake)
    return fake


@pytest.fixture
def new_york(set_env: Callable[..., None]) -> None:
    """``DISPLAY_TIMEZONE=America/New_York``; schedulers built afterwards slot on its clock."""
    set_env(DISPLAY_TIMEZONE=NEW_YORK)
    assert get_settings().display_timezone == NEW_YORK


class ThresholdsFactory(Protocol):
    """The part of the root ``make_thresholds`` fixture this module uses, for type hints."""

    def write(self, overrides: Mapping[str, object] | None = None) -> Path:
        """Write a copy of ``config/`` and return its directory, unloaded."""
        ...


@pytest.fixture
def workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_thresholds: ThresholdsFactory
) -> Path:
    """Run the test in ``tmp_path``: a ``config/`` copy and a ``tests`` link to this suite."""
    config = make_thresholds.write()
    assert config == tmp_path / "config"
    (tmp_path / "tests").symlink_to(TESTS_DIR, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    return config


@pytest.fixture
def simulated(set_env: Callable[..., None]) -> None:
    """Force simulated data with a short backfill; the conftest baseline removes credentials."""
    set_env(DATA_MODE="simulated", BACKFILL_DAYS=BACKFILL_DAYS)
    settings = get_settings()
    assert not [source for source in SOURCES if settings.has_credentials(source)]


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _scheduler(clock: _Clock) -> Scheduler:
    """A scheduler on the settings as set now and the test database, timed by ``clock``."""
    return Scheduler(controller=SourceModeController(get_settings(), clock=clock), clock=clock)


async def _dataset(
    db: Sessions,
    source: Source,
    state: str,
    *,
    retired_at: datetime | None = None,
    parent: int | None = None,
) -> int:
    """Insert one ``source_dataset`` row and return its id.

    A ``parent`` makes it the simulated HubSpot companion of that (LinkedIn) dataset, as the
    ``role_shape`` check requires; otherwise it is a simulated primary.
    """
    async with db() as session, session.begin():
        row = SourceDataset(
            source=source,
            mode="simulated",
            state=state,
            role="primary" if parent is None else "companion",
            parent_dataset_id=parent,
            reason="scheduler dispatch test",
            retired_at=retired_at,
        )
        session.add(row)
        await session.flush()
        return row.id


async def _active_all(db: Sessions) -> dict[Source, int]:
    """An active primary for every source, inserted in reverse ``SOURCES`` order."""
    return {source: await _dataset(db, source, "active") for source in reversed(SOURCES)}


async def _run(
    db: Sessions,
    source: Source,
    job: str,
    started: datetime,
    *,
    status: str = "succeeded",
    rows: int | None = None,
) -> None:
    """Insert one ``sync_run`` row of (``source``, ``job``) started at ``started``."""
    async with db() as session, session.begin():
        session.add(
            SyncRun(
                source=source,
                job=job,
                dataset=None,
                started_at=started,
                finished_at=None if status == "running" else started,
                status=status,
                rows=rows,
            )
        )


async def _sync_runs(db: Sessions, job: str | None = None) -> list[SyncRun]:
    """Every ``sync_run`` row (of ``job`` when given), in insertion order."""
    query = select(SyncRun).order_by(SyncRun.id)
    if job is not None:
        query = query.where(SyncRun.job == job)
    async with db() as session:
        return list(await session.scalars(query))


async def _request(db: Sessions, source: Source, requested: datetime) -> int:
    """Insert a pending ``sync_request`` of ``source`` and return its id."""
    async with db() as session, session.begin():
        row = SyncRequest(source=source, requested_at=requested)
        session.add(row)
        await session.flush()
        return row.id


async def _picked(db: Sessions) -> dict[Source, datetime | None]:
    """``picked_at`` of every ``sync_request`` row, by source."""
    async with db() as session:
        rows = (await session.execute(select(SyncRequest.source, SyncRequest.picked_at))).all()
    picked: dict[Source, datetime | None] = {}
    for value, at in rows:
        (source,) = [known for known in SOURCES if known == value]
        picked[source] = at
    return picked


async def _dataset_ids(db: Sessions) -> list[int]:
    """The ids of every ``source_dataset`` row still present, ascending."""
    async with db() as session:
        return list(await session.scalars(select(SourceDataset.id).order_by(SourceDataset.id)))


def _purge_rows(rows: Sequence[SyncRun]) -> list[tuple[str, str, int | None, str | None]]:
    """(source, status, rows, error) of each ``purge`` row, in insertion order."""
    return [(row.source, row.status, row.rows, row.error) for row in rows if row.job == JOB_PURGE]


async def _tick(scheduler: Scheduler, clock: _Clock, at: datetime) -> TickReport:
    """Move ``clock`` to ``at`` and run one tick there."""
    clock.at = at
    return await scheduler.tick(at)


# ---------------------------------------------------------------------------------------------
# due_jobs: active sources
# ---------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("quiet_sources")
async def test_due_jobs_skips_sources_without_an_active_primary(db: Sessions) -> None:
    """No dataset, only a building or a retired one, or only a companion: nothing is due."""
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _dataset(db, "linkedin", "building")
    await _dataset(db, "hubspot", "retired", retired_at=NOW - timedelta(days=1))

    assert dict(await scheduler.due_jobs(NOW)) == {}

    linkedin = await _dataset(db, "linkedin", "active")
    await _dataset(db, "hubspot", "active", parent=linkedin)

    due = await scheduler.due_jobs(NOW)

    assert dict(due) == {"linkedin": LINKEDIN_JOBS}
    assert "hubspot" not in due
    assert "google" not in due


@pytest.mark.usefixtures("quiet_sources")
async def test_due_jobs_lists_every_job_of_each_active_source_in_order(db: Sessions) -> None:
    """With no history every scheduled job is due, in schedule order and ``SOURCES`` order."""
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _active_all(db)

    due = await scheduler.due_jobs(NOW)

    assert dict(due) == ALL_DUE
    assert list(due) == ["google", "linkedin", "hubspot"]
    assert dict(await scheduler.due_jobs()) == ALL_DUE  # judged on the scheduler clock


# ---------------------------------------------------------------------------------------------
# due_jobs: history
# ---------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("quiet_sources")
async def test_due_jobs_reads_the_latest_run_of_each_source_and_job(db: Sessions) -> None:
    """A run at the slot satisfies it whatever its status; nothing else does.

    At 13:30 UTC the 3- and 6-hourly slots are 12:00 and the restatement's 03:00. Google's
    failed entities and running metrics at 12:00 satisfy theirs; its restatement 1 s before
    03:00 does not. LinkedIn's entities are due although Google's ran (another source), its
    leads although ``lead_match`` and ``probe`` ran, and HubSpot's ``crm`` although
    ``crm_companion``, ``lead_match``, ``probe``, ``purge`` and a LinkedIn ``crm`` row ran.
    """
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _active_all(db)
    await _run(db, "google", "entities", SLOT_12, status="failed")
    await _run(db, "google", "metrics", SLOT_12, status="running")
    await _run(db, "google", "restatement", SLOT_03 - ONE_SECOND)
    await _run(db, "linkedin", "metrics", SLOT_12)
    await _run(db, "linkedin", "restatement", SLOT_03)
    for job in ("lead_match", "probe", "crm"):
        await _run(db, "linkedin", job, SLOT_12)
    for job in ("crm_companion", "lead_match", "probe", JOB_PURGE):
        await _run(db, "hubspot", job, SLOT_12)

    due = await scheduler.due_jobs(NOW)

    assert dict(due) == {
        "google": ("restatement",),
        "linkedin": ("entities", "leads"),
        "hubspot": ("crm",),
    }

    await _run(db, "google", "restatement", SLOT_03, status="failed")
    await _run(db, "linkedin", "entities", NOW)
    await _run(db, "linkedin", "leads", SLOT_12)
    await _run(db, "hubspot", "crm", SLOT_12, status="running")

    assert dict(await scheduler.due_jobs(NOW)) == {}


@pytest.mark.usefixtures("quiet_sources")
async def test_due_jobs_runs_each_job_once_after_downtime(db: Sessions) -> None:
    """Runs just inside or outside the two-day horizon leave every job due exactly once."""
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _active_all(db)
    horizon = timedelta(days=2)
    for source, names in ALL_DUE.items():
        for name in names:
            await _run(db, source, name, NOW - horizon - ONE_SECOND)
            await _run(db, source, name, NOW - horizon + timedelta(minutes=1))

    assert dict(await scheduler.due_jobs(NOW)) == ALL_DUE

    # The catch-up runs start now: each job satisfied once, until its next slot.
    for source, names in ALL_DUE.items():
        for name in names:
            await _run(db, source, name, NOW)
    assert dict(await scheduler.due_jobs(NOW)) == {}
    assert dict(await scheduler.due_jobs(NOW + timedelta(minutes=89))) == {}
    assert dict(await scheduler.due_jobs(datetime(2026, 10, 5, 15, 0, tzinfo=UTC))) == {
        "google": ("entities",),
        "linkedin": ("entities", "leads"),
        "hubspot": ("crm",),
    }
    assert dict(await scheduler.due_jobs(datetime(2026, 10, 5, 18, 0, tzinfo=UTC))) == {
        "google": ("entities", "metrics"),
        "linkedin": ("entities", "metrics", "leads"),
        "hubspot": ("crm",),
    }


@pytest.mark.usefixtures("quiet_sources")
async def test_due_jobs_judges_slots_on_the_display_timezone(
    db: Sessions, set_env: Callable[..., None]
) -> None:
    """New York's 03:00 restatement is due at 07:30Z (03:30 EDT), not at 06:59Z (02:59 EDT).

    Yesterday's run at 03:00 EDT (07:00Z) satisfies the slot until 03:00 EDT today; in UTC
    the same history is due at 06:59Z, because the UTC 03:00 slot has passed by then.
    """
    await _dataset(db, "google", "active")
    await _run(db, "google", "restatement", datetime(2026, 10, 4, 7, 0, tzinfo=UTC))
    before = datetime(2026, 10, 5, 6, 59, tzinfo=UTC)
    after = datetime(2026, 10, 5, 7, 30, tzinfo=UTC)

    set_env(DISPLAY_TIMEZONE=NEW_YORK)
    in_new_york = _scheduler(_Clock(before))
    set_env(DISPLAY_TIMEZONE="UTC")
    in_utc = _scheduler(_Clock(before))

    assert dict(await in_new_york.due_jobs(before)) == {"google": ("entities", "metrics")}
    assert dict(await in_new_york.due_jobs(after)) == {"google": GOOGLE_JOBS}
    assert dict(await in_utc.due_jobs(before)) == {"google": GOOGLE_JOBS}


# ---------------------------------------------------------------------------------------------
# tick: scheduled dispatch
# ---------------------------------------------------------------------------------------------


async def test_tick_dispatches_the_due_jobs_of_each_source_once(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """Each due source gets one scheduled ``run_source_jobs`` call with its due job names.

    The calls follow google, linkedin, hubspot and carry the scheduler's controller and the
    tick's instant; LinkedIn's segment slot (never run) follows as its own call. The report
    lists the jobs run per source, nothing failed and no step erred.
    """
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _active_all(db)

    report = await _tick(scheduler, clock, NOW)

    assert report.errors == ()
    assert report.now == NOW
    assert report.steps == ("due_jobs", "segments", "purge")
    scheduled = source_jobs.of("scheduled")
    assert source_jobs.calls == scheduled
    assert [(call.source, call.options.get("jobs")) for call in scheduled] == [
        ("google", GOOGLE_JOBS),
        ("linkedin", LINKEDIN_JOBS),
        ("hubspot", HUBSPOT_JOBS),
        ("linkedin", ("segments",)),
    ]
    for call in scheduled[:3]:
        assert set(call.options) == {"jobs", "controller", "now"}, call
    assert set(scheduled[3].options) == {"jobs", "controller", "now", "thresholds"}
    for call in scheduled:
        assert call.options["controller"] is scheduler.controller
        assert call.options["now"] == NOW
    assert dict(report.jobs) == {
        "google": GOOGLE_JOBS,
        "linkedin": (*LINKEDIN_JOBS, "segments"),
        "hubspot": HUBSPOT_JOBS,
    }
    assert dict(report.failed_jobs) == {}
    assert report.sync_requests == ()


async def test_tick_reports_failed_jobs_and_isolates_a_failing_source(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """A ``failed`` result lands in ``failed_jobs``; a source whose call raises is isolated."""
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _active_all(db)
    source_jobs.failing = {("google", "metrics"), ("hubspot", "crm")}

    report = await _tick(scheduler, clock, NOW)

    assert report.errors == ()
    assert dict(report.jobs)["google"] == GOOGLE_JOBS
    assert dict(report.failed_jobs) == {"google": ("metrics",), "hubspot": ("crm",)}

    source_jobs.calls.clear()
    source_jobs.failing = set()
    source_jobs.raising = {"google"}
    later = NOW + timedelta(seconds=30)

    isolated = await _tick(scheduler, clock, later)

    assert isolated.errors == ("due_jobs:google",)
    assert [call.source for call in source_jobs.of("scheduled")] == [
        "google",
        "linkedin",
        "hubspot",
        "linkedin",
    ]
    assert "google" not in isolated.jobs
    assert dict(isolated.jobs)["hubspot"] == HUBSPOT_JOBS


# ---------------------------------------------------------------------------------------------
# The feedback loop on the real simulated stack
# ---------------------------------------------------------------------------------------------


def _controller(db: Sessions, clock: _Clock) -> SourceModeController:
    """A controller on the test database and ``clock``, its throttle and retries on fake time."""
    fake = _FakeTime()
    return SourceModeController(
        get_settings(),
        sessionmaker=db,
        clock=clock,
        retry=RetryPolicy(sleep=fake.sleep),
        hubspot_limiter=RateLimiter(4.0, clock=fake, sleep=fake.sleep),
    )


async def _started_at(db: Sessions, at: datetime) -> tuple[set[tuple[str, str]], set[str]]:
    """The (source, job) pairs of the ``sync_run`` rows started at ``at``, and their statuses.

    A job records one row per freshness dataset it refreshed; the companion CRM sync records
    its rows under ``hubspot``, the companion dataset's source.
    """
    rows = [row for row in await _sync_runs(db) if row.started_at == at]
    return {(row.source, row.job) for row in rows}, {row.status for row in rows}


@pytest.mark.usefixtures("simulated", "workdir")
async def test_simulated_ticks_run_exactly_the_jobs_the_history_leaves_due(db: Sessions) -> None:
    """After the start-up backfills (12:00 UTC) only the 3-hourly jobs run at 15:00; then none.

    The backfills record every job they ran, so metrics (12:00 slot), restatement (03:00),
    segments (04:00) are satisfied at 15:00, while entities, leads (with the simulated
    LinkedIn's companion CRM sync) and HubSpot's ``crm`` run; each run writes its
    ``sync_run`` rows at the tick, and the first purge ever its row. The wake 20 minutes
    later runs nothing, and 18:00 adds the 6-hourly metrics.
    """
    clock = _Clock(T0)
    controller = _controller(db, clock)
    started = await bootstrap_sources(get_settings(), controller=controller)
    assert not started.failures, dict(started.failures)
    backfilled, statuses = await _started_at(db, T0)
    assert backfilled == {
        ("google", "entities"),
        ("google", "metrics"),
        ("google", "restatement"),
        ("linkedin", "entities"),
        ("linkedin", "metrics"),
        ("linkedin", "restatement"),
        ("linkedin", "segments"),
        ("linkedin", "leads"),
        ("hubspot", "crm"),
        ("hubspot", "crm_companion"),
    }
    assert statuses == {"succeeded"}
    scheduler = Scheduler(controller=controller, clock=clock)
    three_hourly = T0 + timedelta(hours=3)

    report = await _tick(scheduler, clock, three_hourly)

    assert report.errors == (), report.errors
    assert dict(report.failed_jobs) == {}
    assert dict(report.jobs) == {
        "google": ("entities",),
        "linkedin": ("entities", "leads", "crm_companion"),
        "hubspot": ("crm",),
    }
    assert report.steps == ("due_jobs", "purge")
    assert await _started_at(db, three_hourly) == (
        {
            ("google", "entities"),
            ("google", "purge"),
            ("linkedin", "entities"),
            ("linkedin", "leads"),
            ("hubspot", "crm_companion"),
            ("hubspot", "crm"),
        },
        {"succeeded"},
    )

    quiet = await _tick(scheduler, clock, three_hourly + timedelta(minutes=20))

    assert quiet.errors == ()
    assert dict(quiet.jobs) == {}
    assert quiet.steps == ()
    assert await _started_at(db, three_hourly + timedelta(minutes=20)) == (set(), set())

    six_hourly = T0 + timedelta(hours=6)
    report = await _tick(scheduler, clock, six_hourly)

    assert report.errors == (), report.errors
    assert dict(report.failed_jobs) == {}
    assert dict(report.jobs) == {
        "google": ("entities", "metrics"),
        "linkedin": ("entities", "metrics", "leads", "crm_companion"),
        "hubspot": ("crm",),
    }
    assert report.steps == ("due_jobs",)
    assert await _started_at(db, six_hourly) == (
        {
            ("google", "entities"),
            ("google", "metrics"),
            ("linkedin", "entities"),
            ("linkedin", "metrics"),
            ("linkedin", "leads"),
            ("hubspot", "crm_companion"),
            ("hubspot", "crm"),
        },
        {"succeeded"},
    )


# ---------------------------------------------------------------------------------------------
# tick: manual dispatch (sync_request)
# ---------------------------------------------------------------------------------------------


async def test_tick_claims_every_pending_request_and_runs_all_jobs_manually(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """One tick claims every request and runs each source manually, in request order.

    No source has a dataset, so nothing is due: the requests still run. Each call carries
    trigger ``manual``, the controller and the tick's instant and no job list (all jobs). A
    claimed request is not claimed again by the next tick.
    """
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _request(db, "google", datetime(2026, 10, 5, 13, 10, tzinfo=UTC))
    await _request(db, "hubspot", datetime(2026, 10, 5, 13, 0, tzinfo=UTC))
    await _request(db, "linkedin", datetime(2026, 10, 5, 13, 5, tzinfo=UTC))

    report = await _tick(scheduler, clock, NOW)

    assert report.errors == ()
    assert report.sync_requests == ("hubspot", "linkedin", "google")
    assert "sync_requests" in report.steps
    assert "due_jobs" not in report.steps
    assert source_jobs.calls == source_jobs.of("manual")
    assert [call.source for call in source_jobs.calls] == ["hubspot", "linkedin", "google"]
    for call in source_jobs.calls:
        assert set(call.options) == {"controller", "now"}, call
        assert call.options["controller"] is scheduler.controller
        assert call.options["now"] == NOW
    assert dict(report.jobs) == {source: jobs.SOURCE_JOBS[source] for source in SOURCES}
    assert await _picked(db) == {"google": NOW, "hubspot": NOW, "linkedin": NOW}

    source_jobs.calls.clear()
    again = await _tick(scheduler, clock, NOW + timedelta(seconds=30))

    assert again.sync_requests == ()
    assert "sync_requests" not in again.steps
    assert source_jobs.calls == []
    assert await _picked(db) == {"google": NOW, "hubspot": NOW, "linkedin": NOW}


async def test_requests_are_served_before_the_due_jobs_of_a_tick(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """Within one tick the manual run precedes the scheduled one, and its step comes first."""
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _dataset(db, "google", "active")
    await _request(db, "google", NOW - timedelta(minutes=1))

    report = await _tick(scheduler, clock, NOW)

    assert [(call.source, call.trigger) for call in source_jobs.calls] == [
        ("google", "manual"),
        ("google", "scheduled"),
    ]
    assert source_jobs.calls[1].options["jobs"] == GOOGLE_JOBS
    assert report.steps.index("sync_requests") < report.steps.index("due_jobs")
    assert dict(report.jobs) == {"google": (*jobs.SOURCE_JOBS["google"], *GOOGLE_JOBS)}


async def test_source_lanes_claim_only_their_own_requests(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """The ``linkedin_hubspot`` lane claims HubSpot's and LinkedIn's rows; google's waits."""
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _request(db, "linkedin", datetime(2026, 10, 5, 13, 0, tzinfo=UTC))
    await _request(db, "google", datetime(2026, 10, 5, 13, 1, tzinfo=UTC))
    await _request(db, "hubspot", datetime(2026, 10, 5, 13, 2, tzinfo=UTC))

    shared = await scheduler._lane("linkedin_hubspot")

    assert shared.sync_requests == ("linkedin", "hubspot")
    assert [(call.source, call.trigger) for call in source_jobs.calls] == [
        ("linkedin", "manual"),
        ("hubspot", "manual"),
    ]
    assert await _picked(db) == {"google": None, "hubspot": NOW, "linkedin": NOW}

    clock.at = NOW + timedelta(seconds=30)
    own = await scheduler._lane("google")

    assert own.sync_requests == ("google",)
    assert [call.source for call in source_jobs.calls] == ["linkedin", "hubspot", "google"]
    assert await _picked(db) == {"google": clock.at, "hubspot": NOW, "linkedin": NOW}


# ---------------------------------------------------------------------------------------------
# tick: the nightly purge
# ---------------------------------------------------------------------------------------------

PURGE_FINISH: Final = (
    "event",
    "log_level",
    "source",
    "status",
    "rows",
    "by_source",
    "calls",
    "error",
    "error_type",
)
"""The fields of the purge's ``sync.job.finish`` compared exactly (the duration varies)."""


def _job_lines(entries: Sequence[MutableMapping[str, Any]], job: str) -> list[dict[str, Any]]:
    """The captured ``sync.job.start`` and ``sync.job.finish`` records of ``job``, in order."""
    return [
        dict(entry)
        for entry in entries
        if entry.get("event") in {"sync.job.start", "sync.job.finish"} and entry.get("job") == job
    ]


def _fields(record: Mapping[str, Any], *names: str) -> dict[str, Any]:
    """``record`` restricted to ``names``; a missing field reads as the sentinel ``"<absent>"``."""
    return {name: record.get(name, "<absent>") for name in names}


@pytest.mark.usefixtures("new_york")
async def test_purge_deletes_only_primaries_retired_more_than_30_days_ago(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """At 05:00 EDT the purge removes primaries retired over 30 days ago, companions with them.

    Retired 31 days ago: a Google primary and a LinkedIn primary with its HubSpot companion;
    30 days and 1 s ago: a HubSpot primary. Kept: retired exactly 30 and 29 days ago, an active
    and a building primary. One succeeded ``purge`` row per source purged counts its
    datasets. The wake ten minutes later purges nothing and writes nothing.
    """
    del source_jobs  # Requested so the active primary's due jobs run nothing real.
    clock = _Clock(NY_PURGE)
    scheduler = _scheduler(clock)
    await _run(db, "google", JOB_PURGE, NY_PURGE - timedelta(days=1), rows=0)
    old = NY_PURGE - timedelta(days=31)
    google_old = await _dataset(db, "google", "retired", retired_at=old)
    linkedin_old = await _dataset(db, "linkedin", "retired", retired_at=old)
    companion = await _dataset(db, "hubspot", "retired", retired_at=old, parent=linkedin_old)
    exactly_30 = await _dataset(db, "hubspot", "retired", retired_at=NY_PURGE - RETENTION)
    days_29 = await _dataset(db, "google", "retired", retired_at=NY_PURGE - timedelta(days=29))
    active = await _dataset(db, "hubspot", "active")
    building = await _dataset(db, "linkedin", "building")
    hubspot_old = await _dataset(
        db, "hubspot", "retired", retired_at=NY_PURGE - RETENTION - ONE_SECOND
    )

    with capture_logs() as entries:
        report = await _tick(scheduler, clock, NY_PURGE)

    assert report.errors == ()
    assert "purge" in report.steps
    assert report.purged == (google_old, linkedin_old, hubspot_old)
    # AAP 0.11.1: the purge logs its start and one finish as every sync job does.
    start, finish = _job_lines(entries, JOB_PURGE)
    assert _fields(start, "event", "log_level", "source", "rows", "calls", "duration_ms") == {
        "event": "sync.job.start",
        "log_level": "info",
        "source": "all",
        "rows": 0,
        "calls": 0,
        "duration_ms": 0,
    }
    assert _fields(finish, *PURGE_FINISH) == {
        "event": "sync.job.finish",
        "log_level": "info",
        "source": "all",
        "status": "succeeded",
        "rows": 3,
        "by_source": {"google": 1, "linkedin": 1, "hubspot": 1},
        "calls": 0,
        "error": None,
        "error_type": None,
    }
    assert isinstance(finish["duration_ms"], int)
    assert finish["duration_ms"] >= 0
    assert await _dataset_ids(db) == [exactly_30, days_29, active, building]
    assert companion not in await _dataset_ids(db)
    rows = await _sync_runs(db, JOB_PURGE)
    assert _purge_rows(rows[1:]) == [
        ("google", "succeeded", 1, None),
        ("linkedin", "succeeded", 1, None),
        ("hubspot", "succeeded", 1, None),
    ]
    assert {(row.started_at, row.finished_at, row.dataset) for row in rows[1:]} == {
        (NY_PURGE, NY_PURGE, None)
    }

    with capture_logs() as quiet:
        again = await _tick(scheduler, clock, NY_PURGE + timedelta(minutes=10))

    assert again.purged == ()
    assert "purge" not in again.steps
    assert len(await _sync_runs(db, JOB_PURGE)) == len(rows)
    assert _job_lines(quiet, JOB_PURGE) == [], "a purge not due logs no job lines"


@pytest.mark.usefixtures("new_york")
async def test_purge_slot_recurs_at_05_00_new_york_time(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """Not due at 04:59 EDT the next day; due at 05:00, when the 30-day-old one is 31 days old."""
    del source_jobs  # Requested so no real job runs.
    clock = _Clock(NY_PURGE)
    scheduler = _scheduler(clock)
    await _run(db, "google", JOB_PURGE, NY_PURGE - timedelta(days=1), rows=0)
    exactly_30 = await _dataset(db, "hubspot", "retired", retired_at=NY_PURGE - RETENTION)
    days_29 = await _dataset(db, "google", "retired", retired_at=NY_PURGE - timedelta(days=29))

    first = await _tick(scheduler, clock, NY_PURGE)
    assert (first.purged, first.errors) == ((), ())
    before = await _tick(scheduler, clock, NY_PURGE + timedelta(days=1) - timedelta(minutes=1))
    assert "purge" not in before.steps
    assert await _dataset_ids(db) == [exactly_30, days_29]

    next_day = await _tick(scheduler, clock, NY_PURGE + timedelta(days=1))

    assert "purge" in next_day.steps
    assert next_day.purged == (exactly_30,)
    assert await _dataset_ids(db) == [days_29]
    rows = await _sync_runs(db, JOB_PURGE)
    assert [(row.source, row.rows, row.started_at) for row in rows] == [
        ("google", 0, NY_PURGE - timedelta(days=1)),
        ("google", 0, NY_PURGE),
        ("hubspot", 1, NY_PURGE + timedelta(days=1)),
    ]


@pytest.mark.usefixtures("new_york")
async def test_purge_with_nothing_to_delete_records_one_google_row(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """Nothing purgeable: one ``google`` row with ``rows`` 0 satisfies the slot."""
    del source_jobs  # Requested so no real job runs.
    clock = _Clock(NY_PURGE)
    scheduler = _scheduler(clock)
    await _run(db, "google", JOB_PURGE, NY_PURGE - timedelta(days=1), rows=0)
    await _dataset(db, "google", "active")

    report = await _tick(scheduler, clock, NY_PURGE)

    assert "purge" in report.steps
    assert report.purged == ()
    rows = await _sync_runs(db, JOB_PURGE)
    assert _purge_rows(rows) == [("google", "succeeded", 0, None), ("google", "succeeded", 0, None)]
    assert rows[1].started_at == NY_PURGE

    later = await _tick(scheduler, clock, NY_PURGE + timedelta(minutes=30))

    assert "purge" not in later.steps
    assert len(await _sync_runs(db, JOB_PURGE)) == 2


@pytest.mark.usefixtures("new_york")
async def test_purge_row_of_any_source_satisfies_the_slot(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """A HubSpot ``purge`` row at today's 05:00 EDT keeps a purgeable Google dataset in place."""
    del source_jobs  # Requested so no real job runs.
    clock = _Clock(NY_PURGE)
    scheduler = _scheduler(clock)
    await _run(db, "hubspot", JOB_PURGE, NY_PURGE, rows=0)
    purgeable = await _dataset(db, "google", "retired", retired_at=NY_PURGE - timedelta(days=31))

    report = await _tick(scheduler, clock, NY_PURGE + timedelta(minutes=10))

    assert "purge" not in report.steps
    assert report.purged == ()
    assert await _dataset_ids(db) == [purgeable]
    assert len(await _sync_runs(db, JOB_PURGE)) == 1


@pytest.mark.usefixtures("new_york")
async def test_first_purge_ever_runs_at_once_whatever_the_hour(
    db: Sessions, source_jobs: SourceJobs
) -> None:
    """With no ``purge`` history the first tick purges (14:00 EDT); then 05:00 EDT again."""
    del source_jobs  # Requested so no real job runs.
    afternoon = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)  # 14:00 EDT
    clock = _Clock(afternoon)
    scheduler = _scheduler(clock)
    purgeable = await _dataset(db, "linkedin", "retired", retired_at=afternoon - timedelta(days=40))

    report = await _tick(scheduler, clock, afternoon)

    assert report.purged == (purgeable,)
    assert await _dataset_ids(db) == []
    rows = await _sync_runs(db, JOB_PURGE)
    assert [(row.source, row.rows, row.started_at) for row in rows] == [("linkedin", 1, afternoon)]
    hour_later = await _tick(scheduler, clock, afternoon + timedelta(hours=1))
    assert "purge" not in hour_later.steps
    next_slot = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    before = await _tick(scheduler, clock, next_slot - timedelta(minutes=1))
    assert "purge" not in before.steps
    at_slot = await _tick(scheduler, clock, next_slot)
    assert "purge" in at_slot.steps
    assert len(await _sync_runs(db, JOB_PURGE)) == 2


@pytest.mark.usefixtures("new_york")
async def test_failed_purge_is_recorded_and_not_retried_before_the_next_slot(
    db: Sessions, source_jobs: SourceJobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database error in the purge is isolated, recorded ``failed`` and not retried today."""
    del source_jobs  # Requested so no real job runs.
    calls: list[timedelta] = []

    async def failing_purge(self: DatasetManager, older_than: timedelta = RETENTION) -> list[int]:
        del self  # The failure does not depend on the manager.
        calls.append(older_than)
        raise OperationalError("DELETE", {}, ConnectionError("database restarting"))

    monkeypatch.setattr(DatasetManager, "purge_retired", failing_purge)
    clock = _Clock(NY_PURGE)
    scheduler = _scheduler(clock)
    await _run(db, "google", JOB_PURGE, NY_PURGE - timedelta(days=1), rows=0)
    purgeable = await _dataset(db, "google", "retired", retired_at=NY_PURGE - timedelta(days=31))

    with capture_logs() as entries:
        report = await _tick(scheduler, clock, NY_PURGE)

    assert report.errors == ("purge",)
    assert report.purged == ()
    assert calls == [RETENTION]
    start, finish = _job_lines(entries, JOB_PURGE)
    assert (start["event"], start["source"]) == ("sync.job.start", "all")
    assert _fields(finish, *PURGE_FINISH) == {
        "event": "sync.job.finish",
        "log_level": "error",
        "source": "all",
        "status": "failed",
        "rows": 0,
        "by_source": {},
        "calls": 0,
        "error": "OperationalError",
        "error_type": "OperationalError",
    }
    rows = await _sync_runs(db, JOB_PURGE)
    assert _purge_rows(rows[1:]) == [("google", "failed", None, "OperationalError")]
    assert rows[1].started_at == NY_PURGE
    assert await _dataset_ids(db) == [purgeable]

    retry = await _tick(scheduler, clock, NY_PURGE + timedelta(minutes=10))

    assert retry.errors == ()
    assert "purge" not in retry.steps
    assert calls == [RETENTION]
    assert len(await _sync_runs(db, JOB_PURGE)) == 2


# ---------------------------------------------------------------------------------------------
# tick: recovery probes
# ---------------------------------------------------------------------------------------------

PROBE_SPACING: Final = timedelta(minutes=30)
"""AAP 0.3.2: a recovery probe every 30 min while a source is in automatic fallback."""

PROBE_TAKES: Final = timedelta(minutes=2)
"""How far the fake ``recover`` moves the clock when asked to, as a probe and build would."""

NOT_IN_FALLBACK: Final = "source is no longer in automatic fallback; nothing was probed"
NOT_ACTIVATED: Final = "live dataset was not activated"
"""The ``probe`` row's error without an outcome, and for an outcome without a reason."""

PROBE_DENIED: Final = "google search Q-G1: HTTP 403: The caller does not have permission"
"""A Q-G1 denial as the connector words it; the reason names its query, so no prefix."""

PROBE_FAILED: Final = BuildOutcome(
    source="google",
    mode="live",
    dataset_id=None,
    activated=False,
    reason=PROBE_DENIED,
    probe=ProbeReport.of(
        "google", (ProbeCheck("Q-G1", False, PROBE_DENIED),), calls=1, duration_ms=12
    ),
)
"""``recover``'s outcome for a recovery probe that did not pass: no dataset, its reason."""


class Recovery:
    """Stands in for ``SourceModeController.recover``: records each source, returns an outcome.

    Every call returns :attr:`outcome`, by default :data:`PROBE_FAILED` (the probe did not
    pass; ``None``: the source was no longer in fallback). The probe report an outcome
    carries is first set on the scheduler's row log, as ``recover`` does once its probe
    returns. When :attr:`clock` is set, each call first moves it on by :attr:`takes`, as a
    probe and a live build that take time would, so a row's close is told apart from its
    start.
    """

    def __init__(self) -> None:
        self.calls: list[Source] = []
        self.outcome: BuildOutcome | None = PROBE_FAILED
        self.clock: _Clock | None = None
        self.takes = timedelta(0)

    async def __call__(self, source: Source, run: ProbeRunLog | None = None) -> BuildOutcome | None:
        """Record ``source``, move the clock on when one is set, and return :attr:`outcome`."""
        self.calls.append(source)
        if run is not None and self.outcome is not None and self.outcome.probe is not None:
            run.report = self.outcome.probe
        if self.clock is not None:
            self.clock.at += self.takes
        return self.outcome


@pytest.fixture
def recovery(monkeypatch: pytest.MonkeyPatch, source_jobs: SourceJobs) -> Recovery:
    """Google alone is in automatic fallback, and ``recover`` is a :class:`Recovery` fake.

    ``source_jobs`` (so no real job runs) applies ``quiet_sources`` first; its ``in_fallback``
    patch is replaced here.
    """
    del source_jobs  # Requested so its patches are applied first.
    fake = Recovery()

    async def google_in_fallback(self: SourceModeController, source: Source) -> bool:
        del self  # Only the source decides here.
        return source == "google"

    async def recover(
        self: SourceModeController, source: Source, *, run: ProbeRunLog | None = None
    ) -> BuildOutcome | None:
        del self  # The fake decides alone.
        return await fake(source, run)

    monkeypatch.setattr(SourceModeController, "in_fallback", google_in_fallback)
    monkeypatch.setattr(SourceModeController, "recover", recover)
    return fake


async def _probe_starts(db: Sessions) -> list[tuple[str, datetime]]:
    """(source, started_at) of every ``probe`` row, in insertion order."""
    return [(row.source, row.started_at) for row in await _sync_runs(db, JOB_PROBE)]


async def test_first_probe_runs_at_once_and_its_row_spaces_the_next_wake(
    db: Sessions, recovery: Recovery
) -> None:
    """With no ``probe`` history the first tick probes Google, and Google alone, once.

    The ``probe`` row starts at the tick and is closed at the clock's reading once ``recover``
    returned, two minutes on; a probe that did not pass ends it ``failed`` with the deciding
    check's reason. The next
    wake, 30 s after that, probes nothing: the row spaces it. Today's purge has already run
    (a HubSpot ``purge`` row at 05:00 UTC), so probing is all either tick does and no
    Google ``purge`` row stands in for the ``probe`` row.
    """
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _run(db, "hubspot", JOB_PURGE, datetime(2026, 10, 5, 5, 0, tzinfo=UTC), rows=0)
    recovery.clock = clock
    recovery.takes = PROBE_TAKES

    report = await _tick(scheduler, clock, NOW)

    assert report.errors == ()
    assert report.probed == ("google",)
    assert report.steps == ("probes",)
    assert recovery.calls == ["google"]
    (row,) = await _sync_runs(db, JOB_PROBE)
    assert (row.source, row.started_at, row.status, row.error) == (
        "google",
        NOW,
        "failed",
        PROBE_DENIED,
    )
    assert row.finished_at == clock.at == NOW + PROBE_TAKES

    next_wake = await _tick(scheduler, clock, NOW + PROBE_TAKES + timedelta(seconds=30))

    assert next_wake.errors == ()
    assert next_wake.probed == ()
    assert next_wake.steps == ()
    assert recovery.calls == ["google"]
    assert await _probe_starts(db) == [("google", NOW)]


@pytest.mark.parametrize("status", ["succeeded", "failed", "running"])
async def test_probes_are_spaced_30_minutes_from_the_latest_probe_start(
    db: Sessions, recovery: Recovery, status: str
) -> None:
    """The latest Google ``probe`` row, whatever its status, spaces the next probe 30 min.

    With an older row an hour before it, nothing is probed 29 min 59 s after it; at exactly
    30 min one probe runs, and its own row then spaces the wake 29 min 59 s later.
    """
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _run(db, "google", JOB_PROBE, NOW - 2 * PROBE_SPACING, status="failed")
    await _run(db, "google", JOB_PROBE, NOW, status=status)

    early = await _tick(scheduler, clock, NOW + PROBE_SPACING - ONE_SECOND)

    assert early.errors == ()
    assert early.probed == ()
    assert "probes" not in early.steps
    assert recovery.calls == []

    due = NOW + PROBE_SPACING
    on_time = await _tick(scheduler, clock, due)

    assert on_time.errors == ()
    assert on_time.probed == ("google",)
    assert "probes" in on_time.steps
    assert recovery.calls == ["google"]

    spaced = await _tick(scheduler, clock, due + PROBE_SPACING - ONE_SECOND)

    assert spaced.probed == ()
    assert recovery.calls == ["google"]
    assert await _probe_starts(db) == [
        ("google", NOW - 2 * PROBE_SPACING),
        ("google", NOW),
        ("google", due),
    ]


async def test_only_sources_in_fallback_are_probed_each_spaced_by_its_own_rows(
    db: Sessions, recovery: Recovery
) -> None:
    """LinkedIn and HubSpot, not in fallback, are never probed, with or without history.

    A LinkedIn ``probe`` row and a Google ``entities`` row started at the tick do not space
    Google's probe: only Google's own ``probe`` rows do. Over an hour of 30-minute ticks
    Google alone is probed, at each one.
    """
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    await _run(db, "linkedin", JOB_PROBE, NOW)
    await _run(db, "google", "entities", NOW)
    ticks = (NOW, NOW + PROBE_SPACING, NOW + 2 * PROBE_SPACING)

    reports = [await _tick(scheduler, clock, at) for at in ticks]

    assert [report.errors for report in reports] == [(), (), ()]
    assert [report.probed for report in reports] == [("google",), ("google",), ("google",)]
    assert recovery.calls == ["google", "google", "google"]
    assert await _probe_starts(db) == [("linkedin", NOW)] + [("google", at) for at in ticks]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(
            BuildOutcome(
                source="google", mode="live", dataset_id=7, activated=True, reason=REASON_RECOVERED
            ),
            ("succeeded", None),
            id="live_activated",
        ),
        pytest.param(
            BuildOutcome(
                source="google",
                mode="live",
                dataset_id=7,
                activated=False,
                reason="access denied during backfill",
            ),
            ("failed", "access denied during backfill"),
            id="live_not_activated",
        ),
        pytest.param(
            BuildOutcome(
                source="google",
                mode="simulated",
                dataset_id=8,
                activated=True,
                reason=REASON_NO_CREDENTIALS,
            ),
            ("failed", REASON_NO_CREDENTIALS),
            id="simulated_activated",
        ),
        pytest.param(
            BuildOutcome(source="google", mode="live", dataset_id=7, activated=False, reason=None),
            ("failed", NOT_ACTIVATED),
            id="live_not_activated_without_reason",
        ),
        pytest.param(
            BuildOutcome(
                source="google", mode="simulated", dataset_id=8, activated=True, reason=None
            ),
            ("failed", NOT_ACTIVATED),
            id="simulated_activated_without_reason",
        ),
        pytest.param(PROBE_FAILED, ("failed", PROBE_DENIED), id="probe_not_passed"),
        pytest.param(None, ("failed", NOT_IN_FALLBACK), id="no_longer_in_fallback"),
    ],
)
async def test_probe_row_succeeds_only_when_a_live_dataset_was_activated(
    db: Sessions,
    recovery: Recovery,
    outcome: BuildOutcome | None,
    expected: tuple[str, str | None],
) -> None:
    """An activated live dataset ends the ``probe`` row ``succeeded`` with no error.

    A build not activated, or a simulated one, ends it ``failed`` with the outcome's reason,
    or ``live dataset was not activated`` without one; a probe that did not pass ends it
    ``failed`` with its deciding check's reason, and no outcome (the source left fallback
    first) with ``source is no longer in automatic fallback; nothing was probed``. The row
    is opened at the tick and closed at the clock.
    """
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    recovery.outcome = outcome

    report = await _tick(scheduler, clock, NOW)

    assert report.errors == ()
    assert report.probed == ("google",)
    assert recovery.calls == ["google"]
    (row,) = await _sync_runs(db, JOB_PROBE)
    assert (row.status, row.error) == expected
    assert (row.source, row.started_at, row.finished_at) == ("google", NOW, NOW)


PROBE_START: Final = (
    "event",
    "log_level",
    "source",
    "job",
    "run_id",
    "dataset_id",
    "mode",
    "rows",
    "calls",
    "duration_ms",
)
PROBE_FINISH: Final = (
    "event",
    "log_level",
    "source",
    "job",
    "run_id",
    "dataset_id",
    "status",
    "rows",
    "checks",
    "calls",
    "error",
    "error_type",
)
"""The fields of a probe's ``sync.job`` lines compared exactly (the finish's duration varies)."""


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(
            BuildOutcome(
                source="google",
                mode="live",
                dataset_id=7,
                activated=True,
                reason=REASON_RECOVERED,
                probe=ProbeReport.of("google", (ProbeCheck("Q-G1", True),), calls=1, duration_ms=9),
            ),
            ("info", "succeeded", 7, 1, 1, None),
            id="live_activated",
        ),
        pytest.param(
            PROBE_FAILED, ("error", "failed", None, 1, 1, PROBE_DENIED), id="probe_not_passed"
        ),
        pytest.param(
            None, ("error", "failed", None, 0, 0, NOT_IN_FALLBACK), id="no_longer_in_fallback"
        ),
    ],
)
async def test_each_recovery_probe_logs_its_start_and_one_finish(
    db: Sessions,
    recovery: Recovery,
    outcome: BuildOutcome | None,
    expected: tuple[str, str, int | None, int, int, str | None],
) -> None:
    """AAP 0.11.1: a recovery probe logs ``sync.job.start`` and one ``sync.job.finish``.

    The start names the source, job ``probe``, its row, no dataset, mode ``live`` and zero
    rows, calls and duration. The finish adds the dataset built, the status the row was
    closed with, zero rows, the probe's checks and calls (none without a probe report), the
    duration and the row's own error: for a probe that did not pass, its deciding check's
    reason, never a fixed text without the cause.
    """
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)
    recovery.outcome = outcome
    level, status, dataset_id, checks, calls, error = expected

    with capture_logs() as entries:
        report = await _tick(scheduler, clock, NOW)

    assert report.probed == ("google",)
    (row,) = await _sync_runs(db, JOB_PROBE)
    assert (row.status, row.error) == (status, error)
    start, finish = _job_lines(entries, JOB_PROBE)
    assert _fields(start, *PROBE_START) == {
        "event": "sync.job.start",
        "log_level": "info",
        "source": "google",
        "job": JOB_PROBE,
        "run_id": row.id,
        "dataset_id": None,
        "mode": "live",
        "rows": 0,
        "calls": 0,
        "duration_ms": 0,
    }
    assert _fields(finish, *PROBE_FINISH) == {
        "event": "sync.job.finish",
        "log_level": level,
        "source": "google",
        "job": JOB_PROBE,
        "run_id": row.id,
        "dataset_id": dataset_id,
        "status": status,
        "rows": 0,
        "checks": checks,
        "calls": calls,
        "error": error,
        "error_type": None,
    }
    assert isinstance(finish["duration_ms"], int)
    assert finish["duration_ms"] >= 0


async def test_a_recovery_probe_that_raises_logs_one_failed_finish(
    db: Sessions, recovery: Recovery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database error in ``recover`` closes the row with its class name and logs it once.

    The step is isolated as before; the finish line is ``failed`` at error level with the
    error's class name as error and error type, and no checks or calls (no report came back).
    """
    del recovery  # Requested so Google alone is in automatic fallback.

    async def broken(
        self: SourceModeController, source: Source, *, run: ProbeRunLog | None = None
    ) -> BuildOutcome | None:
        del self, source, run  # The failure, before any probe, depends on none of them.
        raise OperationalError("SELECT", {}, ConnectionError("database restarting"))

    monkeypatch.setattr(SourceModeController, "recover", broken)
    clock = _Clock(NOW)
    scheduler = _scheduler(clock)

    with capture_logs() as entries:
        report = await _tick(scheduler, clock, NOW)

    assert report.errors == ("probes:google",)
    (row,) = await _sync_runs(db, JOB_PROBE)
    assert (row.status, row.error) == ("failed", "OperationalError")
    start, finish = _job_lines(entries, JOB_PROBE)
    assert (start["event"], start["run_id"]) == ("sync.job.start", row.id)
    assert _fields(finish, *PROBE_FINISH) == {
        "event": "sync.job.finish",
        "log_level": "error",
        "source": "google",
        "job": JOB_PROBE,
        "run_id": row.id,
        "dataset_id": None,
        "status": "failed",
        "rows": 0,
        "checks": 0,
        "calls": 0,
        "error": "OperationalError",
        "error_type": "OperationalError",
    }
