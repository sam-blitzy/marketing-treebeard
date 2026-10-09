"""Unit tests for ``ads_engine.api.deps``: ``ApiError``, the request session, clock and guards.

Each test asserts behaviour the module's docstrings document:

* :class:`ApiError`, :func:`not_found`, :func:`conflict` and the shared 401: status, code,
  message and header handling, argument validation, and a ``repr`` that never shows header
  values;
* :func:`get_session`: one session per request, closed on every path, rolled back and
  re-raised on an error thrown into it, and a failing rollback logged by error type only
  while the request's own error still propagates;
* :func:`get_now`, :func:`get_settings_dep`, :func:`get_thresholds` and
  :func:`get_login_limiter`: the UTC clock, the application's own settings, and the
  configuration and login limiter created once per application, also when first requests
  race;
* :func:`require_user`: one 401 for an absent, invalid, expired, orphaned or revoked
  session, the rejection reasons logged without any email, and the verified user;
* :func:`require_csrf`: safe methods pass, mutating methods need ``X-Requested-With:
  ads-engine`` exactly;
* the dependencies wired into a FastAPI app behind Starlette's ``SessionMiddleware``.

No test opens a database connection or the network: the session factory and the request's
database session are in-memory fakes, and ``Settings`` is built from an environment stripped
of every variable it reads, so host-exported credentials never reach a test.
"""

import asyncio
import os
import re
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import AliasChoices
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.middleware.sessions import Session, SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import ads_engine.api.deps as deps
import ads_engine.api.security as security
from ads_engine.analysis.thresholds import ConfigError, Thresholds
from ads_engine.api.deps import (
    ApiError,
    CurrentUser,
    NowDep,
    UserDep,
    conflict,
    get_login_limiter,
    get_now,
    get_session,
    get_settings_dep,
    get_thresholds,
    not_found,
    require_csrf,
    require_user,
)
from ads_engine.api.security import (
    CSRF_HEADER,
    CSRF_VALUE,
    LOGIN_MAX_ATTEMPTS,
    MUTATING_METHODS,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    LoginRateLimiter,
    start_session,
)
from ads_engine.db.models.ops import AppUser
from ads_engine.settings import Settings

ADMIN_EMAIL = "admin@example.com"
REPLACED_EMAIL = "new-admin@example.com"
USER_ID = 7
SIGNING_KEY = "test-signing-key"

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
T0_EPOCH = int(T0.timestamp())

GENERATION = 0
"""The session generation of the test user's row and of the sessions it signs in with."""

VALID_CLAIMS: dict[str, object] = {
    "uid": USER_ID,
    "email": ADMIN_EMAIL,
    "iat": T0_EPOCH,
    "gen": GENERATION,
}

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

CSRF_CODE = "csrf_header_missing"
CSRF_MESSAGE = f"Missing {CSRF_HEADER}: {CSRF_VALUE} header"
EXPIRED_COOKIE = "expires=Thu, 01 Jan 1970 00:00:00 GMT"
PEER = "203.0.113.7"

# How many threads race on a first request in the concurrency tests.
RACERS = 8


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _request(
    *,
    method: str = "GET",
    path: str = "/",
    headers: Sequence[tuple[bytes, bytes]] = (),
    session: dict[str, Any] | None = None,
    app: Starlette | None = None,
) -> Request:
    """Build a Starlette request from a hand-made ASGI HTTP scope.

    ``session`` stands in for the dict ``SessionMiddleware`` would place in the scope, and
    ``app`` for the application ``request.app`` returns. Header names are lowercase, as ASGI
    servers deliver them.
    """
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": list(headers),
    }
    if session is not None:
        scope["session"] = session
    if app is not None:
        scope["app"] = app
    return Request(scope)


def _app_with_state(**state: object) -> Starlette:
    """Return a bare application whose ``app.state`` carries exactly ``state``."""
    app = Starlette()
    for name, value in state.items():
        setattr(app.state, name, value)
    return app


def _assert_unauthenticated(error: ApiError) -> None:
    """Assert ``error`` is the one 401 every authentication failure returns."""
    assert (error.status_code, error.code, error.message, error.headers) == (
        401,
        "unauthenticated",
        "Sign in required",
        None,
    )


