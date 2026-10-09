"""The worker's process lifetime: stop signals, exit codes and the order of its cleanup.

AAP 0.6.2 (``python -m ads_engine.worker.main`` is the ``worker`` container's command, which
``docker compose stop`` ends with SIGTERM) and 0.11.2 (first start). Covered here without a
database, a real signal, a sleep or the real scheduler:

* ``_install_stop_handlers`` and ``_remove_stop_handlers`` on a fake loop;
* ``_run`` with a fake scheduler and a recording ``dispose_engine``. The running loop's
  ``add_signal_handler`` is replaced by a recorder, so the real installation path runs and
  the registered stop callback is invoked as the loop invokes it when a signal arrives:
  graceful stop, a duplicate stop, external cancellation with and without a stop, an invalid
  configuration at process start (stubbed, and from the real loader reading a broken copy of
  ``config/``) that exits before any scheduler exists, an invalid configuration from
  ``startup``, a failure before the scheduler task is awaited, and a scheduler that returns;
* ``main``: invalid settings, ``_run``'s exit code and a fatal error mapped to the process
  exit, with ``configure_logging`` and ``_run`` replaced, so nothing is launched; and the
  invalid-settings line through the real logging pipeline, as one JSON object on stdout.

Every wait on the code under test is bounded by :data:`STEP_TIMEOUT`, so a regression fails
fast instead of hanging. The worker's ``worker_fatal`` ownership cases are in
``test_exception_ownership.py``.
"""

import asyncio
import json
import shutil
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
import structlog
import yaml
from structlog.testing import capture_logs
from structlog.typing import EventDict

import ads_engine
from ads_engine.analysis.thresholds import ConfigError
from ads_engine.logging import get_logger
from ads_engine.settings import Settings, get_settings
from ads_engine.worker import main as worker_main

STEP_TIMEOUT = 5.0
"""Seconds any single wait on the code under test may take before the test fails."""

STARTED = f"scheduler started as {worker_main.SCHEDULER_TASK_NAME}"
INSTALLED = ["add SIGTERM", "add SIGINT"]
REMOVED = ["remove SIGTERM", "remove SIGINT"]
STOPPED: EventDict = {"event": "worker_stopped", "log_level": "info"}

REPO_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
"""The shipped ``config/`` directory, copied and broken by the process-start tests."""

# Rejected by Settings validation; neither may appear in anything the worker writes or logs.
SENTINEL_WEBHOOK_URL = "http://sentinel-workflow.example.invalid/webhook/do-not-echo"
SENTINEL_CUSTOMER_ID = "SENTINEL-CUSTOMER-DO-NOT-ECHO"

# The `errors` of `worker_settings_invalid` for BACKFILL_DAYS=0 and the two sentinels above.
INVALID_ENVIRONMENT_ERRORS = [
    {"variable": "BACKFILL_DAYS", "problem": "Input should be greater than or equal to 1"},
    {
        "variable": "N8N_GOOGLE_ADS_WEBHOOK_URL",
        "problem": "Value error, N8N_GOOGLE_ADS_WEBHOOK_URL must use https:// unless it is a "
        "localhost or 127.0.0.1 URL",
    },
    {
        "variable": "GOOGLE_ADS_CUSTOMER_ID",
        "problem": "Value error, GOOGLE_ADS_CUSTOMER_ID must be 10 digits",
    },
]

Handler = Callable[..., object]


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[EventDict]]:
    """Capture the worker module's structlog events as raw event dicts, in order.

    The module logger is replaced by a fresh one of the same name, so a configuration cached
    by an earlier test cannot hide its events from ``capture_logs``.
    """
    monkeypatch.setattr(worker_main, "_log", get_logger("ads_engine.worker.main"))
    with capture_logs() as entries:
        yield entries


def _names(entries: list[EventDict]) -> list[str]:
    """Return the event names captured so far, in order."""
    return [str(entry["event"]) for entry in entries]


def _stopping(sig: signal.Signals) -> EventDict:
    """The ``worker_stopping`` line of a stop requested by ``sig``."""
    return {"event": "worker_stopping", "log_level": "info", "signal": sig.name}


# ---------------------------------------------------------------------------
# _install_stop_handlers and _remove_stop_handlers on a fake loop
# ---------------------------------------------------------------------------


