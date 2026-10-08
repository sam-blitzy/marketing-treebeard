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
* **Lock lost during startup or the requeue**: ``on_start`` is cancelled, then run again
  after the next acquisition, before any lane pass; a blocked ``scheduler_lock_lost`` requeue
  is cancelled the same way and no lane is dispatched until the lock is held again.
* **Fresh check before dispatch**: a lock session that dies as ``on_start`` returns is found
  by the check that precedes the first dispatch, so no lane runs without the lock.
* **Bounded unwind**: after a lost lock, a task that keeps unwinding past
  ``LOCK_LOST_STOP_TIMEOUT`` aborts the process (the injected abort, exit code 1); SIGTERM and
  a defect wait for every task however long and never abort.
* **Standby and takeover grace**: a second scheduler waits as standby and, once it acquires
  the lock, waits ``LOCK_TAKEOVER_GRACE`` before its ``on_start``.
* **Defects**: a defect in one lane cancels the others and propagates out of ``run_forever``,
  releasing the lock.
* **Check timeout**: a lock check that does not answer within ``LOCK_CHECK_TIMEOUT`` counts as
  a lost lock.
* **Cancellation**: SIGTERM while ``on_start`` runs cancels and awaits it before propagating.
* **Per-lane claims**: the google lane claims only Google's ``sync_request`` rows.

How. Real PostgreSQL 18 through ``conftest.db`` (emptied before each test). The schedulers of
a test share a :class:`VirtualTime`: their monotonic clock (and, when a test asks, their wall
clock) moves only when an injected sleep completes, to that sleep's start plus its duration,
and each sleep really waits one millisecond per virtual second, so 30 s wakes take
milliseconds and concurrent sleeps finish in virtual-time order. Source work that would reach
the simulator is replaced: ``SourceModeController.ensure_dataset`` builds nothing (or blocks
for chosen sources), ``in_fallback`` is false, and ``jobs.run_source_jobs`` is a recording
fake that can block until released or cancelled. A lock session dies the way a crashed or
disconnected one would: ``pg_terminate_backend`` of the backend that holds the scheduler lock
in ``pg_locks``. The injected abort records its code and raises :class:`AbortCalledError`.
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Final, NoReturn

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from ads_engine.db import repositories
from ads_engine.db.models.analysis import AnalysisRequest, AnalysisRun
from ads_engine.db.models.ops import SyncRequest
from ads_engine.settings import Source, get_settings
from ads_engine.sync import jobs
from ads_engine.sync import scheduler as scheduler_module
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.mode import BuildOutcome, SourceModeController
from ads_engine.sync.scheduler import (
    COALESCE_WINDOW,
    LOCK_CHECK_INTERVAL,
    LOCK_CHECK_TIMEOUT,
    LOCK_LOST_STOP_TIMEOUT,
    LOCK_TAKEOVER_GRACE,
    SCHEDULER_LOCK_KEY,
    STANDBY_RETRY,
    WAKE_INTERVAL,
    Scheduler,
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

_CHECK_LATENCY_S: Final = 4.0
"""The virtual time each lock check takes in the slow-check test."""

_LOCK_LOST: Final = "scheduler_lock_lost"


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
    records ``job_cancelled:<source>`` and re-raises the cancellation; a stubborn source keeps
    unwinding until :attr:`release` is set, and only then re-raises. Any other call returns an
    empty report at once.
    """

    def __init__(self, recorder: Recorder) -> None:
        self.calls: list[tuple[Source, str]] = []
        self.blocked: set[Source] = set()
        self.stubborn: set[Source] = set()
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self._recorder = recorder

    async def __call__(
        self, source: Source, *, trigger: jobs.Trigger, **options: object
    ) -> jobs.SourceSyncReport:
        """Record the call; block until released or cancelled when ``source`` is blocked."""
        del options  # The fake runs no job: the job names, controller and instant are unused.
        self.calls.append((source, trigger))
        self._recorder.add(f"job:{source}")
        if source in self.blocked:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self._recorder.add(f"job_cancelled:{source}")
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
        self.recorder.add(f"abort:{code}")
        raise AbortCalledError(code)

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


async def _cancel(task: asyncio.Task[None]) -> None:
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


async def _lock_lost_requests(db: Sessions) -> int:
    """Count the ``scheduler_lock_lost`` analysis requests."""
    async with db() as session:
        found = await session.scalars(
            select(AnalysisRequest.id).where(AnalysisRequest.reason == _LOCK_LOST)
        )
        return len(list(found))


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
        async with db() as session:
            row = await session.get(AnalysisRequest, request_id)
            assert row is not None
            return row.run_id is not None

    await _eventually(served, "a default run served the pending analysis request")

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
    """Killing the lock session cancels the blocked lane first, then reacquires and requeues."""
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
    assert first_holder == killed
    assert not task.done()
    events = recorder.events
    assert recorder.first("job_cancelled:google") < recorder.nth("A:acquired", 2)
    assert recorder.first("job:google") < recorder.first("job_cancelled:google")
    # The second acquisition waits out the takeover grace before any lane runs again.
    second = recorder.nth("A:acquired", 2)
    assert events[second + 1] == f"A:sleep:{LOCK_TAKEOVER_GRACE.total_seconds():g}"
    assert await _picked(db, google_request)
    assert source_jobs.calls == [("google", "manual")]
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

    monkeypatch.setattr(Scheduler, "_lock_alive", staticmethod(slow_lock_alive))
    task = harness.start(harness.scheduler("A"))
    passes = harness.passes["A"]

    async def four_wakes() -> bool:
        return passes["budget"] >= 4

    await _eventually(four_wakes, "four wakes dispatched the budget lane")
    starts = harness.lane_starts["A"]["budget"][:4]
    assert _gaps(starts) == [WAKE_INTERVAL.total_seconds()] * 3
    # The grace, the fence check, then the first wake's own check precede the first dispatch.
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


async def test_unwind_past_the_stop_timeout_after_a_lost_lock_aborts(
    harness: Harness,
    db: Sessions,
    recorder: Recorder,
    source_jobs: SourceJobs,
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: Any,
) -> None:
    """A cancelled task still unwinding after ``LOCK_LOST_STOP_TIMEOUT`` aborts with code 1."""
    monkeypatch.setattr(
        scheduler_module, "LOCK_LOST_STOP_TIMEOUT", timedelta(seconds=_STOP_TIMEOUT_S)
    )
    source_jobs.blocked.add("google")
    source_jobs.stubborn.add("google")
    await _add_sync_requests(db, ("google",), harness.now)
    task = harness.start(harness.scheduler("A"))
    await _wait_event(source_jobs.entered, "the google lane's job started")

    await _kill_lock_session(db)

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
    source_jobs.release.set()


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
