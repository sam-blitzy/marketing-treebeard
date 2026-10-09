"""The running scheduler's lanes, wakes and lock fencing (``ads_engine.sync.scheduler``).

AAP 0.3.2 ("The worker wakes every 30 s to pick up due jobs, pending ``sync_request`` rows and
pending ``analysis_request`` rows"; default analysis "coalesced within 60 s") and AAP 0.2.2
("The worker is a single active instance, enforced by a PostgreSQL advisory lock"). Each test
drives ``Scheduler.run_forever`` itself, as ``worker/main.py`` does:

* **Lanes**: while a source lane is blocked in a long job, later wakes still dispatch the
  analysis and budget lanes, a pending ``analysis_request`` older than ``COALESCE_WINDOW`` is
  served by a default run, wakes stay exactly ``WAKE_INTERVAL`` apart, and the blocked lane
  is never dispatched twice.
* **Wake deadlines**: with every lock check taking 4 s, wakes are still exactly 30 s apart.
* **Coalescing deadline**: a request arriving 1 s after a wake is served at 60 s of age by
  the analysis lane's wait, not at the third wake (about 90 s).
* **Busy-lane reruns**: a wake that finds the ``linkedin_hubspot`` lane busy while a
  ``sync_request`` of its sources is pending checks its triggers and reruns the lane the
  moment its pass ends, after a fresh lock check and before any further wake, never running
  it twice at once; a busy ``analysis`` lane reruns the same way. The trigger check also
  reads LinkedIn's segment slot and due recovery probes, and a failed read is logged while
  the others still run.
* **Lock lost mid-job**: killing the lock session cancels the blocked lane before the lock is
  taken again; after re-acquiring, the scheduler queues a ``scheduler_lock_lost`` request.
* **Interrupted "Sync now"**: a request whose pass the lost lock cancelled is pending again
  once the lock is re-acquired, before any lane runs, and is served at the next wake, even
  when its job closed its own row ``failed`` (``job cancelled``) so the sweep finds nothing
  (``scheduler.sync_requests_requeued``); one whose source got a newer pending request
  meanwhile stays picked and is reported as kept, leaving one pending row; a request served
  earlier in the cancelled pass is not re-queued; a requeue rolled back by a database error
  is logged with the request it keeps, and the next acquisition re-queues it; a claim whose
  ``UPDATE`` fails records nothing, while one ended by an error or a cancellation after its
  rows were recorded keeps its records, which the next requeue clears, re-queueing only a
  claim whose commit landed.
* **Lock lost during startup or the requeue**: ``on_start`` is cancelled, then run again
  after the next acquisition, before any lane pass; a blocked ``scheduler_lock_lost`` requeue
  is cancelled the same way and no lane is dispatched until the lock is held again.
* **Fresh check before dispatch**: a lock session that dies as ``on_start`` returns is found
  by the check that precedes the first dispatch, so no lane runs without the lock.
* **Bounded unwind**: after a lost lock (whose cancellation carries ``jobs.CANCEL_LOCK_LOST``),
  a task that keeps unwinding past ``LOCK_LOST_STOP_TIMEOUT`` aborts the process (the injected
  abort, exit code 1): with no probe at all when the lock check failed with an answer (a
  terminated lock session), or once the one probe gets an answer when the check went silent;
  SIGTERM and a defect cancel plainly, never probe, wait for every task however long and
  never abort.
* **Answered loss and takeover**: with the lock session terminated, a successor session
  taking the lock and a probe that would call the database silent, a stuck unwind is never
  probed for: the abort fires the real ``LOCK_LOST_STOP_TIMEOUT`` after the cancellation, well
  inside ``LOCK_TAKEOVER_GRACE``, before the unwind's delayed write lands. The startup guard
  fences the same way: an answered loss aborts unprobed, a silent one waits on a silent
  database and reruns the startup after the next acquisition.
* **Silent database**: when a lock check went silent and the probe gets no answer, the stuck
  task is awaited (``scheduler.fence_wait_unreachable``) with no abort, the database is
  probed again every ``FENCE_PROBE_INTERVAL``, and once the task has unwound the lock is
  taken again and the lanes resume; a probe answered while the task still unwinds
  (``scheduler.fence_wait_reachable``) leaves it ``LOCK_LOST_STOP_TIMEOUT``, then aborts.
* **Probe classification**: the default probe reads only silence as unreachable (its deadline,
  or libpq's connect timeout against a server that accepts TCP and never answers); success,
  failed authentication, too many clients, a refused connection, a missing database and a
  socket error all read as reachable, logged by class name only.
* **Standby and takeover grace**: a second scheduler waits as standby and, once it acquires
  the lock, waits ``LOCK_TAKEOVER_GRACE`` before its ``on_start``.
* **Orphaned ``sync_run`` recovery**: after the grace and before ``on_start``, rows left
  ``running`` end ``failed`` with the interrupted error and ``finished_at``, while rows created
  after the acquisition stay ``running``; the "Sync now" request whose manual run they belong
  to is pending again and served by its lane, unless its source already has a pending
  request; behind ``sync_writer`` holders
  the sweep defers and later wakes retry only the original rows, one retry at a time; a
  failed sweep is logged and retried; a re-acquisition after a lost lock recovers the row the
  cancelled lane left and re-runs its request, before the ``scheduler_lock_lost`` requeue,
  which then finds that request already pending and leaves it alone.
  Every sweep that finds no writer waits ``SYNC_WRITER_FENCE`` through the injected sleep
  before it writes, and none waits when there is no candidate.
* **Re-queue correlation** (one sweep driven directly): a swept row belongs to the request of
  the latest claim of its lane at or before its ``started_at`` when it is a job of that
  request's manual run and the job's first run since the claim. Rows stamped with their jobs'
  own starts (LinkedIn ``entities`` 0.5 s after the claim, the companion CRM's three dataset
  rows 3 s after it) re-queue their request once; a scheduled run after a completed "Sync
  now", HubSpot's ``crm`` after a LinkedIn claim (and LinkedIn's ``leads`` after a HubSpot
  one), ``probe`` and ``restatement`` rows re-queue nothing; a pending request of the source
  keeps the interrupted one picked; two lanes swept at once each re-queue their own request;
  of HubSpot and LinkedIn claimed in one pass, the interrupted request is re-queued, whichever
  id is greater, with the one served after it, never the one served before.
* **Sync-writer fence**: a CLI writer whose lock session is terminated mid-run is cancelled
  with ``jobs.CANCEL_WRITER_LOCK_LOST`` and ends with ``SyncWriterLockLostError`` (its task's
  cancellation count restored) before the sweep that found the key free commits, so its row
  is never recorded as interrupted while it still runs; a writer that starts during the fence
  makes the sweep defer; a writer still unwinding ``LOCK_LOST_STOP_TIMEOUT`` after the
  cancellation aborts its process (the injected abort); an outside cancellation propagates as
  ``CancelledError``, the lock lost or not; a block that ends normally releases the lock and
  its supervisor after checks that all passed.
* **Defects**: a defect in one lane cancels the others and propagates out of ``run_forever``,
  releasing the lock.
* **Check timeout**: a lock check that does not answer within ``LOCK_CHECK_TIMEOUT`` counts as
  a lost lock.
* **Cancellation**: SIGTERM while ``on_start`` runs cancels and awaits it before propagating.
* **Per-lane claims**: the google lane claims only Google's ``sync_request`` rows.
* **Recovery-probe cancellation** (driving ``tick`` or ``run_forever``): a probe cancelled in
  ``recover`` (directly, by SIGTERM or by a lost lock) logs ``scheduler.probe_cancelled``,
  closes its ``probe`` row ``failed`` with ``recovery probe cancelled`` and re-raises; a
  close the cancellation rolled back ends the same way, a close committed before it stands;
  a cancellation before the row's insert is flushed writes nothing, one after its commit
  closes it; bookkeeping that fails or outlasts ``jobs.CANCEL_CLOSE_TIMEOUT`` is logged as
  ``scheduler.sync_run_close_failed`` and abandoned, and a second cancellation during it
  propagates at once.

How. Real PostgreSQL 18 through ``conftest.db`` (emptied before each test). The schedulers of
a test share a :class:`VirtualTime`: their monotonic clock (and, when a test asks, their wall
clock) moves only when an injected sleep completes, to that sleep's start plus its duration,
and each sleep really waits one millisecond per virtual second, so 30 s wakes take
milliseconds and concurrent sleeps finish in virtual-time order. Source work that would reach
the simulator is replaced: ``SourceModeController.ensure_dataset`` builds nothing (or blocks
for chosen sources), ``in_fallback`` is false (in the probe tests, true for Google alone,
whose ``recover`` blocks until cancelled or returns a chosen outcome), and
``jobs.run_source_jobs`` is a recording fake that can block until released or cancelled. A
lock session dies the way a crashed or disconnected one would: ``pg_terminate_backend`` of
the backend that holds the scheduler lock in ``pg_locks``, so the next check fails with an
answer. A lock check goes silent the way one against a server that stopped answering does:
it hangs past a shortened ``LOCK_CHECK_TIMEOUT``. The injected abort records its code and
raises :class:`AbortCalledError`; the injected database probe (``reachable``) answers from a
list the test sets, then "reachable".
"""

import asyncio
import errno
import hashlib
import socket
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Final, NoReturn

import psycopg
import pytest
from psycopg.errors import ConnectionTimeout
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)

from ads_engine.analysis.lock import ANALYSIS_LOCK_KEY
from ads_engine.db import repositories
from ads_engine.db import session as db_session
from ads_engine.db.models.analysis import AnalysisRequest, AnalysisRun
from ads_engine.db.models.ops import (
    DATASET_HUBSPOT_ASSOCIATIONS,
    DATASET_HUBSPOT_CONTACTS,
    DATASET_HUBSPOT_DEAL_SEARCH,
    SyncRequest,
    SyncRun,
)
from ads_engine.settings import Source, get_settings
from ads_engine.sync import jobs
from ads_engine.sync import scheduler as scheduler_module
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.mode import BuildOutcome, SourceModeController
from ads_engine.sync.scheduler import (
    COALESCE_WINDOW,
    FENCE_PROBE_INTERVAL,
    FENCE_PROBE_TIMEOUT,
    JOB_PROBE,
    LOCK_CHECK_INTERVAL,
    LOCK_CHECK_TIMEOUT,
    LOCK_LOST_STOP_TIMEOUT,
    LOCK_TAKEOVER_GRACE,
    SCHEDULER_LOCK_KEY,
    STANDBY_RETRY,
    SYNC_WRITER_FENCE,
    SYNC_WRITER_LOCK_KEY,
    WAKE_INTERVAL,
    Scheduler,
    SyncWriterLockLostError,
    sync_writer,
)

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]

_WAIT_S: Final = 60.0
"""The longest any single wait of a test may take before it fails."""

_REAL_S_PER_VIRTUAL_S: Final = 0.001
"""How long the injected sleep really waits per virtual second it is asked for."""

_STOP_TIMEOUT_S: Final = 0.05
"""``LOCK_LOST_STOP_TIMEOUT`` (real seconds) in the bounded-unwind tests."""

_OVERSTAY_S: Final = 0.2
"""How long a stubborn task keeps unwinding (real seconds), well past :data:`_STOP_TIMEOUT_S`."""

_PROBE_INTERVAL_S: Final = 0.01
"""``FENCE_PROBE_INTERVAL`` (real seconds) in the silent-database tests."""

_CHECK_LATENCY_S: Final = 4.0
"""The virtual time each lock check takes in the slow-check test."""

_SILENT_CHECK_TIMEOUT_S: Final = 0.5
"""``LOCK_CHECK_TIMEOUT`` (real seconds) in the tests whose lock check goes silent.

Long enough that a real ``SELECT 1`` on a loaded host never times out by accident, short
enough that the silenced check costs the test half a second.
"""

_STUCK_WRITE: Final = "stuck_unwind_write"
"""The ``analysis_request.reason`` a stuck unwind writes once its delayed bookkeeping returns."""

_AUTH_FAILED: Final = 'connection failed: FATAL:  password authentication failed for user "ads"'
"""The text of psycopg's error when a rotated password fails the probe's authentication."""

_LOCK_LOST: Final = "scheduler_lock_lost"
_PROBE_CANCELLED: Final = "recovery probe cancelled"
_INTERRUPTED: Final = "interrupted: the process running this job ended before it finished"


# ---------------------------------------------------------------------------------------------
# Fakes and recorders
# ---------------------------------------------------------------------------------------------


class Recorder:
    """The ordered events every fake of one test appends to."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def add(self, event: str) -> None:
        """Append ``event``."""
        self.events.append(event)

    def first(self, event: str) -> int:
        """Return the index of the first ``event``."""
        return self.events.index(event)

    def nth(self, event: str, n: int) -> int:
        """Return the index of the ``n``-th (1-based) ``event``."""
        positions = [index for index, seen in enumerate(self.events) if seen == event]
        assert len(positions) >= n, f"{event!r} occurred {len(positions)} times, not {n}"
        return positions[n - 1]

    def starting(self, prefix: str) -> list[int]:
        """Return the indexes of every event starting with ``prefix``."""
        return [index for index, seen in enumerate(self.events) if seen.startswith(prefix)]


class AbortCalledError(Exception):
    """Raised by the injected abort, so a test sees the process abort as the loop's outcome."""

    def __init__(self, code: int) -> None:
        """Keep the exit code the scheduler asked for."""
        super().__init__(f"aborted with exit code {code}")
        self.code = code


class VirtualTime:
    """The monotonic (and optionally wall) clock of one test, in virtual seconds.

    Time moves only when :meth:`elapse` completes: to at least the instant it began plus its
    duration, never backwards. It really waits :data:`_REAL_S_PER_VIRTUAL_S` per virtual
    second, so concurrent waits finish in virtual-time order. An elapse that is cancelled
    moves nothing. While :meth:`freeze` is in force, a finished elapse waits for
    :meth:`thaw` before it moves the clock and returns, so no deadline can pass.
    """

    def __init__(self) -> None:
        """Start at virtual time 0, flowing."""
        self.seconds = 0.0
        self._flowing = asyncio.Event()
        self._flowing.set()

    def monotonic(self) -> float:
        """The current virtual time; the schedulers' injected monotonic clock."""
        return self.seconds

    def freeze(self) -> None:
        """Hold every elapse at its end until :meth:`thaw`."""
        self._flowing.clear()

    def thaw(self) -> None:
        """Let held and later elapses move the clock again."""
        self._flowing.set()

    async def elapse(self, seconds: float) -> None:
        """Let ``seconds`` of virtual time pass, as a sleep or a slow call would."""
        began = self.seconds
        await asyncio.sleep(max(seconds, 0.0) * _REAL_S_PER_VIRTUAL_S)
        await self._flowing.wait()
        self.seconds = max(self.seconds, began + seconds)


class FastSleep:
    """The injected sleep: records each request, then lets that much virtual time pass."""

    def __init__(self, recorder: Recorder, name: str, virtual: VirtualTime) -> None:
        self.calls: list[float] = []
        self.timed: list[tuple[str, float, float]] = []
        self._recorder = recorder
        self._name = name
        self._virtual = virtual

    async def __call__(self, seconds: float) -> None:
        """Record ``seconds`` with the calling task and its start, then elapse them."""
        self.calls.append(seconds)
        task = asyncio.current_task()
        self.timed.append((task.get_name() if task else "", self._virtual.seconds, seconds))
        self._recorder.add(f"{self._name}:sleep:{seconds:g}")
        await self._virtual.elapse(seconds)

    def by_task(self, name: str) -> list[tuple[float, float]]:
        """The (start, seconds) of every sleep the task ``name`` asked for."""
        return [(start, seconds) for task, start, seconds in self.timed if task == name]


class SourceJobs:
    """Stands in for ``jobs.run_source_jobs``: records calls, blocks chosen sources.

    A blocked call waits until :attr:`release` is set, then returns. When cancelled first, it
    records ``job_cancelled:<source>``, awaits :attr:`on_cancel` when set (a job's own cancel
    bookkeeping) and re-raises the cancellation; a stubborn source keeps unwinding until
    :attr:`release` is set, and only then re-raises. Any other call returns an empty report at
    once.
    """

    def __init__(self, recorder: Recorder) -> None:
        self.calls: list[tuple[Source, str]] = []
        self.blocked: set[Source] = set()
        self.stubborn: set[Source] = set()
        self.cancel_args: list[tuple[object, ...]] = []
        self.on_call: Callable[[Source, datetime], Awaitable[None]] | None = None
        self.on_cancel: Callable[[Source], Awaitable[None]] | None = None
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self._recorder = recorder

    async def __call__(
        self, source: Source, *, trigger: jobs.Trigger, **options: object
    ) -> jobs.SourceSyncReport:
        """Record the call; block until released or cancelled when ``source`` is blocked.

        :attr:`on_call`, when set, is awaited first with the source and the run's instant, as
        a job opening its ``sync_run`` rows would use it.
        """
        self.calls.append((source, trigger))
        self._recorder.add(f"job:{source}")
        now = options.get("now")
        if self.on_call is not None and isinstance(now, datetime):
            await self.on_call(source, now)
        if source in self.blocked:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError as exc:
                self._recorder.add(f"job_cancelled:{source}")
                self.cancel_args.append(exc.args)
                if self.on_cancel is not None:
                    await self.on_cancel(source)
                self.cancelled.set()
                if source in self.stubborn:
                    await self.release.wait()
                    self._recorder.add(f"job_unwound:{source}")
                raise
            self._recorder.add(f"job_released:{source}")
        return jobs.SourceSyncReport(source=source, trigger=trigger)


class Datasets:
    """Stands in for ``ensure_dataset``: builds nothing, or blocks chosen sources forever."""

    def __init__(self, recorder: Recorder) -> None:
        self.blocked: set[Source] = set()
        self.entered = asyncio.Event()
        self._recorder = recorder

    async def ensure(self, source: Source) -> BuildOutcome | None:
        """Return ``None`` (nothing to build); a blocked source waits until cancelled."""
        if source in self.blocked:
            self._recorder.add(f"datasets_blocked:{source}")
            self.entered.set()
            await asyncio.Event().wait()
        return None


class Recovery:
    """Stands in for ``SourceModeController.recover``: blocks until cancelled, or returns.

    While :attr:`block` is set (the default) a call sets :attr:`entered` and waits until it is
    cancelled, then sets :attr:`cancelled` and re-raises the cancellation. Otherwise it
    returns :attr:`outcome` at once.
    """

    def __init__(self) -> None:
        self.calls: list[Source] = []
        self.block = True
        self.outcome: BuildOutcome | None = None
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, source: Source) -> BuildOutcome | None:
        """Record the call; block until cancelled, or return :attr:`outcome`."""
        self.calls.append(source)
        if self.block:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return self.outcome