class FakeLoop:
    """An event loop's two signal methods: recorded, and failing for the signals given."""

    def __init__(
        self,
        *,
        add_errors: dict[signal.Signals, Exception] | None = None,
        remove_errors: dict[signal.Signals, Exception] | None = None,
    ) -> None:
        self.add_errors = add_errors or {}
        self.remove_errors = remove_errors or {}
        self.added: list[tuple[int, Handler, tuple[object, ...]]] = []
        self.removed: list[int] = []

    def add_signal_handler(self, sig: int, callback: Handler, *args: object) -> None:
        """Record the handler, or raise the error configured for ``sig``."""
        error = self.add_errors.get(signal.Signals(sig))
        if error is not None:
            raise error
        self.added.append((sig, callback, args))

    def remove_signal_handler(self, sig: int) -> bool:
        """Record the removal, then raise the error configured for ``sig``, if any."""
        self.removed.append(sig)
        error = self.remove_errors.get(signal.Signals(sig))
        if error is not None:
            raise error
        return True

    def as_loop(self) -> asyncio.AbstractEventLoop:
        """This fake, typed as the loop the helpers expect."""
        return cast(asyncio.AbstractEventLoop, self)


def _handler(received: list[signal.Signals]) -> Callable[[signal.Signals], None]:
    """A stop handler that records the signals it is called with."""

    def handler(sig: signal.Signals) -> None:
        received.append(sig)

    return handler


def test_stop_handlers_are_installed_for_sigterm_then_sigint_with_the_signal_as_argument(
    captured: list[EventDict],
) -> None:
    loop = FakeLoop()
    received: list[signal.Signals] = []
    handler = _handler(received)

    installed = worker_main._install_stop_handlers(loop.as_loop(), handler)

    assert installed == [signal.SIGTERM, signal.SIGINT]
    assert loop.added == [
        (signal.SIGTERM, handler, (signal.SIGTERM,)),
        (signal.SIGINT, handler, (signal.SIGINT,)),
    ]
    assert received == []
    assert captured == []


