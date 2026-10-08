"""The worker's scheduler: one active worker, a 30 s wake loop, due jobs and analysis coalescing.

AAP 0.5.1 ("Scheduler and call budget": ``Scheduler.run_forever()``, a 30 s loop holding the
scheduler advisory lock, due-job calculation, ``sync_request`` polling and ``analysis_request``
coalescing within 60 s), 0.3.2 (the sync schedule and "Refresh after change"), 0.2.2 ("The
worker is a single active instance, enforced by a PostgreSQL advisory lock") and 0.5.3
("Lock unavailable: the worker leaves its ``analysis_request`` pending").

Single active worker. :meth:`Scheduler.run_forever` checks out one dedicated ``AUTOCOMMIT``
connection (:func:`ads_engine.db.session.dedicated_connection`) and takes the session-level
``pg_try_advisory_lock(SCHEDULER_LOCK_KEY)`` on it. A second worker finds the lock taken, logs
``scheduler.standby`` once and retries every :data:`STANDBY_RETRY`; it takes over the moment
the first worker's database session ends (graceful exit, crash or lost connection), because a
session-level advisory lock dies with its session. Every tick first proves the lock session is
alive (``SELECT 1`` on that connection); when it is not, the scheduler stops work, logs
``scheduler.lock_lost``, returns the connection (which runs ``pg_advisory_unlock_all()`` or
discards it) and reconnects and re-acquires before the next tick, so no tick ever runs without
the lock. The scheduler never takes the ``analysis`` lock: ``analysis.runner.run_analysis``
takes it itself, the one convention the API, the worker and the CLI share.

Schedule (:data:`SCHEDULE`), wall-clock times in ``DISPLAY_TIMEZONE``:

=================  ======================  ==================================================
Job                Sources                 Slots
=================  ======================  ==================================================
``entities``       google, linkedin        every 3 h (00:00, 03:00, 06:00, ...)
``metrics``        google, linkedin        every 6 h (00:00, 06:00, 12:00, 18:00); trailing
                                           3 days
``restatement``    google, linkedin        03:00 (Google 30 days, LinkedIn 14 days)
``leads``          linkedin                every 3 h
``crm``            hubspot                 every 3 h; a simulated LinkedIn's companion CRM
                                           runs inside its ``leads`` run
``segments``       linkedin                04:00, and whenever ``jobs.segments_due`` says the
                                           default window end or ``window.presets`` moved
``purge``          all                     05:00 (see below)
=================  ======================  ==================================================

The AAP asks for a "nightly cleanup" without naming a time. The purge runs at 05:00, after the
03:00 restatement and the 04:00 segment fetch, so the night's heaviest reads never contend with
the cascade deletes of retired datasets.

Due-ness. A job is due when its latest slot (:func:`latest_slot`) is later than the
``started_at`` of the most recent ``sync_run`` of that (source, job), whatever its status. The
history lives in the database, so due-ness survives restarts; slots missed during downtime run
once, not once per missed slot; and the jobs a dataset backfill records (``entities``,
``metrics`` and ``restatement`` for a metrics backfill, ``segments``, ``leads``, ``crm``)
satisfy their slots, so a fresh start does not repeat them on its first tick. Recovery probes
and the purge keep their own ``sync_run`` rows (jobs ``probe`` and ``purge``) for the same
reason.

Tick order (:meth:`Scheduler.tick`): datasets (``ensure_dataset`` per source), recovery probes,
``sync_request`` claims, due jobs, segments, purge, the Google call-budget refresh, and
analysis coalescing. Each step, and each source within a per-source step, is isolated: a
failure in :data:`JOB_FAILURES` is logged as ``scheduler.step_failed`` and the tick continues;
anything else (a defect) propagates, ends the worker and lets the container restart it.
Cancellation is never caught.

Example::

    scheduler = Scheduler(get_settings())
    await scheduler.run_forever(on_start=startup)  # until the task is cancelled
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from types import MappingProxyType
from typing import Any, Final

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from ads_engine.analysis import runner
from ads_engine.analysis.lock import ANALYSIS_LOCK_KEY, RunInProgress
from ads_engine.analysis.thresholds import ConfigError, Thresholds
from ads_engine.connectors.errors import ConnectorError
from ads_engine.db import repositories
from ads_engine.db.models.analysis import AnalysisRequest
from ads_engine.db.models.ops import SourceDataset, SyncRequest, SyncRun
from ads_engine.db.session import dedicated_connection
from ads_engine.logging import get_logger, logged_as
from ads_engine.settings import SOURCES, Settings, Source, get_settings
from ads_engine.sync import jobs
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.datasets import DatasetManager, DatasetStateError
from ads_engine.sync.mode import BOOTSTRAP_ORDER, BuildOutcome, SourceModeController

__all__ = [
    "COALESCE_WINDOW",
    "JOB_FAILURES",
    "JOB_PROBE",
    "JOB_PURGE",
    "PROBE_INTERVAL",
    "SCHEDULE",
    "SCHEDULER_LOCK_KEY",
    "STANDBY_RETRY",
    "WAKE_INTERVAL",
    "ScheduleEntry",
    "Scheduler",
    "TickReport",
    "is_due",
    "latest_slot",
]

_log = get_logger(__name__)

# ---------------------------------------------------------------------------------------------
# Lock key and timing constants
# ---------------------------------------------------------------------------------------------

SCHEDULER_LOCK_KEY: Final[int] = int.from_bytes(
    hashlib.sha256(b"ads_engine:scheduler").digest()[:8], "big", signed=True
)
"""The advisory-lock key of the single-worker scheduler lock (AAP 0.2.2).