class _LogRecorder:
    """Stand-in for the module's structlog logger that keeps every event it receives."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        """Record one info event with its structured fields."""
        self.events.append(("info", event, fields))

    def warning(self, event: str, **fields: object) -> None:
        """Record one warning event with its structured fields."""
        self.events.append(("warning", event, fields))

    def text(self) -> str:
        """Return every recorded event and field as one string, for leak checks."""
        return " ".join(f"{level} {event} {fields!r}" for level, event, fields in self.events)


@pytest.fixture
def log_events(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """Replace the module logger with a recorder for the duration of one test."""
    recorder = _LogRecorder()
    monkeypatch.setattr(deps, "_log", recorder)
    return recorder


@pytest.fixture
def anyio_backend() -> str:
    """Run this module's async tests on asyncio only."""
    return "asyncio"


def _settings_env_names() -> frozenset[str]:
    """Return, lowercased, every environment name a ``Settings`` field can be read from."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(field_name)
        for alias in (field.alias, field.validation_alias):
            if isinstance(alias, str):
                names.add(alias)
            elif isinstance(alias, AliasChoices):
                names.update(choice for choice in alias.choices if isinstance(choice, str))
    return frozenset(name.lower() for name in names)


@pytest.fixture
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable ``Settings`` reads, in any letter case, and force simulation.

    pydantic-settings matches environment names case-insensitively, so a host variable in
    any case would otherwise be read into the settings a test builds.
    """
    names = _settings_env_names()
    for key in list(os.environ):
        if key.lower() in names:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATA_MODE", "simulated")


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    """The repository's own ``config/*.yaml``, loaded once for the module."""
    return Thresholds.load(CONFIG_DIR)


class _FakeSession:
    """An ``AsyncSession`` stand-in: an async context manager that records its lifecycle.

    ``rollback_error``, when given, is raised by :meth:`rollback`, as a database error would
    be on a connection the server has already closed.
    """

    def __init__(self, rollback_error: BaseException | None = None) -> None:
        self.rollback_error = rollback_error
        self.events: list[str] = []
        self.exit_exc_type: type[BaseException] | None = None

    async def __aenter__(self) -> Self:
        """Open the session."""
        self.events.append("open")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the session, recording the exception type it closed under."""
        self.events.append("close")
        self.exit_exc_type = exc_type

    async def rollback(self) -> None:
        """Record the rollback, then fail with ``rollback_error`` when one is set."""
        self.events.append("rollback")
        if self.rollback_error is not None:
            raise self.rollback_error


def _install_sessionmaker(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> list[int]:
    """Make ``get_sessionmaker()()`` return ``session``; return the factory's call log."""
    calls: list[int] = []

    def factory() -> _FakeSession:
        calls.append(len(calls) + 1)
        return session

    monkeypatch.setattr(deps, "get_sessionmaker", lambda: factory)
    return calls


def _session_generator() -> AsyncGenerator[AsyncSession]:
    """Start :func:`get_session` as FastAPI does, typed as the generator it is."""
    return cast(AsyncGenerator[AsyncSession], get_session())


class _UserLookup:
    """Stand-in for the request's ``AsyncSession``: ``get`` returns a fixed ``app_user`` row."""

    def __init__(self, user: AppUser | None) -> None:
        self.user = user
        self.calls: list[tuple[type[object], object]] = []

    async def get(self, entity: type[object], ident: object) -> AppUser | None:
        """Record the lookup and return the configured row."""
        self.calls.append((entity, ident))
        return self.user


def _user(email: str = ADMIN_EMAIL, generation: int = GENERATION) -> AppUser:
    """Build a transient ``app_user`` row with the test user's id.

    A transient row gets no server default, so the session generation is set explicitly.
    """
    return AppUser(
        id=USER_ID,
        email=email,
        password_hash="not-a-real-password-hash",
        session_generation=generation,
    )


async def _require(lookup: _UserLookup, session: Session, now: datetime = T0) -> CurrentUser:
    """Run :func:`require_user` on a request carrying ``session``, with ``lookup`` as the DB."""
    return await require_user(_request(session=session), cast(AsyncSession, lookup), now)


class _CountingLoader:
    """Replacement for :meth:`Thresholds.load` that counts calls and can fail or stall.

    The first ``failures`` calls raise :class:`ConfigError`; every call first sleeps
    ``delay`` seconds, which widens the window in which racing first requests overlap.
    """

    def __init__(self, result: Thresholds, *, failures: int = 0, delay: float = 0.0) -> None:
        self.result = result
        self.failures = failures
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, path: Path | str | None = None) -> Thresholds:
        """Count the call, then fail or return the configured result."""
        with self._lock:
            self.calls += 1
            fail = self.calls <= self.failures
        if self.delay:
            time.sleep(self.delay)
        if fail:
            raise ConfigError("thresholds.yaml", "window.default_days", "must be an integer")
        return self.result