@pytest.mark.parametrize(
    "error",
    [NotImplementedError("signals are not supported"), RuntimeError("not the main thread")],
    ids=["NotImplementedError", "RuntimeError"],
)
@pytest.mark.parametrize("unavailable", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_signal_the_loop_cannot_handle_is_logged_and_skipped(
    captured: list[EventDict], error: Exception, unavailable: signal.Signals
) -> None:
    loop = FakeLoop(add_errors={unavailable: error})
    other = signal.SIGINT if unavailable is signal.SIGTERM else signal.SIGTERM

    installed = worker_main._install_stop_handlers(loop.as_loop(), _handler([]))

    assert installed == [other]
    assert [added[0] for added in loop.added] == [other]
    assert captured == [
        {
            "event": "worker_signal_handler_unavailable",
            "log_level": "warning",
            "signal": unavailable.name,
            "error": str(error),
        }
    ]


def test_no_signal_installed_when_the_loop_handles_neither(captured: list[EventDict]) -> None:
    loop = FakeLoop(
        add_errors={
            signal.SIGTERM: NotImplementedError("no signal support"),
            signal.SIGINT: RuntimeError("not the main thread"),
        }
    )

    installed = worker_main._install_stop_handlers(loop.as_loop(), _handler([]))

    assert installed == []
    assert loop.added == []
    assert [(entry["signal"], entry["error"]) for entry in captured] == [
        ("SIGTERM", "no signal support"),
        ("SIGINT", "not the main thread"),
    ]
    assert {entry["event"] for entry in captured} == {"worker_signal_handler_unavailable"}


def test_any_other_installation_error_propagates(captured: list[EventDict]) -> None:
    error = ValueError("invalid signal number")
    loop = FakeLoop(add_errors={signal.SIGTERM: error})

    with pytest.raises(ValueError, match="invalid signal number") as raised:
        worker_main._install_stop_handlers(loop.as_loop(), _handler([]))

    assert raised.value is error
    assert loop.added == []
    assert captured == []


@pytest.mark.parametrize(
    "installed",
    [[], [signal.SIGINT], [signal.SIGTERM, signal.SIGINT]],
    ids=["none", "SIGINT-only", "both"],
)
def test_only_the_installed_signals_are_removed(installed: list[signal.Signals]) -> None:
    loop = FakeLoop()

    worker_main._remove_stop_handlers(loop.as_loop(), installed)

    assert loop.removed == installed


def test_a_loop_without_signal_support_does_not_stop_the_removal_of_the_rest() -> None:
    loop = FakeLoop(remove_errors={signal.SIGTERM: NotImplementedError("no signal support")})

    worker_main._remove_stop_handlers(loop.as_loop(), [signal.SIGTERM, signal.SIGINT])

    assert loop.removed == [signal.SIGTERM, signal.SIGINT]


# ---------------------------------------------------------------------------
# _cancelled_from_outside
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_not_cancelled_from_outside_in_a_loop_callback_outside_any_task() -> None:
    loop = asyncio.get_running_loop()
    seen: asyncio.Future[bool] = loop.create_future()

    def probe() -> None:
        seen.set_result(worker_main._cancelled_from_outside())

    loop.call_soon(probe)

    async with asyncio.timeout(STEP_TIMEOUT):
        assert await seen is False


@pytest.mark.anyio
async def test_not_cancelled_from_outside_in_a_task_without_a_cancellation_request() -> None:
    assert worker_main._cancelled_from_outside() is False


@pytest.mark.anyio
async def test_cancelled_from_outside_while_the_current_task_has_a_cancellation_request() -> None:
    seen: list[bool] = []

    async def probe() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        seen.append(worker_main._cancelled_from_outside())
        await asyncio.Event().wait()  # receives the requested cancellation

    task = asyncio.create_task(probe())
    with pytest.raises(asyncio.CancelledError):
        await _bounded(task)

    assert seen == [True]


# ---------------------------------------------------------------------------
# _run with a fake scheduler, recorded signal handlers and a recording dispose_engine
# ---------------------------------------------------------------------------


@dataclass
class Lifetime:
    """What one ``_run`` did, in order, and the switches its fake scheduler obeys.

    Attributes:
        log: The captured worker events, read when the engine is disposed.
        events: The scheduler's, the loop's and the engine's steps, in the order they ran.
        schedulers: The settings each fake scheduler was built with.
        started: Set once the scheduler task runs.
        cancelled: Set once the scheduler has caught its cancellation.
        cleanup_gate: When set, the scheduler waits on it after catching its cancellation,
            as the real one waits for its lane tasks; a second cancellation raises there.
        returns: The scheduler returns at once instead of running until cancelled.
        awaits_on_start: The scheduler awaits the ``on_start`` it was given first.
        cancelling: The scheduler task's ``cancelling()`` when it finished its cleanup.
        scheduler_task: The scheduler task, taken when the first stop handler is installed.
        logged_at_dispose: The worker events already logged at each ``dispose_engine``.
        scheduler_at_dispose: The scheduler task's state at each ``dispose_engine``.
    """

    log: list[EventDict]
    events: list[str] = field(default_factory=list)
    schedulers: list[Settings] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_gate: asyncio.Event | None = None
    returns: bool = False
    awaits_on_start: bool = False
    cancelling: list[int] = field(default_factory=list)
    scheduler_task: asyncio.Task[object] | None = None
    logged_at_dispose: list[list[str]] = field(default_factory=list)
    scheduler_at_dispose: list[str] = field(default_factory=list)

    async def run_scheduler(self, on_start: Callable[[], Awaitable[None]] | None) -> None:
        """The fake ``Scheduler.run_forever``: run until cancelled, unless told otherwise."""
        current = asyncio.current_task()
        assert current is not None
        self.events.append(f"scheduler started as {current.get_name()}")
        self.started.set()
        try:
            if self.awaits_on_start:
                assert on_start is not None
                await on_start()
            if self.returns:
                self.events.append("scheduler returned")
                return
            await asyncio.Event().wait()  # until cancelled
        except asyncio.CancelledError:
            self.events.append("scheduler cancelled")
            self.cancelled.set()
            if self.cleanup_gate is not None:
                try:
                    await self.cleanup_gate.wait()
                except asyncio.CancelledError:
                    self.events.append("scheduler cancelled again")
                    raise
            self.cancelling.append(current.cancelling())
            raise
        finally:
            self.events.append("scheduler finished")

    def take_scheduler_task(self) -> None:
        """Remember the scheduler task ``_run`` created, once."""
        if self.scheduler_task is not None:
            return
        (self.scheduler_task,) = (
            task
            for task in asyncio.all_tasks()
            if task.get_name() == worker_main.SCHEDULER_TASK_NAME
        )

    def record_dispose(self) -> None:
        """The fake ``dispose_engine``: record when it ran and what had happened by then."""
        self.events.append("dispose_engine")
        self.logged_at_dispose.append(_names(self.log))
        task = self.scheduler_task
        if task is None:
            state = "unknown"
        elif task.cancelled():
            state = "cancelled"
        elif task.done():
            state = "done"
        else:
            state = "pending"
        self.scheduler_at_dispose.append(state)


@pytest.fixture
def lifetime(monkeypatch: pytest.MonkeyPatch, captured: list[EventDict]) -> Lifetime:
    """Replace the worker's ``Scheduler`` and ``dispose_engine`` with recording fakes."""
    state = Lifetime(log=captured)

    class FakeScheduler:
        def __init__(self, settings: Settings) -> None:
            state.schedulers.append(settings)

        async def run_forever(self, on_start: Callable[[], Awaitable[None]] | None = None) -> None:
            await state.run_scheduler(on_start)

    async def recording_dispose_engine() -> None:
        state.record_dispose()

    monkeypatch.setattr(worker_main, "Scheduler", FakeScheduler)
    monkeypatch.setattr(worker_main, "dispose_engine", recording_dispose_engine)
    return state


class LoopSignals:
    """The running loop's signal methods, recorded instead of touching the process's signals."""

    def __init__(self, lifetime: Lifetime, add_error: Exception | None) -> None:
        self.lifetime = lifetime
        self.add_error = add_error
        self.added: list[tuple[signal.Signals, Handler, tuple[object, ...]]] = []
        self.handlers: dict[signal.Signals, tuple[Handler, tuple[object, ...]]] = {}
        self.removed: list[signal.Signals] = []

    def add_signal_handler(self, sig: int, callback: Handler, *args: object) -> None:
        """Record the handler as the loop registers it, or raise the configured error."""
        self.lifetime.take_scheduler_task()
        if self.add_error is not None:
            raise self.add_error
        stop = signal.Signals(sig)
        self.added.append((stop, callback, args))
        self.handlers[stop] = (callback, args)
        self.lifetime.events.append(f"add {stop.name}")

    def remove_signal_handler(self, sig: int) -> bool:
        """Forget the handler; ``True`` when one was registered, as the loop returns."""
        stop = signal.Signals(sig)
        self.removed.append(stop)
        self.lifetime.events.append(f"remove {stop.name}")
        return self.handlers.pop(stop, None) is not None

    def deliver(self, sig: signal.Signals) -> None:
        """Invoke the callback registered for ``sig`` as the loop does when ``sig`` arrives."""
        callback, args = self.handlers[sig]
        callback(*args)


def _record_loop_signals(
    monkeypatch: pytest.MonkeyPatch, lifetime: Lifetime, *, add_error: Exception | None = None
) -> LoopSignals:
    """Replace the running loop's ``add_signal_handler`` and ``remove_signal_handler``."""
    loop = asyncio.get_running_loop()
    signals = LoopSignals(lifetime, add_error)
    monkeypatch.setattr(loop, "add_signal_handler", signals.add_signal_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", signals.remove_signal_handler)
    return signals


async def _bounded[T](awaitable: Awaitable[T]) -> T:
    """Await ``awaitable``, failing with ``TimeoutError`` after :data:`STEP_TIMEOUT`."""
    async with asyncio.timeout(STEP_TIMEOUT):
        return await awaitable


@asynccontextmanager
async def _running(settings: Settings) -> AsyncIterator[asyncio.Task[int]]:
    """Run ``_run(settings)`` as a task; on exit, cancel and reap it if a test left it running."""
    run = asyncio.create_task(worker_main._run(settings), name="test-worker-run")
    try:
        yield run
    finally:
        if not run.done():
            run.cancel()
            await asyncio.wait((run,), timeout=STEP_TIMEOUT)


SIGNALS = pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])


