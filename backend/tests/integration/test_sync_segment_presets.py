"""A changed ``window.presets`` is fetched once, on the presets detected, by a running worker.

AAP 0.3.2, "LinkedIn demographic segments": segments are fetched whenever ``window.presets``
changes (checked on every worker wake), for each preset in ``window.presets``, and every preset
ends on the default window end. The worker keeps one ``SourceModeController`` for its whole
life, so the configuration it fetches with must be the one its due check read:

* :func:`test_controller_reads_the_configuration_on_every_access`: a controller without an
  injected configuration sees an edited ``window.presets`` on its next ``thresholds`` access.
* :func:`test_injected_thresholds_are_returned_unchanged`: an injected configuration is
  returned as given, whatever the files say.
* :func:`test_contexts_and_source_runs_use_the_given_snapshot`: ``JobContext.for_dataset`` and
  ``run_source_jobs`` run on the ``thresholds`` snapshot they are handed, not the controller's.
* :func:`test_scheduler_fetches_each_preset_change_once`: on the simulated stack, after the
  start-up backfill, adding a 21-day preset and later a 42-day one each makes exactly one
  scheduler segment step fetch the new window ending on the default end and record the new
  presets; the wakes between and after fetch nothing.

The configuration is resolved from the working directory (``./config``), so each test runs in
its own directory holding a copy of ``config/`` and, for the simulator's contract validation, a
``tests`` link to this suite (``./tests/contracts``). Edits go to that copy only. Every clock is
fixed; the HubSpot throttle and the retry policy run on fake time, so no test sleeps.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.analysis.thresholds import THRESHOLDS_FILE, Thresholds
from ads_engine.connectors.http import RetryPolicy
from ads_engine.connectors.hubspot.client import RateLimiter
from ads_engine.db import repositories
from ads_engine.db.models import FactSegmentWindow
from ads_engine.settings import SOURCES, get_settings
from ads_engine.sync import freshness, jobs
from ads_engine.sync.jobs import JobContext
from ads_engine.sync.mode import SourceModeController, bootstrap_sources
from ads_engine.sync.scheduler import Scheduler

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]

BACKEND_DIR: Final = Path(__file__).resolve().parents[2]
TESTS_DIR: Final = BACKEND_DIR / "tests"
"""The suite whose ``contracts/`` the simulator validates every request and payload against."""

T0: Final = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
"""The worker's start: every dataset is built and backfilled at this instant."""

WAKE_GAP: Final = timedelta(minutes=20)
"""Between the scheduler wakes of a test. Every wake falls between 12:20 and 13:40 UTC, after
the 12:00 slots the start-up backfills satisfied and before the next one (15:00), so only the
segment trigger can make a LinkedIn job due."""

DEFAULT_END: Final = date(2026, 10, 1)
"""The default window end at every instant here: yesterday UTC, and yesterday in any Google
account zone, because every instant falls on 2026-10-02 between 12:00 and 15:00 UTC."""

BACKFILL_DAYS: Final = "35"
"""Covers the default 28-day window while keeping every backfill small."""

SHIPPED: Final = (14, 28, 56, 90)
"""``window.presets`` of the shipped ``config/thresholds.yaml``."""

WITH_21: Final = (14, 21, 28, 56, 90)
WITH_21_AND_42: Final = (14, 21, 28, 42, 56, 90)
"""The two successive edits: a second edit proves no snapshot of the first is kept."""

SEGMENTS: Final = "linkedin.segments"


class ThresholdsFactory(Protocol):
    """The call shape of the root ``make_thresholds`` fixture, for type hints only."""

    def __call__(self, overrides: Mapping[str, object] | None = None) -> Thresholds:
        """Write an overridden copy of ``config/`` and load it."""
        ...

    def write(self, overrides: Mapping[str, object] | None = None) -> Path:
        """Write an overridden copy of ``config/`` and return its directory, unloaded."""
        ...


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


class Workdir:
    """The test's working directory and its live ``config/`` copy.

    Attributes:
        config: The ``config`` directory every ``Thresholds.load()`` here resolves.
    """

    def __init__(self, config: Path, factory: ThresholdsFactory) -> None:
        self.config = config
        self._factory = factory

    def set_presets(self, presets: Sequence[int]) -> None:
        """Edit ``window.presets`` of the live copy, as an operator edits the mounted file.

        The edited file is produced by the ``make_thresholds`` fixture, which rejects a key the
        shipped file does not define, and then replaces the live ``thresholds.yaml`` whole.
        """
        edited = self._factory.write({"window.presets": list(presets)})
        shutil.copyfile(edited / THRESHOLDS_FILE, self.config / THRESHOLDS_FILE)