def _race[T](first_request: Callable[[], T]) -> list[T]:
    """Run ``first_request`` on :data:`RACERS` threads released at the same instant."""
    barrier = threading.Barrier(RACERS)

    def racer(_: int) -> T:
        barrier.wait(timeout=10)
        return first_request()

    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        return list(pool.map(racer, range(RACERS)))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_api_error_carries_status_code_message_and_a_copy_of_the_headers() -> None:
    headers = {"Retry-After": "42"}
    message = "Too many login attempts; try again later"
    error = ApiError(429, "rate_limited", message, headers=headers)
    headers["Retry-After"] = "0"
    headers["X-Extra"] = "1"

    assert isinstance(error, Exception)
    assert (error.status_code, error.code, error.message) == (429, "rate_limited", message)
    assert error.headers == {"Retry-After": "42"}
    assert str(error) == message
    assert error.args == (message,)


def test_api_error_defaults_to_no_headers() -> None:
    error = ApiError(422, "feedback_not_allowed", "Feedback is not allowed on a discrepancy")
    assert error.headers is None


def test_api_error_repr_shows_status_code_and_message_but_never_header_values() -> None:
    error = ApiError(
        429, "rate_limited", "Slow down", headers={"Retry-After": "42", "X-Trace": "hidden-value"}
    )
    assert repr(error) == "ApiError(429, 'rate_limited', 'Slow down')"
    assert "hidden-value" not in repr(error)


@pytest.mark.parametrize("status_code", [400, 404, 409, 500, 599])
def test_api_error_accepts_every_http_error_status(status_code: int) -> None:
    assert ApiError(status_code, "code", "message").status_code == status_code


@pytest.mark.parametrize("status_code", [0, 200, 302, 399, 600, True, False])
def test_api_error_rejects_a_status_that_is_not_an_http_error(status_code: int) -> None:
    with pytest.raises(ValueError, match=r"status_code must be an HTTP error status \(400-599\)"):
        ApiError(status_code, "code", "message")


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("", "message", "code must not be empty"),
        ("code", "", "message must not be empty"),
    ],
)
def test_api_error_rejects_an_empty_code_or_message(code: str, message: str, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        ApiError(404, code, message)


def test_not_found_builds_the_404_for_a_missing_resource() -> None:
    error = not_found("Run 12")
    assert (error.status_code, error.code, error.message, error.headers) == (
        404,
        "not_found",
        "Run 12 not found",
        None,
    )


@pytest.mark.parametrize("code", ["run_in_progress", "sync_pending", "subject_retired"])
def test_conflict_builds_a_409_with_the_given_code(code: str) -> None:
    error = conflict(code, "A run is already in progress")
    assert (error.status_code, error.code, error.message, error.headers) == (
        409,
        code,
        "A run is already in progress",
        None,
    )


def test_conflict_rejects_an_empty_code() -> None:
    with pytest.raises(ValueError, match="code must not be empty"):
        conflict("", "message")


def test_unauthenticated_is_one_fresh_401_for_every_cause() -> None:
    first = deps._unauthenticated()
    second = deps._unauthenticated()
    assert first is not second
    _assert_unauthenticated(first)
    _assert_unauthenticated(second)


def test_current_user_is_an_immutable_value() -> None:
    user = CurrentUser(id=USER_ID, email=ADMIN_EMAIL)
    assert user == CurrentUser(id=USER_ID, email=ADMIN_EMAIL)
    with pytest.raises(FrozenInstanceError):
        user.id = USER_ID + 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Database session
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_session_yields_one_session_and_closes_it_after_a_normal_request(
    monkeypatch: pytest.MonkeyPatch, log_events: _LogRecorder
) -> None:
    session = _FakeSession()
    factory_calls = _install_sessionmaker(monkeypatch, session)
    generator = _session_generator()

    assert cast(object, await anext(generator)) is session
    assert session.events == ["open"]
    with pytest.raises(StopAsyncIteration):
        await anext(generator)

    assert session.events == ["open", "close"]
    assert session.exit_exc_type is None
    assert factory_calls == [1]
    assert log_events.events == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("route failed"),
        ApiError(409, "sync_pending", "A sync request is already pending"),
        asyncio.CancelledError(),
    ],
    ids=["exception", "api-error", "cancelled"],
)
async def test_get_session_rolls_back_and_reraises_an_error_thrown_into_it(
    monkeypatch: pytest.MonkeyPatch, log_events: _LogRecorder, error: BaseException
) -> None:
    session = _FakeSession()
    _install_sessionmaker(monkeypatch, session)
    generator = _session_generator()
    await anext(generator)

    with pytest.raises(type(error)) as raised:
        await generator.athrow(error)

    assert raised.value is error
    assert session.events == ["open", "rollback", "close"]
    assert session.exit_exc_type is type(error)
    assert log_events.events == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("rollback_error", "error_type"),
    [
        (SQLAlchemyError("rollback failed: hidden-detail"), "SQLAlchemyError"),
        (
            OperationalError("ROLLBACK", {}, Exception("server closed: hidden-detail")),
            "OperationalError",
        ),
    ],
    ids=["sqlalchemy-error", "operational-error"],
)
async def test_a_failed_rollback_is_logged_by_type_and_the_request_error_still_raised(
    monkeypatch: pytest.MonkeyPatch,
    log_events: _LogRecorder,
    rollback_error: SQLAlchemyError,
    error_type: str,
) -> None:
    session = _FakeSession(rollback_error=rollback_error)
    _install_sessionmaker(monkeypatch, session)
    generator = _session_generator()
    await anext(generator)
    request_error = RuntimeError("route failed")

    with pytest.raises(RuntimeError) as raised:
        await generator.athrow(request_error)

    assert raised.value is request_error
    assert session.events == ["open", "rollback", "close"]
    assert session.exit_exc_type is RuntimeError
    assert log_events.events == [
        ("warning", "api.session_rollback_failed", {"error": error_type}),
    ]
    assert "hidden-detail" not in log_events.text()