@pytest.mark.anyio
@SIGNALS
async def test_a_stop_signal_cancels_the_scheduler_then_cleans_up_and_exits_ok(
    monkeypatch: pytest.MonkeyPatch,
    lifetime: Lifetime,
    captured: list[EventDict],
    sig: signal.Signals,
) -> None:
    signals = _record_loop_signals(monkeypatch, lifetime)
    settings = Settings()

    async with _running(settings) as run:
        await _bounded(lifetime.started.wait())
        signals.deliver(sig)
        exit_code = await _bounded(run)

    assert exit_code == worker_main.EXIT_OK
    assert len(lifetime.schedulers) == 1
    assert lifetime.schedulers[0] is settings
    assert [(stop, args) for stop, _, args in signals.added] == [
        (signal.SIGTERM, (signal.SIGTERM,)),
        (signal.SIGINT, (signal.SIGINT,)),
    ]
    assert len({callback for _, callback, _ in signals.added}) == 1
    # The scheduler is cancelled and joined, the handlers are removed, the engine is
    # disposed, and only then is worker_stopped logged.
    assert lifetime.events == [
        *INSTALLED,
        STARTED,
        "scheduler cancelled",
        "scheduler finished",
        *REMOVED,
        "dispose_engine",
    ]
    assert lifetime.cancelling == [1]
    assert lifetime.logged_at_dispose == [["worker_stopping"]]
    assert lifetime.scheduler_at_dispose == ["cancelled"]
    assert signals.handlers == {}
    assert captured == [_stopping(sig), STOPPED]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (signal.SIGTERM, signal.SIGTERM),
        (signal.SIGTERM, signal.SIGINT),
        (signal.SIGINT, signal.SIGTERM),
    ],
    ids=["SIGTERM-SIGTERM", "SIGTERM-SIGINT", "SIGINT-SIGTERM"],
)
async def test_a_second_stop_signal_while_stopping_only_logs_and_cancels_nothing(
    monkeypatch: pytest.MonkeyPatch,
    lifetime: Lifetime,
    captured: list[EventDict],
    first: signal.Signals,
    second: signal.Signals,
) -> None:
    signals = _record_loop_signals(monkeypatch, lifetime)
    lifetime.cleanup_gate = asyncio.Event()

    async with _running(Settings()) as run:
        await _bounded(lifetime.started.wait())
        signals.deliver(first)
        await _bounded(lifetime.cancelled.wait())
        # The scheduler is still handling the first cancellation: a second one would be
        # raised at its cleanup gate.
        signals.deliver(second)
        lifetime.cleanup_gate.set()
        exit_code = await _bounded(run)

    assert exit_code == worker_main.EXIT_OK
    assert lifetime.events == [
        *INSTALLED,
        STARTED,
        "scheduler cancelled",
        "scheduler finished",
        *REMOVED,
        "dispose_engine",
    ]
    assert lifetime.cancelling == [1]
    assert captured == [
        _stopping(first),
        {"event": "worker_stop_already_requested", "log_level": "info", "signal": second.name},
        STOPPED,
    ]