class Startup:
    """An ``on_start`` whose first call blocks until cancelled; later calls return at once."""

    def __init__(self, recorder: Recorder, name: str = "start", *, block_first: bool) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.done = asyncio.Event()
        self._recorder = recorder
        self._name = name
        self._block_first = block_first

    async def __call__(self) -> None:
        """Record ``<name><n>:begin``; block the first call when asked; record ``:done``."""
        self.calls += 1
        label = f"{self._name}{self.calls}"
        self._recorder.add(f"{label}:begin")
        self.entered.set()
        if self._block_first and self.calls == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._recorder.add(f"{label}:cancelled")
                raise
        self._recorder.add(f"{label}:done")
        self.done.set()


class Harness:
    """Builds schedulers on one virtual time and records what every scheduler does.

    Attributes:
        now: The wall clock's start; it stays fixed unless :attr:`wall_follows` is set.
        wall_follows: Whether the wall clock advances with the virtual time.
        passes: Lane passes started, per scheduler and lane.
        lane_starts: The virtual time each lane pass started, per scheduler and lane.
        max_active: The most passes of one (scheduler, lane) ever running at once.
        aborts: The exit codes the injected abort was called with.
        abort_at: The event-loop time of each abort.
        reachability: The answers the injected ``reachable`` probe gives, in order; once
            they run out it answers :attr:`reachable_by_default`.
        reachable_by_default: The probe's answer once :attr:`reachability` is exhausted.
        probe_at: The event-loop time and answer of each probe.
        during_probe: Awaited inside each probe before it answers, with the probe's number
            (1 for the first).
        lane_checked: Set whenever a busy-lane trigger check returned.
        on_lane_end: Awaited inside a lane pass as it ends, with (scheduler, lane, pass).
    """

    def __init__(self, db: Sessions, recorder: Recorder, jobs_fake: SourceJobs) -> None:
        self.db = db
        self.recorder = recorder
        self.jobs = jobs_fake
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.wall_follows = False
        self.time = VirtualTime()
        self.passes: dict[str, Counter[str]] = {}
        self.lane_starts: dict[str, dict[str, list[float]]] = {}
        self.active: Counter[tuple[str, str]] = Counter()
        self.max_active: Counter[tuple[str, str]] = Counter()
        self.aborts: list[int] = []
        self.abort_at: list[float] = []
        self.reachability: list[bool] = []
        self.reachable_by_default = True
        self.probe_at: list[tuple[float, bool]] = []
        self.during_probe: Callable[[int], Awaitable[None]] | None = None
        self.lane_checked = asyncio.Event()
        self.on_lane_end: Callable[[str, str, int], Awaitable[None]] | None = None
        self.sleeps: dict[str, FastSleep] = {}
        self.names: dict[int, str] = {}
        self._tasks: list[asyncio.Task[None]] = []

    def scheduler(self, name: str) -> Scheduler:
        """Return a scheduler named ``name`` on the shared virtual time and its own sleep."""
        sleep = FastSleep(self.recorder, name, self.time)
        controller = SourceModeController(get_settings(), clock=self.clock)
        built = Scheduler(
            controller=controller,
            clock=self.clock,
            sleep=sleep,
            monotonic=self.time.monotonic,
            abort=self.abort,
            reachable=self.reachable,
        )
        self.names[id(built)] = name
        self.passes[name] = Counter()
        self.lane_starts[name] = {}
        self.sleeps[name] = sleep
        return built

    def clock(self) -> datetime:
        """The wall clock of every scheduler, controller and budget of the test."""
        if self.wall_follows:
            return self.now + timedelta(seconds=self.time.seconds)
        return self.now

    def abort(self, code: int) -> NoReturn:
        """The injected abort: record ``code``, then raise :class:`AbortCalledError`."""
        self.aborts.append(code)
        self.abort_at.append(asyncio.get_running_loop().time())
        self.recorder.add(f"abort:{code}")
        raise AbortCalledError(code)

    async def reachable(self) -> bool:
        """The injected database probe: the next answer of :attr:`reachability`, recorded."""
        answer = self.reachability.pop(0) if self.reachability else self.reachable_by_default
        self.probe_at.append((asyncio.get_running_loop().time(), answer))
        self.recorder.add(f"probe:{'up' if answer else 'down'}")
        if self.during_probe is not None:
            await self.during_probe(len(self.probe_at))
        return answer

    def start(
        self, scheduler: Scheduler, on_start: Callable[[], Awaitable[None]] | None = None
    ) -> asyncio.Task[None]:
        """Run ``scheduler.run_forever(on_start)`` as a task, cancelled at teardown."""
        task = asyncio.create_task(scheduler.run_forever(on_start=on_start))
        self._tasks.append(task)
        return task

    async def stop_all(self) -> None:
        """Cancel every scheduler still running and wait until each, and every task it left
        behind after an abort, has ended."""
        self.time.thaw()
        for task in self._tasks:
            task.cancel()
        self.jobs.release.set()
        if self._tasks:
            await asyncio.wait(self._tasks)
        for task in self._tasks:
            if not task.cancelled():
                task.exception()
        leftovers = [
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith("scheduler.") and not task.done()
        ]
        for task in leftovers:
            task.cancel()
        if leftovers:
            await asyncio.wait(leftovers)


# ---------------------------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------------------------


async def _eventually(check: Callable[[], Awaitable[bool]], what: str) -> None:
    """Poll ``check`` until it holds, failing after :data:`_WAIT_S`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_S
    while not await check():
        if loop.time() > deadline:
            pytest.fail(f"timed out after {_WAIT_S}s waiting until {what}")
        await asyncio.sleep(0.01)


async def _wait_event(event: asyncio.Event, what: str) -> None:
    """Wait for ``event``, failing after :data:`_WAIT_S`."""
    try:
        async with asyncio.timeout(_WAIT_S):
            await event.wait()
    except TimeoutError:
        pytest.fail(f"timed out after {_WAIT_S}s waiting until {what}")


async def _cancel(task: asyncio.Task[Any]) -> None:
    """Cancel a scheduler as the worker's SIGTERM handler does; it must end cancelled."""
    task.cancel()
    await asyncio.wait((task,))
    if not task.cancelled():
        error = task.exception()
        raise AssertionError(f"the scheduler ended with {error!r}, not cancelled") from error


def _gaps(instants: list[float]) -> list[float]:
    """The differences between successive virtual instants."""
    return [later - earlier for earlier, later in pairwise(instants)]


def _lock_ids() -> tuple[int, int]:
    """``pg_locks`` (classid, objid) of the bigint advisory key :data:`SCHEDULER_LOCK_KEY`."""
    unsigned = SCHEDULER_LOCK_KEY & 0xFFFF_FFFF_FFFF_FFFF
    return unsigned >> 32, unsigned & 0xFFFF_FFFF


async def _lock_holder(db: Sessions) -> int | None:
    """Return the backend pid holding the scheduler lock in this database, or ``None``."""
    classid, objid = _lock_ids()
    async with db() as session:
        pid = await session.scalar(
            text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
                " AND classid = CAST(:classid AS oid) AND objid = CAST(:objid AS oid)"
                " AND objsubid = 1"
                " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            ),
            {"classid": classid, "objid": objid},
        )
    return None if pid is None else int(pid)


async def _kill_lock_session(db: Sessions) -> int:
    """Terminate the backend holding the scheduler lock; wait until the server has ended it."""
    holder: list[int] = []

    async def held() -> bool:
        pid = await _lock_holder(db)
        if pid is not None:
            holder.append(pid)
        return pid is not None

    await _eventually(held, "a session holds the scheduler lock")
    pid = holder[-1]
    async with db() as session:
        await session.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

    async def gone() -> bool:
        async with db() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
            )
        return int(count or 0) == 0

    await _eventually(gone, f"backend {pid} has ended")
    return pid


async def _add_sync_requests(db: Sessions, sources: Iterable[Source], at: datetime) -> list[int]:
    """Insert one pending ``sync_request`` per source; return their ids in that order."""
    async with db() as session, session.begin():
        rows = [SyncRequest(source=source, requested_at=at) for source in sources]
        session.add_all(rows)
        await session.flush()
        return [row.id for row in rows]


async def _picked(db: Sessions, request_id: int) -> bool:
    """Whether ``sync_request`` ``request_id`` has been claimed."""
    async with db() as session:
        row = await session.get(SyncRequest, request_id)
        assert row is not None
        return row.picked_at is not None


async def _picked_at(db: Sessions, request_id: int) -> datetime | None:
    """The ``picked_at`` of ``sync_request`` ``request_id``."""
    async with db() as session:
        row = await session.get(SyncRequest, request_id)
        assert row is not None
        return row.picked_at


async def _add_picked_request(db: Sessions, source: Source, picked_at: datetime) -> int:
    """Insert one ``sync_request`` already claimed at ``picked_at``; return its id."""
    async with db() as session, session.begin():
        row = SyncRequest(
            source=source, requested_at=picked_at - timedelta(seconds=5), picked_at=picked_at
        )
        session.add(row)
        await session.flush()
        return row.id


async def _add_sync_run(
    db: Sessions,
    source: Source,
    job: str,
    started_at: datetime,
    *,
    status: str = "running",
    dataset: str | None = None,
) -> int:
    """Insert one ``sync_run`` row (``running`` unless told otherwise); return its id."""
    async with db() as session, session.begin():
        row = SyncRun(
            source=source,
            job=job,
            dataset=dataset,
            started_at=started_at,
            finished_at=None if status == "running" else started_at,
            status=status,
        )
        session.add(row)
        await session.flush()
        return row.id


async def _sync_run(db: Sessions, run_id: int) -> SyncRun:
    """The ``sync_run`` row ``run_id``."""
    async with db() as session:
        row = await session.get(SyncRun, run_id)
        assert row is not None
        return row


async def _status(db: Sessions, run_id: int) -> str:
    """The status of ``sync_run`` row ``run_id``."""
    return (await _sync_run(db, run_id)).status


async def _lock_lost_requests(db: Sessions) -> int:
    """Count the ``scheduler_lock_lost`` analysis requests."""
    async with db() as session:
        found = await session.scalars(
            select(AnalysisRequest.id).where(AnalysisRequest.reason == _LOCK_LOST)
        )
        return len(list(found))


async def _probe_rows(db: Sessions) -> list[SyncRun]:
    """Every ``probe`` ``sync_run`` row, oldest first."""
    async with db() as session:
        found = await session.scalars(
            select(SyncRun).where(SyncRun.job == JOB_PROBE).order_by(SyncRun.id)
        )
        return list(found)


async def _pending_ids(db: Sessions, source: Source) -> list[int]:
    """The ids of the pending (unclaimed) ``sync_request`` rows of ``source``, oldest first."""
    async with db() as session:
        found = await session.scalars(
            select(SyncRequest.id)
            .where(SyncRequest.source == source, SyncRequest.picked_at.is_(None))
            .order_by(SyncRequest.id)
        )
        return [int(request_id) for request_id in found]