Derived like ``analysis.lock.ANALYSIS_LOCK_KEY`` but from the name ``"ads_engine:scheduler"``:
deterministic in every process and release, and within PostgreSQL ``bigint``. Each lock key is
a constant defined once, by its owner; this module owns this one.
"""

if SCHEDULER_LOCK_KEY == ANALYSIS_LOCK_KEY:  # pragma: no cover - distinct SHA-256 inputs
    # One key for both locks would let a running analysis block every worker as a standby,
    # and a standby worker block every analysis run.
    raise RuntimeError("SCHEDULER_LOCK_KEY must differ from analysis.lock.ANALYSIS_LOCK_KEY")

WAKE_INTERVAL: Final = timedelta(seconds=30)
"""How long the scheduler sleeps after each tick (AAP 0.3.2: the worker wakes every 30 s)."""

COALESCE_WINDOW: Final = timedelta(seconds=60)
"""Pending ``analysis_request`` rows run once the oldest is this old, all in one run."""

PROBE_INTERVAL: Final = timedelta(minutes=30)
"""The least time between two recovery probes of a source in automatic fallback."""

STANDBY_RETRY: Final = timedelta(seconds=30)
"""How often a standby worker retries the lock, and a lost connection is re-established."""

JOB_PROBE: Final = "probe"
"""The ``sync_run.job`` of a recovery probe; its ``started_at`` spaces the probes."""

JOB_PURGE: Final = "purge"
"""The ``sync_run.job`` of the nightly purge of retired datasets."""

_TRY_LOCK: Final = text("SELECT pg_try_advisory_lock(:key)")
_ALIVE: Final = text("SELECT 1")

# Every slot of SCHEDULE recurs at most 24 h apart, 25 h across a DST change, so a run that
# started more than two days ago can never be at or after the latest slot: its job is due
# exactly as if it had never run. Reading only this horizon keeps the history query small
# however long sync_run grows (it is operational history and is never purged).
_HISTORY_HORIZON: Final = timedelta(days=2)

_ANALYSIS_REASON_LIMIT: Final = 500
_ERROR_LIMIT: Final = 500
_RERUN_REASON: Final = "analysis_lock_lost"

# ---------------------------------------------------------------------------------------------
# Schedule model
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One recurring job of :data:`SCHEDULE`, timed by wall clock in ``DISPLAY_TIMEZONE``.

    Exactly one of :attr:`every_hours` and :attr:`at` is set.

    Attributes:
        job: The ``sync_run.job`` name the job records, for example ``entities``.
        sources: The sources the job runs for, in run order.
        every_hours: Slots at every multiple of this many hours from local midnight
            (``3`` gives 00:00, 03:00, ...); it must divide 24.
        at: One slot a day at this local wall-clock time, without ``tzinfo``.

    Raises:
        ValueError: On construction, when the timing or the sources are invalid.
    """

    job: str
    sources: tuple[Source, ...]
    every_hours: int | None = None
    at: time | None = None

    def __post_init__(self) -> None:
        """Reject an entry whose timing is ambiguous or whose sources are unknown."""
        if not self.job.strip():
            raise ValueError("a schedule entry needs a job name")
        if not self.sources:
            raise ValueError(f"schedule entry {self.job!r} names no source")
        unknown = sorted(set(self.sources) - set(SOURCES))
        if unknown:
            raise ValueError(f"schedule entry {self.job!r} names unknown sources {unknown}")
        if (self.every_hours is None) == (self.at is None):
            raise ValueError(f"schedule entry {self.job!r} needs exactly one of every_hours, at")
        hours: object = self.every_hours
        if hours is not None and (
            isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 24
        ):
            raise ValueError(f"every_hours of {self.job!r} must be 1 to 24, found {hours!r}")
        if isinstance(hours, int) and 24 % hours:
            raise ValueError(f"every_hours of {self.job!r} must divide 24, found {hours}")
        if self.at is not None and self.at.tzinfo is not None:
            raise ValueError(f"at of {self.job!r} is a wall-clock time and takes no tzinfo")

    @property
    def wall_times(self) -> tuple[time, ...]:
        """The entry's local wall-clock slot times within one day, ascending."""
        if self.at is not None:
            return (self.at,)
        step = self.every_hours if self.every_hours is not None else 24
        return tuple(time(hour) for hour in range(0, 24, step))


SCHEDULE: Final[tuple[ScheduleEntry, ...]] = (
    ScheduleEntry(jobs.JOB_ENTITIES, ("google", "linkedin"), every_hours=3),
    ScheduleEntry(jobs.JOB_METRICS, ("google", "linkedin"), every_hours=6),
    ScheduleEntry(jobs.JOB_RESTATEMENT, ("google", "linkedin"), at=time(3, 0)),
    ScheduleEntry(jobs.JOB_LEADS, ("linkedin",), every_hours=3),
    ScheduleEntry(jobs.JOB_CRM, ("hubspot",), every_hours=3),
    ScheduleEntry(jobs.JOB_SEGMENTS, ("linkedin",), at=time(4, 0)),
    ScheduleEntry(JOB_PURGE, SOURCES, at=time(5, 0)),
)
"""The worker's recurring jobs (AAP 0.3.2), wall-clock in ``DISPLAY_TIMEZONE``.

The first five are sync jobs the due-jobs step runs, in the order entities, metrics,
restatement, leads, crm. ``segments`` also runs on the window trigger, and ``purge`` is the
nightly cleanup at 05:00, after the restatement and the segment fetch; both have steps of
their own.
"""

_ENTRIES: Final[Mapping[str, ScheduleEntry]] = MappingProxyType(
    {entry.job: entry for entry in SCHEDULE}
)
_SYNC_JOBS: Final[tuple[str, ...]] = (
    jobs.JOB_ENTITIES,
    jobs.JOB_METRICS,
    jobs.JOB_RESTATEMENT,
    jobs.JOB_LEADS,
    jobs.JOB_CRM,
)
if len(_ENTRIES) != len(SCHEDULE) or not set(_SYNC_JOBS) <= set(_ENTRIES):  # pragma: no cover
    raise RuntimeError("SCHEDULE must name each job once, the sync jobs included")


def _aware(value: datetime, what: str) -> datetime:
    """Return ``value`` when it is a timezone-aware instant.

    Raises:
        TypeError: When ``value`` is not a ``datetime``.
        ValueError: When ``value`` is naive.
    """
    candidate: object = value
    if not isinstance(candidate, datetime):
        raise TypeError(f"{what} must be a timezone-aware datetime, found {candidate!r}")
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise ValueError(
            f"{what} must be a timezone-aware datetime (an instant), found a naive one"
        )
    return candidate