@pytest.fixture
def workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_thresholds: ThresholdsFactory
) -> Workdir:
    """Run the test in ``tmp_path``: a ``config/`` copy and a ``tests`` link to this suite.

    ``make_thresholds`` writes its first, unedited copy to ``tmp_path / "config"``, the
    directory ``resolve_config_dir`` picks from this working directory.
    """
    config = make_thresholds.write()
    assert config == tmp_path / "config"
    (tmp_path / "tests").symlink_to(TESTS_DIR, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    assert Thresholds.load().presets == SHIPPED
    return Workdir(config, make_thresholds)


@pytest.fixture
def simulated(set_env: Callable[..., None]) -> None:
    """Force simulated data with a short backfill; the conftest baseline removes credentials."""
    set_env(DATA_MODE="simulated", BACKFILL_DAYS=BACKFILL_DAYS)
    settings = get_settings()
    assert not [source for source in SOURCES if settings.has_credentials(source)]


def _controller(
    db: Sessions, clock: _Clock, *, thresholds: Thresholds | None = None
) -> SourceModeController:
    """A controller on the test database and ``clock``, its throttle and retries on fake time."""
    fake = _FakeTime()
    return SourceModeController(
        get_settings(),
        sessionmaker=db,
        clock=clock,
        retry=RetryPolicy(sleep=fake.sleep),
        hubspot_limiter=RateLimiter(4.0, clock=fake, sleep=fake.sleep),
        thresholds=thresholds,
    )


async def _linkedin_dataset(db: Sessions) -> int:
    """The active LinkedIn primary dataset's id; it must exist."""
    async with db() as session:
        dataset_id = await repositories.active_dataset_id(session, "linkedin")
    assert dataset_id is not None, "LinkedIn has no active dataset"
    return dataset_id


async def _segment_reason(db: Sessions, dataset_id: int) -> str | None:
    """The ``linkedin.segments`` freshness reason of ``dataset_id``; the row must exist."""
    async with db() as session:
        row = (await freshness.load_freshness(session, dataset_id)).get(SEGMENTS)
    assert row is not None, "no linkedin.segments freshness row"
    assert row.covered_through == DEFAULT_END
    return row.reason


async def _window_lengths(db: Sessions, dataset_id: int) -> set[int]:
    """The lengths in days of the stored segment windows; every one must end on the default end."""
    async with db() as session:
        windows = (
            await session.execute(
                select(FactSegmentWindow.window_start, FactSegmentWindow.window_end)
                .where(FactSegmentWindow.dataset_id == dataset_id)
                .distinct()
            )
        ).all()
    assert {end for _, end in windows} == {DEFAULT_END}, windows
    return {(end - start).days + 1 for start, end in windows}


async def _due(db: Sessions, at: datetime) -> bool:
    """``segments_due`` as the next wake judges it: on the configuration files as they are now."""
    async with db() as session:
        return await jobs.segments_due(session, now=at)


# ---------------------------------------------------------------------------------------------
# The controller's configuration
# ---------------------------------------------------------------------------------------------


def test_controller_reads_the_configuration_on_every_access(workdir: Workdir) -> None:
    """A controller kept across an edit of ``window.presets`` sees the edit on its next access."""
    controller = SourceModeController(get_settings())
    first = controller.thresholds
    assert first.presets == SHIPPED

    workdir.set_presets(WITH_21)
    second = controller.thresholds
    assert second.presets == WITH_21
    assert second.config_hash != first.config_hash

    workdir.set_presets(WITH_21_AND_42)
    assert controller.thresholds.presets == WITH_21_AND_42


def test_injected_thresholds_are_returned_unchanged(
    workdir: Workdir, make_thresholds: ThresholdsFactory
) -> None:
    """An injected configuration is the test seam: returned as given, whatever the files say."""
    injected = make_thresholds({"window.presets": [14, 28, 35, 56, 90]})
    controller = SourceModeController(get_settings(), thresholds=injected)

    workdir.set_presets(WITH_21)

    assert Thresholds.load().presets == WITH_21
    assert controller.thresholds is injected
    assert controller.thresholds is injected


# ---------------------------------------------------------------------------------------------
# One snapshot from the caller to every job
# ---------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("simulated")
async def test_contexts_and_source_runs_use_the_given_snapshot(
    db: Sessions, workdir: Workdir, make_thresholds: ThresholdsFactory
) -> None:
    """``for_dataset`` and ``run_source_jobs`` run on the snapshot passed, not the controller's.

    The controller carries the shipped presets as an injected configuration; the snapshot
    passed adds a 21-day preset. The segment fetch stores the 21-day window ending on the
    default end and records the snapshot's presets.
    """
    del workdir  # Requested for the working directory and its configuration copy.
    clock = _Clock(T0)
    shipped = Thresholds.load()
    controller = _controller(db, clock, thresholds=shipped)
    built = await controller.ensure_dataset("linkedin")
    assert built is not None
    assert built.activated
    dataset_id = await _linkedin_dataset(db)
    assert await _segment_reason(db, dataset_id) == jobs.segment_reason(SHIPPED, DEFAULT_END)

    snapshot = make_thresholds({"window.presets": list(WITH_21)})
    async with controller.open_clients("linkedin", "simulated") as clients:
        given = await JobContext.for_dataset(controller, dataset_id, clients, thresholds=snapshot)
        default = await JobContext.for_dataset(controller, dataset_id, clients)
    assert given.thresholds is snapshot
    assert default.thresholds is shipped

    clock.at = T0 + timedelta(hours=1)
    report = await jobs.run_source_jobs(
        "linkedin",
        trigger="manual",
        jobs=(jobs.JOB_SEGMENTS,),
        controller=controller,
        now=clock.at,
        thresholds=snapshot,
    )

    assert report.ok, report.to_json()
    assert [result.job for result in report.results] == [jobs.JOB_SEGMENTS]
    assert await _segment_reason(db, dataset_id) == jobs.segment_reason(WITH_21, DEFAULT_END)
    assert await _window_lengths(db, dataset_id) == set(WITH_21)


# ---------------------------------------------------------------------------------------------
# The scheduler's segment step on a long-lived controller
# ---------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("simulated")
async def test_scheduler_fetches_each_preset_change_once(db: Sessions, workdir: Workdir) -> None:
    """Each edit of ``window.presets`` is fetched by one segment step, then never again.

    One controller and one scheduler live across both edits, as in the worker. Each edit makes
    the next tick run LinkedIn's ``segments`` job alone: it stores the new window ending on the
    default end, records the new presets, and leaves nothing due, so the following wakes run
    no LinkedIn job.
    """
    clock = _Clock(T0)
    controller = _controller(db, clock)
    started = await bootstrap_sources(get_settings(), controller=controller)
    assert not started.failures, dict(started.failures)
    dataset_id = await _linkedin_dataset(db)
    assert await _segment_reason(db, dataset_id) == jobs.segment_reason(SHIPPED, DEFAULT_END)
    assert await _window_lengths(db, dataset_id) == set(SHIPPED)
    scheduler = Scheduler(controller=controller, clock=clock)

    async def wake(at: datetime) -> tuple[str, ...]:
        """Run one tick at ``at`` and return the LinkedIn jobs it ran."""
        clock.at = at
        report = await scheduler.tick(at)
        assert not report.errors, report.errors
        assert not report.failed_jobs, dict(report.failed_jobs)
        return report.jobs.get("linkedin", ())

    assert await wake(T0 + WAKE_GAP) == ()

    for step, presets in enumerate((WITH_21, WITH_21_AND_42), start=1):
        workdir.set_presets(presets)
        detected = T0 + WAKE_GAP * (2 * step)
        assert await _due(db, detected)

        assert await wake(detected) == (jobs.JOB_SEGMENTS,)

        assert await _segment_reason(db, dataset_id) == jobs.segment_reason(presets, DEFAULT_END)
        assert set(presets) <= await _window_lengths(db, dataset_id)
        assert not await _due(db, detected)
        assert await wake(detected + WAKE_GAP) == ()

    assert await _window_lengths(db, dataset_id) == set(WITH_21_AND_42)