def _snapshot_before_lanes_resume(
    monkeypatch: pytest.MonkeyPatch, read: Callable[[], Awaitable[Any]]
) -> list[Any]:
    """Record ``read()`` once, as the lanes are about to resume after the first re-acquisition.

    ``Scheduler.run_forever`` starts supervising the lanes only after the orphaned-run recovery
    and the lock-loss requeue of that acquisition returned, so the returned list, once filled,
    holds the state those two left before any lane could claim a request again.
    """
    supervise = Scheduler._supervise
    seen: list[Any] = []
    calls = 0

    async def snapshot_then_supervise(
        self: Scheduler, conn: AsyncConnection, orphans: tuple[int, ...] = ()
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            seen.append(await read())
        await supervise(self, conn, orphans)

    monkeypatch.setattr(Scheduler, "_supervise", snapshot_then_supervise)
    return seen


def _tick_task(scheduler: Scheduler, now: datetime) -> asyncio.Task[Any]:
    """Run one ``tick`` as a task named like a lane, so the harness cancels it at teardown."""
    return asyncio.create_task(scheduler.tick(now), name="scheduler.tick")


def _logged(captured_logs: Any, event: str) -> list[dict[str, Any]]:
    """The captured log records of ``event``, in order."""
    return [record for record in captured_logs() if record.get("event") == event]


def _silent_lock_check(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> asyncio.Event:
    """Arm a lock check that goes silent, the way one against a server that stopped answering does.

    ``LOCK_CHECK_TIMEOUT`` shrinks to :data:`_SILENT_CHECK_TIMEOUT_S`. Once the returned event
    is set, the next lock check records ``check_silent`` and hangs until that timeout ends it
    (the event is cleared); every other check runs the real ``SELECT 1``. The lock session
    itself stays alive, so the lock is released only when the scheduler returns its connection.
    """
    monkeypatch.setattr(
        scheduler_module, "LOCK_CHECK_TIMEOUT", timedelta(seconds=_SILENT_CHECK_TIMEOUT_S)
    )
    silence_next = asyncio.Event()
    alive = Scheduler._lock_alive

    async def lock_alive(conn: AsyncConnection) -> bool:
        if silence_next.is_set():
            silence_next.clear()
            recorder.add("check_silent")
            await asyncio.Event().wait()
        return await alive(conn)

    monkeypatch.setattr(Scheduler, "_lock_alive", staticmethod(lock_alive))
    return silence_next


def _lock_lost_causes(captured_logs: Any) -> list[str]:
    """The ``cause`` of every ``scheduler.lock_lost`` record, in order."""
    return [str(record.get("cause")) for record in _logged(captured_logs, "scheduler.lock_lost")]


async def _stuck_writes(db: Sessions) -> int:
    """Count the :data:`_STUCK_WRITE` analysis requests a stuck unwind wrote."""
    async with db() as session:
        found = await session.scalars(
            select(AnalysisRequest.id).where(AnalysisRequest.reason == _STUCK_WRITE)
        )
        return len(list(found))


@asynccontextmanager
async def _silent_server() -> AsyncIterator[int]:
    """Serve TCP on a free loopback port that accepts every connection and never answers.

    Yields:
        The port; every accepted connection is closed, with the server, on exit.
    """
    accepted: list[asyncio.StreamWriter] = []

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Keep the connection open and silent until the server shuts down."""
        del reader
        accepted.append(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        for writer in accepted:
            writer.close()
        server.close()
        await server.wait_closed()


@contextmanager
def _refusing_port() -> Iterator[int]:
    """Hold a loopback port that is bound but not listening, so every connection is refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]


# ---------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def recorder() -> Recorder:
    """The test's ordered event log."""
    return Recorder()


@pytest.fixture
def source_jobs(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> SourceJobs:
    """``jobs.run_source_jobs`` replaced by a recording fake (the scheduler calls it there)."""
    fake = SourceJobs(recorder)
    monkeypatch.setattr(jobs, "run_source_jobs", fake)
    return fake


@pytest.fixture
def datasets(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> Datasets:
    """``ensure_dataset`` builds nothing and no source is in automatic fallback."""
    fake = Datasets(recorder)

    async def ensure_dataset(
        self: SourceModeController, source: Source, *, force_simulated: bool = False
    ) -> BuildOutcome | None:
        del self, force_simulated  # The fake decides per source only.
        return await fake.ensure(source)

    async def in_fallback(self: SourceModeController, source: Source) -> bool:
        del self, source  # No source is in automatic fallback.
        return False

    monkeypatch.setattr(SourceModeController, "ensure_dataset", ensure_dataset)
    monkeypatch.setattr(SourceModeController, "in_fallback", in_fallback)
    return fake


@pytest.fixture
def recovery(monkeypatch: pytest.MonkeyPatch, datasets: Datasets) -> Recovery:
    """Google alone is in automatic fallback, and ``recover`` is a :class:`Recovery` fake."""
    del datasets  # Requested so its ``in_fallback`` patch is applied first, then replaced.
    fake = Recovery()

    async def google_in_fallback(self: SourceModeController, source: Source) -> bool:
        del self  # Only the source decides here.
        return source == "google"

    async def recover(self: SourceModeController, source: Source) -> BuildOutcome | None:
        del self  # The fake decides alone.
        return await fake(source)

    monkeypatch.setattr(SourceModeController, "in_fallback", google_in_fallback)
    monkeypatch.setattr(SourceModeController, "recover", recover)
    return fake


@pytest.fixture
async def probe_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Callable[[str], None]]:
    """Point the default fence probe (``db.session.ping_database``) at a URL the test chooses.

    The returned function sets ``DATABASE_URL`` and forgets the cached settings and engines,
    so the next probe builds its engine on that URL. On teardown the engines are disposed and
    forgotten and the settings cache is cleared, so no later test reaches that URL.
    """

    def use(url: str) -> None:
        monkeypatch.setenv("DATABASE_URL", url)
        get_settings.cache_clear()
        db_session.reset_engine()

    try:
        yield use
    finally:
        await db_session.dispose_engine()
        db_session.reset_engine()
        get_settings.cache_clear()


@pytest.fixture
async def harness(
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    datasets: Datasets,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Harness]:
    """Schedulers on the emptied test database, each pass, check and acquisition recorded.

    Recorded events: ``<name>:lane:<lane>`` and ``<name>:lane_end:<lane>`` around each lane
    pass (with its virtual start time and concurrency), ``<name>:check:ok`` or
    ``<name>:check:lost`` after each lock check, ``<name>:lane_check:<lane>:<triggers>``
    after each busy-lane trigger check, and ``<name>:acquired`` after each acquisition.
    Every scheduler still running is cancelled and awaited at teardown, before ``conftest.db``
    disposes the engine, so no lock or connection outlives the test.
    """
    del datasets  # Requested for its patches.
    built = Harness(db, recorder, source_jobs)
    lane = Scheduler._lane
    acquire = Scheduler._acquire
    check_lock = Scheduler._check_lock
    check_lane = Scheduler._check_lane

    async def recorded_lane(self: Scheduler, name: str) -> Any:
        owner = built.names.get(id(self), "?")
        built.passes[owner][name] += 1
        built.lane_starts[owner].setdefault(name, []).append(built.time.seconds)
        key = (owner, name)
        built.active[key] += 1
        built.max_active[key] = max(built.max_active[key], built.active[key])
        recorder.add(f"{owner}:lane:{name}")
        try:
            return await lane(self, name)
        finally:
            built.active[key] -= 1
            if built.on_lane_end is not None:
                await built.on_lane_end(owner, name, built.passes[owner][name])
            recorder.add(f"{owner}:lane_end:{name}")

    async def recorded_acquire(self: Scheduler, conn: AsyncConnection) -> None:
        await acquire(self, conn)
        recorder.add(f"{built.names.get(id(self), '?')}:acquired")

    async def recorded_check_lock(self: Scheduler, conn: AsyncConnection) -> bool:
        held = await check_lock(self, conn)
        recorder.add(f"{built.names.get(id(self), '?')}:check:{'ok' if held else 'lost'}")
        return held

    async def recorded_check_lane(self: Scheduler, name: str) -> tuple[str, ...]:
        fired = await check_lane(self, name)
        recorder.add(f"{built.names.get(id(self), '?')}:lane_check:{name}:{','.join(fired)}")
        built.lane_checked.set()
        return fired

    monkeypatch.setattr(Scheduler, "_lane", recorded_lane)
    monkeypatch.setattr(Scheduler, "_acquire", recorded_acquire)
    monkeypatch.setattr(Scheduler, "_check_lock", recorded_check_lock)
    monkeypatch.setattr(Scheduler, "_check_lane", recorded_check_lane)
    try:
        yield built
    finally:
        await built.stop_all()


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


def test_lock_timing_constants_fence_a_takeover() -> None:
    """The grace outlasts a dying holder's detection and bounded unwind; wakes fall on checks."""
    assert LOCK_CHECK_INTERVAL.total_seconds() == 5
    assert LOCK_CHECK_TIMEOUT.total_seconds() == 5
    assert LOCK_LOST_STOP_TIMEOUT.total_seconds() == 5
    assert LOCK_TAKEOVER_GRACE.total_seconds() == 20
    assert LOCK_CHECK_INTERVAL + LOCK_CHECK_TIMEOUT + LOCK_LOST_STOP_TIMEOUT < LOCK_TAKEOVER_GRACE
    assert jobs.CANCEL_CLOSE_TIMEOUT < LOCK_LOST_STOP_TIMEOUT
    assert WAKE_INTERVAL.total_seconds() == 30
    assert not WAKE_INTERVAL % LOCK_CHECK_INTERVAL
    assert "LOCK_LOST_STOP_TIMEOUT" in scheduler_module.__all__
    # Once a silent database answers again: the probe in flight, the interval, one more probe
    # and the stop timeout all end inside the next holder's grace.
    assert FENCE_PROBE_TIMEOUT.total_seconds() == 3
    assert FENCE_PROBE_INTERVAL.total_seconds() == 2
    assert FENCE_PROBE_INTERVAL + FENCE_PROBE_TIMEOUT + LOCK_LOST_STOP_TIMEOUT < LOCK_TAKEOVER_GRACE
    assert (
        2 * FENCE_PROBE_TIMEOUT + FENCE_PROBE_INTERVAL + LOCK_LOST_STOP_TIMEOUT
        < LOCK_TAKEOVER_GRACE
    )
    assert {"FENCE_PROBE_INTERVAL", "FENCE_PROBE_TIMEOUT"} <= set(scheduler_module.__all__)


def test_sync_writer_lock_key_is_derived_from_its_name_and_distinct() -> None:
    """The writer key is derived like the scheduler key and collides with no other lock."""
    digest = hashlib.sha256(b"ads_engine:sync_writer").digest()
    assert int.from_bytes(digest[:8], "big", signed=True) == SYNC_WRITER_LOCK_KEY
    assert SYNC_WRITER_LOCK_KEY not in (SCHEDULER_LOCK_KEY, ANALYSIS_LOCK_KEY)
    assert {"SYNC_WRITER_LOCK_KEY", "sync_writer"} <= set(scheduler_module.__all__)


def test_sync_writer_fence_outlasts_a_writer_noticing_and_stopping() -> None:
    """A writer whose session died has stopped, or aborted, before the recovery's fence ends."""
    assert SYNC_WRITER_FENCE.total_seconds() == 20
    assert LOCK_CHECK_INTERVAL + LOCK_CHECK_TIMEOUT + LOCK_LOST_STOP_TIMEOUT < SYNC_WRITER_FENCE
    assert jobs.CANCEL_WRITER_LOCK_LOST == "sync writer lock lost"
    assert "CANCEL_WRITER_LOCK_LOST" in jobs.__all__
    assert {"SYNC_WRITER_FENCE", "SyncWriterLockLostError"} <= set(scheduler_module.__all__)


def test_next_deadline_keeps_the_grid_and_skips_missed_points() -> None:
    """A late deadline never shifts the grid; points already passed fire once, not as a burst."""
    next_deadline = scheduler_module._next_deadline
    assert next_deadline(0.0, 30.0, 4.0) == 30.0
    assert next_deadline(0.0, 30.0, 30.0) == 30.0
    assert next_deadline(30.0, 30.0, 60.0) == 60.0
    assert next_deadline(30.0, 30.0, 100.0) == 120.0
    assert next_deadline(5.0, 5.0, 9.0) == 10.0


async def test_blocked_source_lane_never_starves_analysis_or_budget(
    harness: Harness, db: Sessions, source_jobs: SourceJobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While Google's lane is blocked, wakes keep serving analysis and the budget refresh."""
    budget_calls = 0
    used_24h = GoogleCallBudget.used_24h

    async def counted_used_24h(self: GoogleCallBudget) -> int:
        nonlocal budget_calls
        budget_calls += 1
        return await used_24h(self)

    monkeypatch.setattr(GoogleCallBudget, "used_24h", counted_used_24h)
    source_jobs.blocked.add("google")
    (google_request,) = await _add_sync_requests(db, ("google",), harness.now)
    scheduler = harness.scheduler("A")
    passes = harness.passes["A"]
    sleep = harness.sleeps["A"]
    task = harness.start(scheduler)

    await _wait_event(source_jobs.entered, "the google lane's sync request job started")
    budget_at_block = passes["budget"]
    analysis_at_block = passes["analysis"]
    async with db() as session, session.begin():
        request = AnalysisRequest(
            reason="test", requested_at=harness.now - COALESCE_WINDOW - timedelta(minutes=4)
        )
        session.add(request)
        await session.flush()
        request_id = request.id

    async def served() -> bool:
        # The runner stamps ``run_id`` once the ``running`` row commits, before the run
        # computes, so the request is served only once that run has also finished.
        async with db() as session:
            row = await session.get(AnalysisRequest, request_id)
            assert row is not None
            if row.run_id is None:
                return False
            run = await session.get(AnalysisRun, row.run_id)
            assert run is not None
            return run.status != "running"

    await _eventually(served, "a default run served the pending analysis request and finished")

    async def two_more_budget_passes() -> bool:
        return passes["budget"] >= budget_at_block + 2 and budget_calls >= budget_at_block + 2

    await _eventually(two_more_budget_passes, "two more wakes dispatched the budget lane")

    assert not task.done()
    assert not source_jobs.cancelled.is_set()
    assert passes["google"] == 1, "the blocked google lane was dispatched again"
    assert source_jobs.calls == [("google", "manual")]
    assert passes["analysis"] > analysis_at_block
    assert await _picked(db, google_request)
    # Wakes fall exactly WAKE_INTERVAL apart on the monotonic clock, and between deadlines
    # the supervisor sleeps only until the next lock-check deadline.
    budget_starts = harness.lane_starts["A"]["budget"]
    assert set(_gaps(budget_starts)) == {WAKE_INTERVAL.total_seconds()}
    assert set(sleep.calls) == {
        LOCK_TAKEOVER_GRACE.total_seconds(),
        LOCK_CHECK_INTERVAL.total_seconds(),
    }
    assert harness.max_active[("A", "google")] == 1
    async with db() as session:
        row = await session.get(AnalysisRequest, request_id)
        assert row is not None
        assert row.picked_at is not None
        run = await session.get(AnalysisRun, row.run_id)
        assert run is not None
        assert (run.kind, run.trigger, run.status) == ("default", "event", "succeeded")

    await _cancel(task)
    assert source_jobs.cancelled.is_set(), "cancellation did not reach the blocked lane"
    assert await _lock_holder(db) is None


async def test_lock_lost_mid_job_cancels_lane_before_reacquiring(
    harness: Harness, db: Sessions, recorder: Recorder, source_jobs: SourceJobs
) -> None:
    """Killing the lock session cancels the blocked lane first, then reacquires and requeues.

    The cancelled job left no ``sync_run`` row, so the requeue, not the orphaned-run sweep,
    makes its "Sync now" pending again, and the lane serves it once more.
    """
    source_jobs.blocked.add("google")
    (google_request,) = await _add_sync_requests(db, ("google",), harness.now)
    scheduler = harness.scheduler("A")
    task = harness.start(scheduler)
    await _wait_event(source_jobs.entered, "the google lane's job started")
    first_holder = await _lock_holder(db)

    killed = await _kill_lock_session(db)

    await _wait_event(source_jobs.cancelled, "the lock loss cancelled the google lane")

    async def requeued() -> bool:
        return await _lock_lost_requests(db) == 1

    await _eventually(requeued, "the scheduler queued a scheduler_lock_lost request")

    async def reacquired() -> bool:
        holder = await _lock_holder(db)
        return holder is not None and holder != killed

    await _eventually(reacquired, "the scheduler holds the lock on a new session")

    async def served_again() -> bool:
        return len(source_jobs.calls) >= 2

    await _eventually(served_again, "the re-queued sync request was served again")
    assert first_holder == killed
    assert not task.done()
    events = recorder.events
    assert recorder.first("job_cancelled:google") < recorder.nth("A:acquired", 2)
    assert recorder.first("job:google") < recorder.first("job_cancelled:google")
    # The second acquisition waits out the takeover grace before any lane runs again.
    second = recorder.nth("A:acquired", 2)
    assert events[second + 1] == f"A:sleep:{LOCK_TAKEOVER_GRACE.total_seconds():g}"
    assert second < recorder.nth("job:google", 2)
    assert await _picked(db, google_request)
    assert source_jobs.calls == [("google", "manual"), ("google", "manual")]
    await _cancel(task)


async def test_lock_lost_during_startup_reruns_it_before_any_lane(
    harness: Harness, db: Sessions, recorder: Recorder
) -> None:
    """A lock lost while ``on_start`` runs cancels it; it runs again before the first lane."""
    startup = Startup(recorder, block_first=True)
    scheduler = harness.scheduler("A")
    task = harness.start(scheduler, on_start=startup)
    await _wait_event(startup.entered, "on_start began")

    await _kill_lock_session(db)

    await _wait_event(startup.done, "on_start completed after the re-acquisition")

    async def a_lane_ran() -> bool:
        return sum(harness.passes["A"].values()) > 0

    await _eventually(a_lane_ran, "a lane pass ran after the startup")
    assert not task.done()
    assert startup.calls == 2
    first_lane = recorder.starting("A:lane:")[0]
    assert recorder.first("start1:begin") < recorder.first("start1:cancelled")
    assert recorder.first("start1:cancelled") < recorder.nth("A:acquired", 2)
    assert recorder.nth("A:acquired", 2) < recorder.first("start2:begin")
    assert recorder.first("start2:done") < first_lane
    assert "start1:done" not in recorder.events
    # The startup had not completed, so it reruns instead of a lock-lost request.
    assert await _lock_lost_requests(db) == 0
    await _cancel(task)


async def test_standby_waits_the_takeover_grace_before_on_start(
    harness: Harness, db: Sessions, recorder: Recorder
) -> None:
    """A second scheduler stays standby, then waits ``LOCK_TAKEOVER_GRACE`` before startup."""
    first = harness.scheduler("A")
    second = harness.scheduler("B")
    startup_a = Startup(recorder, "a_start", block_first=False)
    startup_b = Startup(recorder, "b_start", block_first=False)
    task_a = harness.start(first, on_start=startup_a)
    await _wait_event(startup_a.done, "the first scheduler's startup completed")

    async def first_lanes_ran() -> bool:
        return harness.passes["A"]["analysis"] > 0

    await _eventually(first_lanes_ran, "the first scheduler ran its lanes")
    task_b = harness.start(second, on_start=startup_b)
    standby_sleep = f"B:sleep:{STANDBY_RETRY.total_seconds():g}"

    async def standing_by() -> bool:
        return recorder.events.count(standby_sleep) >= 2

    await _eventually(standing_by, "the second scheduler retried as a standby")
    assert "B:acquired" not in recorder.events
    assert startup_b.calls == 0

    await _cancel(task_a)
    await _wait_event(startup_b.done, "the second scheduler's startup completed")

    acquired = recorder.first("B:acquired")
    grace = recorder.events.index(f"B:sleep:{LOCK_TAKEOVER_GRACE.total_seconds():g}", acquired)
    b_events = [index for index, event in enumerate(recorder.events) if event.startswith("B:")]
    # Every lane pass of the first scheduler ended before the second one took the lock.
    a_ends = recorder.starting("A:lane_end:")
    assert len(a_ends) == len(recorder.starting("A:lane:"))
    assert max(a_ends) < acquired
    assert b_events[b_events.index(acquired) + 1] == grace
    assert grace < recorder.first("b_start1:begin")
    first_b_lane = recorder.starting("B:lane:")
    assert first_b_lane == [] or first_b_lane[0] > recorder.first("b_start1:done")
    assert startup_a.calls == 1
    assert startup_b.calls == 1
    assert not task_b.done()
    await _cancel(task_b)


async def test_defect_in_one_lane_cancels_the_others_and_propagates(
    harness: Harness,
    db: Sessions,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A defect ends ``run_forever`` with that error after every other lane was cancelled."""

    class LaneDefectError(RuntimeError):
        """An error the scheduler's steps do not isolate."""

    async def broken_used_24h(self: GoogleCallBudget) -> int:
        del self  # The defect does not depend on the budget.
        await source_jobs.entered.wait()
        raise LaneDefectError("budget lane defect")

    monkeypatch.setattr(GoogleCallBudget, "used_24h", broken_used_24h)
    source_jobs.blocked.add("google")
    await _add_sync_requests(db, ("google",), harness.now)
    scheduler = harness.scheduler("A")
    task = harness.start(scheduler)

    try:
        async with asyncio.timeout(_WAIT_S):
            await asyncio.wait((task,))
    except TimeoutError:
        pytest.fail("the defect did not end run_forever")
    assert not task.cancelled()
    error = task.exception()
    assert isinstance(error, LaneDefectError)
    assert str(error) == "budget lane defect"
    assert source_jobs.cancelled.is_set(), "the blocked google lane was not cancelled"
    assert await _lock_holder(db) is None


async def test_lock_check_timeout_counts_as_lost(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock check that does not answer in time cancels the lanes like a failed one."""
    monkeypatch.setattr(scheduler_module, "LOCK_CHECK_TIMEOUT", timedelta(milliseconds=50))
    hang_next = asyncio.Event()
    alive = Scheduler._lock_alive

    async def lock_alive(conn: AsyncConnection) -> bool:
        if hang_next.is_set():
            hang_next.clear()
            recorder.add("check_hangs")
            await asyncio.Event().wait()
        return await alive(conn)

    monkeypatch.setattr(Scheduler, "_lock_alive", staticmethod(lock_alive))
    source_jobs.blocked.add("google")
    await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")

    hang_next.set()

    await _wait_event(source_jobs.cancelled, "the timed-out check cancelled the google lane")

    async def requeued() -> bool:
        return await _lock_lost_requests(db) == 1

    await _eventually(requeued, "the scheduler re-acquired and queued scheduler_lock_lost")
    assert recorder.first("check_hangs") < recorder.first("job_cancelled:google")
    assert recorder.first("job_cancelled:google") < recorder.nth("A:acquired", 2)
    assert not task.done()
    await _cancel(task)


async def test_cancellation_during_startup_cancels_and_awaits_it(
    harness: Harness, db: Sessions, recorder: Recorder
) -> None:
    """SIGTERM while ``on_start`` runs cancels and awaits it, then propagates; no lane runs."""
    startup = Startup(recorder, block_first=True)
    task = harness.start(harness.scheduler("A"), on_start=startup)
    await _wait_event(startup.entered, "on_start began")

    await _cancel(task)

    assert "start1:cancelled" in recorder.events
    assert recorder.starting("A:lane:") == []
    assert await _lock_holder(db) is None


async def test_google_lane_claims_only_google_sync_requests(
    harness: Harness, db: Sessions, source_jobs: SourceJobs, datasets: Datasets
) -> None:
    """With the HubSpot and LinkedIn lane blocked, Google's lane claims only its own row."""
    datasets.blocked.add("hubspot")
    google, hubspot, linkedin = await _add_sync_requests(
        db, ("google", "hubspot", "linkedin"), harness.now
    )
    scheduler = harness.scheduler("A")
    passes = harness.passes["A"]
    task = harness.start(scheduler)
    await _wait_event(datasets.entered, "the linkedin_hubspot lane blocked in its datasets step")

    async def google_claimed() -> bool:
        return await _picked(db, google)

    await _eventually(google_claimed, "the google lane claimed its sync request")

    async def more_google_passes() -> bool:
        return passes["google"] >= 3

    await _eventually(more_google_passes, "the google lane ran on later wakes")
    assert not await _picked(db, hubspot)
    assert not await _picked(db, linkedin)
    assert source_jobs.calls == [("google", "manual")]
    assert passes["linkedin_hubspot"] == 1, "the blocked lane was dispatched again"
    await _cancel(task)


async def test_wakes_stay_30_s_apart_when_each_lock_check_takes_seconds(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wakes fall on absolute deadlines: 4 s lock checks never stretch their spacing."""
    alive = Scheduler._lock_alive

    async def slow_lock_alive(conn: AsyncConnection) -> bool:
        await harness.time.elapse(_CHECK_LATENCY_S)
        return await alive(conn)

    async def nothing_orphaned(self: Scheduler, pending: list[int]) -> None:
        # Instant, so the real time of its read moves no virtual time before the first wake.
        del self, pending

    monkeypatch.setattr(Scheduler, "_lock_alive", staticmethod(slow_lock_alive))
    monkeypatch.setattr(Scheduler, "_recover_orphaned_runs", nothing_orphaned)
    task = harness.start(harness.scheduler("A"))
    passes = harness.passes["A"]

    async def four_wakes() -> bool:
        return passes["budget"] >= 4

    await _eventually(four_wakes, "four wakes dispatched the budget lane")
    starts = harness.lane_starts["A"]["budget"][:4]
    assert _gaps(starts) == [WAKE_INTERVAL.total_seconds()] * 3
    # The grace, the fence check, an instant recovery, then the first wake's own check precede
    # the first dispatch.
    assert starts[0] == LOCK_TAKEOVER_GRACE.total_seconds() + 2 * _CHECK_LATENCY_S
    await _cancel(task)


async def test_request_after_a_wake_is_coalesced_at_60_s_not_at_the_third_wake(
    harness: Harness, db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The analysis lane sleeps until the oldest request is 60 s old, then serves it."""
    harness.wall_follows = True
    requested: list[float] = []
    served_at: list[float] = []

    async def queue_after_first_analysis_pass(owner: str, lane: str, count: int) -> None:
        if lane != "analysis" or count != 1 or requested:
            return
        harness.time.freeze()
        try:
            arrival = harness.lane_starts[owner]["analysis"][0] + 1
            async with db() as session, session.begin():
                session.add(
                    AnalysisRequest(
                        reason="test", requested_at=harness.now + timedelta(seconds=arrival)
                    )
                )
            requested.append(arrival)
        finally:
            harness.time.thaw()

    coalesce = Scheduler._coalesce_analysis

    async def recorded_coalesce(self: Scheduler, tick: Any) -> None:
        began = harness.time.seconds
        await coalesce(self, tick)
        if tick.analysis_run_id is not None:
            served_at.append(began)

    harness.on_lane_end = queue_after_first_analysis_pass
    monkeypatch.setattr(Scheduler, "_coalesce_analysis", recorded_coalesce)
    task = harness.start(harness.scheduler("A"))

    async def served() -> bool:
        return bool(served_at)

    await _eventually(served, "a default run served the request")
    (arrival,) = requested
    (served_time,) = served_at
    window = COALESCE_WINDOW.total_seconds()
    wake = WAKE_INTERVAL.total_seconds()
    # The next wake saw the request and the lane slept exactly until it was 60 s old.
    (wait,) = harness.sleeps["A"].by_task("scheduler.lane.analysis")
    start, seconds = wait
    assert arrival - 1 + wake <= start < arrival - 1 + 2 * wake
    assert start + seconds == arrival + window
    # Served at 60 s of age, not at the third wake after it (89 s).
    assert window <= served_time - arrival < window + LOCK_CHECK_INTERVAL.total_seconds()
    async with db() as session:
        runs = await session.scalar(select(func.count()).select_from(AnalysisRun))
        pending = await session.scalar(
            select(func.count())
            .select_from(AnalysisRequest)
            .where(AnalysisRequest.picked_at.is_(None))
        )
    assert (runs, pending) == (1, 0)
    await _cancel(task)


async def test_busy_source_lane_with_pending_work_reruns_the_moment_it_ends(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    captured_logs: Any,
) -> None:
    """A wake finding linkedin_hubspot busy checks its triggers; it reruns at once, once."""
    source_jobs.blocked.add("hubspot")
    await _add_sync_requests(db, ("hubspot",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the linkedin_hubspot lane's hubspot job started")
    (linkedin_request,) = await _add_sync_requests(db, ("linkedin",), harness.now)

    async def work_seen() -> bool:
        return "A:lane_check:linkedin_hubspot:sync_requests" in recorder.events

    await _eventually(work_seen, "a wake's trigger check saw the linkedin sync request")
    assert harness.passes["A"]["linkedin_hubspot"] == 1
    assert not await _picked(db, linkedin_request), "the check claimed the request"
    # No deadline can pass from here on, so only an immediate rerun can dispatch the lane.
    harness.time.freeze()
    released_at = harness.time.seconds
    recorder.add("released")
    source_jobs.release.set()

    async def linkedin_served() -> bool:
        return ("linkedin", "manual") in source_jobs.calls

    await _eventually(linkedin_served, "the rerun served the linkedin sync request")
    events = recorder.events
    released = recorder.first("released")
    first_end = recorder.nth("A:lane_end:linkedin_hubspot", 1)
    rerun = recorder.nth("A:lane:linkedin_hubspot", 2)
    assert released < first_end < rerun
    assert not [index for index in recorder.starting("A:lane:budget") if released < index < rerun]
    assert harness.lane_starts["A"]["linkedin_hubspot"][1] == released_at
    supervisor = [event for event in events[first_end:rerun] if event.startswith("A:check:")]
    assert supervisor, "no lock check preceded the rerun"
    assert supervisor[-1] == "A:check:ok", "the rerun had no fresh lock check"
    assert harness.max_active[("A", "linkedin_hubspot")] == 1
    assert source_jobs.calls == [("hubspot", "manual"), ("linkedin", "manual")]
    assert await _picked(db, linkedin_request)
    pending = [
        record
        for record in captured_logs()
        if record.get("event") == "scheduler.lane_work_pending"
        and record.get("lane") == "linkedin_hubspot"
    ]
    assert pending, "no scheduler.lane_work_pending line was logged"
    assert pending[0]["triggers"] == ["sync_requests"]
    harness.time.thaw()
    await _cancel(task)


async def test_busy_analysis_lane_reruns_the_moment_it_ends(
    harness: Harness, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake finding the analysis lane busy marks it; it reruns at once when it ends."""
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocking_wait(self: Scheduler, tick: Any) -> bool:
        del self, tick  # The fake decides by call count only.
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return False

    monkeypatch.setattr(Scheduler, "_await_coalescing", blocking_wait)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(entered, "the first analysis pass blocked")
    passes = harness.passes["A"]

    async def another_wake() -> bool:
        return passes["budget"] >= 2

    await _eventually(another_wake, "a later wake found the analysis lane busy")
    harness.time.freeze()
    released_at = harness.time.seconds
    recorder.add("released")
    release.set()

    async def rerun() -> bool:
        return passes["analysis"] >= 2

    await _eventually(rerun, "the analysis lane ran again")
    released = recorder.first("released")
    second = recorder.nth("A:lane:analysis", 2)
    assert released < recorder.nth("A:lane_end:analysis", 1) < second
    assert not [index for index in recorder.starting("A:lane:budget") if released < index < second]
    assert harness.lane_starts["A"]["analysis"][1] == released_at
    assert harness.max_active[("A", "analysis")] == 1
    harness.time.thaw()
    await _cancel(task)


async def test_lock_lost_while_the_requeue_is_blocked_dispatches_no_lane(
    harness: Harness, db: Sessions, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock-loss requeue is guarded: losing the lock again cancels it before any lane."""
    enqueue = repositories.enqueue_analysis_request
    blocked = asyncio.Event()
    attempts = 0

    async def blocking_enqueue(session: AsyncSession, reason: str) -> None:
        nonlocal attempts
        if reason == _LOCK_LOST:
            attempts += 1
            if attempts == 1:
                recorder.add("requeue_blocked")
                blocked.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    recorder.add("requeue_cancelled")
                    raise
        await enqueue(session, reason)

    monkeypatch.setattr(repositories, "enqueue_analysis_request", blocking_enqueue)
    task = harness.start(harness.scheduler("A"))

    async def a_lane_ran() -> bool:
        return sum(harness.passes["A"].values()) > 0

    await _eventually(a_lane_ran, "the first wake dispatched the lanes")
    await _kill_lock_session(db)
    await _wait_event(blocked, "the requeue after the re-acquisition blocked")
    await _kill_lock_session(db)

    async def requeued() -> bool:
        return await _lock_lost_requests(db) == 1

    await _eventually(requeued, "the requeue ran again after a third acquisition")

    async def lanes_resumed() -> bool:
        lanes = recorder.starting("A:lane:")
        return bool(lanes) and lanes[-1] > recorder.nth("A:acquired", 3)

    await _eventually(lanes_resumed, "the lanes ran again after the third acquisition")
    second, third = recorder.nth("A:acquired", 2), recorder.nth("A:acquired", 3)
    assert second < recorder.first("requeue_blocked") < recorder.first("requeue_cancelled") < third
    assert not [index for index in recorder.starting("A:lane:") if second < index < third]
    assert attempts == 2
    assert harness.aborts == []
    assert not task.done()
    await _cancel(task)


async def test_first_dispatch_after_acquisition_follows_a_fresh_lock_check(
    harness: Harness, db: Sessions, recorder: Recorder
) -> None:
    """A lock session that dies as ``on_start`` returns is caught before any lane runs."""

    async def on_start() -> None:
        recorder.add("start:begin")
        # Held still so the startup guard's own periodic check cannot see the loss first.
        harness.time.freeze()
        await _kill_lock_session(db)
        recorder.add("start:done")

    task = harness.start(harness.scheduler("A"), on_start=on_start)

    async def lost_seen() -> bool:
        return "A:check:lost" in recorder.events

    await _eventually(lost_seen, "a lock check found the dead session")
    harness.time.thaw()

    async def lanes_ran() -> bool:
        return bool(recorder.starting("A:lane:"))

    await _eventually(lanes_ran, "the lanes ran after the re-acquisition")
    done = recorder.first("start:done")
    lost = recorder.first("A:check:lost")
    second = recorder.nth("A:acquired", 2)
    first_lane = recorder.starting("A:lane:")[0]
    assert done < lost < second < first_lane
    assert recorder.events.count("start:begin") == 1, "a completed startup ran again"
    checks = [event for event in recorder.events[:first_lane] if event.startswith("A:check:")]
    assert checks[-1] == "A:check:ok"
    assert await _lock_lost_requests(db) == 1
    await _cancel(task)


@pytest.mark.parametrize("loss", ["answered", "silent"])
async def test_unwind_past_the_stop_timeout_after_a_lost_lock_aborts(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
    loss: str,
) -> None:
    """A cancelled task still unwinding after ``LOCK_LOST_STOP_TIMEOUT`` aborts with code 1.

    ``answered``: the lock session is terminated, so the check fails with an answer and the
    database is never probed. ``silent``: the check times out, so the database gets one probe,
    which the database answers.
    """
    monkeypatch.setattr(
        scheduler_module, "LOCK_LOST_STOP_TIMEOUT", timedelta(seconds=_STOP_TIMEOUT_S)
    )
    silence = _silent_lock_check(monkeypatch, recorder)
    source_jobs.blocked.add("google")
    source_jobs.stubborn.add("google")
    await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")

    if loss == "answered":
        await _kill_lock_session(db)
    else:
        silence.set()

    try:
        async with asyncio.timeout(_WAIT_S):
            await asyncio.wait((task,))
    except TimeoutError:
        pytest.fail("the stuck unwind did not abort the scheduler")
    error = task.exception()
    assert isinstance(error, AbortCalledError)
    assert error.code == 1
    assert harness.aborts == [1]
    assert recorder.first("job_cancelled:google") < recorder.first("abort:1")
    assert "job_unwound:google" not in recorder.events
    assert recorder.events.count("A:acquired") == 1, "the lock was taken again before the abort"
    (timeout,) = [
        record
        for record in captured_logs()
        if record.get("event") == "scheduler.fence_stop_timeout"
    ]
    assert timeout["lanes"] == ["google"]
    assert timeout["reason"] == "lock_lost"
    assert timeout["level"] == "critical"
    assert _lock_lost_causes(captured_logs) == [loss]
    if loss == "answered":
        # The check got an answer: the server may already have given the lock to another
        # worker, so the stuck task is bounded with no probe, whatever a probe would say.
        assert harness.probe_at == []
        assert "check_silent" not in recorder.events
    else:
        # The database answered the one probe, so the stuck task could still write: no wait.
        assert [answer for _, answer in harness.probe_at] == [True]
        assert recorder.first("check_silent") < recorder.first("job_cancelled:google")
        assert recorder.first("job_cancelled:google") < recorder.first("probe:up")
        assert recorder.first("probe:up") < recorder.first("abort:1")
    assert _logged(captured_logs, "scheduler.fence_wait_unreachable") == []
    # A lost lock cancels with the message a cancelled job logs at error level.
    assert source_jobs.cancel_args == [(jobs.CANCEL_LOCK_LOST,)]
    source_jobs.release.set()


async def test_stuck_unwind_on_a_silent_database_is_awaited_then_the_lanes_resume(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """A silent check, then a silent database: a stuck unwind cannot write, so it is awaited."""
    monkeypatch.setattr(
        scheduler_module, "LOCK_LOST_STOP_TIMEOUT", timedelta(seconds=_STOP_TIMEOUT_S)
    )
    monkeypatch.setattr(
        scheduler_module, "FENCE_PROBE_INTERVAL", timedelta(seconds=_PROBE_INTERVAL_S)
    )
    silence = _silent_lock_check(monkeypatch, recorder)
    harness.reachable_by_default = False
    source_jobs.blocked.add("google")
    source_jobs.stubborn.add("google")
    await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")

    silence.set()

    async def probed_again() -> bool:
        return recorder.events.count("probe:down") >= 3

    await _eventually(probed_again, "the scheduler kept probing the silent database")
    await asyncio.sleep(_OVERSTAY_S)
    assert not task.done(), "the scheduler stopped waiting for its stuck task"
    assert harness.aborts == []
    assert recorder.events.count("A:acquired") == 1, "the lock was taken again mid-unwind"
    source_jobs.release.set()

    async def serving_again() -> bool:
        acquisitions = [index for index, seen in enumerate(recorder.events) if seen == "A:acquired"]
        return len(acquisitions) >= 2 and any(
            index > acquisitions[1] for index in recorder.starting("A:lane:")
        )

    await _eventually(serving_again, "the scheduler re-acquired the lock and ran its lanes")

    async def requeued() -> bool:
        return await _lock_lost_requests(db) == 1

    await _eventually(requeued, "the scheduler queued a scheduler_lock_lost request")
    assert harness.aborts == []
    assert not task.done()
    assert recorder.first("job_unwound:google") < recorder.nth("A:acquired", 2)
    assert recorder.first("check_silent") < recorder.first("job_cancelled:google")
    assert source_jobs.cancel_args == [(jobs.CANCEL_LOCK_LOST,)]
    assert _lock_lost_causes(captured_logs) == ["silent"]
    (silent,) = _logged(captured_logs, "scheduler.fence_wait_unreachable")
    assert silent["level"] == "error"
    assert silent["lanes"] == ["google"]
    assert silent["reason"] == "lock_lost"
    assert silent["probe_timeout_s"] == FENCE_PROBE_TIMEOUT.total_seconds()
    assert silent["probe_interval_s"] == _PROBE_INTERVAL_S
    assert _logged(captured_logs, "scheduler.fence_wait_reachable") == []
    assert _logged(captured_logs, "scheduler.fence_stop_timeout") == []
    (stopped,) = _logged(captured_logs, "scheduler.lanes_cancelled")
    assert "google" in stopped["lanes"]
    assert stopped["reason"] == "lock_lost"
    await _cancel(task)


async def test_database_answering_again_bounds_a_stuck_unwind_and_aborts(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """After a silent check, a probe answered at last gives a stuck unwind the stop timeout."""
    monkeypatch.setattr(
        scheduler_module, "LOCK_LOST_STOP_TIMEOUT", timedelta(seconds=_STOP_TIMEOUT_S)
    )
    monkeypatch.setattr(
        scheduler_module, "FENCE_PROBE_INTERVAL", timedelta(seconds=_PROBE_INTERVAL_S)
    )
    silence = _silent_lock_check(monkeypatch, recorder)
    harness.reachability = [False, False]
    source_jobs.blocked.add("google")
    source_jobs.stubborn.add("google")
    await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")

    silence.set()

    try:
        async with asyncio.timeout(_WAIT_S):
            await asyncio.wait((task,))
    except TimeoutError:
        pytest.fail("the stuck unwind did not abort once the database answered")
    assert _lock_lost_causes(captured_logs) == ["silent"]
    error = task.exception()
    assert isinstance(error, AbortCalledError)
    assert harness.aborts == [1]
    assert [answer for _, answer in harness.probe_at] == [False, False, True]
    answered_at = harness.probe_at[-1][0]
    waited = harness.abort_at[0] - answered_at
    assert _STOP_TIMEOUT_S * 0.9 <= waited < _STOP_TIMEOUT_S + 0.5
    assert "job_unwound:google" not in recorder.events
    assert recorder.events.count("A:acquired") == 1
    events = [record.get("event") for record in captured_logs()]
    silent = events.index("scheduler.fence_wait_unreachable")
    answered = events.index("scheduler.fence_wait_reachable")
    timeout = events.index("scheduler.fence_stop_timeout")
    assert silent < answered < timeout
    (reachable,) = _logged(captured_logs, "scheduler.fence_wait_reachable")
    assert reachable["level"] == "warning"
    assert reachable["lanes"] == ["google"]
    assert reachable["timeout_s"] == _STOP_TIMEOUT_S
    (stop,) = _logged(captured_logs, "scheduler.fence_stop_timeout")
    assert stop["level"] == "critical"
    assert stop["lanes"] == ["google"]
    source_jobs.release.set()


@pytest.mark.parametrize(
    ("answers", "silent_logged"),
    [
        pytest.param((False,), False, id="during_the_first_probe"),
        pytest.param((False, True), True, id="during_the_answered_probe"),
    ],
)
async def test_a_stuck_unwind_ending_during_a_probe_is_not_reported_and_never_aborts(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
    answers: tuple[bool, ...],
    silent_logged: bool,
) -> None:
    """A task that ends while a probe runs is not logged as still running, and nothing aborts.

    The lock check goes silent, so the database is probed at all.
    """
    monkeypatch.setattr(
        scheduler_module, "LOCK_LOST_STOP_TIMEOUT", timedelta(seconds=_STOP_TIMEOUT_S)
    )
    monkeypatch.setattr(
        scheduler_module, "FENCE_PROBE_INTERVAL", timedelta(seconds=_PROBE_INTERVAL_S)
    )
    silence = _silent_lock_check(monkeypatch, recorder)
    harness.reachability = list(answers)

    async def unwind_during(probe: int) -> None:
        """Let the stuck task end inside the last scripted probe, before it answers."""
        if probe != len(answers):
            return
        lanes = [task for task in asyncio.all_tasks() if task.get_name() == "scheduler.lane.google"]
        assert lanes, "the stuck google lane is not running during the probe"
        source_jobs.release.set()
        _, still_running = await asyncio.wait(lanes, timeout=_WAIT_S)
        assert not still_running, "the released google lane did not end"

    harness.during_probe = unwind_during
    source_jobs.blocked.add("google")
    source_jobs.stubborn.add("google")
    await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")

    silence.set()

    async def serving_again() -> bool:
        acquisitions = [index for index, seen in enumerate(recorder.events) if seen == "A:acquired"]
        return len(acquisitions) >= 2 and any(
            index > acquisitions[1] for index in recorder.starting("A:lane:")
        )

    await _eventually(serving_again, "the scheduler re-acquired the lock and ran its lanes")
    assert harness.aborts == []
    assert not task.done()
    assert [answer for _, answer in harness.probe_at] == list(answers)
    assert recorder.first("job_unwound:google") < recorder.nth("A:acquired", 2)
    assert len(_logged(captured_logs, "scheduler.fence_wait_unreachable")) == int(silent_logged)
    assert _logged(captured_logs, "scheduler.fence_wait_reachable") == []
    assert _logged(captured_logs, "scheduler.fence_stop_timeout") == []
    await _cancel(task)


async def test_lock_lost_to_an_answered_check_aborts_unprobed_inside_the_successors_grace(
    harness: Harness,
    db: Sessions,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """A lock lost to an answered check bounds a stuck unwind: no probe, abort before its write.

    The case of a rotated database password: the lock backend is terminated, a successor
    session takes the lock at once, and the probe would call the database silent (the
    injected probe always answers ``False``, standing in for any probe that misreads an
    answered error as silence). The scheduler never probes: it aborts the real
    ``LOCK_LOST_STOP_TIMEOUT`` after the cancellation, inside the successor's
    ``LOCK_TAKEOVER_GRACE``, while the stuck unwind's write, due ``LOCK_LOST_STOP_TIMEOUT``
    plus 2 s after the cancellation, has not landed.
    """
    del source_jobs  # Its fake is replaced below by a job whose unwind writes late.
    harness.reachable_by_default = False
    loop = asyncio.get_running_loop()
    stop_s = LOCK_LOST_STOP_TIMEOUT.total_seconds()
    entered, cancelled = asyncio.Event(), asyncio.Event()
    cancelled_at: list[float] = []
    written_at: list[float] = []

    async def late_writing_job(
        source: Source, *, trigger: jobs.Trigger, **options: object
    ) -> jobs.SourceSyncReport:
        """Google's job blocks; once cancelled, its bookkeeping writes only after a delay."""
        del options  # Only the source decides here.
        if source == "google":
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_at.append(loop.time())
                cancelled.set()
                await asyncio.sleep(stop_s + 2.0)
                async with db() as session, session.begin():
                    await repositories.enqueue_analysis_request(session, _STUCK_WRITE)
                written_at.append(loop.time())
                raise
        return jobs.SourceSyncReport(source=source, trigger=trigger)

    monkeypatch.setattr(jobs, "run_source_jobs", late_writing_job)
    await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(entered, "the google lane's job started")

    await _kill_lock_session(db)
    await _wait_event(cancelled, "the lost lock cancelled the google lane")

    async with db_session.dedicated_connection() as successor:
        took_over = await successor.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": SCHEDULER_LOCK_KEY}
        )
        took_over_at = loop.time()
        successor_pid = await successor.scalar(text("SELECT pg_backend_pid()"))
        assert took_over is True, "no successor could take the lock the terminated session held"
        assert not task.done(), "the scheduler ended before the successor took the lock"
        try:
            async with asyncio.timeout(_WAIT_S):
                await asyncio.wait((task,))
        except TimeoutError:
            pytest.fail("the stuck unwind did not abort the scheduler")
        writes_at_abort = await _stuck_writes(db)
        holder_at_abort = await _lock_holder(db)

    error = task.exception()
    assert isinstance(error, AbortCalledError)
    assert error.code == 1
    assert harness.aborts == [1]
    # The check got an answer, so the probe that would call the database silent never ran.
    assert harness.probe_at == []
    waited = harness.abort_at[0] - cancelled_at[0]
    assert stop_s * 0.9 <= waited < stop_s + 1.0
    assert harness.abort_at[0] - took_over_at < LOCK_TAKEOVER_GRACE.total_seconds()
    assert holder_at_abort == successor_pid
    assert writes_at_abort == 0
    assert written_at == []
    assert _lock_lost_causes(captured_logs) == ["answered"]
    (stop,) = _logged(captured_logs, "scheduler.fence_stop_timeout")
    assert (stop["level"], stop["lanes"], stop["reason"]) == ("critical", ["google"], "lock_lost")
    for unexpected in (
        "scheduler.fence_wait_unreachable",
        "scheduler.fence_wait_reachable",
        "scheduler.fence_probe_failed",
    ):
        assert _logged(captured_logs, unexpected) == []
    # In the worker the abort ends the process here; the test ends the stuck lane instead.
    lanes = [lane for lane in asyncio.all_tasks() if lane.get_name() == "scheduler.lane.google"]
    assert lanes, "the stuck google lane ended before the abort"
    for lane in lanes:
        lane.cancel()
    _, still_running = await asyncio.wait(lanes, timeout=_WAIT_S)
    assert not still_running
    assert written_at == []
    assert await _stuck_writes(db) == 0


@pytest.mark.parametrize("loss", ["answered", "silent"])
async def test_a_stuck_startup_after_a_lost_lock_is_fenced_by_how_the_check_failed(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
    loss: str,
) -> None:
    """The startup's guard fences a stuck unwind exactly as the lanes' supervision does.

    ``answered``: the terminated lock session aborts the process after the stop timeout with
    no probe, although the probe would call the database silent. ``silent``: the check times
    out and the probe finds the database silent, so the startup's unwind is awaited with no
    abort, and the startup runs again after the next acquisition.
    """
    monkeypatch.setattr(
        scheduler_module, "LOCK_LOST_STOP_TIMEOUT", timedelta(seconds=_STOP_TIMEOUT_S)
    )
    monkeypatch.setattr(
        scheduler_module, "FENCE_PROBE_INTERVAL", timedelta(seconds=_PROBE_INTERVAL_S)
    )
    silence = _silent_lock_check(monkeypatch, recorder)
    harness.reachable_by_default = False
    entered, cancelled = asyncio.Event(), asyncio.Event()
    # The harness sets this at teardown too, so a failing run never leaves the unwind stuck.
    release = harness.jobs.release
    calls = 0

    async def stuck_startup() -> None:
        """The first call blocks and, once cancelled, unwinds until released; later ones return."""
        nonlocal calls
        calls += 1
        recorder.add(f"start{calls}:begin")
        if calls > 1:
            return
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            recorder.add("start1:cancelled")
            cancelled.set()
            await release.wait()
            recorder.add("start1:unwound")
            raise

    task = harness.start(harness.scheduler("A"), on_start=stuck_startup)
    await _wait_event(entered, "on_start began")

    if loss == "answered":
        await _kill_lock_session(db)
    else:
        silence.set()
    await _wait_event(cancelled, "the lost lock cancelled on_start")

    if loss == "answered":
        try:
            async with asyncio.timeout(_WAIT_S):
                await asyncio.wait((task,))
        except TimeoutError:
            pytest.fail("the stuck startup did not abort the scheduler")
        assert isinstance(task.exception(), AbortCalledError)
        assert harness.aborts == [1]
        assert harness.probe_at == []
        assert "start1:unwound" not in recorder.events
        (stop,) = _logged(captured_logs, "scheduler.fence_stop_timeout")
        assert stop["lanes"] == ["on_start"]
        assert _logged(captured_logs, "scheduler.fence_wait_unreachable") == []
        release.set()
    else:

        async def probed_again() -> bool:
            return recorder.events.count("probe:down") >= 3

        await _eventually(probed_again, "the scheduler kept probing the silent database")
        await asyncio.sleep(_OVERSTAY_S)
        assert not task.done(), "the scheduler stopped waiting for its stuck startup"
        assert harness.aborts == []
        assert recorder.events.count("A:acquired") == 1, "the lock was taken again mid-unwind"
        release.set()

        async def started_again() -> bool:
            return "start2:begin" in recorder.events

        await _eventually(started_again, "the startup ran again after the re-acquisition")
        unwound = recorder.first("start1:unwound")
        assert unwound < recorder.nth("A:acquired", 2) < recorder.first("start2:begin")
        assert harness.aborts == []
        (silent,) = _logged(captured_logs, "scheduler.fence_wait_unreachable")
        assert silent["lanes"] == ["on_start"]
        assert _logged(captured_logs, "scheduler.fence_stop_timeout") == []
        await _cancel(task)
    assert _lock_lost_causes(captured_logs) == [loss]


@pytest.mark.parametrize(
    ("outcome", "reachable", "logged_error"),
    [
        pytest.param("succeeds", True, None, id="success"),
        pytest.param("authentication_fails", True, "OperationalError", id="auth_failure"),
        pytest.param("too_many_clients", True, "OperationalError", id="too_many_clients"),
        pytest.param("socket_reset", True, "ConnectionResetError", id="socket_reset"),
        pytest.param("network_unreachable", True, "OSError", id="os_error"),
        pytest.param("connect_times_out", False, None, id="libpq_connect_timeout"),
        pytest.param("hangs", False, None, id="probe_deadline"),
    ],
)
async def test_the_default_probe_reads_only_silence_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
    outcome: str,
    reachable: bool,
    logged_error: str | None,
) -> None:
    """``_database_reachable`` answers ``False`` only when nothing answered the probe.

    Failure is not silence: a server that rejects the login or has too many clients, or a
    socket the peer reset or the network refused, answered this process and may answer
    another worker, so it reads as reachable and only its class name is logged. Only
    libpq's connect timeout (psycopg's ``ConnectionTimeout`` wrapped by SQLAlchemy) and the
    probe's own deadline read as unreachable, and log nothing.
    """
    monkeypatch.setattr(scheduler_module, "FENCE_PROBE_TIMEOUT", timedelta(milliseconds=50))
    failures: dict[str, Exception] = {
        "authentication_fails": OperationalError(
            None, None, psycopg.OperationalError(_AUTH_FAILED)
        ),
        "too_many_clients": OperationalError(
            None,
            None,
            psycopg.OperationalError("connection failed: FATAL:  sorry, too many clients already"),
        ),
        "socket_reset": ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
        "network_unreachable": OSError(errno.ENETUNREACH, "Network is unreachable"),
        "connect_times_out": OperationalError(
            None, None, ConnectionTimeout("connection timeout expired")
        ),
    }

    async def ping() -> None:
        if outcome == "hangs":
            await asyncio.Event().wait()
        if outcome in failures:
            raise failures[outcome]

    monkeypatch.setattr(scheduler_module, "ping_database", ping)

    assert await scheduler_module._database_reachable() is reachable

    failed = _logged(captured_logs, "scheduler.fence_probe_failed")
    assert [record["error"] for record in failed] == (
        [] if logged_error is None else [logged_error]
    )
    assert all(record["level"] == "warning" for record in failed)
    for leaked in ("password authentication", "too many clients", "Connection reset", "Network"):
        assert leaked not in captured_logs.text


@pytest.mark.parametrize("endpoint", ["silent_server", "refusing_port", "missing_database"])
async def test_the_default_probe_classifies_real_endpoints(
    probe_database_url: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
    endpoint: str,
) -> None:
    """The default probe against what a socket or server really returns.

    ``silent_server`` accepts TCP and never answers: with the probe's deadline at 10 s,
    libpq's 2 s ``connect_timeout`` ends the attempt first, and the ``ConnectionTimeout``
    SQLAlchemy wraps reads as unreachable. A refused connection, and the test server
    rejecting a database that does not exist, answered: both read as reachable.
    """
    monkeypatch.setattr(scheduler_module, "FENCE_PROBE_TIMEOUT", timedelta(seconds=10))
    missing = "ads_engine_fence_probe_missing"
    loop = asyncio.get_running_loop()
    if endpoint == "silent_server":
        async with _silent_server() as port:
            probe_database_url(f"postgresql+psycopg://ads@127.0.0.1:{port}/ads?connect_timeout=2")
            started = loop.time()
            reachable = await scheduler_module._database_reachable()
            elapsed = loop.time() - started
        assert reachable is False
        assert 1.5 <= elapsed < 8.0, "the probe's deadline, not libpq's connect timeout, ended it"
        assert _logged(captured_logs, "scheduler.fence_probe_failed") == []
        return
    if endpoint == "refusing_port":
        with _refusing_port() as port:
            probe_database_url(f"postgresql+psycopg://ads@127.0.0.1:{port}/ads?connect_timeout=2")
            reachable = await scheduler_module._database_reachable()
    else:
        url = make_url(get_settings().database_url.get_secret_value()).set(database=missing)
        probe_database_url(url.render_as_string(hide_password=False))
        reachable = await scheduler_module._database_reachable()
    assert reachable is True
    (failed,) = _logged(captured_logs, "scheduler.fence_probe_failed")
    assert failed["error"] == "OperationalError"
    assert missing not in captured_logs.text


async def test_sigterm_waits_for_a_slow_unwind_and_never_aborts(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On SIGTERM the lock is still held: every task is awaited however long, no abort."""
    monkeypatch.setattr(
        scheduler_module, "LOCK_LOST_STOP_TIMEOUT", timedelta(seconds=_STOP_TIMEOUT_S)
    )
    source_jobs.blocked.add("google")
    source_jobs.stubborn.add("google")
    await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")

    task.cancel()
    await _wait_event(source_jobs.cancelled, "SIGTERM cancelled the google lane")
    await asyncio.sleep(_OVERSTAY_S)
    assert not task.done(), "the scheduler stopped before its lane had unwound"
    assert harness.aborts == []
    source_jobs.release.set()

    await _cancel(task)
    assert recorder.first("job_unwound:google") < len(recorder.events)
    assert harness.aborts == []
    # SIGTERM cancels plainly (a cancelled job logs a warning) and never probes the database.
    assert source_jobs.cancel_args == [()]
    assert harness.probe_at == []
    assert await _lock_holder(db) is None


async def test_defect_waits_for_a_slow_unwind_and_never_aborts(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A defect ends the worker only after every other task unwound, and never aborts."""

    class LaneDefectError(RuntimeError):
        """An error the scheduler's steps do not isolate."""

    async def broken_used_24h(self: GoogleCallBudget) -> int:
        del self  # The defect does not depend on the budget.
        await source_jobs.entered.wait()
        raise LaneDefectError("budget lane defect")

    monkeypatch.setattr(
        scheduler_module, "LOCK_LOST_STOP_TIMEOUT", timedelta(seconds=_STOP_TIMEOUT_S)
    )
    monkeypatch.setattr(GoogleCallBudget, "used_24h", broken_used_24h)
    source_jobs.blocked.add("google")
    source_jobs.stubborn.add("google")
    await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))

    await _wait_event(source_jobs.cancelled, "the defect cancelled the google lane")
    await asyncio.sleep(_OVERSTAY_S)
    assert not task.done(), "the defect ended the scheduler before its lane had unwound"
    assert harness.aborts == []
    source_jobs.release.set()

    try:
        async with asyncio.timeout(_WAIT_S):
            await asyncio.wait((task,))
    except TimeoutError:
        pytest.fail("the defect did not end run_forever")
    assert isinstance(task.exception(), LaneDefectError)
    assert "job_unwound:google" in recorder.events
    assert harness.aborts == []
    assert await _lock_holder(db) is None


async def test_lane_check_reads_segment_and_probe_triggers_and_isolates_failures(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, captured_logs: Any
) -> None:
    """A busy lane's check reads every trigger; a failed read is logged and the rest still run."""

    async def linkedin_active(session: AsyncSession, source: Source) -> int | None:
        del session  # Only the source decides here.
        return 1 if source == "linkedin" else None

    async def linkedin_in_fallback(self: SourceModeController, source: Source) -> bool:
        del self  # Only the source decides here.
        return source == "linkedin"

    async def broken_due_jobs(self: Scheduler, now: datetime | None = None) -> Any:
        del self, now  # The read fails whatever it is asked.
        raise OperationalError("SELECT", {}, ConnectionError("database restarting"))

    monkeypatch.setattr(repositories, "active_dataset_id", linkedin_active)
    monkeypatch.setattr(SourceModeController, "in_fallback", linkedin_in_fallback)
    monkeypatch.setattr(Scheduler, "due_jobs", broken_due_jobs)
    scheduler = harness.scheduler("A")

    # The 04:00 slot has never run (no sync_run history), and no probe has ever run either.
    fired = await scheduler._check_lane("linkedin_hubspot")

    assert fired == ("segments", "probes")
    records = captured_logs()
    failed = [record for record in records if record.get("event") == "scheduler.step_failed"]
    assert [record["step"] for record in failed] == ["lane_check:linkedin_hubspot:due_jobs"]
    (pending,) = [
        record for record in records if record.get("event") == "scheduler.lane_work_pending"
    ]
    assert pending["lane"] == "linkedin_hubspot"
    assert pending["triggers"] == ["segments", "probes"]
    assert await scheduler._check_lane("google") == ()


async def test_cancelled_probe_closes_its_row_failed_and_propagates(
    harness: Harness, db: Sessions, recovery: Recovery, captured_logs: Any
) -> None:
    """A probe cancelled in ``recover`` ends its row ``failed``; the cancellation propagates."""
    task = _tick_task(harness.scheduler("A"), harness.now)
    await _wait_event(recovery.entered, "the google recovery probe started")
    (opened,) = await _probe_rows(db)
    assert (opened.source, opened.status, opened.finished_at) == ("google", "running", None)

    await _cancel(task)

    assert recovery.cancelled.is_set()
    assert recovery.calls == ["google"]
    (row,) = await _probe_rows(db)
    assert row.id == opened.id
    assert (row.status, row.error) == ("failed", _PROBE_CANCELLED)
    assert row.finished_at == harness.now
    (cancelled,) = _logged(captured_logs, "scheduler.probe_cancelled")
    assert (cancelled["level"], cancelled["source"], cancelled["run_id"]) == (
        "warning",
        "google",
        row.id,
    )
    assert _logged(captured_logs, "scheduler.sync_run_close_failed") == []


async def test_sigterm_during_a_recovery_probe_closes_its_row_failed(
    harness: Harness, db: Sessions, recovery: Recovery
) -> None:
    """SIGTERM while the google lane probes cancels it; the probe row ends ``failed``."""
    task = harness.start(harness.scheduler("A"))
    await _wait_event(recovery.entered, "the google lane's recovery probe started")

    await _cancel(task)

    assert recovery.cancelled.is_set()
    (row,) = await _probe_rows(db)
    assert (row.status, row.error) == ("failed", _PROBE_CANCELLED)
    assert row.finished_at is not None
    assert harness.aborts == []
    assert await _lock_holder(db) is None


async def test_lock_lost_during_a_recovery_probe_closes_its_row_failed(
    harness: Harness, db: Sessions, recovery: Recovery
) -> None:
    """A lock lost while the google lane probes cancels it; its row ends ``failed`` in time."""
    task = harness.start(harness.scheduler("A"))
    await _wait_event(recovery.entered, "the google lane's recovery probe started")

    await _kill_lock_session(db)

    await _wait_event(recovery.cancelled, "the lock loss cancelled the recovery probe")

    async def requeued() -> bool:
        return await _lock_lost_requests(db) == 1

    await _eventually(requeued, "the scheduler re-acquired and queued scheduler_lock_lost")
    (row,) = await _probe_rows(db)
    assert (row.status, row.error) == ("failed", _PROBE_CANCELLED)
    assert row.finished_at is not None
    # The cancelled probe's start still spaces the probes: it is not due again yet.
    assert recovery.calls == ["google"]
    assert harness.aborts == []
    assert not task.done()
    await _cancel(task)


@pytest.mark.parametrize(
    ("committed", "activated", "expected"),
    [
        (False, False, ("failed", _PROBE_CANCELLED)),
        (True, False, ("failed", "recovery probe did not pass")),
        (True, True, ("succeeded", None)),
    ],
    ids=["rolled_back_close", "committed_failure", "committed_success"],
)
async def test_cancellation_in_the_probe_close_keeps_a_committed_outcome(
    harness: Harness,
    db: Sessions,
    recovery: Recovery,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
    committed: bool,
    activated: bool,
    expected: tuple[str, str | None],
) -> None:
    """A close the cancellation rolled back ends ``failed``; a committed one stands."""
    recovery.block = False
    if activated:
        recovery.outcome = BuildOutcome(
            source="google", mode="live", dataset_id=1, activated=True, reason="recovered"
        )
    close_run = Scheduler._close_run
    closing = asyncio.Event()

    async def close_then_hang(
        self: Scheduler, run_id: int, status: str, *, error: str | None = None
    ) -> None:
        if closing.is_set():
            # The cancellation's own close.
            await close_run(self, run_id, status, error=error)
            return
        if committed:
            await close_run(self, run_id, status, error=error)
            closing.set()
            await asyncio.Event().wait()
        else:
            # The close's UPDATE ran; the cancellation lands before its commit.
            async with db() as session, session.begin():
                await session.execute(
                    update(SyncRun)
                    .where(SyncRun.id == run_id)
                    .values(status=status, error=error, finished_at=harness.now)
                )
                closing.set()
                await asyncio.Event().wait()

    monkeypatch.setattr(Scheduler, "_close_run", close_then_hang)
    task = _tick_task(harness.scheduler("A"), harness.now)
    await _wait_event(closing, "the probe's own close began")

    await _cancel(task)

    (row,) = await _probe_rows(db)
    assert (row.status, row.error) == expected
    assert row.finished_at == harness.now
    assert len(_logged(captured_logs, "scheduler.probe_cancelled")) == 1
    assert _logged(captured_logs, "scheduler.sync_run_close_failed") == []


@pytest.mark.parametrize("where", ["flush", "commit"])
async def test_cancellation_while_the_probe_row_opens_leaves_no_running_row(
    harness: Harness,
    db: Sessions,
    recovery: Recovery,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
    where: str,
) -> None:
    """Cancelled before the insert is flushed, nothing is written; after its commit, the row
    is closed ``failed``; ``recover`` never runs."""
    hung = asyncio.Event()
    armed = False
    open_run = Scheduler._open_run
    flush = AsyncSession.flush
    exit_transaction = AsyncSessionTransaction.__aexit__

    async def armed_open_run(self: Scheduler, source: Source, job: str, started: datetime) -> int:
        nonlocal armed
        armed = True
        return await open_run(self, source, job, started)

    async def hang() -> None:
        nonlocal armed
        armed = False
        hung.set()
        await asyncio.Event().wait()

    async def hanging_flush(self: AsyncSession, objects: Any = None) -> None:
        if armed:
            await hang()
        await flush(self, objects)

    async def hanging_exit(
        self: AsyncSessionTransaction, type_: Any, value: Any, traceback: Any
    ) -> None:
        await exit_transaction(self, type_, value, traceback)
        if armed and type_ is None:
            await hang()

    monkeypatch.setattr(Scheduler, "_open_run", armed_open_run)
    if where == "flush":
        monkeypatch.setattr(AsyncSession, "flush", hanging_flush)
    else:
        monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", hanging_exit)
    task = _tick_task(harness.scheduler("A"), harness.now)
    await _wait_event(hung, f"the probe row's {where} began")

    await _cancel(task)

    assert recovery.calls == []
    rows = await _probe_rows(db)
    cancelled = _logged(captured_logs, "scheduler.probe_cancelled")
    if where == "flush":
        assert rows == []
        assert cancelled == []
    else:
        (row,) = rows
        assert (row.status, row.error) == ("failed", _PROBE_CANCELLED)
        assert row.finished_at == harness.now
        assert [record["run_id"] for record in cancelled] == [row.id]


@pytest.mark.parametrize("failure", ["fails", "hangs"])
async def test_failed_probe_bookkeeping_is_logged_and_the_cancellation_still_wins(
    harness: Harness,
    db: Sessions,
    recovery: Recovery,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
    failure: str,
) -> None:
    """A cancelled probe's close that fails, or outlasts ``CANCEL_CLOSE_TIMEOUT``, is logged
    and abandoned; the cancellation still propagates."""
    bound = jobs.CANCEL_CLOSE_TIMEOUT.total_seconds()
    monkeypatch.setattr(jobs, "CANCEL_CLOSE_TIMEOUT", timedelta(milliseconds=50))
    close_run = Scheduler._close_run

    async def broken_close_run(
        self: Scheduler, run_id: int, status: str, *, error: str | None = None
    ) -> None:
        if error == _PROBE_CANCELLED:
            if failure == "fails":
                raise OperationalError("UPDATE", {}, ConnectionError("database restarting"))
            await asyncio.Event().wait()
        await close_run(self, run_id, status, error=error)

    monkeypatch.setattr(Scheduler, "_close_run", broken_close_run)
    task = _tick_task(harness.scheduler("A"), harness.now)
    await _wait_event(recovery.entered, "the google recovery probe started")
    loop = asyncio.get_running_loop()
    began = loop.time()

    await _cancel(task)

    assert loop.time() - began < bound
    (row,) = await _probe_rows(db)
    assert (row.status, row.finished_at, row.error) == ("running", None, None)
    assert len(_logged(captured_logs, "scheduler.probe_cancelled")) == 1
    (close_failed,) = _logged(captured_logs, "scheduler.sync_run_close_failed")
    assert close_failed["level"] == "error"
    assert close_failed["run_id"] == row.id
    assert close_failed["error"] == ("OperationalError" if failure == "fails" else "TimeoutError")


async def test_second_cancellation_during_probe_bookkeeping_propagates_at_once(
    harness: Harness,
    db: Sessions,
    recovery: Recovery,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """A further cancellation while a cancelled probe closes its row ends the task at once."""
    hung = asyncio.Event()
    close_run = Scheduler._close_run

    async def hanging_close_run(
        self: Scheduler, run_id: int, status: str, *, error: str | None = None
    ) -> None:
        if error == _PROBE_CANCELLED:
            hung.set()
            await asyncio.Event().wait()
        await close_run(self, run_id, status, error=error)

    monkeypatch.setattr(Scheduler, "_close_run", hanging_close_run)
    task = _tick_task(harness.scheduler("A"), harness.now)
    await _wait_event(recovery.entered, "the google recovery probe started")
    task.cancel()
    await _wait_event(hung, "the cancelled probe's close began")
    loop = asyncio.get_running_loop()
    began = loop.time()

    await _cancel(task)

    assert loop.time() - began < jobs.CANCEL_CLOSE_TIMEOUT.total_seconds() / 2
    (row,) = await _probe_rows(db)
    assert row.status == "running"
    assert _logged(captured_logs, "scheduler.sync_run_close_failed") == []


# ---------------------------------------------------------------------------------------------
# Orphaned sync_run recovery
# ---------------------------------------------------------------------------------------------


async def test_acquisition_fails_orphaned_runs_before_on_start_and_spares_later_ones(
    harness: Harness, db: Sessions, recorder: Recorder, captured_logs: Any
) -> None:
    """Rows left ``running`` before the acquisition end failed as interrupted, before startup."""
    earlier = harness.now - timedelta(hours=2)
    orphan = await _add_sync_run(db, "google", jobs.JOB_ENTITIES, earlier)
    companion = await _add_sync_run(db, "hubspot", jobs.JOB_CRM_COMPANION, earlier)
    finished = await _add_sync_run(db, "linkedin", jobs.JOB_LEADS, earlier, status="succeeded")
    seen_at_start: dict[int, str] = {}
    later: list[int] = []

    async def on_start() -> None:
        for run_id in (orphan, companion):
            seen_at_start[run_id] = await _status(db, run_id)
        # A row this holder's own work opens after the acquisition.
        later.append(await _add_sync_run(db, "google", jobs.JOB_METRICS, harness.now))

    task = harness.start(harness.scheduler("A"), on_start=on_start)

    async def lanes_ran() -> bool:
        return bool(recorder.starting("A:lane:"))

    await _eventually(lanes_ran, "the lanes ran after the startup")
    assert seen_at_start == {orphan: "failed", companion: "failed"}
    for run_id in (orphan, companion):
        row = await _sync_run(db, run_id)
        assert (row.status, row.error, row.finished_at) == ("failed", _INTERRUPTED, harness.now)
    kept = await _sync_run(db, finished)
    assert (kept.status, kept.error) == ("succeeded", None)
    (started_later,) = later
    assert await _status(db, started_later) == "running", "a row of this holder was swept"
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["level"] == "warning"
    assert recovered["count"] == 2
    assert recovered["run_ids"] == sorted([orphan, companion])
    assert recovered["requeued_request_ids"] == []
    # The sweep found no writer, then waited the writer fence through the injected sleep
    # (virtual time) before it wrote; the startup, which saw the rows failed, followed it.
    fence_s = SYNC_WRITER_FENCE.total_seconds()
    recovery_sleeps = harness.sleeps["A"].by_task("scheduler.sync_run_recovery")
    assert [seconds for _, seconds in recovery_sleeps] == [fence_s]
    (fenced,) = _logged(captured_logs, "scheduler.sync_run_recovery_fence_wait")
    assert (fenced["level"], fenced["candidates"], fenced["fence_s"]) == ("info", 2, fence_s)
    events = [record.get("event") for record in captured_logs()]
    assert events.index("scheduler.sync_run_recovery_fence_wait") < events.index(
        "scheduler.sync_runs_recovered"
    )
    assert not task.done()
    await _cancel(task)


async def test_recovery_without_candidates_spends_no_fence(
    harness: Harness, recorder: Recorder, captured_logs: Any
) -> None:
    """With no row ``running`` at the acquisition, nothing waits the writer fence."""
    task = harness.start(harness.scheduler("A"))

    async def lanes_ran() -> bool:
        return bool(recorder.starting("A:lane:"))

    await _eventually(lanes_ran, "the lanes ran after the acquisition")
    assert harness.sleeps["A"].by_task("scheduler.sync_run_recovery") == []
    assert _logged(captured_logs, "scheduler.sync_run_recovery_fence_wait") == []
    assert _logged(captured_logs, "scheduler.sync_runs_recovered") == []
    await _cancel(task)


async def test_recovery_requeues_the_interrupted_sync_now_and_the_lanes_serve_it(
    harness: Harness, db: Sessions, source_jobs: SourceJobs, captured_logs: Any
) -> None:
    """A "Sync now" whose jobs a dead process left running is pending again, then served."""
    google_at = harness.now - timedelta(minutes=10)
    google = await _add_picked_request(db, "google", google_at)
    google_run = await _add_sync_run(db, "google", jobs.JOB_ENTITIES, google_at)
    linkedin_at = harness.now - timedelta(minutes=20)
    linkedin = await _add_picked_request(db, "linkedin", linkedin_at)
    # A simulated LinkedIn's companion CRM rows are HubSpot rows of the same lane pass.
    companion_run = await _add_sync_run(db, "hubspot", jobs.JOB_CRM_COMPANION, linkedin_at)
    served_at = harness.now - timedelta(hours=1)
    served = await _add_picked_request(db, "google", served_at)

    task = harness.start(harness.scheduler("A"))

    async def both_served() -> bool:
        return len(source_jobs.calls) >= 2

    await _eventually(both_served, "the lanes served both re-queued requests")
    assert sorted(source_jobs.calls) == [("google", "manual"), ("linkedin", "manual")]
    assert await _picked_at(db, google) == harness.now
    assert await _picked_at(db, linkedin) == harness.now
    assert await _picked_at(db, served) == served_at, "a request no swept row ran was re-queued"
    for run_id in (google_run, companion_run):
        assert await _status(db, run_id) == "failed"
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["run_ids"] == sorted([google_run, companion_run])
    assert sorted(recovered["requeued_request_ids"]) == sorted([google, linkedin])
    assert sorted(recovered["requeued_sources"]) == ["google", "linkedin"]
    assert recovered["already_pending_request_ids"] == []
    await _cancel(task)


async def test_recovery_keeps_a_pending_request_and_leaves_the_interrupted_one_picked(
    harness: Harness, db: Sessions, source_jobs: SourceJobs, captured_logs: Any
) -> None:
    """One pending request per source: an existing pending one wins over the re-queue."""
    picked_at = harness.now - timedelta(minutes=10)
    interrupted = await _add_picked_request(db, "google", picked_at)
    swept = await _add_sync_run(db, "google", jobs.JOB_METRICS, picked_at)
    (pending,) = await _add_sync_requests(db, ("google",), harness.now - timedelta(minutes=1))

    task = harness.start(harness.scheduler("A"))

    async def served() -> bool:
        return await _picked(db, pending)

    await _eventually(served, "the google lane claimed the pending request")
    assert await _status(db, swept) == "failed"
    assert await _picked_at(db, interrupted) == picked_at
    assert source_jobs.calls == [("google", "manual")]
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["run_ids"] == [swept]
    assert recovered["requeued_request_ids"] == []
    assert recovered["already_pending_request_ids"] == [interrupted]
    await _cancel(task)


def _sweep_clock() -> tuple[datetime, datetime]:
    """The sweep's wall clock, and a claim ten minutes earlier with a lane pass's fraction."""
    now = datetime.now(UTC).replace(microsecond=0)
    return now, now - timedelta(minutes=10) + timedelta(microseconds=550_000)


async def _sweep(now: datetime, *candidates: int) -> None:
    """Sweep ``candidates`` once on a wall clock fixed at ``now``; it must commit.

    The writer fence is not waited: these tests correlate rows with requests, and the fence
    is timed by the tests above and the sync-writer tests.
    """

    async def no_fence(seconds: float) -> None:
        del seconds  # Not waited; see the docstring.

    assert await _fence_scheduler(no_fence, now)._sweep_orphaned_runs(candidates)


async def test_sweep_requeues_a_sync_now_from_its_jobs_own_start_stamps(
    db: Sessions, captured_logs: Any
) -> None:
    """Rows of a "Sync now" start after its claim, each job at its own start: re-queued once.

    LinkedIn's ``entities`` row starts 0.5 s after the claim and the companion CRM's three
    dataset rows 3 s after it, as the jobs stamp them; none equals ``picked_at``.
    """
    now, claim_at = _sweep_clock()
    request = await _add_picked_request(db, "linkedin", claim_at)
    entities = await _add_sync_run(
        db, "linkedin", jobs.JOB_ENTITIES, claim_at + timedelta(milliseconds=500)
    )
    companion_at = claim_at + timedelta(seconds=3)
    companion = [
        await _add_sync_run(db, "hubspot", jobs.JOB_CRM_COMPANION, companion_at, dataset=name)
        for name in (
            DATASET_HUBSPOT_CONTACTS,
            DATASET_HUBSPOT_ASSOCIATIONS,
            DATASET_HUBSPOT_DEAL_SEARCH,
        )
    ]

    await _sweep(now, entities, *companion)

    assert await _picked_at(db, request) is None
    assert await _pending_ids(db, "linkedin") == [request]
    for run_id in (entities, *companion):
        row = await _sync_run(db, run_id)
        assert (row.status, row.error, row.finished_at) == ("failed", _INTERRUPTED, now)
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["run_ids"] == sorted([entities, *companion])
    assert recovered["requeued_request_ids"] == [request]
    assert recovered["requeued_sources"] == ["linkedin"]
    assert recovered["already_pending_request_ids"] == []


async def test_sweep_never_takes_a_later_scheduled_run_for_a_completed_sync_now(
    db: Sessions, captured_logs: Any
) -> None:
    """A run of a job the "Sync now" already ran is a second run, not the request's own."""
    now, claim_at = _sweep_clock()
    claim_at -= timedelta(hours=3)
    request = await _add_picked_request(db, "linkedin", claim_at)
    for offset, job in enumerate(jobs.SOURCE_JOBS["linkedin"], start=1):
        await _add_sync_run(
            db, "linkedin", job, claim_at + timedelta(seconds=offset), status="succeeded"
        )
    scheduled = await _add_sync_run(
        db, "linkedin", jobs.JOB_ENTITIES, claim_at + timedelta(hours=3)
    )

    await _sweep(now, scheduled)

    assert await _status(db, scheduled) == "failed"
    assert await _picked_at(db, request) == claim_at
    assert await _pending_ids(db, "linkedin") == []
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["run_ids"] == [scheduled]
    assert recovered["requeued_request_ids"] == []
    assert recovered["already_pending_request_ids"] == []


@pytest.mark.parametrize(
    ("claimed", "swept"),
    [
        pytest.param("linkedin", ("hubspot", jobs.JOB_CRM), id="hubspot_crm_after_linkedin"),
        pytest.param("hubspot", ("linkedin", jobs.JOB_LEADS), id="linkedin_leads_after_hubspot"),
        pytest.param("google", ("google", JOB_PROBE), id="google_probe"),
        pytest.param("linkedin", ("linkedin", JOB_PROBE), id="linkedin_probe"),
        pytest.param("google", ("google", jobs.JOB_RESTATEMENT), id="google_restatement"),
    ],
)
async def test_sweep_requeues_nothing_for_a_row_outside_the_claims_manual_run(
    db: Sessions, captured_logs: Any, claimed: Source, swept: tuple[Source, str]
) -> None:
    """A row that is no job of the claimed source's manual run belongs to no request.

    HubSpot's primary ``crm`` after a LinkedIn claim (and LinkedIn's rows after a HubSpot
    one), a recovery ``probe``, and a job the manual run never runs re-queue nothing.
    """
    now, claim_at = _sweep_clock()
    request = await _add_picked_request(db, claimed, claim_at)
    source, job = swept
    orphan = await _add_sync_run(db, source, job, claim_at + timedelta(seconds=5))

    await _sweep(now, orphan)

    assert await _status(db, orphan) == "failed"
    assert await _picked_at(db, request) == claim_at
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["run_ids"] == [orphan]
    assert recovered["requeued_request_ids"] == []
    assert recovered["already_pending_request_ids"] == []


async def test_sweep_keeps_the_interrupted_request_picked_behind_a_pending_one(
    db: Sessions, captured_logs: Any
) -> None:
    """One pending request per source: a newer pending one wins, the interrupted one is kept."""
    now, claim_at = _sweep_clock()
    interrupted = await _add_picked_request(db, "google", claim_at)
    orphan = await _add_sync_run(db, "google", jobs.JOB_METRICS, claim_at + timedelta(seconds=2))
    (pending,) = await _add_sync_requests(db, ("google",), now - timedelta(minutes=1))

    await _sweep(now, orphan)

    assert await _picked_at(db, interrupted) == claim_at
    assert await _pending_ids(db, "google") == [pending]
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["requeued_request_ids"] == []
    assert recovered["requeued_sources"] == []
    assert recovered["already_pending_request_ids"] == [interrupted]


async def test_sweep_of_two_lanes_requeues_each_lanes_own_request(
    db: Sessions, captured_logs: Any
) -> None:
    """Rows of both source lanes swept at once each re-queue the request of their own lane."""
    now, google_at = _sweep_clock()
    linkedin_at = google_at + timedelta(minutes=2, microseconds=125_000)
    google = await _add_picked_request(db, "google", google_at)
    linkedin = await _add_picked_request(db, "linkedin", linkedin_at)
    google_run = await _add_sync_run(
        db, "google", jobs.JOB_ENTITIES, google_at + timedelta(milliseconds=400)
    )
    linkedin_run = await _add_sync_run(
        db, "linkedin", jobs.JOB_LEADS, linkedin_at + timedelta(seconds=7)
    )

    await _sweep(now, google_run, linkedin_run)

    assert await _pending_ids(db, "google") == [google]
    assert await _pending_ids(db, "linkedin") == [linkedin]
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["run_ids"] == sorted([google_run, linkedin_run])
    assert recovered["requeued_request_ids"] == [google, linkedin]
    assert recovered["requeued_sources"] == ["google", "linkedin"]
    assert recovered["already_pending_request_ids"] == []


@pytest.mark.parametrize(
    ("claimed", "closed", "swept", "requeued"),
    [
        pytest.param(
            ("linkedin", "hubspot"),
            None,
            ("linkedin", jobs.JOB_ENTITIES),
            ("linkedin", "hubspot"),
            id="first_served_linkedin_interrupted",
        ),
        pytest.param(
            ("hubspot", "linkedin"),
            None,
            ("hubspot", jobs.JOB_CRM),
            ("hubspot", "linkedin"),
            id="first_served_hubspot_interrupted",
        ),
        pytest.param(
            ("hubspot", "linkedin"),
            ("hubspot", jobs.JOB_CRM),
            ("linkedin", jobs.JOB_ENTITIES),
            ("linkedin",),
            id="last_served_linkedin_interrupted",
        ),
    ],
)
async def test_sweep_of_a_shared_claim_requeues_the_interrupted_request_and_those_after_it(
    db: Sessions,
    captured_logs: Any,
    claimed: tuple[Source, Source],
    closed: tuple[Source, str] | None,
    swept: tuple[Source, str],
    requeued: tuple[Source, ...],
) -> None:
    """HubSpot and LinkedIn claimed by one pass share one ``picked_at``.

    The request the swept row belongs to is re-queued whichever id is greater, with the one
    the pass serves after it (never reached); one served before it stays picked.
    """
    now, claim_at = _sweep_clock()
    ids = {source: await _add_picked_request(db, source, claim_at) for source in claimed}
    if closed is not None:
        await _add_sync_run(
            db, closed[0], closed[1], claim_at + timedelta(milliseconds=200), status="succeeded"
        )
    orphan = await _add_sync_run(db, swept[0], swept[1], claim_at + timedelta(seconds=4))

    await _sweep(now, orphan)

    for source, request_id in ids.items():
        expected = None if source in requeued else claim_at
        assert await _picked_at(db, request_id) == expected, source
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["requeued_request_ids"] == [ids[source] for source in requeued]
    assert recovered["requeued_sources"] == list(requeued)
    assert recovered["already_pending_request_ids"] == []


async def test_recovery_defers_behind_live_sync_writers_then_sweeps_only_its_candidates(
    harness: Harness,
    db: Sessions,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """While CLI writers hold the lock shared, the sweep waits; then only the original rows go."""
    retry = Scheduler._retry_recovery
    attempts = 0
    active = 0
    most_active = 0

    async def tracked_retry(self: Scheduler, candidates: tuple[int, ...]) -> tuple[int, ...]:
        nonlocal attempts, active, most_active
        attempts += 1
        active += 1
        most_active = max(most_active, active)
        try:
            return await retry(self, candidates)
        finally:
            active -= 1

    monkeypatch.setattr(Scheduler, "_retry_recovery", tracked_retry)
    orphan = await _add_sync_run(db, "google", jobs.JOB_ENTITIES, harness.now - timedelta(hours=1))
    holding = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def cli_sync(index: int) -> None:
        async with sync_writer():
            holding[index].set()
            await release.wait()

    # Two commands at once: the writer lock is shared.
    writers = [asyncio.create_task(cli_sync(index)) for index in range(2)]
    for index, event in enumerate(holding):
        await _wait_event(event, f"stand-in CLI {index} holds the sync-writer lock")
    later: list[int] = []

    async def on_start() -> None:
        later.append(await _add_sync_run(db, "google", jobs.JOB_METRICS, harness.now))

    task = harness.start(harness.scheduler("A"), on_start=on_start)

    async def deferred_twice() -> bool:
        return len(_logged(captured_logs, "scheduler.sync_run_recovery_deferred")) >= 2

    await _eventually(deferred_twice, "the recovery deferred at acquisition and at a wake")
    assert await _status(db, orphan) == "running"
    # A row a live CLI opens after the acquisition is never a candidate.
    cli_row = await _add_sync_run(db, "linkedin", jobs.JOB_LEADS, harness.now)
    release.set()
    await asyncio.wait(writers)
    for writer in writers:
        writer.result()

    async def swept() -> bool:
        return await _status(db, orphan) == "failed"

    await _eventually(swept, "a wake swept the orphan once the writers finished")
    attempts_at_sweep = attempts
    budget_at_sweep = harness.passes["A"]["budget"]

    async def two_more_wakes() -> bool:
        return harness.passes["A"]["budget"] >= budget_at_sweep + 2

    await _eventually(two_more_wakes, "two more wakes passed after the sweep")
    assert attempts == attempts_at_sweep, "a done recovery was dispatched again"
    assert most_active == 1
    assert (await _sync_run(db, orphan)).error == _INTERRUPTED
    assert await _status(db, cli_row) == "running"
    assert await _status(db, later[0]) == "running"
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["run_ids"] == [orphan]
    deferrals = _logged(captured_logs, "scheduler.sync_run_recovery_deferred")
    assert {(record["level"], record["candidates"]) for record in deferrals} == {("info", 1)}
    # A sweep deferred behind a live writer returns at once; only the one that found the key
    # free waited the writer fence before sweeping.
    (fenced,) = _logged(captured_logs, "scheduler.sync_run_recovery_fence_wait")
    assert fenced["candidates"] == 1
    assert not task.done()
    await _cancel(task)


async def test_failed_recovery_sweep_is_logged_and_retried_at_a_wake(
    harness: Harness, db: Sessions, monkeypatch: pytest.MonkeyPatch, captured_logs: Any
) -> None:
    """A database error in the sweep never ends the worker; a later wake sweeps the same rows."""
    sweep = Scheduler._sweep_orphaned_runs
    failures = 1

    async def flaky_sweep(self: Scheduler, candidates: tuple[int, ...]) -> bool:
        nonlocal failures
        if failures:
            failures -= 1
            raise OperationalError("UPDATE sync_run", {}, ConnectionError("database restarting"))
        return await sweep(self, candidates)

    monkeypatch.setattr(Scheduler, "_sweep_orphaned_runs", flaky_sweep)
    orphan = await _add_sync_run(db, "linkedin", jobs.JOB_LEADS, harness.now - timedelta(hours=1))

    task = harness.start(harness.scheduler("A"))

    async def swept() -> bool:
        return await _status(db, orphan) == "failed"

    await _eventually(swept, "a wake retried the sweep that failed at the acquisition")
    (failed,) = _logged(captured_logs, "scheduler.sync_run_recovery_failed")
    assert failed["level"] == "error"
    assert (failed["stage"], failed["error"], failed["candidates"]) == (
        "sweep",
        "OperationalError",
        1,
    )
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["run_ids"] == [orphan]
    assert not task.done()
    assert harness.aborts == []
    await _cancel(task)


async def test_reacquisition_recovers_rows_a_cancelled_lane_left_and_reruns_its_request(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """A lock loss that strands a job's row: the next acquisition fails it and re-queues the run.

    The sweep re-queues the request first; the lock-loss requeue then finds its in-flight
    claim no longer matching and leaves it alone: no constraint error, and only the sweep
    reports it.
    """
    source_jobs.blocked.add("google")
    opened: list[int] = []

    async def open_row(source: Source, now: datetime) -> None:
        # The job's own row, whose close the lost lock's cancellation abandoned.
        if not opened:
            opened.append(await _add_sync_run(db, source, jobs.JOB_ENTITIES, now))

    source_jobs.on_call = open_row
    (request,) = await _add_sync_requests(db, ("google",), harness.now)

    async def request_state() -> tuple[datetime | None, list[int]]:
        return await _picked_at(db, request), await _pending_ids(db, "google")

    before_lanes = _snapshot_before_lanes_resume(monkeypatch, request_state)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")

    await _kill_lock_session(db)
    await _wait_event(source_jobs.cancelled, "the lock loss cancelled the google lane")

    async def served_again() -> bool:
        return len(source_jobs.calls) >= 2

    await _eventually(served_again, "the re-queued request was served after re-acquiring")
    (run_id,) = opened
    row = await _sync_run(db, run_id)
    assert (row.status, row.error) == ("failed", _INTERRUPTED)
    assert source_jobs.calls == [("google", "manual"), ("google", "manual")]
    assert recorder.nth("A:acquired", 2) < recorder.nth("job:google", 2)
    (recovered,) = _logged(captured_logs, "scheduler.sync_runs_recovered")
    assert recovered["run_ids"] == [run_id]
    assert recovered["requeued_request_ids"] == [request]
    assert recovered["requeued_sources"] == ["google"]
    assert recovered["already_pending_request_ids"] == []
    events = [record.get("event") for record in captured_logs()]
    assert events.index("scheduler.sync_runs_recovered") < events.index(
        "scheduler.lock_lost_requeued"
    )
    # Pending exactly once before any lane ran; the requeue matched nothing and failed nothing.
    assert before_lanes == [(None, [request])]
    assert _logged(captured_logs, "scheduler.sync_requests_requeued") == []
    assert _logged(captured_logs, "scheduler.lock_lost_requeue_failed") == []
    await _cancel(task)


async def test_reacquisition_requeues_a_sync_now_whose_cancelled_job_closed_its_rows(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """A lost lock cancels a "Sync now" whose job closes its own row: the request runs again.

    The lock session is terminated (an answered loss) while the job runs; its cancel
    bookkeeping records its row ``failed`` (``job cancelled``) before the lane ends, so no row
    is left ``running`` and the orphaned-run sweep has nothing to recover. The lock-loss
    requeue makes the request pending again from the scheduler's in-flight record, and the
    next wake serves it.
    """
    source_jobs.blocked.add("google")
    opened: list[int] = []
    closed: list[int] = []

    async def open_row(source: Source, now: datetime) -> None:
        if not opened:
            opened.append(await _add_sync_run(db, source, jobs.JOB_ENTITIES, now))

    async def close_row(source: Source) -> None:
        del source  # The job's one row is closed.
        if closed:
            return
        async with db() as session, session.begin():
            await session.execute(
                update(SyncRun)
                .where(SyncRun.id == opened[0], SyncRun.status == "running")
                .values(status="failed", finished_at=harness.now, error="job cancelled")
            )
        closed.append(opened[0])

    source_jobs.on_call = open_row
    source_jobs.on_cancel = close_row
    (request,) = await _add_sync_requests(db, ("google",), harness.now)

    async def request_state() -> tuple[datetime | None, list[int]]:
        return await _picked_at(db, request), await _pending_ids(db, "google")

    before_lanes = _snapshot_before_lanes_resume(monkeypatch, request_state)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")
    assert await _picked_at(db, request) == harness.now

    await _kill_lock_session(db)
    await _wait_event(source_jobs.cancelled, "the lock loss cancelled the google lane")

    async def served_again() -> bool:
        return len(source_jobs.calls) >= 2

    await _eventually(served_again, "the re-queued request was served after re-acquiring")
    (run_id,) = opened
    row = await _sync_run(db, run_id)
    assert (row.status, row.error) == ("failed", "job cancelled")
    assert closed == [run_id]
    # Pending again after the re-acquisition, before any lane ran; then claimed again.
    assert before_lanes == [(None, [request])]
    assert await _picked_at(db, request) == harness.now
    assert source_jobs.calls == [("google", "manual"), ("google", "manual")]
    assert recorder.nth("A:acquired", 2) < recorder.nth("job:google", 2)
    assert _logged(captured_logs, "scheduler.sync_runs_recovered") == []
    (requeued,) = _logged(captured_logs, "scheduler.sync_requests_requeued")
    assert requeued["level"] == "warning"
    assert requeued["requeued_request_ids"] == [request]
    assert requeued["requeued_sources"] == ["google"]
    assert requeued["already_pending_request_ids"] == []
    assert _lock_lost_causes(captured_logs) == ["answered"]
    assert await _lock_lost_requests(db) == 1
    assert harness.aborts == []
    await _cancel(task)


async def test_lock_loss_requeue_keeps_an_interrupted_sync_now_picked_behind_a_pending_one(
    harness: Harness,
    db: Sessions,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """One pending request per source: a request made while the job ran wins over the requeue."""
    source_jobs.blocked.add("google")
    (interrupted,) = await _add_sync_requests(db, ("google",), harness.now)

    async def google_requests() -> tuple[datetime | None, list[int]]:
        return await _picked_at(db, interrupted), await _pending_ids(db, "google")

    before_lanes = _snapshot_before_lanes_resume(monkeypatch, google_requests)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")
    # The operator asks again while the first request's job runs: the API accepts it, since
    # the first one is picked.
    (pending,) = await _add_sync_requests(db, ("google",), harness.now + timedelta(seconds=1))

    await _kill_lock_session(db)
    await _wait_event(source_jobs.cancelled, "the lock loss cancelled the google lane")

    async def served_again() -> bool:
        return len(source_jobs.calls) >= 2

    await _eventually(served_again, "the pending request was served after re-acquiring")
    # Exactly one pending row before any lane ran: the newer request; the interrupted one
    # kept its claim.
    assert before_lanes == [(harness.now, [pending])]
    assert await _picked_at(db, interrupted) == harness.now
    assert await _picked(db, pending)
    assert source_jobs.calls == [("google", "manual"), ("google", "manual")]
    (requeued,) = _logged(captured_logs, "scheduler.sync_requests_requeued")
    assert requeued["requeued_request_ids"] == []
    assert requeued["requeued_sources"] == []
    assert requeued["already_pending_request_ids"] == [interrupted]
    assert _logged(captured_logs, "scheduler.lock_lost_requeue_failed") == []
    await _cancel(task)


async def test_lock_loss_requeues_only_the_sync_now_its_pass_had_not_served(
    harness: Harness,
    db: Sessions,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """A request whose jobs completed before the loss stays served; only the cut one re-runs.

    One ``linkedin_hubspot`` pass claims both requests and serves HubSpot's (the older) to
    completion before LinkedIn's job blocks; the lost lock cancels the pass there.
    """
    source_jobs.blocked.add("linkedin")
    (hubspot,) = await _add_sync_requests(db, ("hubspot",), harness.now - timedelta(minutes=1))
    (linkedin,) = await _add_sync_requests(db, ("linkedin",), harness.now)

    async def lane_requests() -> tuple[datetime | None, datetime | None]:
        return await _picked_at(db, hubspot), await _picked_at(db, linkedin)

    before_lanes = _snapshot_before_lanes_resume(monkeypatch, lane_requests)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the linkedin job started after hubspot's completed")
    assert source_jobs.calls == [("hubspot", "manual"), ("linkedin", "manual")]

    await _kill_lock_session(db)
    await _wait_event(source_jobs.cancelled, "the lock loss cancelled the linkedin job")

    async def served_again() -> bool:
        return len(source_jobs.calls) >= 3

    await _eventually(served_again, "the re-queued linkedin request was served again")
    assert before_lanes == [(harness.now, None)]
    assert source_jobs.calls == [
        ("hubspot", "manual"),
        ("linkedin", "manual"),
        ("linkedin", "manual"),
    ]
    assert await _picked_at(db, hubspot) == harness.now
    (requeued,) = _logged(captured_logs, "scheduler.sync_requests_requeued")
    assert requeued["requeued_request_ids"] == [linkedin]
    assert requeued["requeued_sources"] == ["linkedin"]
    assert requeued["already_pending_request_ids"] == []
    await _cancel(task)


async def test_a_failed_lock_loss_requeue_keeps_the_claim_for_the_next_acquisition(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """A database error rolls the requeue back; the claim is re-queued at the next acquisition."""
    enqueue = repositories.enqueue_analysis_request
    failures = 1

    async def flaky_enqueue(session: AsyncSession, reason: str) -> None:
        nonlocal failures
        if reason == _LOCK_LOST and failures:
            failures -= 1
            raise OperationalError(
                "INSERT analysis_request", {}, ConnectionError("database restarting")
            )
        await enqueue(session, reason)

    monkeypatch.setattr(repositories, "enqueue_analysis_request", flaky_enqueue)
    source_jobs.blocked.add("google")
    (request,) = await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")

    await _kill_lock_session(db)
    await _wait_event(source_jobs.cancelled, "the lock loss cancelled the google lane")

    async def google_ran_again() -> bool:
        acquisitions = [index for index, seen in enumerate(recorder.events) if seen == "A:acquired"]
        return len(acquisitions) >= 2 and any(
            index > acquisitions[1] for index in recorder.starting("A:lane_end:google")
        )

    await _eventually(google_ran_again, "the google lane ran after the failed requeue")
    # The requeue's transaction rolled back, so the request kept its claim; nothing served it.
    assert await _picked_at(db, request) == harness.now
    assert source_jobs.calls == [("google", "manual")]
    (failed,) = _logged(captured_logs, "scheduler.lock_lost_requeue_failed")
    assert failed["level"] == "error"
    assert (failed["error"], failed["sync_request_ids"]) == ("OperationalError", [request])
    assert _logged(captured_logs, "scheduler.sync_requests_requeued") == []

    await _kill_lock_session(db)

    async def served_again() -> bool:
        return len(source_jobs.calls) >= 2

    await _eventually(served_again, "the next acquisition re-queued the kept claim")
    assert recorder.nth("A:acquired", 3) < recorder.nth("job:google", 2)
    (requeued,) = _logged(captured_logs, "scheduler.sync_requests_requeued")
    assert requeued["requeued_request_ids"] == [request]
    assert await _lock_lost_requests(db) == 1
    assert harness.aborts == []
    await _cancel(task)


@pytest.mark.parametrize(
    "failure", ["update_error", "commit_error", "lost_after_commit", "cancellation"]
)
async def test_a_failed_claim_keeps_its_records_and_the_requeue_restores_only_committed_ones(
    harness: Harness,
    db: Sessions,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
    failure: str,
) -> None:
    """A claim's records outlive whatever ends it; the next requeue undoes only what landed.

    * ``update_error``: the claim's ``UPDATE`` fails before ``RETURNING`` answers, so
      nothing is recorded and both requests stay pending.
    * ``commit_error``: the commit fails and rolls back after both rows were recorded; the
      records stay, and the lock-loss requeue matches no ``picked_at`` and changes nothing.
    * ``lost_after_commit``: the commit lands, then an error reports the connection lost;
      the records stay, so the requeue makes both picked requests pending again.
    * ``cancellation``: lands inside the transaction once the first row was recorded; the
      transaction rolls back and that record stays, matching nothing.

    The requeue clears every record in each case.
    """
    google, linkedin = await _add_sync_requests(db, ("google", "linkedin"), harness.now)
    scheduler = harness.scheduler("A")
    narrow = scheduler_module._as_source
    execute = AsyncSession.execute
    exit_transaction = AsyncSessionTransaction.__aexit__
    lost = OperationalError("COMMIT", {}, ConnectionError("server closed the connection"))
    armed = True
    narrowed = 0

    def cancelling_as_source(value: str) -> Source | None:
        nonlocal narrowed
        narrowed += 1
        if narrowed == 2:
            raise asyncio.CancelledError
        return narrow(value)

    async def failing_execute(self: AsyncSession, statement: Any, *args: Any, **kw: Any) -> Any:
        nonlocal armed
        if armed:
            armed = False
            raise OperationalError(
                "UPDATE sync_request", {}, ConnectionError("database restarting")
            )
        return await execute(self, statement, *args, **kw)

    async def failing_exit(
        self: AsyncSessionTransaction, type_: Any, value: Any, traceback: Any
    ) -> None:
        nonlocal armed
        if not armed or type_ is not None:
            await exit_transaction(self, type_, value, traceback)
            return
        armed = False
        if failure == "commit_error":
            # The commit fails: the transaction rolls back as on any error inside it.
            await exit_transaction(self, type(lost), lost, None)
        else:
            # The commit lands; its outcome is lost on the way back.
            await exit_transaction(self, None, None, None)
        raise lost

    if failure == "update_error":
        monkeypatch.setattr(AsyncSession, "execute", failing_execute)
    elif failure == "cancellation":
        monkeypatch.setattr(scheduler_module, "_as_source", cancelling_as_source)
    else:
        monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", failing_exit)
    expected: type[BaseException] = (
        asyncio.CancelledError if failure == "cancellation" else OperationalError
    )
    with pytest.raises(expected):
        await scheduler._claim_sync_requests(harness.now, ("google", "linkedin"))
    armed = False
    monkeypatch.setattr(scheduler_module, "_as_source", narrow)
    records = dict(scheduler._claims_in_flight)
    if failure == "update_error":
        assert records == {}
    elif failure == "cancellation":
        ((request_id, claim),) = records.items()
        assert request_id in (google, linkedin)
        assert claim == ("google" if request_id == google else "linkedin", harness.now)
    else:
        assert records == {google: ("google", harness.now), linkedin: ("linkedin", harness.now)}
    claimed = failure == "lost_after_commit"
    assert await _pending_ids(db, "google") == ([] if claimed else [google])
    assert await _pending_ids(db, "linkedin") == ([] if claimed else [linkedin])

    await scheduler._requeue_after_loss()

    assert scheduler._claims_in_flight == {}
    assert await _pending_ids(db, "google") == [google]
    assert await _pending_ids(db, "linkedin") == [linkedin]
    requeued = _logged(captured_logs, "scheduler.sync_requests_requeued")
    if claimed:
        (logged,) = requeued
        assert logged["requeued_request_ids"] == [google, linkedin]
        assert logged["requeued_sources"] == ["google", "linkedin"]
        assert logged["already_pending_request_ids"] == []
    else:
        assert requeued == []
    assert _logged(captured_logs, "scheduler.lock_lost_requeue_failed") == []
    assert await _lock_lost_requests(db) == 1


# ---------------------------------------------------------------------------------------------
# Sync-writer fence
# ---------------------------------------------------------------------------------------------

_WRITER_CHECK_EVERY: Final = timedelta(milliseconds=20)
"""``LOCK_CHECK_INTERVAL`` (real time) in the sync-writer tests."""

_WRITER_CHECK_TIMEOUT: Final = timedelta(milliseconds=500)
"""``LOCK_CHECK_TIMEOUT`` (real time) in the sync-writer tests."""

_WRITER_STOP_TIMEOUT: Final = timedelta(milliseconds=300)
"""``LOCK_LOST_STOP_TIMEOUT`` (real time) in the sync-writer tests."""

_WRITER_FENCE: Final = timedelta(seconds=1)
"""``SYNC_WRITER_FENCE`` (real time) in the sync-writer tests: longer than the three above."""

_SUPERVISOR: Final = "sync_writer.supervisor"


def _shorten_writer_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the writer's checks, stop timeout and the recovery's fence in real milliseconds."""
    assert _WRITER_CHECK_EVERY + _WRITER_CHECK_TIMEOUT + _WRITER_STOP_TIMEOUT < _WRITER_FENCE
    monkeypatch.setattr(scheduler_module, "LOCK_CHECK_INTERVAL", _WRITER_CHECK_EVERY)
    monkeypatch.setattr(scheduler_module, "LOCK_CHECK_TIMEOUT", _WRITER_CHECK_TIMEOUT)
    monkeypatch.setattr(scheduler_module, "LOCK_LOST_STOP_TIMEOUT", _WRITER_STOP_TIMEOUT)
    monkeypatch.setattr(scheduler_module, "SYNC_WRITER_FENCE", _WRITER_FENCE)


async def _writer_lock_holders(db: Sessions) -> list[int]:
    """The backend pids holding :data:`SYNC_WRITER_LOCK_KEY` shared in this database."""
    unsigned = SYNC_WRITER_LOCK_KEY & 0xFFFF_FFFF_FFFF_FFFF
    async with db() as session:
        found = await session.scalars(
            text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
                " AND mode = 'ShareLock'"
                " AND classid = CAST(:classid AS oid) AND objid = CAST(:objid AS oid)"
                " AND objsubid = 1"
                " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                " ORDER BY pid"
            ),
            {"classid": unsigned >> 32, "objid": unsigned & 0xFFFF_FFFF},
        )
        return [int(pid) for pid in found]


async def _kill_writer_lock_session(db: Sessions) -> int:
    """Terminate the one backend holding the sync-writer lock; wait until the server ended it."""
    (pid,) = await _writer_lock_holders(db)
    async with db() as session:
        await session.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

    async def gone() -> bool:
        async with db() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
            )
        return int(count or 0) == 0

    await _eventually(gone, f"writer backend {pid} has ended")
    return pid


def _supervisors() -> list[asyncio.Task[Any]]:
    """Every sync-writer supervisor task of the event loop, ended or not."""
    return [task for task in asyncio.all_tasks() if task.get_name() == _SUPERVISOR]


def _fence_scheduler(sleep: Callable[[float], Awaitable[None]], now: datetime) -> Scheduler:
    """A scheduler on a fixed wall clock whose injected sleep times the writer fence."""
    controller = SourceModeController(get_settings(), clock=lambda: now)
    return Scheduler(controller=controller, clock=lambda: now, sleep=sleep)


def _assert_lock_lost_logged(captured_logs: Any) -> dict[str, Any]:
    """The one ``sync_writer.lock_lost``: error level, the error's class name, no message text."""
    (lost,) = _logged(captured_logs, "sync_writer.lock_lost")
    assert lost["level"] == "error"
    assert lost["error"] == "OperationalError"
    assert lost["timeout_s"] == _WRITER_CHECK_TIMEOUT.total_seconds()
    assert "terminating" not in repr(lost), "the exception text reached the log"
    return lost


async def test_sweep_after_a_writer_lost_its_lock_waits_until_the_writer_has_stopped(
    db: Sessions, monkeypatch: pytest.MonkeyPatch, captured_logs: Any
) -> None:
    """A CLI writer whose lock session dies stops before the sweep that found the key free.

    The writer's lock checks wait on a gate until the sweep's fence has begun, so the sweep's
    first transaction always finds the key free while the writer still runs: the moment the
    sweep used to record the live row as interrupted.
    """
    _shorten_writer_timing(monkeypatch)
    gate = asyncio.Event()
    failure = scheduler_module._WriterFence._failure

    async def gated_failure(self: Any) -> str | None:
        await gate.wait()
        return await failure(self)

    monkeypatch.setattr(scheduler_module._WriterFence, "_failure", gated_failure)
    now = datetime.now(UTC).replace(microsecond=0)
    opened: list[int] = []
    entered = asyncio.Event()
    outcome: list[tuple[str, int]] = []

    async def cli_writer() -> None:
        try:
            async with sync_writer():
                opened.append(await _add_sync_run(db, "google", jobs.JOB_ENTITIES, now))
                entered.set()
                await asyncio.Event().wait()  # A long job, still writing until stopped.
        except SyncWriterLockLostError:
            current = asyncio.current_task()
            assert current is not None
            outcome.append(("lock_lost", current.cancelling()))
            raise

    writer = asyncio.create_task(cli_writer())
    await _wait_event(entered, "the writer opened its running row")
    await _kill_writer_lock_session(db)
    fence_waits: list[tuple[float, bool]] = []

    async def fence_sleep(seconds: float) -> None:
        # The key was free when the fence began: the writer's session is gone, the writer
        # itself still running. Its lock checks go on from here.
        fence_waits.append((seconds, writer.done()))
        gate.set()
        await asyncio.sleep(seconds)
        fence_waits.append((seconds, writer.done()))

    try:
        swept = await _fence_scheduler(fence_sleep, now)._sweep_orphaned_runs(tuple(opened))
    finally:
        gate.set()
        await asyncio.wait((writer,), timeout=_WAIT_S)

    assert swept
    fence_s = _WRITER_FENCE.total_seconds()
    assert fence_waits == [(fence_s, False), (fence_s, True)], (
        "the sweep wrote while the writer still ran"
    )
    assert isinstance(writer.exception(), SyncWriterLockLostError)
    assert outcome == [("lock_lost", 0)], "the supervisor's cancellation was not withdrawn"
    (run_id,) = opened
    row = await _sync_run(db, run_id)
    assert (row.status, row.error, row.finished_at) == ("failed", _INTERRUPTED, now)
    _assert_lock_lost_logged(captured_logs)
    events = [record.get("event") for record in captured_logs()]
    assert events.index("scheduler.sync_run_recovery_fence_wait") < events.index(
        "sync_writer.lock_lost"
    )
    assert events.index("sync_writer.lock_lost") < events.index("scheduler.sync_runs_recovered")
    assert _logged(captured_logs, "sync_writer.fence_stop_timeout") == []
    assert await _writer_lock_holders(db) == []
    assert all(task.done() for task in _supervisors())


async def test_sweep_defers_when_a_writer_starts_during_the_fence(
    db: Sessions, monkeypatch: pytest.MonkeyPatch, captured_logs: Any
) -> None:
    """The sweep takes the key again after the fence: a writer started meanwhile defers it."""
    _shorten_writer_timing(monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)
    orphan = await _add_sync_run(db, "linkedin", jobs.JOB_LEADS, now - timedelta(hours=1))
    holding = asyncio.Event()
    release = asyncio.Event()

    async def cli_writer() -> None:
        async with sync_writer():
            holding.set()
            await release.wait()

    writers: list[asyncio.Task[None]] = []

    async def fence_sleep(seconds: float) -> None:
        writers.append(asyncio.create_task(cli_writer()))
        await _wait_event(holding, "a writer took the lock during the fence")
        await asyncio.sleep(seconds)

    try:
        swept = await _fence_scheduler(fence_sleep, now)._sweep_orphaned_runs((orphan,))
        assert not swept
        assert await _status(db, orphan) == "running"
        (fenced,) = _logged(captured_logs, "scheduler.sync_run_recovery_fence_wait")
        (deferred,) = _logged(captured_logs, "scheduler.sync_run_recovery_deferred")
        assert (fenced["candidates"], deferred["candidates"]) == (1, 1)
        assert _logged(captured_logs, "scheduler.sync_runs_recovered") == []
    finally:
        release.set()
        await asyncio.wait(writers)
    (writer,) = writers
    writer.result()
    assert _logged(captured_logs, "sync_writer.lock_lost") == []


async def test_writer_unwinding_past_the_stop_timeout_after_a_lost_lock_aborts(
    db: Sessions, monkeypatch: pytest.MonkeyPatch, captured_logs: Any
) -> None:
    """A writer that keeps writing after its lock is lost ends its process (injected abort)."""
    _shorten_writer_timing(monkeypatch)
    loop = asyncio.get_running_loop()
    aborts: list[tuple[int, float]] = []
    entered = asyncio.Event()
    aborted = asyncio.Event()
    release = asyncio.Event()
    cancelled: list[tuple[tuple[object, ...], float]] = []

    def abort(code: int) -> NoReturn:
        aborts.append((code, loop.time()))
        aborted.set()
        raise AbortCalledError(code)

    async def stubborn_writer() -> None:
        async with sync_writer(abort=abort):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                cancelled.append((exc.args, loop.time()))
                await release.wait()  # Still unwinding, and writing, past the stop timeout.
                raise

    writer = asyncio.create_task(stubborn_writer())
    try:
        await _wait_event(entered, "the writer holds its lock")
        await _kill_writer_lock_session(db)
        await _wait_event(aborted, "the writer's fence aborted the process")
        assert not writer.done(), "the abort fired only after the writer had ended"
    finally:
        release.set()
        await asyncio.wait((writer,), timeout=_WAIT_S)
    assert writer.done()
    assert isinstance(writer.exception(), AbortCalledError)
    [(code, aborted_at)] = aborts
    assert code == 1
    [(args, cancelled_at)] = cancelled
    assert args == (jobs.CANCEL_WRITER_LOCK_LOST,)
    assert aborted_at - cancelled_at >= _WRITER_STOP_TIMEOUT.total_seconds() - 0.05
    _assert_lock_lost_logged(captured_logs)
    (stuck,) = _logged(captured_logs, "sync_writer.fence_stop_timeout")
    assert stuck["level"] == "critical"
    assert stuck["timeout_s"] == _WRITER_STOP_TIMEOUT.total_seconds()
    assert await _writer_lock_holders(db) == []
    assert all(task.done() for task in _supervisors())


@pytest.mark.parametrize("lost", [False, True], ids=["lock_held", "lock_lost"])
async def test_outside_cancellation_of_a_writer_propagates_as_cancelled(
    db: Sessions, monkeypatch: pytest.MonkeyPatch, captured_logs: Any, lost: bool
) -> None:
    """SIGTERM of a CLI sync stays a cancellation, even while its lost lock is unwinding it."""
    _shorten_writer_timing(monkeypatch)
    entered = asyncio.Event()
    unwinding = asyncio.Event()
    aborts: list[int] = []

    def abort(code: int) -> NoReturn:
        aborts.append(code)
        raise AbortCalledError(code)

    async def writer_body() -> None:
        async with sync_writer(abort=abort):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if lost:
                    # The supervisor's cancellation: unwind until the outside one arrives.
                    unwinding.set()
                    await asyncio.Event().wait()
                raise

    writer = asyncio.create_task(writer_body())
    await _wait_event(entered, "the writer holds its lock")
    if lost:
        await _kill_writer_lock_session(db)
        await _wait_event(unwinding, "the lost lock cancelled the block")
    else:
        await asyncio.sleep(_WRITER_CHECK_EVERY.total_seconds() * 5)  # Checks pass meanwhile.
    writer.cancel()
    await asyncio.wait((writer,), timeout=_WAIT_S)

    assert writer.cancelled(), f"the writer ended with {writer.exception()!r}"
    assert aborts == []
    assert len(_logged(captured_logs, "sync_writer.lock_lost")) == int(lost)
    assert await _writer_lock_holders(db) == []
    assert all(task.done() for task in _supervisors())


async def test_writer_block_ending_normally_releases_the_lock_and_stops_its_supervisor(
    db: Sessions, monkeypatch: pytest.MonkeyPatch, captured_logs: Any
) -> None:
    """A block that returns ends after passing checks: lock released, supervisor cancelled."""
    _shorten_writer_timing(monkeypatch)
    failure = scheduler_module._WriterFence._failure
    checks: list[str | None] = []

    async def recorded_failure(self: Any) -> str | None:
        result = await failure(self)
        checks.append(result)
        return result

    monkeypatch.setattr(scheduler_module._WriterFence, "_failure", recorded_failure)
    seen: dict[str, Any] = {}

    async def short_sync() -> None:
        async with sync_writer():
            seen["holders"] = await _writer_lock_holders(db)
            seen["supervisors"] = [task for task in _supervisors() if not task.done()]
            await asyncio.sleep(_WRITER_CHECK_EVERY.total_seconds() * 6)
        current = asyncio.current_task()
        assert current is not None
        seen["cancelling"] = current.cancelling()

    await asyncio.wait_for(asyncio.create_task(short_sync()), _WAIT_S)

    assert len(seen["holders"]) == 1
    (supervisor,) = seen["supervisors"]
    assert supervisor.done()
    assert supervisor.cancelled()
    assert checks, "no lock check ran while the block did"
    assert set(checks) == {None}
    assert seen["cancelling"] == 0
    assert await _writer_lock_holders(db) == []
    assert _logged(captured_logs, "sync_writer.lock_lost") == []