def _wall_clock_instant(day: date, wall: time, tz: tzinfo) -> datetime:
    """Turn a local wall-clock time on ``day`` in ``tz`` into a UTC instant, DST-safely.

    The local time is built with ``fold=0``. By PEP 495 that resolves the two DST edge cases
    the schedule needs: a local time that occurs twice (clocks set back) takes its first
    occurrence, and a local time that does not exist (clocks set forward) is read with the
    offset in force before the change, which maps it forward by the length of the gap
    (02:30 on a spring-forward night in ``America/New_York`` becomes 03:30 EDT).

    Args:
        day: The local date.
        wall: The local wall-clock time, without ``tzinfo``.
        tz: The schedule's time zone.

    Returns:
        The instant, in UTC.
    """
    return datetime.combine(day, wall, tzinfo=tz).replace(fold=0).astimezone(UTC)


def latest_slot(entry: ScheduleEntry, now: datetime, tz: tzinfo) -> datetime:
    """Return the most recent slot boundary of ``entry`` at or before ``now``.

    Slots are local wall-clock times in ``tz`` (``DISPLAY_TIMEZONE``): every
    ``entry.every_hours`` hours from local midnight, or once a day at ``entry.at``. Each
    candidate is built as a local time and converted to UTC (:func:`_wall_clock_instant`), so
    DST changes move the slots with the wall clock: across a spring-forward night a 3-hourly
    entry's 00:00 and 03:00 slots are two real hours apart, and a nonexistent slot time maps
    forward. Pure: no clock, database or settings are read.

    Example::

        >>> from zoneinfo import ZoneInfo
        >>> entry = ScheduleEntry("entities", ("google",), every_hours=3)
        >>> now = datetime(2026, 10, 1, 13, 15, tzinfo=UTC)  # 09:15 in New York (EDT)
        >>> latest_slot(entry, now, ZoneInfo("America/New_York")).isoformat()
        '2026-10-01T13:00:00+00:00'

    Args:
        entry: The schedule entry.
        now: The instant to judge at; timezone-aware.
        tz: The schedule's time zone.

    Returns:
        The latest slot at or before ``now``, as a UTC instant.

    Raises:
        TypeError: When ``now`` is not a ``datetime``.
        ValueError: When ``now`` is naive.
    """
    instant = _aware(now, "now")
    local_today = instant.astimezone(tz).date()
    best: datetime | None = None
    # Two days back always hold a slot before `now` (entries recur at least daily), and the
    # day ahead is included so an ambiguous local midnight cannot be missed.
    for offset in (-2, -1, 0, 1):
        day = local_today + timedelta(days=offset)
        for wall in entry.wall_times:
            candidate = _wall_clock_instant(day, wall, tz)
            if candidate <= instant and (best is None or candidate > best):
                best = candidate
    if best is None:  # pragma: no cover - unreachable: two whole days precede `now`
        raise RuntimeError(f"no slot of {entry.job!r} found before {instant.isoformat()}")
    return best


def is_due(entry: ScheduleEntry, now: datetime, tz: tzinfo, last_started: datetime | None) -> bool:
    """Return whether ``entry`` is due at ``now`` given its last run's start.

    Due when the job never ran, or when its latest slot (:func:`latest_slot`) is later than
    the ``started_at`` of its most recent ``sync_run``, whatever that run's status. A slot
    missed during downtime therefore runs once, and a run that started at or after the
    latest slot (the dataset backfill's own runs included) satisfies it.

    Args:
        entry: The schedule entry.
        now: The instant to judge at; timezone-aware.
        tz: The schedule's time zone.
        last_started: The latest ``started_at`` of the (source, job), or ``None``.

    Returns:
        Whether the job should run now.

    Raises:
        TypeError: When ``now`` or ``last_started`` is not a ``datetime``.
        ValueError: When ``now`` or ``last_started`` is naive.
    """
    slot = latest_slot(entry, now, tz)
    if last_started is None:
        return True
    return slot > _aware(last_started, "last_started")


# ---------------------------------------------------------------------------------------------
# Failure isolation and the tick report
# ---------------------------------------------------------------------------------------------