@pytest.mark.anyio
async def test_an_external_cancellation_without_a_signal_is_raised_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, lifetime: Lifetime, captured: list[EventDict]
) -> None:
    _record_loop_signals(monkeypatch, lifetime)

    async with _running(Settings()) as run:
        await _bounded(lifetime.started.wait())
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _bounded(run)

    assert run.cancelled()
    assert lifetime.events == [
        *INSTALLED,
        STARTED,
        "scheduler cancelled",
        "scheduler finished",
        *REMOVED,
        "dispose_engine",
    ]
    assert lifetime.logged_at_dispose == [[]]
    assert captured == [STOPPED]


@pytest.mark.anyio
@SIGNALS
async def test_an_external_cancellation_after_a_stop_signal_is_raised_not_a_graceful_exit(
    monkeypatch: pytest.MonkeyPatch,
    lifetime: Lifetime,
    captured: list[EventDict],
    sig: signal.Signals,
) -> None:
    signals = _record_loop_signals(monkeypatch, lifetime)
    lifetime.cleanup_gate = asyncio.Event()

    async with _running(Settings()) as run:
        await _bounded(lifetime.started.wait())
        signals.deliver(sig)
        await _bounded(lifetime.cancelled.wait())
        run.cancel()  # _run's own task now has a cancellation request of its own
        with pytest.raises(asyncio.CancelledError):
            await _bounded(run)

    assert run.cancelled()
    assert lifetime.events == [
        *INSTALLED,
        STARTED,
        "scheduler cancelled",
        "scheduler cancelled again",
        "scheduler finished",
        *REMOVED,
        "dispose_engine",
    ]
    assert captured == [_stopping(sig), STOPPED]


def _scheduler_tasks() -> list[asyncio.Task[object]]:
    """The running loop's tasks named :data:`~ads_engine.worker.main.SCHEDULER_TASK_NAME`."""
    return [
        task for task in asyncio.all_tasks() if task.get_name() == worker_main.SCHEDULER_TASK_NAME
    ]


def _config_without_the_default_preset(base: Path) -> Path:
    """Copy the shipped ``config/*.yaml`` into ``base/config``, dropping the default preset.

    ``window.default_days`` is removed from ``window.presets`` in ``thresholds.yaml``, which
    the real loader rejects; every other file is copied unchanged.

    Returns:
        The broken copy's directory, ``base/config``.
    """
    directory = base / "config"
    directory.mkdir()
    sources = sorted(REPO_CONFIG_DIR.glob("*.yaml"))
    assert [source.name for source in sources] == [
        "content_assets.yaml",
        "hubspot_mapping.yaml",
        "thresholds.yaml",
    ]
    for source in sources:
        shutil.copyfile(source, directory / source.name)
    thresholds_file = directory / "thresholds.yaml"
    tree: dict[str, Any] = yaml.safe_load(thresholds_file.read_text(encoding="utf-8"))
    window: dict[str, Any] = tree["window"]
    default_days = window["default_days"]
    assert default_days in window["presets"]
    window["presets"] = [days for days in window["presets"] if days != default_days]
    thresholds_file.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")
    return directory


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["window.presets", None], ids=["key", "whole-file"])
async def test_an_invalid_configuration_at_process_start_exits_1_before_any_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    lifetime: Lifetime,
    captured: list[EventDict],
    key: str | None,
) -> None:
    signals = _record_loop_signals(monkeypatch, lifetime)
    error = ConfigError("thresholds.yaml", key, "must contain window.default_days (28)")
    loads: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class InvalidConfiguration:
        """The worker's ``Thresholds``, whose load fails as an invalid ``config/`` does."""

        @staticmethod
        def load(*args: object, **kwargs: object) -> NoReturn:
            loads.append((args, kwargs))
            raise error

    monkeypatch.setattr(worker_main, "Thresholds", InvalidConfiguration)

    async with _running(Settings()) as run:
        exit_code = await _bounded(run)
        assert _scheduler_tasks() == []

    assert exit_code == worker_main.EXIT_FAILURE
    # Loaded once, with no path: the shared resolver (./config, else ../config) picks it.
    assert loads == [((), {})]
    # No scheduler was built or run, so no lock was tried; no stop handler was installed.
    assert lifetime.schedulers == []
    assert not lifetime.started.is_set()
    assert signals.added == []
    assert signals.removed == []
    assert lifetime.events == ["dispose_engine"]
    assert lifetime.logged_at_dispose == [["worker_config_invalid"]]
    assert lifetime.scheduler_at_dispose == ["unknown"]
    assert captured == [
        {
            "event": "worker_config_invalid",
            "log_level": "error",
            "config_file": "thresholds.yaml",
            "config_key": key,
            "detail": "must contain window.default_days (28)",
            "error": str(error),
        },
        STOPPED,
    ]