# ---------------------------------------------------------------------------
# Clock and settings
# ---------------------------------------------------------------------------


def test_get_now_returns_the_current_instant_in_utc() -> None:
    before = datetime.now(UTC)
    now = get_now()
    after = datetime.now(UTC)

    assert now.tzinfo is UTC
    assert now.utcoffset() == timedelta(0)
    assert before <= now <= after


@pytest.mark.usefixtures("isolated_environment")
def test_get_settings_dep_returns_the_settings_the_app_was_built_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = Settings(app_base_url="http://127.0.0.1:20640")

    def environment_settings() -> Settings:
        raise AssertionError("stored settings must not fall back to the environment")

    monkeypatch.setattr(deps, "get_settings", environment_settings)
    request = _request(app=_app_with_state(settings=configured))

    assert get_settings_dep(request) is configured
    assert configured.data_mode == "simulated"


@pytest.mark.usefixtures("isolated_environment")
@pytest.mark.parametrize(
    "state",
    [{}, {"settings": None}, {"settings": {"app_base_url": "http://localhost:8080"}}],
    ids=["absent", "none", "not-settings"],
)
def test_get_settings_dep_falls_back_to_the_process_settings(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, object]
) -> None:
    process_settings = Settings()
    calls: list[int] = []

    def environment_settings() -> Settings:
        calls.append(len(calls) + 1)
        return process_settings

    monkeypatch.setattr(deps, "get_settings", environment_settings)

    assert get_settings_dep(_request(app=_app_with_state(**state))) is process_settings
    assert calls == [1]


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def test_get_thresholds_loads_the_repository_config_on_first_use(
    monkeypatch: pytest.MonkeyPatch, log_events: _LogRecorder
) -> None:
    monkeypatch.chdir(REPO_ROOT / "backend")
    app = Starlette()

    loaded = get_thresholds(_request(app=app))

    assert loaded.source_dir == CONFIG_DIR
    assert app.state.thresholds is loaded
    assert log_events.events == [
        ("info", "api.thresholds_loaded", {"config_hash": loaded.config_hash}),
    ]


def test_get_thresholds_returns_the_stored_configuration_without_loading(
    monkeypatch: pytest.MonkeyPatch, log_events: _LogRecorder, thresholds: Thresholds
) -> None:
    loader = _CountingLoader(thresholds)
    monkeypatch.setattr(Thresholds, "load", loader)
    app = _app_with_state(thresholds=thresholds)

    assert get_thresholds(_request(app=app)) is thresholds
    assert loader.calls == 0
    assert log_events.events == []


