"""The worker's scheduler: one active worker, 30 s wakes, lanes of due jobs and analysis coalescing.

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
session-level advisory lock dies with its session. Work is fenced by that lock:

* after every acquisition the scheduler waits :data:`LOCK_TAKEOVER_GRACE` (20 s) and proves
  the lock again before any work (``scheduler.lock_fence_wait``);
* while it holds the lock, everything it runs is a task watched by lock checks: the startup
  (``on_start``) and, after a re-acquisition, the ``scheduler_lock_lost`` requeue run under
  one guard, then the lanes and their trigger checks; the lock session is proved alive
  (``SELECT 1`` on that connection, within :data:`LOCK_CHECK_TIMEOUT`) on absolute deadlines
  every :data:`LOCK_CHECK_INTERVAL` (5 s), and again immediately before every lane dispatch,
  the first one included;
* when a check fails or times out, the scheduler logs ``scheduler.lock_lost``, cancels every
  task and waits for them at most :data:`LOCK_LOST_STOP_TIMEOUT` (5 s), because cancelled
  work still writes while it unwinds (a cancelled build discards its dataset, a cancelled
  job closes its ``sync_run`` rows). Once they have ended (``scheduler.lanes_cancelled``) it
  returns the connection (which runs ``pg_advisory_unlock_all()`` or discards it) and
  reconnects and re-acquires. An interrupted startup runs again on the next acquisition;
  otherwise one ``scheduler_lock_lost`` ``analysis_request`` is queued. A task still running
  after the timeout makes it log ``scheduler.fence_stop_timeout`` and abort the process with
  exit code 1; the container restarts the worker, exactly as after a defect.

A holder whose session died thus has stopped all its work, or its process, within 15 s,
before any other worker's 20 s grace has passed, so two workers never run work at the same
time (both bounds are checked at import). The scheduler never takes the ``analysis`` lock:
``analysis.runner.run_analysis`` takes it itself, the one convention the API, the worker and
the CLI share, and it additionally fences an analysis run's own writes: the run's dedicated
connection keeps that lock until the run's unwind has ended.

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

The AAP asks for a "nightly cleanup" without naming a time. The purge's 05:00 slot follows the
03:00 restatement and 04:00 segment-fetch slots, so it usually runs after those jobs, but lanes
do not wait for one another: the purge's cascade deletes may overlap any job still running,
such as a long restatement, segment fetch or dataset backfill. That is safe, because the purge
deletes only datasets retired more than :data:`~ads_engine.sync.datasets.RETENTION` (30 days)
ago, and no job writes a retired dataset (:func:`~ads_engine.db.repositories.ensure_writable`
refuses one).

Due-ness. A job is due when its latest slot (:func:`latest_slot`) is later than the
``started_at`` of the most recent ``sync_run`` of that (source, job), whatever its status. The
history lives in the database, so due-ness survives restarts; slots missed during downtime run
once, not once per missed slot; and the jobs a dataset backfill records (``entities``,
``metrics`` and ``restatement`` for a metrics backfill, ``segments``, ``leads``, ``crm``)
satisfy their slots, so a fresh start does not repeat them on its first tick. Probes and the
purge keep their own ``sync_run`` rows (jobs ``probe`` and ``purge``) for the same reason;
``probe`` rows record the recovery probes run here and the probes ``ensure_dataset`` runs
alike, so one spacing of :data:`PROBE_INTERVAL` covers both.

Wakes and lanes. The worker wakes at once after the grace and the startup, then every
:data:`WAKE_INTERVAL` (30 s) from that first wake on absolute deadlines of a monotonic clock,
however long any work or lock check runs. Each wake dispatches every idle lane as a task; a
lane whose previous pass is still running is not dispatched again (``scheduler.lane_busy``),
so at most one task per lane runs and no job ever runs twice at once. A long Google request or
backfill therefore delays only its own lane, never request pickup elsewhere or the 60 s
analysis coalescing. A busy lane is still looked into at every wake: a busy source lane gets
one short trigger check at a time (unclaimed ``sync_request`` rows, due jobs, LinkedIn's
segment trigger, due recovery probes, all only read), and when any fired
(``scheduler.lane_work_pending``) the lane is dispatched again the moment its pass ends
(``scheduler.lane_rerun``) instead of at the next wake; a busy ``analysis`` lane is rerun
the same way. The ``analysis`` lane waits, once a wake has seen a request, until the oldest
pending request is :data:`COALESCE_WINDOW` old, so a default run starts about 60 s after the
first request. The lanes:

=====================  ==========================================================================
Lane                   One pass
=====================  ==========================================================================
``google``             Google's datasets, recovery probe, ``sync_request``, due jobs
``linkedin_hubspot``   the same for HubSpot and LinkedIn, then LinkedIn's segments; one lane, so
                       every HubSpot dataset has one CRM writer, HubSpot's primary is ensured
                       before a LinkedIn build stages leads against it, and LinkedIn's leads run
                       before HubSpot's ``crm`` job
``purge``              the 05:00 purge
``budget``             the Google call-budget refresh
``analysis``           the wait until the oldest request is 60 s old, then analysis coalescing
=====================  ==========================================================================

Each pass is logged as ``scheduler.lane``. :meth:`Scheduler.tick` runs the same steps for
every source sequentially, in the order datasets (``ensure_dataset`` per source), recovery
probes, ``sync_request`` claims, due jobs, segments, purge, the Google call-budget refresh,
and analysis coalescing, and never waits. Each step, and each source within a per-source
step, is isolated: a failure in :data:`JOB_FAILURES` is logged as ``scheduler.step_failed``
and the pass continues; anything else (a defect) cancels and awaits the other lanes,
propagates, ends the worker and lets the container restart it. Cancellation is never
swallowed: every lane is cancelled and awaited, then it propagates.

Example::

    scheduler = Scheduler(get_settings())
    await scheduler.run_forever(on_start=startup)  # until the task is cancelled
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from time import monotonic as system_monotonic
from types import MappingProxyType
from typing import Any, Final, NoReturn

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from ads_engine.analysis import runner
from ads_engine.analysis.lock import ANALYSIS_LOCK_KEY, RunInProgress
from ads_engine.analysis.thresholds import ConfigError, Thresholds
from ads_engine.connectors.errors import ConnectorError
from ads_engine.db import repositories
from ads_engine.db.models.ops import (
    DATASET_ROLE_PRIMARY,
    DATASET_STATE_RETIRED,
    SOURCE_GOOGLE,
    SYNC_RUN_FAILED,
    SYNC_RUN_RUNNING,
    SYNC_RUN_SUCCEEDED,
    SourceDataset,
    SyncRequest,
    SyncRun,
)
from ads_engine.db.session import dedicated_connection
from ads_engine.logging import get_logger, logged_as
from ads_engine.settings import SOURCES, Settings, Source, get_settings
from ads_engine.sync import jobs
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.datasets import DatasetManager, DatasetStateError
from ads_engine.sync.mode import (
    BOOTSTRAP_ORDER,
    JOB_PROBE,
    PROBE_INTERVAL,
    BuildOutcome,
    SourceModeController,
)

__all__ = [
    "COALESCE_WINDOW",
    "JOB_FAILURES",
    "JOB_PROBE",
    "JOB_PURGE",
    "LOCK_CHECK_INTERVAL",
    "LOCK_CHECK_TIMEOUT",
    "LOCK_LOST_STOP_TIMEOUT",
    "LOCK_TAKEOVER_GRACE",
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
"""The spacing of the scheduler's wakes (AAP 0.3.2: the worker wakes every 30 s).

