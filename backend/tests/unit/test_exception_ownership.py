"""One failure, one traceback: callers report an already-logged failure without re-logging it.

AAP 0.11.1 ("Error handling", "Logging points"): the analysis runner logs a failed run as
``analysis_run_failed`` with its traceback and marks the exception with
:func:`ads_engine.logging.mark_logged`. Every caller that receives that exception reads
:func:`ads_engine.logging.logged_as` and logs its own outcome event as a warning naming the
owner (``logged_as``) without a second traceback; an exception nobody logged keeps the
caller's original event, level and traceback. Covered here, without a database:

* ``Scheduler._step`` (``scheduler.step_failed``): the step is still recorded as failed;
* the worker's startup analysis (``startup_analysis_failed``): the request is still requeued;
* the worker's ``_run`` (``worker_fatal``): the exception is still raised;
* the API's catch-all (``api.unhandled_error``) and ``ConfigError`` (``api.config_error``)
  handlers: the response is unchanged, and an exception the catch-all logs is marked so
  uvicorn's own record of it is dropped.

The CLI's ``cli_failed`` is covered in ``test_cli.py``, the runner's marker in
``integration/test_analysis_runner.py``, the uvicorn filter in ``test_logging.py``.
"""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import OperationalError
from starlette.requests import Request
from structlog.testing import capture_logs
from structlog.typing import EventDict

from ads_engine.analysis.thresholds import ConfigError, Thresholds
from ads_engine.api import app as api_app
from ads_engine.logging import get_logger, logged_as, mark_logged
from ads_engine.settings import Settings
from ads_engine.sync import scheduler as scheduler_module
from ads_engine.worker import main as worker_main

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
OWNER = "analysis_run_failed"


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[EventDict]]:
    """Capture every structlog event of the modules under test as its raw event dict.

    Each module's logger is replaced by a fresh one of the same name for the test: a module
    logger first used while ``create_app()``'s configuration cached loggers keeps that
    configuration's processors, and ``capture_logs`` would never see its events.
    """
    for module in (api_app, scheduler_module, worker_main):
        monkeypatch.setattr(module, "_log", get_logger(module.__name__))
    with capture_logs() as entries:
        yield entries


def _events(entries: list[EventDict], name: str) -> list[EventDict]:
    """Return the captured events called ``name``, in order."""
    return [entry for entry in entries if entry.get("event") == name]


def _owned_config_error() -> ConfigError:
    """A contradicting override, as the runner raises and logs it from ``check_overrides``."""
    error = ConfigError("content_assets.yaml", "assets[0].creative_overrides[0]", "contradicts")
    mark_logged(error, OWNER)
    return error


def _db_outage() -> OperationalError:
    """A database error no owner logged."""
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


# ---------------------------------------------------------------------------
# Scheduler._step
# ---------------------------------------------------------------------------


async def test_a_step_failure_already_logged_is_a_warning_without_a_traceback(
    captured: list[EventDict],
) -> None:
    scheduler = scheduler_module.Scheduler(Settings(), clock=lambda: NOW)
    tick = scheduler_module._Tick(NOW)
    error = _owned_config_error()

    async with scheduler._step(tick, "analysis"):
        raise error

    assert tick.errors == ["analysis"]
    (event,) = _events(captured, "scheduler.step_failed")
    assert event == {
        "event": "scheduler.step_failed",
        "log_level": "warning",
        "step": "analysis",
        "error": "ConfigError",
        "logged_as": OWNER,
    }


async def test_a_step_failure_no_owner_logged_keeps_its_traceback(
    captured: list[EventDict],
) -> None:
    scheduler = scheduler_module.Scheduler(Settings(), clock=lambda: NOW)
    tick = scheduler_module._Tick(NOW)
    error = _db_outage()

    async with scheduler._step(tick, "purge"):
        raise error

    assert tick.errors == ["purge"]
    (event,) = _events(captured, "scheduler.step_failed")
    assert event == {
        "event": "scheduler.step_failed",
        "log_level": "error",
        "step": "purge",
        "error": "OperationalError",
        "exc_info": error,
    }


# ---------------------------------------------------------------------------
# The worker: startup analysis and _run
# ---------------------------------------------------------------------------


def _startup_with(monkeypatch: pytest.MonkeyPatch, error: Exception) -> list[str]:
    """Make the startup run fail with ``error``; return the list the requeue appends to."""
    requeued: list[str] = []

    @asynccontextmanager
    async def no_session() -> AsyncIterator[None]:
        yield None

    async def pending(_session: object) -> tuple[int, ...]:
        return (41, 42)

    async def window(**_kwargs: Any) -> object:
        return SimpleNamespace(start=NOW.date(), end=NOW.date(), days=1)

    async def failing_run(*_args: Any, **_kwargs: Any) -> object:
        raise error

    async def requeue() -> None:
        requeued.append(worker_main.REASON_STARTUP)

    monkeypatch.setattr(worker_main, "session_scope", no_session)
    monkeypatch.setattr(worker_main, "_pending_request_ids", pending)
    monkeypatch.setattr(worker_main, "default_window", window)
    monkeypatch.setattr(worker_main, "run_analysis", failing_run)
    monkeypatch.setattr(worker_main, "_requeue_startup_analysis", requeue)
    return requeued