@pytest.mark.anyio
async def test_a_config_dir_lacking_the_default_preset_stops_the_worker_before_the_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    lifetime: Lifetime,
    captured: list[EventDict],
    tmp_path: Path,
) -> None:
    signals = _record_loop_signals(monkeypatch, lifetime)
    directory = _config_without_the_default_preset(tmp_path)
    monkeypatch.chdir(tmp_path)  # ./config is now the broken copy, as /app/config in the image
    shipped = yaml.safe_load((REPO_CONFIG_DIR / "thresholds.yaml").read_text(encoding="utf-8"))
    default_days = shipped["window"]["default_days"]
    remaining = tuple(days for days in shipped["window"]["presets"] if days != default_days)
    detail = f"must contain window.default_days ({default_days}), found {remaining!r}"

    async with _running(Settings()) as run:
        exit_code = await _bounded(run)
        assert _scheduler_tasks() == []

    assert (directory / "thresholds.yaml").is_file()
    assert exit_code == worker_main.EXIT_FAILURE
    assert lifetime.schedulers == []
    assert not lifetime.started.is_set()
    assert signals.added == []
    assert lifetime.events == ["dispose_engine"]
    assert captured == [
        {
            "event": "worker_config_invalid",
            "log_level": "error",
            "config_file": "thresholds.yaml",
            "config_key": "window.presets",
            "detail": detail,
            "error": f"thresholds.yaml: window.presets: {detail}",
        },
        STOPPED,
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["window.presets", None], ids=["key", "whole-file"])
async def test_an_invalid_configuration_from_startup_exits_1_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    lifetime: Lifetime,
    captured: list[EventDict],
    key: str | None,
) -> None:
    _record_loop_signals(monkeypatch, lifetime)
    lifetime.awaits_on_start = True
    error = ConfigError("thresholds.yaml", key, "must list window.default_days")
    received: list[Settings | None] = []

    async def failing_startup(settings: Settings | None = None) -> None:
        received.append(settings)
        raise error

    monkeypatch.setattr(worker_main, "startup", failing_startup)
    settings = Settings()

    async with _running(settings) as run:
        exit_code = await _bounded(run)

    assert exit_code == worker_main.EXIT_FAILURE
    assert len(received) == 1
    assert received[0] is settings
    assert lifetime.events == [
        *INSTALLED,
        STARTED,
        "scheduler finished",
        *REMOVED,
        "dispose_engine",
    ]
    assert lifetime.logged_at_dispose == [["worker_config_invalid"]]
    assert captured == [
        {
            "event": "worker_config_invalid",
            "log_level": "error",
            "config_file": "thresholds.yaml",
            "config_key": key,
            "detail": "must list window.default_days",
            "error": str(error),
        },
        STOPPED,
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("eager", [False, True], ids=["scheduler-not-started", "scheduler-running"])
async def test_a_failure_before_the_scheduler_is_awaited_cancels_and_joins_it_before_disposal(
    monkeypatch: pytest.MonkeyPatch,
    lifetime: Lifetime,
    captured: list[EventDict],
    eager: bool,
) -> None:
    error = ValueError("invalid signal number")
    signals = _record_loop_signals(monkeypatch, lifetime, add_error=error)
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if eager:
        # The scheduler task then runs up to its first await inside create_task(), so it is
        # already running when the stop handlers fail to install.
        loop.set_task_factory(asyncio.eager_task_factory)
    try:
        with pytest.raises(ValueError, match="invalid signal number") as raised:
            await _bounded(worker_main._run(Settings()))
    finally:
        loop.set_task_factory(previous_factory)

    assert raised.value is error
    # A task cancelled before it started never runs its body; a running one is cancelled
    # and joined. Either way it is finished before the engine is disposed.
    scheduler_steps = [STARTED, "scheduler cancelled", "scheduler finished"] if eager else []
    assert lifetime.events == [*scheduler_steps, "dispose_engine"]
    assert lifetime.scheduler_at_dispose == ["cancelled"]
    assert lifetime.cancelling == ([1] if eager else [])
    assert signals.removed == []
    assert captured == [
        {"event": "worker_fatal", "log_level": "error", "exc_info": True},
        STOPPED,
    ]


@pytest.mark.anyio
async def test_a_scheduler_that_returns_ends_the_worker_ok_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, lifetime: Lifetime, captured: list[EventDict]
) -> None:
    _record_loop_signals(monkeypatch, lifetime)
    lifetime.returns = True

    async with _running(Settings()) as run:
        exit_code = await _bounded(run)

    assert exit_code == worker_main.EXIT_OK
    assert lifetime.events == [
        *INSTALLED,
        STARTED,
        "scheduler returned",
        "scheduler finished",
        *REMOVED,
        "dispose_engine",
    ]
    assert lifetime.scheduler_at_dispose == ["done"]
    assert captured == [STOPPED]