def test_get_thresholds_loads_once_and_reuses_the_stored_result(
    monkeypatch: pytest.MonkeyPatch, log_events: _LogRecorder, thresholds: Thresholds
) -> None:
    loader = _CountingLoader(thresholds)
    monkeypatch.setattr(Thresholds, "load", loader)
    app = Starlette()

    assert get_thresholds(_request(app=app)) is thresholds
    assert app.state.thresholds is thresholds
    assert get_thresholds(_request(app=app)) is thresholds

    assert loader.calls == 1
    assert log_events.events == [
        ("info", "api.thresholds_loaded", {"config_hash": thresholds.config_hash}),
    ]


def test_get_thresholds_does_not_cache_a_load_failure(
    monkeypatch: pytest.MonkeyPatch, log_events: _LogRecorder, thresholds: Thresholds
) -> None:
    loader = _CountingLoader(thresholds, failures=1)
    monkeypatch.setattr(Thresholds, "load", loader)
    app = Starlette()

    with pytest.raises(ConfigError, match=r"thresholds\.yaml: window\.default_days"):
        get_thresholds(_request(app=app))
    assert getattr(app.state, "thresholds", None) is None
    assert log_events.events == []

    assert get_thresholds(_request(app=app)) is thresholds
    assert app.state.thresholds is thresholds
    assert loader.calls == 2


@pytest.mark.parametrize("stored", ["thresholds.yaml", {"window": {}}, 42])
def test_get_thresholds_rejects_a_foreign_object_in_app_state(
    monkeypatch: pytest.MonkeyPatch, thresholds: Thresholds, stored: object
) -> None:
    loader = _CountingLoader(thresholds)
    monkeypatch.setattr(Thresholds, "load", loader)
    expected = f"app.state.thresholds must be a Thresholds instance, not {type(stored).__name__}"

    with pytest.raises(TypeError, match=re.escape(expected)):
        get_thresholds(_request(app=_app_with_state(thresholds=stored)))
    assert loader.calls == 0


@pytest.mark.usefixtures("log_events")
def test_get_thresholds_loads_once_when_first_requests_race(
    monkeypatch: pytest.MonkeyPatch, thresholds: Thresholds
) -> None:
    loader = _CountingLoader(thresholds, delay=0.05)
    monkeypatch.setattr(Thresholds, "load", loader)
    app = Starlette()

    results = _race(lambda: get_thresholds(_request(app=app)))

    assert loader.calls == 1
    assert all(result is thresholds for result in results)
    assert app.state.thresholds is thresholds


# ---------------------------------------------------------------------------
# Login limiter
# ---------------------------------------------------------------------------


def test_get_login_limiter_returns_the_application_limiter() -> None:
    limiter = LoginRateLimiter()
    app = _app_with_state(login_limiter=limiter)
    assert get_login_limiter(_request(app=app)) is limiter


def test_get_login_limiter_creates_and_stores_a_default_limiter_on_first_use() -> None:
    app = Starlette()

    limiter = get_login_limiter(_request(app=app))

    assert type(limiter) is LoginRateLimiter
    assert app.state.login_limiter is limiter
    assert get_login_limiter(_request(app=app)) is limiter
    assert all(limiter.check(PEER).allowed for _ in range(LOGIN_MAX_ATTEMPTS))
    assert not limiter.check(PEER).allowed


@pytest.mark.parametrize("stored", ["limiter", {"attempts": 5}, 5])
def test_get_login_limiter_rejects_a_foreign_object_in_app_state(stored: object) -> None:
    expected = (
        f"app.state.login_limiter must be a LoginRateLimiter instance, not {type(stored).__name__}"
    )
    with pytest.raises(TypeError, match=re.escape(expected)):
        get_login_limiter(_request(app=_app_with_state(login_limiter=stored)))


def test_get_login_limiter_creates_one_limiter_when_first_requests_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[LoginRateLimiter] = []
    created_lock = threading.Lock()

    class _SlowLimiter(LoginRateLimiter):
        """A limiter whose construction stalls, so racing first requests overlap."""

        def __init__(self) -> None:
            time.sleep(0.05)
            super().__init__()
            with created_lock:
                created.append(self)

    monkeypatch.setattr(security, "LoginRateLimiter", _SlowLimiter)
    app = Starlette()

    results = _race(lambda: get_login_limiter(_request(app=app)))

    assert len(created) == 1
    assert all(result is created[0] for result in results)
    assert app.state.login_limiter is created[0]