Wakes fall on absolute deadlines of the scheduler's monotonic clock: the first one follows
the takeover grace and the startup at once, then one every 30 s from it, however long a lock
check or any lane takes. A deadline already passed when the loop gets to it (an event loop
held by CPU-bound work) fires once, late, and the next wake is the next future point of the
same grid, never a burst of catch-up wakes. A wake never waits for a lane: it dispatches
every lane that is idle, however long another lane is still running.
"""

LOCK_CHECK_INTERVAL: Final = timedelta(seconds=5)
"""How often the active worker proves its scheduler-lock session alive while work runs.

Checked on absolute deadlines of the monotonic clock, during the startup, the lock-loss
requeue and between wakes alike, so a lost lock is noticed within this interval (plus
:data:`LOCK_CHECK_TIMEOUT`), not only at the next wake. It divides :data:`WAKE_INTERVAL`, so
every wake falls on a check deadline and one check serves both.
"""

LOCK_CHECK_TIMEOUT: Final = timedelta(seconds=5)
"""How long one lock check may take before the lock counts as lost.

A check that does not answer in time (a hung or partitioned connection) is treated exactly as
a failed one: every lane is cancelled and awaited, and the scheduler reconnects.
"""

LOCK_LOST_STOP_TIMEOUT: Final = timedelta(seconds=5)
"""The most time the scheduler waits for its cancelled tasks to end after losing its lock.

Cancelled work can still write while it unwinds: a cancelled dataset build discards its
building dataset (a cascade ``DELETE``), a cancelled sync job or recovery probe records its
own ``sync_run`` rows as failed (bounded by ``jobs.CANCEL_CLOSE_TIMEOUT``, checked at import
to be shorter), a cancelled analysis run records its failure. A task still running after
this long is not waited for: the scheduler logs ``scheduler.fence_stop_timeout`` and aborts
the process (exit code 1), so the unwind of the lost holder always ends inside the next
holder's :data:`LOCK_TAKEOVER_GRACE`. Only a lost lock is bounded this way: on SIGTERM or a
defect the lock is still held, so every task is awaited however long it takes.
"""

LOCK_TAKEOVER_GRACE: Final = timedelta(seconds=20)
"""How long a worker waits after every lock acquisition before it runs any work.

A previous holder whose lock session died notices the loss within
:data:`LOCK_CHECK_INTERVAL` plus :data:`LOCK_CHECK_TIMEOUT`, cancels its work and waits at
most :data:`LOCK_LOST_STOP_TIMEOUT` for it to end, or aborts its process. Waiting longer than
all three before the startup or the first wake (checked at import) means two workers never
run work at the same time. The lock is proved alive again once the grace has passed. The
bound assumes a responsive event loop: CPU-bound work that does not yield (an analysis run's
rule evaluation) delays the checks for its duration. An analysis run's own writes are
additionally fenced by the ``analysis`` advisory lock, which its dedicated connection keeps
until the run's unwind has ended.
"""

COALESCE_WINDOW: Final = timedelta(seconds=60)
"""Pending ``analysis_request`` rows run once the oldest is this old, all in one run.