# ---------------------------------------------------------------------------
# main: settings, exit codes and fatal errors, with configure_logging and _run replaced
# ---------------------------------------------------------------------------


@dataclass
class EntryCalls:
    """What ``main`` handed to the collaborators these tests replace.

    Attributes:
        log: The captured worker events.
        configure_logging: The positional and keyword arguments of each call.
        logged_at_configure: The worker events already logged at each of those calls.
        run: The settings each ``_run`` call received.
        logged_at_run: The worker events already logged when each ``_run`` started.
    """

    log: list[EventDict]
    configure_logging: list[tuple[tuple[object, ...], dict[str, object]]] = field(
        default_factory=list
    )
    logged_at_configure: list[list[str]] = field(default_factory=list)
    run: list[Settings] = field(default_factory=list)
    logged_at_run: list[list[str]] = field(default_factory=list)


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch, captured: list[EventDict]) -> EntryCalls:
    """Replace ``configure_logging`` with a recorder, leaving the global logging untouched."""
    calls = EntryCalls(log=captured)

    def recording_configure_logging(*args: object, **kwargs: object) -> None:
        calls.configure_logging.append((args, kwargs))
        calls.logged_at_configure.append(_names(calls.log))

    monkeypatch.setattr(worker_main, "configure_logging", recording_configure_logging)
    return calls


def _replace_run(
    monkeypatch: pytest.MonkeyPatch, calls: EntryCalls, outcome: int | Exception
) -> None:
    """Make ``_run`` record its call and return ``outcome``, or raise it."""

    async def fake_run(settings: Settings) -> int:
        calls.run.append(settings)
        calls.logged_at_run.append(_names(calls.log))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(worker_main, "_run", fake_run)


def _starting(settings: Settings) -> EventDict:
    """The ``worker_starting`` line ``main`` logs for ``settings``."""
    return {
        "event": "worker_starting",
        "log_level": "info",
        "version": ads_engine.__version__,
        "data_mode": settings.data_mode,
        "display_timezone": settings.display_timezone,
        "backfill_days": settings.backfill_days,
    }