# ---------------------------------------------------------------------------
# require_user
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_require_user_without_a_session_is_401_and_not_logged(
    log_events: _LogRecorder,
) -> None:
    session = Session()
    lookup = _UserLookup(_user())

    with pytest.raises(ApiError) as raised:
        await _require(lookup, session)

    _assert_unauthenticated(raised.value)
    assert lookup.calls == []
    assert session.modified is False
    assert log_events.events == []


@pytest.mark.anyio
async def test_require_user_logs_a_session_cookie_the_middleware_refused(
    log_events: _LogRecorder,
) -> None:
    """A present cookie with an empty session is 401 and one ``invalid_signature`` warning."""
    session = Session()
    lookup = _UserLookup(_user())
    request = _request(session=session, headers=[(b"cookie", b"ads_engine_session=garbage")])

    with pytest.raises(ApiError) as raised:
        await require_user(request, cast(AsyncSession, lookup), T0)

    _assert_unauthenticated(raised.value)
    assert lookup.calls == []
    assert session.modified is False
    assert log_events.events == [
        ("warning", "auth.session_rejected", {"reason": "invalid_signature"}),
    ]
    assert "garbage" not in log_events.text()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("claims", "now"),
    [
        (VALID_CLAIMS, T0 + timedelta(seconds=SESSION_MAX_AGE)),
        ({**VALID_CLAIMS, "iat": T0_EPOCH - SESSION_MAX_AGE}, T0),
        ({**VALID_CLAIMS, "iat": T0_EPOCH + 1}, T0),
        ({**VALID_CLAIMS, "uid": str(USER_ID)}, T0),
        ({"uid": USER_ID, "iat": T0_EPOCH, "gen": GENERATION}, T0),
        ({"uid": USER_ID, "email": ADMIN_EMAIL, "iat": T0_EPOCH}, T0),
    ],
    ids=[
        "expired-at-now",
        "issued-12h-ago",
        "issued-ahead",
        "malformed-uid",
        "missing-email",
        "missing-generation",
    ],
)
async def test_require_user_rejects_an_invalid_or_expired_session(
    log_events: _LogRecorder, claims: dict[str, object], now: datetime
) -> None:
    session = Session(claims)
    lookup = _UserLookup(_user())

    with pytest.raises(ApiError) as raised:
        await _require(lookup, session, now)

    _assert_unauthenticated(raised.value)
    assert lookup.calls == []
    assert dict(session) == {}
    assert session.modified is True
    assert log_events.events == [
        ("info", "auth.session_rejected", {"reason": "invalid_or_expired"}),
    ]
    assert ADMIN_EMAIL not in log_events.text()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stored_email", "reason"),
    [(None, "user_missing"), (REPLACED_EMAIL, "user_replaced")],
    ids=["user-missing", "user-replaced"],
)
async def test_require_user_ends_a_session_whose_user_no_longer_matches(
    log_events: _LogRecorder, stored_email: str | None, reason: str
) -> None:
    session = Session(VALID_CLAIMS)
    lookup = _UserLookup(None if stored_email is None else _user(stored_email))

    with pytest.raises(ApiError) as raised:
        await _require(lookup, session)

    _assert_unauthenticated(raised.value)
    assert lookup.calls == [(AppUser, USER_ID)]
    assert dict(session) == {}
    assert session.modified is True
    assert log_events.events == [
        ("warning", "auth.session_rejected", {"reason": reason, "user_id": USER_ID}),
    ]
    assert ADMIN_EMAIL not in log_events.text()
    assert REPLACED_EMAIL not in log_events.text()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("claimed", "stored"),
    [(0, 1), (4, 5), (1, 0)],
    ids=["signed-out-once", "signed-out-again", "generation-ahead"],
)
async def test_require_user_ends_a_session_revoked_by_a_sign_out(
    log_events: _LogRecorder, claimed: int, stored: int
) -> None:
    """A generation other than the row's is 401, clears the session and logs the user id."""
    session = Session({**VALID_CLAIMS, "gen": claimed})
    lookup = _UserLookup(_user(generation=stored))

    with pytest.raises(ApiError) as raised:
        await _require(lookup, session)

    _assert_unauthenticated(raised.value)
    assert lookup.calls == [(AppUser, USER_ID)]
    assert dict(session) == {}
    assert session.modified is True
    assert log_events.events == [
        ("warning", "auth.session_rejected", {"reason": "session_revoked", "user_id": USER_ID}),
    ]
    assert ADMIN_EMAIL not in log_events.text()