async def test_a_startup_run_failure_already_logged_is_requeued_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, captured: list[EventDict]
) -> None:
    requeued = _startup_with(monkeypatch, _owned_config_error())

    await worker_main._startup_analysis(cast(Thresholds, SimpleNamespace()))

    assert requeued == [worker_main.REASON_STARTUP]
    (event,) = _events(captured, "startup_analysis_failed")
    assert event == {
        "event": "startup_analysis_failed",
        "log_level": "warning",
        "error": "ConfigError",
        "requests": 2,
        "logged_as": OWNER,
    }


async def test_a_startup_run_failure_no_owner_logged_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch, captured: list[EventDict]
) -> None:
    requeued = _startup_with(monkeypatch, _db_outage())

    await worker_main._startup_analysis(cast(Thresholds, SimpleNamespace()))

    assert requeued == [worker_main.REASON_STARTUP]
    (event,) = _events(captured, "startup_analysis_failed")
    assert (event["log_level"], event["error"], event["requests"]) == (
        "error",
        "OperationalError",
        2,
    )
    assert event["exc_info"] is True
    assert "logged_as" not in event


def _scheduler_failing_with(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Replace the worker's scheduler with one whose loop fails at once with ``error``."""

    class FailingScheduler:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def run_forever(self, **_kwargs: Any) -> None:
            raise error

    async def no_engine() -> None:
        return None

    monkeypatch.setattr(worker_main, "Scheduler", FailingScheduler)
    monkeypatch.setattr(worker_main, "dispose_engine", no_engine)


async def test_a_fatal_failure_already_logged_is_raised_without_a_second_traceback(
    monkeypatch: pytest.MonkeyPatch, captured: list[EventDict]
) -> None:
    error = RuntimeError("rule exploded")
    mark_logged(error, OWNER)
    _scheduler_failing_with(monkeypatch, error)

    with pytest.raises(RuntimeError) as raised:
        await worker_main._run(Settings())

    assert raised.value is error
    (event,) = _events(captured, "worker_fatal")
    assert event == {
        "event": "worker_fatal",
        "log_level": "warning",
        "error": "RuntimeError",
        "logged_as": OWNER,
    }
    assert _events(captured, "worker_stopped")


async def test_a_fatal_failure_no_owner_logged_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch, captured: list[EventDict]
) -> None:
    error = RuntimeError("scheduler bug")
    _scheduler_failing_with(monkeypatch, error)

    with pytest.raises(RuntimeError):
        await worker_main._run(Settings())

    (event,) = _events(captured, "worker_fatal")
    assert event == {"event": "worker_fatal", "log_level": "error", "exc_info": True}


# ---------------------------------------------------------------------------
# The API's error handlers
# ---------------------------------------------------------------------------


def _request() -> Request:
    """A ``POST /api/v1/runs`` request, as the handlers receive it."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/runs",
        "raw_path": b"/api/v1/runs",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
    }
    return Request(scope)


def _error_code(body: bytes | memoryview) -> object:
    """The ``error.code`` of an error envelope."""
    return json.loads(bytes(body))["error"]["code"]


async def _unexpected(error: RuntimeError) -> tuple[int, object]:
    """Run the catch-all handler as Starlette does: inside the ``except`` that caught it."""
    try:
        raise error
    except RuntimeError as caught:
        response = await api_app._handle_unexpected(_request(), caught)
    return response.status_code, _error_code(response.body)


async def test_an_unhandled_error_already_logged_is_a_warning_without_a_traceback(
    captured: list[EventDict],
) -> None:
    error = RuntimeError("rule exploded")
    mark_logged(error, OWNER)

    assert await _unexpected(error) == (500, "internal_error")

    (event,) = _events(captured, "api.unhandled_error")
    assert event == {
        "event": "api.unhandled_error",
        "log_level": "warning",
        "path": "/api/v1/runs",
        "method": "POST",
        "error_type": "RuntimeError",
        "logged_as": OWNER,
    }
    assert logged_as(error) == OWNER


async def test_an_unhandled_error_is_logged_once_and_marked_for_uvicorn(
    captured: list[EventDict],
) -> None:
    error = RuntimeError("route bug")

    assert await _unexpected(error) == (500, "internal_error")

    (event,) = _events(captured, "api.unhandled_error")
    assert event == {
        "event": "api.unhandled_error",
        "log_level": "error",
        "path": "/api/v1/runs",
        "method": "POST",
        "error_type": "RuntimeError",
        "exc_info": True,
    }
    # Starlette re-raises it after the response; uvicorn's record of it is then dropped.
    assert logged_as(error) == "api.unhandled_error"


async def test_a_config_error_already_logged_is_a_warning(captured: list[EventDict]) -> None:
    error = _owned_config_error()

    response = await api_app._handle_config_error(_request(), error)

    assert (response.status_code, _error_code(response.body)) == (500, "config_error")
    (event,) = _events(captured, "api.config_error")
    assert event == {
        "event": "api.config_error",
        "log_level": "warning",
        "error": str(error),
        "file": "content_assets.yaml",
        "key": "assets[0].creative_overrides[0]",
        "path": "/api/v1/runs",
        "method": "POST",
        "logged_as": OWNER,
    }


async def test_a_config_error_no_owner_logged_stays_an_error(captured: list[EventDict]) -> None:
    error = ConfigError("thresholds.yaml", "window.presets", "must list the default")

    response = await api_app._handle_config_error(_request(), error)

    assert (response.status_code, _error_code(response.body)) == (500, "config_error")
    (event,) = _events(captured, "api.config_error")
    assert event == {
        "event": "api.config_error",
        "log_level": "error",
        "error": str(error),
        "file": "thresholds.yaml",
        "key": "window.presets",
        "path": "/api/v1/runs",
        "method": "POST",
    }
    assert logged_as(error) is None