def test_an_invalid_environment_exits_1_naming_each_variable_but_never_its_value(
    monkeypatch: pytest.MonkeyPatch,
    set_env: Callable[..., None],
    entry: EntryCalls,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _replace_run(monkeypatch, entry, worker_main.EXIT_OK)
    set_env(
        BACKFILL_DAYS="0",
        N8N_GOOGLE_ADS_WEBHOOK_URL=SENTINEL_WEBHOOK_URL,
        GOOGLE_ADS_CUSTOMER_ID=SENTINEL_CUSTOMER_ID,
    )

    with pytest.raises(SystemExit) as exited:
        worker_main.main()

    assert exited.value.code == worker_main.EXIT_FAILURE
    # Raised `from None`: the ValidationError, which may quote the inputs, is not chained.
    assert exited.value.__cause__ is None
    assert exited.value.__suppress_context__ is True
    # Logging is configured on stdout (no stream given) from the documented defaults, so the
    # environment that failed validation is not read again, before the one line is logged.
    ((args, kwargs),) = entry.configure_logging
    assert (args, list(kwargs)) == ((), ["settings"])
    defaults = kwargs["settings"]
    assert isinstance(defaults, Settings)
    assert defaults.model_fields_set == set()
    assert defaults == Settings.model_construct()
    assert entry.logged_at_configure == [[]]
    assert entry.log == [
        {
            "event": "worker_settings_invalid",
            "log_level": "error",
            "error_count": 3,
            "errors": INVALID_ENVIRONMENT_ERRORS,
        }
    ]
    assert entry.run == []
    out, err = capsys.readouterr()
    assert (out, err) == ("", "")
    for leaked in (SENTINEL_WEBHOOK_URL, "sentinel-workflow", SENTINEL_CUSTOMER_ID, "SENTINEL"):
        assert leaked not in repr(entry.log)
        assert leaked not in repr(defaults)


def test_the_invalid_environment_line_is_one_redacted_json_object_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    set_env: Callable[..., None],
    captured_logs: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Through the real logging pipeline, stdout holds the one JSON line and stderr nothing.

    Every problem text survives the redaction unchanged, and no supplied value is written.
    ``captured_logs`` is requested only to restore the global logging state afterwards.
    """
    real_configure_logging = worker_main.configure_logging

    def configure_without_cache(**kwargs: Any) -> None:
        real_configure_logging(**kwargs)
        # A logger first used under this configuration must not keep it after the test.
        structlog.configure(cache_logger_on_first_use=False)

    async def refuse_to_run(settings: Settings) -> int:
        raise AssertionError("the worker ran with an invalid environment")

    monkeypatch.setattr(worker_main, "configure_logging", configure_without_cache)
    monkeypatch.setattr(worker_main, "_log", get_logger("ads_engine.worker.main"))
    monkeypatch.setattr(worker_main, "_run", refuse_to_run)
    set_env(
        BACKFILL_DAYS="0",
        N8N_GOOGLE_ADS_WEBHOOK_URL=SENTINEL_WEBHOOK_URL,
        GOOGLE_ADS_CUSTOMER_ID=SENTINEL_CUSTOMER_ID,
    )

    with pytest.raises(SystemExit) as exited:
        worker_main.main()

    assert exited.value.code == worker_main.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert err == ""
    (record,) = [json.loads(line) for line in out.splitlines()]
    assert record == {
        "event": "worker_settings_invalid",
        "level": "error",
        "logger": "ads_engine.worker.main",
        "timestamp": record["timestamp"],
        "error_count": 3,
        "errors": INVALID_ENVIRONMENT_ERRORS,
    }
    assert record["timestamp"].endswith("Z")
    for leaked in (SENTINEL_WEBHOOK_URL, "sentinel-workflow", SENTINEL_CUSTOMER_ID, "SENTINEL"):
        assert leaked not in out


def test_a_graceful_run_returns_normally_after_logging_worker_starting(
    monkeypatch: pytest.MonkeyPatch, set_env: Callable[..., None], entry: EntryCalls
) -> None:
    _replace_run(monkeypatch, entry, worker_main.EXIT_OK)
    set_env(DATA_MODE="simulated", DISPLAY_TIMEZONE="Europe/Berlin", BACKFILL_DAYS="30")
    settings = get_settings()

    worker_main.main()  # returns: no SystemExit after a graceful shutdown

    assert (settings.data_mode, settings.display_timezone, settings.backfill_days) == (
        "simulated",
        "Europe/Berlin",
        30,
    )
    assert entry.configure_logging == [((), {"settings": settings})]
    assert entry.configure_logging[0][1]["settings"] is settings
    assert entry.logged_at_configure == [[]]
    assert len(entry.run) == 1
    assert entry.run[0] is settings
    assert entry.logged_at_run == [["worker_starting"]]
    assert entry.log == [_starting(settings)]


def test_a_run_ending_in_failure_exits_with_its_code(
    monkeypatch: pytest.MonkeyPatch, entry: EntryCalls
) -> None:
    _replace_run(monkeypatch, entry, worker_main.EXIT_FAILURE)

    with pytest.raises(SystemExit) as exited:
        worker_main.main()

    assert exited.value.code == worker_main.EXIT_FAILURE
    settings = get_settings()
    assert len(entry.run) == 1
    assert entry.run[0] is settings
    assert entry.log == [_starting(settings)]


def test_a_fatal_error_exits_1_chained_to_it_and_logs_worker_exited(
    monkeypatch: pytest.MonkeyPatch, entry: EntryCalls
) -> None:
    error = RuntimeError("scheduler bug")
    _replace_run(monkeypatch, entry, error)

    with pytest.raises(SystemExit) as exited:
        worker_main.main()

    assert exited.value.code == worker_main.EXIT_FAILURE
    assert exited.value.__cause__ is error
    settings = get_settings()
    assert len(entry.run) == 1
    assert entry.log == [
        _starting(settings),
        {
            "event": "worker_exited",
            "log_level": "error",
            "error": "RuntimeError",
            "exit_code": worker_main.EXIT_FAILURE,
        },
    ]