@pytest.mark.anyio
async def test_require_user_returns_the_verified_user(log_events: _LogRecorder) -> None:
    session = Session(VALID_CLAIMS)
    lookup = _UserLookup(_user())

    user = await _require(lookup, session, T0 + timedelta(seconds=SESSION_MAX_AGE - 1))

    assert user == CurrentUser(id=USER_ID, email=ADMIN_EMAIL)
    assert lookup.calls == [(AppUser, USER_ID)]
    assert dict(session) == VALID_CLAIMS
    assert session.modified is False
    assert log_events.events == []


# ---------------------------------------------------------------------------
# require_csrf
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_require_csrf_lets_safe_methods_through_without_the_header(
    log_events: _LogRecorder, method: str
) -> None:
    require_csrf(_request(method=method))
    assert log_events.events == []


@pytest.mark.parametrize("method", sorted(MUTATING_METHODS))
def test_require_csrf_passes_a_mutating_request_with_the_header(
    log_events: _LogRecorder, method: str
) -> None:
    require_csrf(_request(method=method, headers=[(b"x-requested-with", CSRF_VALUE.encode())]))
    assert log_events.events == []


@pytest.mark.parametrize("method", sorted(MUTATING_METHODS))
@pytest.mark.parametrize(
    "headers",
    [
        (),
        ((b"x-requested-with", b"ADS-ENGINE"),),
        ((b"x-requested-with", b"XMLHttpRequest"),),
        ((b"x-requested-with", b""),),
    ],
    ids=["missing", "wrong-case", "other-value", "empty"],
)
def test_require_csrf_rejects_a_mutating_request_without_the_exact_header(
    log_events: _LogRecorder, method: str, headers: tuple[tuple[bytes, bytes], ...]
) -> None:
    with pytest.raises(ApiError) as raised:
        require_csrf(_request(method=method, path="/api/v1/runs", headers=headers))

    error = raised.value
    assert (error.status_code, error.code, error.message, error.headers) == (
        403,
        CSRF_CODE,
        CSRF_MESSAGE,
        None,
    )
    assert log_events.events == [
        ("warning", "auth.csrf_rejected", {"method": method, "path": "/api/v1/runs"}),
    ]


# ---------------------------------------------------------------------------
# The dependencies inside a FastAPI app
# ---------------------------------------------------------------------------


def _api_app(lookup: _UserLookup, clock: list[datetime]) -> FastAPI:
    """Build a FastAPI app that uses the dependencies as the route modules do.

    The ``ApiError`` handler renders the AAP error shape, ``get_session`` yields ``lookup``
    and ``get_now`` reads ``clock[0]``, through ``app.dependency_overrides``.
    """
    app = FastAPI()

    async def render_api_error(request: Request, exc: Exception) -> JSONResponse:
        """Render an ``ApiError`` as ``{"error": {"code": ..., "message": ...}}``."""
        error = cast(ApiError, exc)
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}},
            status_code=error.status_code,
            headers=error.headers,
        )

    app.add_exception_handler(ApiError, render_api_error)

    @app.post("/login", dependencies=[Depends(require_csrf)])
    async def login(request: Request, now: NowDep) -> dict[str, str]:
        """Sign the test user in at the injected instant."""
        start_session(request, user_id=USER_ID, email=ADMIN_EMAIL, generation=GENERATION, now=now)
        return {"email": ADMIN_EMAIL}

    @app.get("/me")
    async def me(user: UserDep) -> dict[str, str | int]:
        """Return the verified user."""
        return {"id": user.id, "email": user.email}

    @app.post("/mutate", dependencies=[Depends(require_csrf)])
    async def mutate(user: UserDep) -> dict[str, int]:
        """A mutating route behind both guards."""
        return {"user_id": user.id}

    async def session_override() -> AsyncIterator[_UserLookup]:
        """Yield the fake database session for the request."""
        yield lookup

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_now] = lambda: clock[0]
    app.add_middleware(
        SessionMiddleware,
        secret_key=SIGNING_KEY,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
    )
    return app