JOB_FAILURES: Final[tuple[type[Exception], ...]] = (
    ConnectorError,
    SQLAlchemyError,
    DatasetStateError,
    repositories.DatasetNotWritable,
    httpx.HTTPError,
    ConfigError,
)
"""The exception families one scheduler step isolates (and the worker's startup reuses).

A connector error that escaped a job, a database error (an outage included: the next tick
retries, and the lock check reconnects), an illegal or raced dataset transition, a write to a
dataset retired meanwhile, an HTTP failure below the connector taxonomy, and an invalid
configuration file. Anything else is a defect and propagates.

``analysis.window.InvalidWindow`` is deliberately absent: the scheduler only builds the
default window, which ``Thresholds.load`` guarantees is valid (``window.default_days`` is a
preset, and presets may not exceed ``window.max_days``), so it is unreachable here, and
``analysis/window.py`` is not among this module's dependencies.
"""


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one :meth:`Scheduler.tick` did.

    Attributes:
        now: The tick's instant: every scheduling decision and job of the tick used it.
        steps: The steps that acted, in tick order: ``datasets`` (a build), ``probes``,
            ``sync_requests`` (requests claimed), ``due_jobs``, ``segments``, ``purge`` and
            ``analysis`` (a run, or a deferral behind the analysis lock).
        jobs: The ``sync_run`` job names run per source, in the order run, the companion
            CRM sync (``crm_companion``) included.
        failed_jobs: The jobs of :attr:`jobs` that ended failed (their connector errors were
            already handled by ``SourceModeController.on_error``).
        builds: The dataset build ``ensure_dataset`` ran per source.
        probed: The sources a recovery probe ran for.
        sync_requests: The sources whose pending ``sync_request`` was claimed.
        purged: The retired datasets the purge deleted.
        google_calls_24h: The Google webhook calls counted in the sliding 24 h, or ``None``
            when the refresh failed.
        analysis_run_id: The default run computed from coalesced requests, or ``None``.
        analysis_deferred: Whether pending requests were left for the next tick because
            another process held the ``analysis`` lock.
        errors: The steps that failed, as ``step`` or ``step:source``.
    """

    now: datetime
    steps: tuple[str, ...] = ()
    jobs: Mapping[Source, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    failed_jobs: Mapping[Source, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    builds: Mapping[Source, BuildOutcome] = field(default_factory=lambda: MappingProxyType({}))
    probed: tuple[Source, ...] = ()
    sync_requests: tuple[Source, ...] = ()
    purged: tuple[int, ...] = ()
    google_calls_24h: int | None = None
    analysis_run_id: int | None = None
    analysis_deferred: bool = False
    errors: tuple[str, ...] = ()

    @property
    def did_work(self) -> bool:
        """Whether any step acted or failed (the tick is then logged at info level)."""
        return bool(self.steps or self.errors)

    def to_log(self) -> dict[str, Any]:
        """Return the report as plain log fields; counts and ids only, never credentials."""
        return {
            "now": self.now.isoformat(),
            "steps": list(self.steps),
            "jobs": {source: list(names) for source, names in self.jobs.items()},
            "failed_jobs": {source: list(names) for source, names in self.failed_jobs.items()},
            "builds": {
                source: {
                    "mode": outcome.mode,
                    "dataset_id": outcome.dataset_id,
                    "activated": outcome.activated,
                }
                for source, outcome in self.builds.items()
            },
            "probed": list(self.probed),
            "sync_requests": list(self.sync_requests),
            "purged": list(self.purged),
            "google_calls_24h": self.google_calls_24h,
            "analysis_run_id": self.analysis_run_id,
            "analysis_deferred": self.analysis_deferred,
            "errors": list(self.errors),
        }


class _Tick:
    """The mutable record of a tick in progress, frozen into a :class:`TickReport` at its end."""

    __slots__ = (
        "analysis_deferred",
        "analysis_run_id",
        "builds",
        "errors",
        "failed_jobs",
        "google_calls_24h",
        "jobs",
        "now",
        "probed",
        "purged",
        "steps",
        "sync_requests",
    )

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.steps: list[str] = []
        self.jobs: dict[Source, list[str]] = {}
        self.failed_jobs: dict[Source, list[str]] = {}
        self.builds: dict[Source, BuildOutcome] = {}
        self.probed: list[Source] = []
        self.sync_requests: list[Source] = []
        self.purged: list[int] = []
        self.google_calls_24h: int | None = None
        self.analysis_run_id: int | None = None
        self.analysis_deferred = False
        self.errors: list[str] = []

    def acted(self, step: str) -> None:
        """Note that ``step`` did work, once."""
        if step not in self.steps:
            self.steps.append(step)

    def ran(self, step: str, report: jobs.SourceSyncReport) -> None:
        """Note the jobs one ``run_source_jobs`` call ran under ``step``."""
        if not report.results:
            return
        self.acted(step)
        names = self.jobs.setdefault(report.source, [])
        for result in report.results:
            names.append(result.job)
            if result.status != "succeeded":
                self.failed_jobs.setdefault(report.source, []).append(result.job)

    def freeze(self) -> TickReport:
        """Return the finished report."""
        return TickReport(
            now=self.now,
            steps=tuple(self.steps),
            jobs=MappingProxyType({source: tuple(names) for source, names in self.jobs.items()}),
            failed_jobs=MappingProxyType(
                {source: tuple(names) for source, names in self.failed_jobs.items()}
            ),
            builds=MappingProxyType(dict(self.builds)),
            probed=tuple(self.probed),
            sync_requests=tuple(self.sync_requests),
            purged=tuple(self.purged),
            google_calls_24h=self.google_calls_24h,
            analysis_run_id=self.analysis_run_id,
            analysis_deferred=self.analysis_deferred,
            errors=tuple(self.errors),
        )


def _as_source(value: str) -> Source | None:
    """Narrow a stored source name to :data:`~ads_engine.settings.Source`, or ``None``."""
    for source in SOURCES:
        if value == source:
            return source
    return None


def _join_reasons(reasons: Iterable[str]) -> str:
    """The distinct request reasons in first-seen order, comma-joined, at most 500 chars."""
    distinct = dict.fromkeys(reason.strip() for reason in reasons if reason.strip())
    return ", ".join(distinct)[:_ANALYSIS_REASON_LIMIT]


def _truncate(value: str, limit: int = _ERROR_LIMIT) -> str:
    """Cut ``value`` to ``limit`` characters for a ``sync_run.error``."""
    return value if len(value) <= limit else value[: limit - 1] + "…"


# ---------------------------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------------------------


class Scheduler:
    """The worker's 30 s loop: the scheduler lock, due jobs, sync requests and analysis requests.

    One instance serves the worker process (``worker/main.py``). It holds no schedule state of
    its own: due-ness is read from ``sync_run``, requests from ``sync_request`` and
    ``analysis_request``, so a restarted or standby worker continues exactly where the last
    active one stopped.

    Example:
        A test drives single ticks on a fixed clock::

            scheduler = Scheduler(settings, controller=controller, clock=lambda: T0)
            report = await scheduler.tick(T0)
            assert report.jobs.get("google") is None  # the backfill satisfied every slot
    """

    __slots__ = ("_budget", "_clock", "_controller", "_datasets", "_settings", "_sleep", "_tz")

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        controller: SourceModeController | None = None,
        budget: GoogleCallBudget | None = None,
        datasets: DatasetManager | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Configure the scheduler; nothing is read, locked or sent until it runs.

        Args:
            settings: The process settings; the controller's, or
                :func:`~ads_engine.settings.get_settings`, when omitted.
            controller: The source-mode controller every build, probe and job goes through;
                one on ``settings``, ``budget``, ``datasets`` and ``clock`` when omitted.
            budget: The Google call budget refreshed on every tick; the controller's own
                budget when it is a :class:`GoogleCallBudget`, else one on ``settings`` and
                ``clock``.
            datasets: The dataset manager the purge uses; the controller's when omitted.
            clock: Returns the current instant, timezone-aware; the controller's clock when
                omitted.
            sleep: Awaits the given number of seconds; :func:`asyncio.sleep` when omitted.
                Tests inject both to run the loop without waiting.
        """
        resolved = settings
        if resolved is None:
            resolved = controller.settings if controller is not None else get_settings()
        if controller is None:
            controller = SourceModeController(
                resolved, datasets=datasets, budget=budget, clock=clock
            )
        if budget is None:
            shared = controller.budget
            budget = (
                shared
                if isinstance(shared, GoogleCallBudget)
                else GoogleCallBudget(resolved, clock=clock)
            )
        self._settings: Settings = resolved
        self._controller: SourceModeController = controller
        self._budget: GoogleCallBudget = budget
        self._datasets: DatasetManager = datasets if datasets is not None else controller.datasets
        self._clock: Callable[[], datetime] = clock if clock is not None else controller.now
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else asyncio.sleep
        )
        # Validated by Settings (an IANA zone the tz database loads); slots follow it only.
        self._tz: tzinfo = resolved.display_tz

    def __repr__(self) -> str:
        """Return the schedule's time zone only; settings carry secrets and are never shown."""
        return f"{type(self).__name__}(display_timezone={self._settings.display_timezone!r})"

    @property
    def controller(self) -> SourceModeController:
        """The source-mode controller the scheduler drives."""
        return self._controller

    def _now(self) -> datetime:
        """Read the clock once.

        Raises:
            ValueError: When the clock returns a naive datetime.
        """
        return _aware(self._clock(), "the scheduler clock")

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        """The controller's session factory (injected, or the process-wide one)."""
        return self._controller.sessionmaker

    # -- the loop ----------------------------------------------------------------------------

    async def run_forever(self, on_start: Callable[[], Awaitable[None]] | None = None) -> None:
        """Hold the scheduler lock and tick every :data:`WAKE_INTERVAL` until cancelled.

        1. Check out a dedicated connection and take ``pg_try_advisory_lock`` with
           :data:`SCHEDULER_LOCK_KEY`. While another session holds it, log
           ``scheduler.standby`` once and retry every :data:`STANDBY_RETRY`: this worker is a
           standby until the active one's session ends.
        2. Once the lock is held, log ``scheduler.lock_acquired`` and await ``on_start``
           (``worker/main.py``'s startup: content assets, the config-change check, source
           bootstrap and the first default analysis). It runs once per call, on the first
           acquisition only, so it always precedes the first tick and runs only on the
           single active worker.
        3. Loop: prove the lock session alive (``SELECT 1``), :meth:`tick`, sleep. When the
           check fails, log ``scheduler.lock_lost``, return the connection (its exit runs
           ``pg_advisory_unlock_all()``, or discards a broken connection) and go back to
           step 1, so a tick never runs without the lock.

        A database error while connecting or taking the lock is logged as
        ``scheduler.lock_unavailable`` and retried after :data:`STANDBY_RETRY`, so a
        database restart never ends the worker. Cancellation (the worker's SIGTERM or SIGINT
        handler) is never caught: it propagates from the current await, and the dedicated
        connection's exit releases the lock.

        Args:
            on_start: Awaited once, holding the lock, before the first tick.

        Raises:
            asyncio.CancelledError: When the task running the loop is cancelled.
            Exception: Whatever ``on_start`` raises, or a defect a tick does not isolate;
                the lock is released on the way out.
        """
        start = on_start
        while True:
            async with AsyncExitStack() as stack:
                try:
                    conn = await stack.enter_async_context(dedicated_connection())
                    await self._acquire(conn)
                except SQLAlchemyError as exc:
                    _log.warning(
                        "scheduler.lock_unavailable",
                        error=type(exc).__name__,
                        retry_in_s=STANDBY_RETRY.total_seconds(),
                    )
                else:
                    if start is not None:
                        pending, start = start, None
                        await pending()
                    await self._hold(conn)
                    # The lock was lost: reconnect at once rather than after a pause.
                    continue
            await self._sleep(STANDBY_RETRY.total_seconds())

    async def _acquire(self, conn: AsyncConnection) -> None:
        """Wait on ``conn`` until it holds the scheduler lock.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: When the connection fails while waiting.
        """
        announced = False
        while True:
            acquired = bool(await conn.scalar(_TRY_LOCK, {"key": SCHEDULER_LOCK_KEY}))
            if acquired:
                _log.info("scheduler.lock_acquired", after_standby=announced)
                return
            if not announced:
                _log.info(
                    "scheduler.standby",
                    reason="another worker holds the scheduler lock",
                    retry_in_s=STANDBY_RETRY.total_seconds(),
                )
                announced = True
            await self._sleep(STANDBY_RETRY.total_seconds())

    async def _hold(self, conn: AsyncConnection) -> None:
        """Tick while the lock session on ``conn`` stays alive; return once it is lost."""
        while await self._lock_alive(conn):
            await self.tick()
            await self._sleep(WAKE_INTERVAL.total_seconds())

    @staticmethod
    async def _lock_alive(conn: AsyncConnection) -> bool:
        """Return whether the lock session still exists; a session-level lock lives with it.

        A failed check (any :class:`~sqlalchemy.exc.SQLAlchemyError`, the driver's
        ``DBAPIError`` included) is logged as ``scheduler.lock_lost``.
        """
        try:
            await conn.execute(_ALIVE)
        except SQLAlchemyError as exc:
            _log.error("scheduler.lock_lost", error=type(exc).__name__)
            return False
        return True

    # -- one tick ----------------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> TickReport:
        """Run one wake of the scheduler: every step below, in order, each isolated.

        1. **Datasets**: ``ensure_dataset`` per source, in ``BOOTSTRAP_ORDER`` (pending
           live-to-simulated switches after a denial, first builds, a first live build once
           credentials are present).
        2. **Recovery probes**: for each source in automatic fallback, ``recover`` at most
           every :data:`PROBE_INTERVAL`, recorded as a ``probe`` ``sync_run``.
        3. **Sync requests**: claim every pending ``sync_request`` (``FOR UPDATE SKIP
           LOCKED``) and run the source's jobs with trigger ``manual``.
        4. **Due jobs**: the due ``entities``, ``metrics``, ``restatement``, ``leads`` and
           ``crm`` jobs of each source with an active dataset, one ``run_source_jobs`` call
           per source in the order google, linkedin, hubspot, so LinkedIn's new leads are
           read before HubSpot matches them.
        5. **Segments**: LinkedIn's segment fetch when its 04:00 slot is due or
           ``jobs.segments_due`` reports the window end or the presets moved.
        6. **Purge**: at 05:00, ``purge_retired`` and its ``purge`` ``sync_run`` rows.
        7. **Budget**: ``used_24h`` keeps ``source_connection.calls_24h`` current.
        8. **Analysis**: when the oldest pending ``analysis_request`` is at least
           :data:`COALESCE_WINDOW` old, one default run serves every pending request; behind
           another holder of the ``analysis`` lock the requests stay pending.

        A step failing with one of :data:`JOB_FAILURES` is logged as ``scheduler.step_failed``
        (with its traceback, or as a warning naming the event that already logged it in full)
        and recorded in :attr:`TickReport.errors`; the tick continues. The tick is logged as
        ``scheduler.tick``: at debug level when nothing happened, at info level otherwise.

        Args:
            now: The tick's instant (timezone-aware) for every scheduling decision and job;
                the scheduler clock when omitted. The analysis step reads the clock again,
                because the steps before it may run for minutes and the run's "as of" time
                should be current.

        Returns:
            What the tick did.

        Raises:
            TypeError: When ``now`` is not a ``datetime``.
            ValueError: When ``now`` or the clock is naive.
        """
        instant = self._now() if now is None else _aware(now, "now")
        tick = _Tick(instant)
        await self._ensure_datasets(tick)
        await self._probe_fallbacks(tick)
        await self._serve_sync_requests(tick)
        await self._run_due_jobs(tick)
        await self._run_segments(tick)
        await self._purge(tick)
        await self._refresh_budget(tick)
        await self._coalesce_analysis(tick)
        report = tick.freeze()
        if report.did_work:
            _log.info("scheduler.tick", **report.to_log())
        else:
            _log.debug("scheduler.tick", **report.to_log())
        return report

    @asynccontextmanager
    async def _step(self, tick: _Tick, name: str) -> AsyncIterator[None]:
        """Isolate one step: a :data:`JOB_FAILURES` error is logged and recorded, not raised.

        ``scheduler.step_failed`` carries the traceback, unless the error's owner already
        logged it in full (an analysis run's ``analysis_run_failed``): then it is a warning
        naming that event (``logged_as``). Anything else, cancellation included, propagates
        unchanged.
        """
        try:
            yield
        except JOB_FAILURES as exc:
            tick.errors.append(name)
            owner = logged_as(exc)
            if owner is None:
                _log.error(
                    "scheduler.step_failed", step=name, error=type(exc).__name__, exc_info=exc
                )
            else:
                _log.warning(
                    "scheduler.step_failed", step=name, error=type(exc).__name__, logged_as=owner
                )

    # step 1 ----------------------------------------------------------------------------------

    async def _ensure_datasets(self, tick: _Tick) -> None:
        """Give each source an active primary dataset of the mode it resolves to."""
        for source in BOOTSTRAP_ORDER:
            async with self._step(tick, f"datasets:{source}"):
                outcome = await self._controller.ensure_dataset(source)
                if outcome is not None:
                    tick.builds[source] = outcome
                    tick.acted("datasets")

    # step 2 ----------------------------------------------------------------------------------

    async def _probe_fallbacks(self, tick: _Tick) -> None:
        """Probe each source in automatic fallback, at most every :data:`PROBE_INTERVAL`."""
        for source in BOOTSTRAP_ORDER:
            async with self._step(tick, f"probes:{source}"):
                if not await self._controller.in_fallback(source):
                    continue
                history = await self._history(tick.now, (JOB_PROBE,))
                last = history.get((source, JOB_PROBE))
                if last is not None and tick.now - last < PROBE_INTERVAL:
                    continue
                await self._probe(tick, source)

    async def _probe(self, tick: _Tick, source: Source) -> None:
        """Run one recovery probe of ``source`` under a ``probe`` ``sync_run`` row.

        The row is inserted (and committed) before the probe, so probes stay spaced even when
        one fails. It ends ``succeeded`` when the probe passed and the live dataset built
        from it was activated, ``failed`` otherwise, with the reason.
        """
        run_id = await self._open_run(source, JOB_PROBE, tick.now)
        tick.probed.append(source)
        tick.acted("probes")
        try:
            outcome = await self._controller.recover(source)
        except Exception as exc:
            await self._close_run_quietly(run_id, error=type(exc).__name__)
            raise
        if outcome is None:
            await self._close_run(run_id, "failed", error="recovery probe did not pass")
        elif outcome.activated and outcome.mode == "live":
            await self._close_run(run_id, "succeeded")
        else:
            await self._close_run(
                run_id, "failed", error=outcome.reason or "live dataset was not activated"
            )

    # step 3 ----------------------------------------------------------------------------------

    async def _serve_sync_requests(self, tick: _Tick) -> None:
        """Claim the pending "Sync now" requests and run each source's jobs."""
        claimed: tuple[Source, ...] = ()
        async with self._step(tick, "sync_requests"):
            claimed = await self._claim_sync_requests(tick.now)
        for source in claimed:
            tick.sync_requests.append(source)
            tick.acted("sync_requests")
            async with self._step(tick, f"sync_requests:{source}"):
                report = await jobs.run_source_jobs(
                    source, trigger="manual", controller=self._controller, now=tick.now
                )
                tick.ran("sync_requests", report)

    async def _claim_sync_requests(self, now: datetime) -> tuple[Source, ...]:
        """Stamp ``picked_at`` on every pending ``sync_request`` and return their sources.

        ``FOR UPDATE SKIP LOCKED`` keeps a row being claimed elsewhere out of this claim. The
        sources come back in request order.
        """
        pending = (
            select(SyncRequest.id)
            .where(SyncRequest.picked_at.is_(None))
            .order_by(SyncRequest.requested_at)
            .with_for_update(skip_locked=True)
        )
        async with self._sessions()() as session, session.begin():
            rows = (
                await session.execute(
                    update(SyncRequest)
                    .where(SyncRequest.id.in_(pending))
                    .values(picked_at=now)
                    .returning(SyncRequest.id, SyncRequest.source, SyncRequest.requested_at)
                    .execution_options(synchronize_session=False)
                )
            ).all()
        ordered: list[Source] = []
        for request_id, value, _ in sorted(rows, key=lambda row: (row[2], row[0])):
            source = _as_source(str(value))
            if source is None:  # pragma: no cover - the CHECK constraint allows SOURCES only
                _log.warning("scheduler.sync_request_unknown_source", request_id=request_id)
                continue
            if source not in ordered:
                ordered.append(source)
        return tuple(ordered)

    # step 4 ----------------------------------------------------------------------------------

    async def due_jobs(self, now: datetime | None = None) -> Mapping[Source, tuple[str, ...]]:
        """Return the due sync jobs of every source with an active primary dataset.

        The jobs are those of :data:`SCHEDULE` the due-jobs step runs (``entities``,
        ``metrics``, ``restatement``, ``leads``, ``crm``), each judged by :func:`is_due`
        against the latest ``sync_run`` of its (source, job). A source without an active
        dataset is left out: the datasets step builds it, and its backfill records the runs
        that satisfy the slots.

        Args:
            now: The instant to judge at; the scheduler clock when omitted.

        Returns:
            The due job names per source, in :data:`~ads_engine.settings.SOURCES` order and
            schedule order; sources with nothing due are absent.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: When the history cannot be read.
        """
        instant = self._now() if now is None else _aware(now, "now")
        async with self._sessions()() as session:
            active = await repositories.active_datasets(session)
            history = await self._read_history(session, instant, _SYNC_JOBS)
        due: dict[Source, tuple[str, ...]] = {}
        for source in SOURCES:
            if source not in active:
                continue
            names = tuple(
                name
                for name in _SYNC_JOBS
                if source in _ENTRIES[name].sources
                and is_due(_ENTRIES[name], instant, self._tz, history.get((source, name)))
            )
            if names:
                due[source] = names
        return MappingProxyType(due)

    async def _run_due_jobs(self, tick: _Tick) -> None:
        """Run each source's due sync jobs in one ``run_source_jobs`` call."""
        due: Mapping[Source, tuple[str, ...]] = MappingProxyType({})
        async with self._step(tick, "due_jobs"):
            due = await self.due_jobs(tick.now)
        for source, names in due.items():
            async with self._step(tick, f"due_jobs:{source}"):
                report = await jobs.run_source_jobs(
                    source,
                    trigger="scheduled",
                    jobs=names,
                    controller=self._controller,
                    now=tick.now,
                )
                tick.ran("due_jobs", report)

    # step 5 ----------------------------------------------------------------------------------

    async def _run_segments(self, tick: _Tick) -> None:
        """Fetch LinkedIn's segments at 04:00 or when the window trigger fires (AAP 0.3.2).

        The trigger (``jobs.segments_due``) fires when the default window end has passed the
        latest fetched segment end or ``window.presets`` changed; it backs off a failed fetch
        itself. Without an active LinkedIn dataset nothing runs: the dataset's backfill
        fetches its segments. Detection and execution share one configuration snapshot, read
        from the controller once per step, so the presets judged due are the presets fetched
        and recorded, and a fetch satisfies the trigger instead of repeating on every wake.
        """
        entry = _ENTRIES[jobs.JOB_SEGMENTS]
        async with self._step(tick, "segments"):
            async with self._sessions()() as session:
                if await repositories.active_dataset_id(session, "linkedin") is None:
                    return
                config = self._controller.thresholds
                history = await self._read_history(session, tick.now, (jobs.JOB_SEGMENTS,))
                slot_due = is_due(entry, tick.now, self._tz, history.get(("linkedin", entry.job)))
                run_now = slot_due or await jobs.segments_due(
                    session, now=tick.now, thresholds=config
                )
            if not run_now:
                return
            _log.info("scheduler.segments_due", slot=slot_due, window_changed=not slot_due)
            report = await jobs.run_source_jobs(
                "linkedin",
                trigger="scheduled",
                jobs=(jobs.JOB_SEGMENTS,),
                controller=self._controller,
                now=tick.now,
                thresholds=config,
            )
            tick.ran("segments", report)

    # step 6 ----------------------------------------------------------------------------------

    async def _purge(self, tick: _Tick) -> None:
        """Purge retired datasets once their 05:00 slot is due."""
        entry = _ENTRIES[JOB_PURGE]
        async with self._step(tick, "purge"):
            history = await self._history(tick.now, (JOB_PURGE,))
            starts = [started for (_, job), started in history.items() if job == JOB_PURGE]
            if not is_due(entry, tick.now, self._tz, max(starts, default=None)):
                return
            tick.acted("purge")
            sources_of = await self._retired_sources()
            try:
                purged = await self._datasets.purge_retired()
            except Exception as exc:
                await self._record_purge_quietly(tick.now, error=type(exc).__name__)
                raise
            counts: dict[Source, int] = {}
            for dataset_id in purged:
                source = sources_of.get(dataset_id)
                if source is not None:
                    counts[source] = counts.get(source, 0) + 1
            await self._record_purge(tick.now, counts)
            tick.purged.extend(purged)
            _log.info("scheduler.purge", purged=len(purged), by_source=counts)

    async def _retired_sources(self) -> dict[int, Source]:
        """Map every retired primary dataset to its source, read before the purge deletes it."""
        async with self._sessions()() as session:
            rows = (
                await session.execute(
                    select(SourceDataset.id, SourceDataset.source).where(
                        SourceDataset.state == "retired", SourceDataset.role == "primary"
                    )
                )
            ).all()
        mapped: dict[int, Source] = {}
        for dataset_id, value in rows:
            source = _as_source(str(value))
            if source is not None:
                mapped[int(dataset_id)] = source
        return mapped

    async def _record_purge(
        self, started: datetime, counts: Mapping[Source, int], *, error: str | None = None
    ) -> None:
        """Write the purge's ``sync_run`` rows: one per source purged, else one for google.

        Due-ness reads the latest ``purge`` row of any source, so the slot is satisfied
        whether or not anything was purged; a failed purge is recorded too, so it is retried
        at the next slot rather than on every wake.
        """
        finished = self._now()
        rows = dict(counts) if counts and error is None else {"google": 0}
        async with self._sessions()() as session, session.begin():
            session.add_all(
                SyncRun(
                    source=source,
                    job=JOB_PURGE,
                    dataset=None,
                    started_at=started,
                    finished_at=finished,
                    status="failed" if error is not None else "succeeded",
                    rows=count if error is None else None,
                    error=error,
                )
                for source, count in rows.items()
            )

    async def _record_purge_quietly(self, started: datetime, *, error: str) -> None:
        """Record a failed purge; a database error here is logged, never raised over it."""
        try:
            await self._record_purge(started, {}, error=error)
        except SQLAlchemyError as exc:
            _log.error("scheduler.purge_record_failed", error=type(exc).__name__)

    # step 7 ----------------------------------------------------------------------------------

    async def _refresh_budget(self, tick: _Tick) -> None:
        """Prune the Google call window so ``/sources`` shows a current ``calls_24h``."""
        async with self._step(tick, "budget"):
            tick.google_calls_24h = await self._budget.used_24h()

    # step 8 ----------------------------------------------------------------------------------

    async def _coalesce_analysis(self, tick: _Tick) -> None:
        """Serve the pending ``analysis_request`` rows with one default run, once coalesced.

        Every request pending when the step reads them is served by the run (its ids are
        passed to ``run_analysis``, which stamps them with the run); a request arriving
        meanwhile stays pending for the next tick. Behind another holder of the ``analysis``
        lock (``RunInProgress``) the requests stay pending and ``scheduler.analysis_deferred``
        is logged. A run that lost its lock to another process (``RunNoLongerRunning``) had
        its requests stamped by the runner as served by the failed run, so a fresh request is
        queued for the default run to catch up.
        """
        async with self._step(tick, "analysis"):
            at = self._now()
            async with self._sessions()() as session:
                pending = (
                    await session.execute(
                        select(
                            AnalysisRequest.id,
                            AnalysisRequest.requested_at,
                            AnalysisRequest.reason,
                        )
                        .where(AnalysisRequest.picked_at.is_(None))
                        .order_by(AnalysisRequest.requested_at, AnalysisRequest.id)
                    )
                ).all()
            if not pending:
                return
            oldest: datetime = pending[0][1]
            if at - oldest < COALESCE_WINDOW:
                return
            request_ids = tuple(int(row[0]) for row in pending)
            reason = _join_reasons(str(row[2]) for row in pending)
            tick.acted("analysis")
            thresholds = Thresholds.load()
            window = await runner.default_window(now=at, thresholds=thresholds)
            try:
                run = await runner.run_analysis(
                    window,
                    "default",
                    "event",
                    reason=reason,
                    request_ids=request_ids,
                    thresholds=thresholds,
                    now=at,
                )
            except RunInProgress:
                tick.analysis_deferred = True
                _log.info(
                    "scheduler.analysis_deferred",
                    reason="run_in_progress",
                    requests=len(request_ids),
                )
                return
            except runner.RunNoLongerRunning as exc:
                tick.errors.append("analysis")
                _log.error("scheduler.analysis_lock_lost", run_id=exc.run_id, status=exc.status)
                async with self._sessions()() as session, session.begin():
                    await repositories.enqueue_analysis_request(session, _RERUN_REASON)
                return
            tick.analysis_run_id = run.id

    # -- sync_run helpers ----------------------------------------------------------------------

    async def _history(
        self, now: datetime, names: Sequence[str]
    ) -> dict[tuple[str, str], datetime]:
        """The latest ``sync_run.started_at`` per (source, job) for ``names``, in a session."""
        async with self._sessions()() as session:
            return await self._read_history(session, now, names)

    @staticmethod
    async def _read_history(
        session: AsyncSession, now: datetime, names: Sequence[str]
    ) -> dict[tuple[str, str], datetime]:
        """The latest ``sync_run.started_at`` per (source, job) for ``names``.

        Only runs within :data:`_HISTORY_HORIZON` of ``now`` are read; an older run leaves
        its job due exactly as no run would.
        """
        rows = (
            await session.execute(
                select(SyncRun.source, SyncRun.job, func.max(SyncRun.started_at))
                .where(SyncRun.job.in_(list(names)), SyncRun.started_at >= now - _HISTORY_HORIZON)
                .group_by(SyncRun.source, SyncRun.job)
            )
        ).all()
        return {(str(source), str(job)): started for source, job, started in rows}

    async def _open_run(self, source: Source, job: str, started: datetime) -> int:
        """Insert and commit a ``running`` ``sync_run`` row; return its id."""
        async with self._sessions()() as session, session.begin():
            row = SyncRun(
                source=source, job=job, dataset=None, started_at=started, status="running"
            )
            session.add(row)
            await session.flush()
            return row.id

    async def _close_run(self, run_id: int, status: str, *, error: str | None = None) -> None:
        """Close a ``sync_run`` row the scheduler opened."""
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id)
                .values(
                    status=status,
                    finished_at=self._now(),
                    error=None if error is None else _truncate(error),
                )
                .execution_options(synchronize_session=False)
            )

    async def _close_run_quietly(self, run_id: int, *, error: str) -> None:
        """Mark a row failed while another error propagates; a database error is only logged."""
        try:
            await self._close_run(run_id, "failed", error=error)
        except SQLAlchemyError as exc:
            _log.error("scheduler.sync_run_close_failed", run_id=run_id, error=type(exc).__name__)