The running scheduler's analysis lane sleeps until that moment once a wake has seen the
request, so the run starts about 60 s after the first request (AAP 0.3.2: "coalesced within
60 s"); :meth:`Scheduler.tick` never waits and serves the backlog at a tick once it is due.
"""

STANDBY_RETRY: Final = timedelta(seconds=30)
"""How often a standby worker retries the lock, and a lost connection is re-established."""

# PROBE_INTERVAL and JOB_PROBE (re-exported above) are defined once, in sync/mode.py: every
# probe is spaced by them, the ones SourceModeController.ensure_dataset runs included, and that
# module cannot import this one.

JOB_PURGE: Final = "purge"
"""The ``sync_run.job`` of the nightly purge of retired datasets."""

if (  # pragma: no cover - the constants above satisfy it
    LOCK_CHECK_INTERVAL + LOCK_CHECK_TIMEOUT + LOCK_LOST_STOP_TIMEOUT >= LOCK_TAKEOVER_GRACE
):
    # A shorter grace would let a new holder start while the previous one, whose lock session
    # died, has not yet noticed the loss, or is still waiting for its cancelled work to end.
    raise RuntimeError(
        "LOCK_CHECK_INTERVAL + LOCK_CHECK_TIMEOUT + LOCK_LOST_STOP_TIMEOUT must be less than "
        "LOCK_TAKEOVER_GRACE"
    )

if jobs.CANCEL_CLOSE_TIMEOUT >= LOCK_LOST_STOP_TIMEOUT:  # pragma: no cover
    # A job cancelled by a lost lock records its own sync_run rows as failed before it ends;
    # bookkeeping outlasting the stop timeout would abort the worker on every lost lock.
    raise RuntimeError("jobs.CANCEL_CLOSE_TIMEOUT must be less than LOCK_LOST_STOP_TIMEOUT")

if WAKE_INTERVAL < LOCK_CHECK_INTERVAL or WAKE_INTERVAL % LOCK_CHECK_INTERVAL:  # pragma: no cover
    raise RuntimeError("WAKE_INTERVAL must be a whole multiple of LOCK_CHECK_INTERVAL")

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
_LOCK_LOST_REASON: Final = "scheduler_lock_lost"

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
nightly cleanup, whose 05:00 slot follows the restatement and segment-fetch slots but does not
wait for those jobs to end; both have steps of their own.
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

# ---------------------------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------------------------

_LANE_PURGE: Final = "purge"
_LANE_BUDGET: Final = "budget"
_LANE_ANALYSIS: Final = "analysis"

_SOURCE_LANES: Final[Mapping[str, tuple[Source, ...]]] = MappingProxyType(
    {
        "google": ("google",),
        # HubSpot and LinkedIn share one lane, so their work is serialized. A LinkedIn build
        # runs LeadMatchJob and CrmSyncJob on HubSpot's active primary, and a simulated
        # LinkedIn's leads job syncs its companion CRM dataset, so every HubSpot dataset keeps
        # a single CRM writer. Within the lane, datasets are ensured in BOOTSTRAP_ORDER (the
        # HubSpot primary before the LinkedIn build stages its leads against it) and due jobs
        # run in SOURCES order (LinkedIn's leads before HubSpot's crm job matches them).
        "linkedin_hubspot": ("hubspot", "linkedin"),
    }
)
"""Each source lane and the sources whose steps it runs (datasets to segments)."""

_LANES: Final[tuple[str, ...]] = (*_SOURCE_LANES, _LANE_PURGE, _LANE_BUDGET, _LANE_ANALYSIS)
"""Every lane, in dispatch order; each runs as at most one task at a time."""

if sorted(source for group in _SOURCE_LANES.values() for source in group) != sorted(
    SOURCES
):  # pragma: no cover - the source lanes are literals above
    raise RuntimeError("the source lanes must cover every source exactly once")


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
    """What one :meth:`Scheduler.tick`, or one lane pass of the running scheduler, did.

    A lane pass fills in only its own steps' fields.

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


def _next_deadline(deadline: float, interval: float, now: float) -> float:
    """Return the deadline that follows ``deadline`` on its grid, skipping points before ``now``.

    The grid is ``deadline + k * interval`` for ``k >= 1``, in seconds of the monotonic clock.
    The result is its first point at or after ``now``, so a deadline handled late (after a
    slow lock check, or an event loop held by CPU-bound work) never shifts the grid, and
    points missed meanwhile fire once at the next one rather than as a burst.

    Example::

        >>> _next_deadline(0.0, 30.0, 4.0), _next_deadline(30.0, 30.0, 100.0)
        (30.0, 120.0)
    """
    following = deadline + interval
    if following >= now:
        return following
    return following + math.ceil((now - following) / interval) * interval


# ---------------------------------------------------------------------------------------------
# Lane and startup tasks
# ---------------------------------------------------------------------------------------------

_LANES_CANCELLED: Final = "scheduler.lanes_cancelled"
_START_CANCELLED: Final = "scheduler.start_cancelled"
_REQUEUE_CANCELLED: Final = "scheduler.lock_lost_requeue_cancelled"


async def _invoke(factory: Callable[[], Awaitable[None]]) -> None:
    """Await ``factory()``; a task needs a coroutine, and guarded work returns any awaitable."""
    await factory()


class _Lanes:
    """The running scheduler's tasks: at most one pass and one trigger check per lane.

    Attributes:
        passes: The lane passes still tracked, by lane.
        checks: The trigger checks still tracked (:meth:`Scheduler._check_lane`), by
            source lane.
        watched: The pass each tracked check was dispatched against, by source lane.
        rerun: The lanes to dispatch again as soon as they are idle, without waiting for the
            next wake.
    """

    __slots__ = ("checks", "passes", "rerun", "watched")

    def __init__(self) -> None:
        """Start with no task tracked and no lane marked."""
        self.passes: dict[str, asyncio.Task[TickReport]] = {}
        self.checks: dict[str, asyncio.Task[tuple[str, ...]]] = {}
        self.watched: dict[str, asyncio.Task[TickReport]] = {}
        self.rerun: set[str] = set()

    def tasks(self) -> list[asyncio.Task[Any]]:
        """Every pass and check still tracked."""
        return [*self.passes.values(), *self.checks.values()]

    def ready(self) -> list[str]:
        """The lanes marked for a rerun that no pass occupies, in dispatch order."""
        return [lane for lane in _LANES if lane in self.rerun and lane not in self.passes]

    def reap(self) -> None:
        """Forget every pass and check that has ended; mark reruns; re-raise a defect.

        A pass ending normally, or cancelled by something other than the scheduler (logged
        as ``scheduler.lane_cancelled``), is simply forgotten, so the next wake dispatches it
        again, or at once when it is marked for a rerun. A check that found work pending
        marks its lane for a rerun while the pass it watched still runs or has ended; a
        newer pass already started after it, so its result is then dropped.

        Raises:
            BaseException: The exception a pass or check ended with (a defect:
                :data:`JOB_FAILURES` are isolated inside the steps and the trigger reads),
                logged as ``scheduler.lane_failed`` or ``scheduler.lane_check_failed``. That
                task is forgotten; the tasks still running stay tracked for the caller to
                stop.
        """
        for lane, task in tuple(self.passes.items()):
            if not task.done():
                continue
            del self.passes[lane]
            if task.cancelled():
                _log.warning("scheduler.lane_cancelled", lane=lane)
                continue
            error = task.exception()
            if error is not None:
                _log.error("scheduler.lane_failed", lane=lane, error=type(error).__name__)
                raise error
        for lane, check in tuple(self.checks.items()):
            if not check.done():
                continue
            del self.checks[lane]
            watched = self.watched.pop(lane, None)
            if check.cancelled():
                _log.warning("scheduler.lane_check_cancelled", lane=lane)
                continue
            failure = check.exception()
            if failure is not None:
                _log.error("scheduler.lane_check_failed", lane=lane, error=type(failure).__name__)
                raise failure
            current = self.passes.get(lane)
            if check.result() and (current is None or current is watched):
                self.rerun.add(lane)

    def take(self) -> dict[str, asyncio.Task[Any]]:
        """Return every tracked task by name (a check as ``<lane>.check``), then forget all."""
        tasks: dict[str, asyncio.Task[Any]] = dict(self.passes)
        tasks.update({f"{lane}.check": check for lane, check in self.checks.items()})
        self.passes.clear()
        self.checks.clear()
        self.watched.clear()
        self.rerun.clear()
        return tasks


def _flush_streams() -> None:
    """Flush stdout and stderr before the process aborts, so its last log lines are kept.

    A stream that is missing, closed or broken cannot be flushed; the abort goes ahead
    regardless, so those errors are suppressed here deliberately.
    """
    for stream in (sys.stdout, sys.stderr):
        with suppress(AttributeError, OSError, ValueError):
            stream.flush()


async def _stop(
    tasks: dict[str, asyncio.Task[Any]],
    *,
    reason: str,
    event: str = _LANES_CANCELLED,
    abort: Callable[[int], NoReturn] | None = None,
) -> None:
    """Cancel every task of ``tasks`` and wait until each has ended; ``tasks`` is emptied.

    Without ``abort`` (SIGTERM or a defect: the lock is still held until every task has
    ended) the wait is unbounded, so nothing outlives this call and no cancelled work can
    write after the lock connection is released. With ``abort`` (a lost lock: another worker
    may take over) cancelled work could still write while it unwinds, so the wait is bounded
    by :data:`LOCK_LOST_STOP_TIMEOUT`: a task still running then is logged as
    ``scheduler.fence_stop_timeout`` (critical, with the task names), stdout and stderr are
    flushed and ``abort(1)`` ends the process before the next holder's
    :data:`LOCK_TAKEOVER_GRACE` has passed. The container restarts the worker, exactly as
    after a defect, and a dataset left ``building`` is discarded by ``sync/mode.py``'s
    abandoned-build sweep.

    A cancellation of the caller while it waits is held back until the wait ends and is then
    re-raised, as :class:`asyncio.TaskGroup` does. Every ended task's outcome is retrieved:
    an exception other than the cancellation (a task that was already done with a defect, or
    one raised while unwinding) is logged as ``scheduler.lane_failed``. The tasks actually
    cancelled are logged as ``event`` with their names and ``reason``.

    Args:
        tasks: The running tasks by name (a lane, ``<lane>.check``, ``on_start`` or
            ``lock_lost_requeue``).
        reason: Why they stop: ``lock_lost``, ``cancelled`` or ``defect``.
        event: The log event naming the cancelled tasks.
        abort: Ends the process with the given exit code; given only when the lock was
            lost, which bounds the wait.

    Raises:
        asyncio.CancelledError: When the caller was cancelled while waiting, once the wait
            has ended.
    """
    stopping = dict(tasks)
    tasks.clear()
    cancelled = [name for name, task in stopping.items() if not task.done()]
    for name in cancelled:
        stopping[name].cancel()
    loop = asyncio.get_running_loop()
    deadline = None if abort is None else loop.time() + LOCK_LOST_STOP_TIMEOUT.total_seconds()
    interrupted: asyncio.CancelledError | None = None
    while pending := [task for task in stopping.values() if not task.done()]:
        timeout = None if deadline is None else deadline - loop.time()
        if timeout is not None and timeout <= 0:
            break
        try:
            await asyncio.wait(pending, timeout=timeout)
        except asyncio.CancelledError as exc:
            interrupted = exc
    for name, task in stopping.items():
        if not task.done() or task.cancelled():
            continue
        error = task.exception()
        if error is not None:
            _log.error(
                "scheduler.lane_failed", lane=name, error=type(error).__name__, exc_info=error
            )
    if cancelled:
        _log.warning(event, lanes=cancelled, reason=reason)
    stuck = [name for name, task in stopping.items() if not task.done()]
    if stuck and abort is not None:
        _log.critical(
            "scheduler.fence_stop_timeout",
            lanes=stuck,
            reason=reason,
            timeout_s=LOCK_LOST_STOP_TIMEOUT.total_seconds(),
        )
        _flush_streams()
        abort(1)
    if interrupted is not None:
        raise interrupted


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

    __slots__ = (
        "_abort",
        "_budget",
        "_clock",
        "_controller",
        "_datasets",
        "_monotonic",
        "_settings",
        "_sleep",
        "_tz",
    )

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        controller: SourceModeController | None = None,
        budget: GoogleCallBudget | None = None,
        datasets: DatasetManager | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
        abort: Callable[[int], NoReturn] | None = None,
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
                It times the standby retries, the takeover grace, the waits until the next
                lock-check or wake deadline, and the analysis lane's coalescing wait.
            monotonic: Returns seconds on a monotonic clock; :func:`time.monotonic` when
                omitted. The wakes and the lock checks fall on absolute deadlines of this
                clock (the injected ``sleep`` only waits until the next one), so wakes stay
                :data:`WAKE_INTERVAL` apart however long a check takes. Tests inject it
                with ``clock`` and ``sleep``, advancing with their sleep, to run the loop
                without waiting.
            abort: Ends the process with the given exit code; :func:`os._exit` when
                omitted. Called with ``1`` only when, after a lost lock, a cancelled task is
                still running after :data:`LOCK_LOST_STOP_TIMEOUT`: the container restarts
                the worker, exactly as after a defect, and a dataset build left ``building``
                is discarded by ``sync/mode.py``'s abandoned-build sweep. Never called on
                SIGTERM or a defect, where the lock is still held until every task has
                ended. Tests inject one that records the code and raises.
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
        self._monotonic: Callable[[], float] = (
            monotonic if monotonic is not None else system_monotonic
        )
        self._abort: Callable[[int], NoReturn] = abort if abort is not None else os._exit
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
        """Hold the scheduler lock and run the lanes, waking every :data:`WAKE_INTERVAL`.

        1. Check out a dedicated connection and take ``pg_try_advisory_lock`` with
           :data:`SCHEDULER_LOCK_KEY`. While another session holds it, log
           ``scheduler.standby`` once and retry every :data:`STANDBY_RETRY`: this worker is a
           standby until the active one's session ends.
        2. Once the lock is held (``scheduler.lock_acquired``), wait
           :data:`LOCK_TAKEOVER_GRACE` (``scheduler.lock_fence_wait``) and prove the lock alive
           again before any work. This happens after every acquisition: a previous holder
           whose lock session died has by then noticed the loss and cancelled its own work,
           so two workers never run work at the same time.
        3. Run ``on_start`` (``worker/main.py``'s startup: content assets, the config-change
           check, source bootstrap and the first default analysis) under :meth:`_guard`,
           which proves the lock alive every :data:`LOCK_CHECK_INTERVAL`. This refines
           "``on_start`` runs once, on the first acquisition": it still completes exactly
           once per call, always before the first wake and only on the single active worker,
           but when the lock is lost while it runs it is cancelled and awaited, stays
           pending, and runs again from the start after the next acquisition. It counts as
           done only once it returns normally.
        4. Supervise the lanes (:meth:`_supervise`): the first wake follows at once, then one
           every :data:`WAKE_INTERVAL` on absolute deadlines, each dispatching every idle
           lane, while the lock is proved alive every :data:`LOCK_CHECK_INTERVAL` and
           immediately before every dispatch, the first one included. When the lock is lost
           (a failed check, or one taking longer than :data:`LOCK_CHECK_TIMEOUT`), every task
           is cancelled and awaited (at most :data:`LOCK_LOST_STOP_TIMEOUT`, else the process
           aborts), the connection is returned (its exit runs ``pg_advisory_unlock_all()``,
           or discards a broken connection) and the loop goes back to step 1 at once, so no
           work ever runs without the lock.
        5. After a re-acquisition once ``on_start`` has completed, one ``analysis_request``
           with reason ``scheduler_lock_lost`` is queued, under :meth:`_guard` like the
           startup, before the lanes start again, so the work the loss cancelled (an
           analysis run, a sync's completion request) is followed by a default run.

        A database error while connecting or taking the lock is logged as
        ``scheduler.lock_unavailable`` and retried after :data:`STANDBY_RETRY`, so a
        database restart never ends the worker. Cancellation (the worker's SIGTERM or SIGINT
        handler) is never swallowed: ``on_start`` or every lane is cancelled and awaited
        first, then the cancellation propagates and the dedicated connection's exit releases
        the lock.

        Args:
            on_start: Completed once, holding the lock, before the first wake.

        Raises:
            asyncio.CancelledError: When the task running the loop is cancelled.
            Exception: Whatever ``on_start`` raises, or a defect a lane does not isolate
                (the other lanes are cancelled and awaited first); the lock is released on
                the way out.
        """
        start = on_start
        started = False
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
                    if await self._fence(conn):
                        if started:
                            ready = await self._guard(
                                conn,
                                self._requeue_after_loss,
                                name="lock_lost_requeue",
                                event=_REQUEUE_CANCELLED,
                            )
                        else:
                            ready = start is None or await self._guard(
                                conn, start, name="on_start", event=_START_CANCELLED
                            )
                            if ready:
                                start, started = None, True
                        if ready:
                            await self._supervise(conn)
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

    async def _fence(self, conn: AsyncConnection) -> bool:
        """Wait :data:`LOCK_TAKEOVER_GRACE` after an acquisition, then prove the lock alive.

        Returns:
            Whether the lock is still held, so work may start.
        """
        _log.info("scheduler.lock_fence_wait", grace_s=LOCK_TAKEOVER_GRACE.total_seconds())
        await self._sleep(LOCK_TAKEOVER_GRACE.total_seconds())
        return await self._check_lock(conn)

    async def _guard(
        self,
        conn: AsyncConnection,
        work: Callable[[], Awaitable[None]],
        *,
        name: str,
        event: str,
    ) -> bool:
        """Run ``work`` as a task while proving the lock on ``conn`` alive throughout.

        The guard of the work :meth:`run_forever` runs outside the lanes: ``on_start`` and
        the lock-loss requeue (:meth:`_requeue_after_loss`), so neither can keep writing
        unmonitored after the lock session died. The lock is proved (:meth:`_check_lock`)
        on absolute deadlines every :data:`LOCK_CHECK_INTERVAL` of the monotonic clock, and
        the wait ends as soon as ``work`` does, so what follows it (the first wake) starts
        at once.

        Args:
            conn: The lock connection.
            work: Returns the awaitable to run.
            name: The task's name in logs, ``on_start`` or ``lock_lost_requeue``.
            event: The log event naming the task when it is cancelled.

        Returns:
            ``True`` once ``work`` returned normally; ``False`` when the lock was lost while
            it ran, after it was cancelled and awaited (``event``), so it is still pending
            for the next acquisition.

        Raises:
            asyncio.CancelledError: When the scheduler is cancelled, once ``work`` has been
                cancelled and awaited.
            Exception: Whatever ``work`` raised, or the injected sleep raised (once ``work``
                has been cancelled and awaited).
        """
        task = asyncio.create_task(_invoke(work), name=f"scheduler.{name}")
        tracked: dict[str, asyncio.Task[Any]] = {name: task}
        check_every = LOCK_CHECK_INTERVAL.total_seconds()
        try:
            check_at = self._monotonic() + check_every
            while not task.done():
                delay = check_at - self._monotonic()
                if delay > 0:
                    sleeper = asyncio.ensure_future(self._sleep(delay))
                    try:
                        await asyncio.wait((sleeper, task), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        if not sleeper.done():
                            sleeper.cancel()
                    if sleeper.done() and not sleeper.cancelled():
                        sleeper.result()  # An error of the injected sleep propagates.
                    continue
                if not await self._check_lock(conn):
                    await _stop(tracked, reason="lock_lost", event=event, abort=self._abort)
                    return False
                check_at = _next_deadline(check_at, check_every, self._monotonic())
        finally:
            if tracked and not task.done():
                await _stop(tracked, reason="cancelled", event=event)
        task.result()
        return True

    async def _supervise(self, conn: AsyncConnection) -> None:
        """Run the lanes while the lock on ``conn`` holds; return once it is lost.

        Wakes and lock checks fall on absolute deadlines of the monotonic clock: the first
        wake at once, then one every :data:`WAKE_INTERVAL` from it, and a lock check
        (:meth:`_check_lock`) every :data:`LOCK_CHECK_INTERVAL`. Between them the injected
        sleep waits only until the earliest pending deadline, and returns early when a lane
        ends, so neither a slow check nor a lane that runs for minutes ever moves a wake.
        Each wake dispatches every idle lane (:data:`_LANES`) as a task; a lane whose
        previous pass is still running is not dispatched again (``scheduler.lane_busy``), so
        at most one task per lane runs and no job is ever run twice at once. A busy lane is
        still looked into at every wake (:meth:`_wake`): a source lane's triggers are read
        by a check task, and a lane with work pending is dispatched again
        (``scheduler.lane_rerun``) the moment its pass ends, without waiting for the next
        wake.

        Every task (lane passes and trigger checks) ends before this method does, whatever
        ends it, so no cancelled work can write once the lock connection is released
        (``scheduler.lanes_cancelled`` names the tasks cancelled and why):

        * the lock is lost: every task is cancelled and awaited for at most
          :data:`LOCK_LOST_STOP_TIMEOUT`, then this method returns; a task still unwinding
          after that aborts the process (``scheduler.fence_stop_timeout``, :func:`_stop`);
        * the scheduler is cancelled (SIGTERM): every task is cancelled and awaited however
          long it takes (the lock is still held), then the cancellation propagates;
        * a pass or check ends with a defect (an exception its steps do not isolate): the
          other tasks are cancelled and awaited the same way, then the defect propagates out
          of :meth:`run_forever`.

        Raises:
            asyncio.CancelledError: When the scheduler is cancelled.
            Exception: The first lane defect.
        """
        lanes = _Lanes()
        reason = "lock_lost"
        lost = False
        check_every = LOCK_CHECK_INTERVAL.total_seconds()
        wake_every = WAKE_INTERVAL.total_seconds()
        try:
            wake_at = self._monotonic()
            check_at = wake_at + check_every
            while True:
                now = self._monotonic()
                wake_due, check_due = now >= wake_at, now >= check_at
                rerun_due = bool(lanes.ready())
                if wake_due or check_due or rerun_due:
                    if not await self._check_lock(conn):
                        lost = True
                        # A lane that ended with a defect meanwhile still ends the worker.
                        lanes.reap()
                        return
                    checked = self._monotonic()
                    if check_due:
                        check_at = _next_deadline(check_at, check_every, checked)
                    if wake_due:
                        wake_at = _next_deadline(wake_at, wake_every, checked)
                        self._wake(lanes)
                    elif rerun_due:
                        self._rerun(lanes)
                await self._pause(lanes, min(wake_at, check_at))
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception:
            reason = "defect"
            raise
        finally:
            # Once the lock is lost, a defect found while reaping must not unbound the wait.
            await _stop(
                lanes.take(),
                reason="lock_lost" if lost else reason,
                abort=self._abort if lost else None,
            )

    def _wake(self, lanes: _Lanes) -> None:
        """Dispatch every idle lane; look into each busy one so its pending work is not missed.

        An idle lane (a lane marked for a rerun included) is dispatched as a task. A busy
        lane is left running (``scheduler.lane_busy``), and unless it is already marked for
        a rerun:

        * a busy source lane gets one trigger check (:meth:`_check_lane`) at a time, which
          reads its cheap triggers while its pass runs and marks it for a rerun when any
          fired, so the segment window and the other triggers are checked on every wake;
        * a busy ``analysis`` lane is marked for a rerun, so a request queued while it runs
          is coalesced right after it.

        Raises:
            Exception: The defect of a pass or check that ended since the last look.
        """
        lanes.reap()
        for lane in _LANES:
            if lane not in lanes.passes:
                self._dispatch(lanes, lane)
                continue
            _log.debug("scheduler.lane_busy", lane=lane)
            if lane in lanes.rerun:
                continue
            if lane == _LANE_ANALYSIS:
                lanes.rerun.add(lane)
            elif lane in _SOURCE_LANES and lane not in lanes.checks:
                lanes.watched[lane] = lanes.passes[lane]
                lanes.checks[lane] = asyncio.create_task(
                    self._check_lane(lane), name=f"scheduler.check.{lane}"
                )

    def _rerun(self, lanes: _Lanes) -> None:
        """Dispatch at once every lane marked for a rerun whose pass has ended.

        Called only right after a lock check passed, like every dispatch. The rerun keeps
        the lane's serialization: it is dispatched only once no pass of that lane runs.

        Raises:
            Exception: The defect of a pass or check that ended since the last look.
        """
        lanes.reap()
        for lane in lanes.ready():
            _log.info("scheduler.lane_rerun", lane=lane)
            self._dispatch(lanes, lane)

    def _dispatch(self, lanes: _Lanes, lane: str) -> None:
        """Start one pass of the idle ``lane`` as a task; it is no longer marked for a rerun."""
        lanes.rerun.discard(lane)
        lanes.passes[lane] = asyncio.create_task(self._lane(lane), name=f"scheduler.lane.{lane}")

    async def _pause(self, lanes: _Lanes, until: float) -> None:
        """Wait until the monotonic deadline ``until``, returning early when a task ends.

        The injected sleep waits only the time left until ``until``, nothing when it has
        passed. A pass or check ending wakes the wait at once, and every task that ended
        meanwhile is reaped before this returns, so the caller sees only tasks still running
        and can rerun a marked lane immediately.

        Raises:
            Exception: A pass's or check's defect, as soon as it ends with it, or whatever
                the injected sleep raised.
        """
        delay = until - self._monotonic()
        if delay <= 0:
            lanes.reap()
            return
        sleeper = asyncio.ensure_future(self._sleep(delay))
        try:
            await asyncio.wait((sleeper, *lanes.tasks()), return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not sleeper.done():
                sleeper.cancel()
        lanes.reap()
        if sleeper.done() and not sleeper.cancelled():
            sleeper.result()  # An error of the injected sleep propagates.

    async def _check_lock(self, conn: AsyncConnection) -> bool:
        """Prove the lock session alive within :data:`LOCK_CHECK_TIMEOUT`.

        A check that does not answer in time counts as a lost lock and is logged as
        ``scheduler.lock_lost`` too, with ``error`` ``TimeoutError``.
        """
        try:
            async with asyncio.timeout(LOCK_CHECK_TIMEOUT.total_seconds()):
                return await self._lock_alive(conn)
        except TimeoutError as exc:
            _log.error(
                "scheduler.lock_lost",
                error=type(exc).__name__,
                timeout_s=LOCK_CHECK_TIMEOUT.total_seconds(),
            )
            return False

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

    async def _requeue_after_loss(self) -> None:
        """Queue one default run after a re-acquisition, for the work the lost lock cancelled.

        A lane cancelled by the loss may have been an analysis run, or a sync whose
        completion request was never written, so a ``scheduler_lock_lost`` request makes a
        default run follow. It commits in a transaction of its own and runs under
        :meth:`_guard`, so the lock is proved alive while it waits (a blocked ``INSERT``):
        when the lock is lost meanwhile, it is cancelled and awaited
        (``scheduler.lock_lost_requeue_cancelled``) and runs again after the next
        acquisition, before any lane. A :data:`JOB_FAILURES` error is logged as
        ``scheduler.lock_lost_requeue_failed``, never raised: the lock check reconnects, and
        the next sync or activation queues a request of its own.
        """
        try:
            async with self._sessions()() as session, session.begin():
                await repositories.enqueue_analysis_request(session, _LOCK_LOST_REASON)
        except JOB_FAILURES as exc:
            _log.error("scheduler.lock_lost_requeue_failed", error=type(exc).__name__)
            return
        _log.info("scheduler.lock_lost_requeued", reason=_LOCK_LOST_REASON)

    async def _lane(self, lane: str) -> TickReport:
        """Run one pass of ``lane`` under a tick record of its own; log it as ``scheduler.lane``.

        A source lane runs, for its sources only, the steps datasets, recovery probes, sync
        requests, due jobs and segments, in that order; the ``purge`` and ``budget`` lanes
        run that one step. The ``analysis`` lane first waits, when a request is pending,
        until its oldest is :data:`COALESCE_WINDOW` old (:meth:`_await_coalescing`), then
        runs the coalescing step, so a default run starts about 60 s after the first request
        rather than at the first wake after that. Every step is isolated exactly as in
        :meth:`tick`. The pass is logged at info level when it did work, at debug level
        otherwise.

        Raises:
            ValueError: When ``lane`` is not one of :data:`_LANES`, or the clock is naive.
        """
        tick = _Tick(self._now())
        sources = _SOURCE_LANES.get(lane)
        if sources is not None:
            await self._source_steps(tick, sources)
        elif lane == _LANE_PURGE:
            await self._purge(tick)
        elif lane == _LANE_BUDGET:
            await self._refresh_budget(tick)
        elif lane == _LANE_ANALYSIS:
            if await self._await_coalescing(tick):
                await self._coalesce_analysis(tick)
        else:
            raise ValueError(f"unknown scheduler lane {lane!r}")
        report = tick.freeze()
        if report.did_work:
            _log.info("scheduler.lane", lane=lane, **report.to_log())
        else:
            _log.debug("scheduler.lane", lane=lane, **report.to_log())
        return report

    async def _check_lane(self, lane: str) -> tuple[str, ...]:
        """Read the cheap triggers of a busy source lane; return those that fired.

        Dispatched by a wake that finds ``lane`` still running, at most one at a time per
        lane, so what a wake must pick up (AAP 0.3.2: due jobs, ``sync_request`` rows and the
        segment trigger "checked on every worker wake") is read on every wake even while a
        long job holds the lane. It only reads, independent of the busy pass; the fetch
        itself stays inside the lane, which is marked to run again the moment its pass ends
        when any trigger fired (logged as ``scheduler.lane_work_pending``). The triggers, in
        this order:

        * ``sync_requests``: an unclaimed ``sync_request`` of the lane's sources;
        * ``due_jobs``: a due sync job (:meth:`due_jobs`) of the lane's sources;
        * ``segments``: when LinkedIn is in the lane, its 04:00 slot is due or
          ``jobs.segments_due`` reports the window end or the presets moved;
        * ``probes``: a lane source in automatic fallback is due its recovery probe.

        Each read is isolated like a step: a :data:`JOB_FAILURES` error is logged as
        ``scheduler.step_failed`` (step ``lane_check:<lane>:<trigger>``) and the other reads
        still run.

        Raises:
            KeyError: When ``lane`` is not a source lane.
        """
        sources = _SOURCE_LANES[lane]
        tick = _Tick(self._now())
        fired: list[str] = []
        async with self._step(tick, f"lane_check:{lane}:sync_requests"):
            if await self._sync_requests_pending(sources):
                fired.append("sync_requests")
        async with self._step(tick, f"lane_check:{lane}:due_jobs"):
            due = await self.due_jobs(tick.now)
            if any(source in due for source in sources):
                fired.append("due_jobs")
        if "linkedin" in sources:
            async with self._step(tick, f"lane_check:{lane}:segments"):
                _, segments_due, _ = await self._segments_due(tick.now)
                if segments_due:
                    fired.append("segments")
        for source in BOOTSTRAP_ORDER:
            if source not in sources or "probes" in fired:
                continue
            async with self._step(tick, f"lane_check:{lane}:probes:{source}"):
                if await self._probe_due(source, tick.now):
                    fired.append("probes")
        if fired:
            _log.info("scheduler.lane_work_pending", lane=lane, triggers=fired)
        return tuple(fired)

    # -- one tick ----------------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> TickReport:
        """Run every step of one wake below, in order, each isolated, for every source.

        The sequential form of one wake, for tests and callers that need a single pass; the
        running worker does not call it. :meth:`run_forever` runs the same steps split into
        lanes instead (steps 1 to 5 per source lane, then a lane each for the purge, the
        budget and the analysis), so a long job delays only its own lane.

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
        await self._source_steps(tick, SOURCES)
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

    async def _source_steps(self, tick: _Tick, sources: tuple[Source, ...]) -> None:
        """Run steps 1 to 5 for ``sources``: datasets, probes, sync requests, due jobs, segments.

        :meth:`tick` passes every source; a source lane passes its own.
        """
        await self._ensure_datasets(tick, sources)
        await self._probe_fallbacks(tick, sources)
        await self._serve_sync_requests(tick, sources)
        await self._run_due_jobs(tick, sources)
        await self._run_segments(tick, sources)

    # step 1 ----------------------------------------------------------------------------------

    async def _ensure_datasets(self, tick: _Tick, sources: tuple[Source, ...] = SOURCES) -> None:
        """Give each of ``sources`` an active primary dataset of its mode, in bootstrap order."""
        for source in BOOTSTRAP_ORDER:
            if source not in sources:
                continue
            async with self._step(tick, f"datasets:{source}"):
                outcome = await self._controller.ensure_dataset(source)
                if outcome is not None:
                    tick.builds[source] = outcome
                    tick.acted("datasets")

    # step 2 ----------------------------------------------------------------------------------

    async def _probe_fallbacks(self, tick: _Tick, sources: tuple[Source, ...] = SOURCES) -> None:
        """Probe each of ``sources`` in automatic fallback, at most every :data:`PROBE_INTERVAL`."""
        for source in BOOTSTRAP_ORDER:
            if source not in sources:
                continue
            async with self._step(tick, f"probes:{source}"):
                if not await self._probe_due(source, tick.now):
                    continue
                await self._probe(tick, source)

    async def _probe_due(self, source: Source, now: datetime) -> bool:
        """Whether ``source`` is in automatic fallback and its last probe is old enough.

        Due when no ``probe`` ``sync_run`` of the source started within
        :data:`PROBE_INTERVAL` of ``now``. Those rows record every probe: this scheduler's
        recovery probes and the probes ``ensure_dataset`` runs (the worker's bootstrap, a wake
        that resolves live without live data). A fallback entered by such a probe therefore
        gets its first recovery probe 30 minutes after it, not at the next wake. A fallback
        entered by a denial during a live job has no probe of its own: it is probed once the
        source's latest probe, if any, is that old. Only reads.
        """
        if not await self._controller.in_fallback(source):
            return False
        history = await self._history(now, (JOB_PROBE,))
        last = history.get((source, JOB_PROBE))
        return last is None or now - last >= PROBE_INTERVAL

    async def _probe(self, tick: _Tick, source: Source) -> None:
        """Run one recovery probe of ``source`` under a ``probe`` ``sync_run`` row.

        The row is inserted (and committed) before the probe, so probes stay spaced even when
        one fails. It ends ``succeeded`` when the probe passed and the live dataset built
        from it was activated, ``failed`` otherwise, with the reason.

        Raises:
            Exception: Any error of ``recover``, unchanged, once the row was closed as failed
                with the error's class name (best effort: a database error doing so is
                logged as ``scheduler.sync_run_close_failed``, never raised in its place).
            asyncio.CancelledError: Unchanged, when the task running the probe is cancelled
                (a lost scheduler lock, the worker's graceful shutdown) once the row has an
                id, wherever the probe then was, the row's own close included
                (:meth:`_close_cancelled_probe`). It is logged as
                ``scheduler.probe_cancelled`` and, within ``jobs.CANCEL_CLOSE_TIMEOUT``, the
                row is closed ``failed`` with ``recovery probe cancelled`` if it is still
                ``running``; a close committed before the cancellation stands. Bookkeeping
                that times out or fails is logged as ``scheduler.sync_run_close_failed`` and
                abandoned, and a further cancellation while it runs propagates at once.
        """
        run_id = await self._open_run(source, JOB_PROBE, tick.now)
        tick.probed.append(source)
        tick.acted("probes")
        try:
            try:
                outcome = await self._controller.recover(source)
            except Exception as exc:
                await self._close_run_quietly(run_id, error=type(exc).__name__)
                raise
            if outcome is None:
                await self._close_run(run_id, SYNC_RUN_FAILED, error="recovery probe did not pass")
            elif outcome.activated and outcome.mode == "live":
                await self._close_run(run_id, SYNC_RUN_SUCCEEDED)
            else:
                await self._close_run(
                    run_id,
                    SYNC_RUN_FAILED,
                    error=outcome.reason or "live dataset was not activated",
                )
        except asyncio.CancelledError:
            # Also a cancellation that lands in a close above, whose transaction rolled back.
            await self._close_cancelled_probe(source, run_id)
            raise

    # step 3 ----------------------------------------------------------------------------------

    async def _serve_sync_requests(
        self, tick: _Tick, sources: tuple[Source, ...] = SOURCES
    ) -> None:
        """Claim the pending "Sync now" requests of ``sources`` and run each source's jobs."""
        claimed: tuple[Source, ...] = ()
        async with self._step(tick, "sync_requests"):
            claimed = await self._claim_sync_requests(tick.now, sources)
        for source in claimed:
            tick.sync_requests.append(source)
            tick.acted("sync_requests")
            async with self._step(tick, f"sync_requests:{source}"):
                report = await jobs.run_source_jobs(
                    source, trigger="manual", controller=self._controller, now=tick.now
                )
                tick.ran("sync_requests", report)

    async def _claim_sync_requests(
        self, now: datetime, sources: tuple[Source, ...] = SOURCES
    ) -> tuple[Source, ...]:
        """Stamp ``picked_at`` on the pending ``sync_request`` rows of ``sources``.

        ``FOR UPDATE SKIP LOCKED`` keeps a row being claimed elsewhere out of this claim, and
        a lane claims only its own sources' rows, so another lane's requests stay pending for
        that lane. The sources come back in request order.
        """
        pending = (
            select(SyncRequest.id)
            .where(SyncRequest.picked_at.is_(None), SyncRequest.source.in_(sources))
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

    async def _sync_requests_pending(self, sources: tuple[Source, ...]) -> bool:
        """Whether any of ``sources`` has an unclaimed ``sync_request``; reads, never claims."""
        async with self._sessions()() as session:
            found = await session.scalar(
                select(SyncRequest.id)
                .where(SyncRequest.picked_at.is_(None), SyncRequest.source.in_(sources))
                .limit(1)
            )
        return found is not None

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

    async def _run_due_jobs(self, tick: _Tick, sources: tuple[Source, ...] = SOURCES) -> None:
        """Run the due sync jobs of each of ``sources`` in one ``run_source_jobs`` call each.

        The sources run in :data:`~ads_engine.settings.SOURCES` order (the order
        :meth:`due_jobs` returns), so LinkedIn's leads always precede HubSpot's ``crm`` job.
        """
        due: Mapping[Source, tuple[str, ...]] = MappingProxyType({})
        async with self._step(tick, "due_jobs"):
            due = await self.due_jobs(tick.now)
        for source, names in due.items():
            if source not in sources:
                continue
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

    async def _run_segments(self, tick: _Tick, sources: tuple[Source, ...] = SOURCES) -> None:
        """Fetch LinkedIn's segments at 04:00 or when the window trigger fires (AAP 0.3.2).

        The trigger (``jobs.segments_due``) fires when the default window end has passed the
        latest fetched segment end or ``window.presets`` changed; it backs off a failed fetch
        itself. Without an active LinkedIn dataset nothing runs: the dataset's backfill
        fetches its segments. Nothing runs either when ``sources`` leaves LinkedIn out (a lane
        of other sources). Detection and execution share one configuration snapshot, read
        from the controller once per step (:meth:`_segments_due`), so the presets judged due
        are the presets fetched and recorded, and a fetch satisfies the trigger instead of
        repeating on every wake.
        """
        if "linkedin" not in sources:
            return
        async with self._step(tick, "segments"):
            slot_due, run_now, config = await self._segments_due(tick.now)
            if not run_now or config is None:
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

    async def _segments_due(self, now: datetime) -> tuple[bool, bool, Thresholds | None]:
        """Whether LinkedIn's 04:00 segment slot is due, whether a fetch is due at all, and why.

        Returns ``(slot_due, due, config)``: ``due`` holds when the slot is due or
        ``jobs.segments_due`` reports the window end or ``window.presets`` moved, judged on
        ``config``, the configuration snapshot read from the controller once here, which the
        segment step then hands to the fetch so detection and execution agree. Without an
        active LinkedIn dataset nothing is read further and the result is
        ``(False, False, None)``. Only reads.

        Raises:
            ConfigError: When the configuration cannot be loaded.
            sqlalchemy.exc.SQLAlchemyError: When the history or freshness cannot be read.
        """
        entry = _ENTRIES[jobs.JOB_SEGMENTS]
        async with self._sessions()() as session:
            if await repositories.active_dataset_id(session, "linkedin") is None:
                return False, False, None
            config = self._controller.thresholds
            history = await self._read_history(session, now, (jobs.JOB_SEGMENTS,))
            slot_due = is_due(entry, now, self._tz, history.get(("linkedin", entry.job)))
            due = slot_due or await jobs.segments_due(session, now=now, thresholds=config)
            return slot_due, due, config

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
                        SourceDataset.state == DATASET_STATE_RETIRED,
                        SourceDataset.role == DATASET_ROLE_PRIMARY,
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
        rows = dict(counts) if counts and error is None else {SOURCE_GOOGLE: 0}
        async with self._sessions()() as session, session.begin():
            session.add_all(
                SyncRun(
                    source=source,
                    job=JOB_PURGE,
                    dataset=None,
                    started_at=started,
                    finished_at=finished,
                    status=SYNC_RUN_FAILED if error is not None else SYNC_RUN_SUCCEEDED,
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

    async def _await_coalescing(self, tick: _Tick) -> bool:
        """In the analysis lane, wait until the oldest pending request is coalesced.

        Reads the backlog once (``runner.pending_requests``). When the oldest request is
        younger than :data:`COALESCE_WINDOW` by the scheduler clock, sleeps (the injected
        sleep) until it is that old, at most :data:`COALESCE_WINDOW`, so a clock skewed
        against the database's ``requested_at`` never holds the lane longer. Every wake is at
        most :data:`WAKE_INTERVAL` apart, so a new request is seen well before its 60 s
        deadline and the run starts at about that deadline instead of at the first wake
        after it. Requests queued meanwhile join the same run. :meth:`tick` never waits: it
        calls :meth:`_coalesce_analysis` directly.

        Returns:
            Whether a request is pending, so the coalescing step should run; ``False`` too
            when the read failed (a :data:`JOB_FAILURES` error, logged and recorded on
            ``tick`` as the ``analysis`` step).
        """
        pending = False
        async with self._step(tick, "analysis"):
            async with self._sessions()() as session:
                backlog = await runner.pending_requests(session)
            if backlog is not None:
                left = backlog.oldest_requested_at + COALESCE_WINDOW - self._now()
                if left > timedelta(0):
                    wait_s = min(left, COALESCE_WINDOW).total_seconds()
                    _log.debug(
                        "scheduler.analysis_coalescing", wait_s=wait_s, requests=backlog.count
                    )
                    await self._sleep(wait_s)
                pending = True
        return pending

    async def _coalesce_analysis(self, tick: _Tick) -> None:
        """Serve the pending ``analysis_request`` backlog with one default run, once coalesced.

        The backlog is read as one bounded summary (``runner.pending_requests``: the count,
        the oldest request, the highest id and a few reasons), however many requests piled up.
        Once the oldest is :data:`COALESCE_WINDOW` old, one default run serves every request
        pending up to that highest id (``serve_through``): the runner claims them all with one
        set-based update before it loads its context, and a request queued after the read
        stays pending for the next tick. Behind another holder of the ``analysis`` lock
        (``RunInProgress``) nothing is claimed, the requests stay pending and
        ``scheduler.analysis_deferred`` is logged. A run that lost its lock to another process
        (``RunNoLongerRunning``) keeps the requests it claimed stamped as served by the failed
        run, so a fresh request is queued for the default run to catch up.
        """
        async with self._step(tick, "analysis"):
            at = self._now()
            async with self._sessions()() as session:
                backlog = await runner.pending_requests(session)
            if backlog is None or at - backlog.oldest_requested_at < COALESCE_WINDOW:
                return
            tick.acted("analysis")
            thresholds = Thresholds.load()
            window = await runner.default_window(now=at, thresholds=thresholds)
            try:
                run = await runner.run_analysis(
                    window,
                    "default",
                    "event",
                    reason=_join_reasons(backlog.reasons),
                    serve_through=backlog.through_id,
                    thresholds=thresholds,
                    now=at,
                )
            except RunInProgress:
                tick.analysis_deferred = True
                _log.info(
                    "scheduler.analysis_deferred",
                    reason="run_in_progress",
                    requests=backlog.count,
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
        """Insert and commit a ``running`` ``sync_run`` row; return its id.

        Only recovery probes open their row this way. A cancellation that lands once the row
        has its id (in the commit, which may or may not have reached the server) closes it
        as a cancelled probe (:meth:`_close_cancelled_probe`) before it propagates; one that
        lands earlier rolls the insert back.
        """
        run_id: int | None = None
        try:
            async with self._sessions()() as session, session.begin():
                row = SyncRun(
                    source=source,
                    job=job,
                    dataset=None,
                    started_at=started,
                    status=SYNC_RUN_RUNNING,
                )
                session.add(row)
                await session.flush()
                run_id = row.id
                return run_id
        except asyncio.CancelledError:
            if run_id is not None:
                await self._close_cancelled_probe(source, run_id)
            raise

    async def _close_run(self, run_id: int, status: str, *, error: str | None = None) -> None:
        """Close a still-``running`` ``sync_run`` row the scheduler opened.

        A row a committed close already ended is left as it was, so a late close (a
        cancellation's) never overwrites the outcome recorded before it.
        """
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id, SyncRun.status == SYNC_RUN_RUNNING)
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
            await self._close_run(run_id, SYNC_RUN_FAILED, error=error)
        except SQLAlchemyError as exc:
            _log.error("scheduler.sync_run_close_failed", run_id=run_id, error=type(exc).__name__)

    async def _close_cancelled_probe(self, source: Source, run_id: int) -> None:
        """Log ``scheduler.probe_cancelled`` and close the probe's row as cancelled, bounded.

        A lost lock cancels and awaits the lanes for at most :data:`LOCK_LOST_STOP_TIMEOUT`,
        so this bookkeeping is abandoned after ``jobs.CANCEL_CLOSE_TIMEOUT`` (checked at
        import to be shorter), or at once when it fails, and logged as
        ``scheduler.sync_run_close_failed``; a further cancellation propagates at once. Only a
        row still ``running`` is closed: ``failed`` with ``recovery probe cancelled``, a fixed
        reason that carries no error text. Nothing else is written.

        Args:
            source: The probed source.
            run_id: The probe's ``sync_run`` row.
        """
        _log.warning("scheduler.probe_cancelled", source=source, run_id=run_id)
        try:
            async with asyncio.timeout(jobs.CANCEL_CLOSE_TIMEOUT.total_seconds()):
                await self._close_run_quietly(run_id, error="recovery probe cancelled")
        except TimeoutError as exc:
            # Best effort: the cancellation must end the probe within the lock-loss bound.
            _log.error("scheduler.sync_run_close_failed", run_id=run_id, error=type(exc).__name__)