@pytest.mark.anyio
async def test_dependencies_guard_routes_behind_the_session_middleware(
    log_events: _LogRecorder,
) -> None:
    lookup = _UserLookup(_user())
    clock = [T0]
    transport = httpx.ASGITransport(app=_api_app(lookup, clock))
    csrf = {CSRF_HEADER: CSRF_VALUE}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        anonymous = await client.get("/me")
        assert anonymous.status_code == 401
        assert anonymous.json() == {
            "error": {"code": "unauthenticated", "message": "Sign in required"}
        }

        forged = await client.post("/login")
        assert forged.status_code == 403
        assert forged.json() == {"error": {"code": CSRF_CODE, "message": CSRF_MESSAGE}}
        assert client.cookies.get(SESSION_COOKIE) is None

        assert (await client.post("/login", headers=csrf)).status_code == 200
        assert client.cookies.get(SESSION_COOKIE)

        me = await client.get("/me")
        assert me.status_code == 200
        assert me.json() == {"id": USER_ID, "email": ADMIN_EMAIL}
        assert lookup.calls == [(AppUser, USER_ID)]

        assert (await client.post("/mutate")).status_code == 403
        mutated = await client.post("/mutate", headers=csrf)
        assert mutated.status_code == 200
        assert mutated.json() == {"user_id": USER_ID}

        clock[0] = T0 + timedelta(seconds=SESSION_MAX_AGE)
        expired = await client.get("/me")
        assert expired.status_code == 401
        assert EXPIRED_COOKIE in expired.headers["set-cookie"]
        assert client.cookies.get(SESSION_COOKIE) is None

        assert (await client.post("/login", headers=csrf)).status_code == 200
        lookup.user = _user(REPLACED_EMAIL)
        replaced = await client.get("/me")
        assert replaced.status_code == 401
        assert replaced.json()["error"]["code"] == "unauthenticated"
        assert EXPIRED_COOKIE in replaced.headers["set-cookie"]
        assert client.cookies.get(SESSION_COOKIE) is None

    assert log_events.events == [
        ("warning", "auth.csrf_rejected", {"method": "POST", "path": "/login"}),
        ("warning", "auth.csrf_rejected", {"method": "POST", "path": "/mutate"}),
        ("info", "auth.session_rejected", {"reason": "invalid_or_expired"}),
        ("warning", "auth.session_rejected", {"reason": "user_replaced", "user_id": USER_ID}),
    ]
    assert ADMIN_EMAIL not in log_events.text()
    assert REPLACED_EMAIL not in log_events.text()


def _tampered(value: str) -> str:
    """Return ``value`` with its last two characters replaced by two different ones."""
    replacement = "AA" if value[-2:] != "AA" else "BB"
    return f"{value[:-2]}{replacement}"


@pytest.mark.anyio
@pytest.mark.parametrize("presented", ["tampered", "garbage"])
async def test_a_refused_session_cookie_is_logged_once_behind_the_session_middleware(
    log_events: _LogRecorder, presented: str
) -> None:
    """A tampered or garbage cookie the real middleware refuses is 401 and logged once."""
    lookup = _UserLookup(_user())
    transport = httpx.ASGITransport(app=_api_app(lookup, [T0]))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.post("/login", headers={CSRF_HEADER: CSRF_VALUE})).status_code == 200
        valid = client.cookies.get(SESSION_COOKIE)
        assert valid
        client.cookies.clear()
        value = _tampered(valid) if presented == "tampered" else "garbage"

        refused = await client.get("/me", headers={"Cookie": f"{SESSION_COOKIE}={value}"})

    assert refused.status_code == 401
    assert refused.json() == {"error": {"code": "unauthenticated", "message": "Sign in required"}}
    assert lookup.calls == []
    assert log_events.events == [
        ("warning", "auth.session_rejected", {"reason": "invalid_signature"}),
    ]
    assert value not in log_events.text()
    assert ADMIN_EMAIL not in log_events.text()


@pytest.mark.anyio
async def test_a_request_without_a_session_cookie_logs_no_rejection(
    log_events: _LogRecorder,
) -> None:
    """No session cookie, with or without other cookies, is a routine 401 and not logged."""
    lookup = _UserLookup(_user())
    transport = httpx.ASGITransport(app=_api_app(lookup, [T0]))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        anonymous = await client.get("/me", headers={"Cookie": "unrelated=value"})
        assert anonymous.status_code == 401
        assert (await client.get("/me")).status_code == 401

    assert lookup.calls == []
    assert log_events.events == []
