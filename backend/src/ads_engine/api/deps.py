"""Request dependencies of the API: ``ApiError``, the database session, clock and guards.

Every route module builds on the dependencies defined here and on nothing else from the
request scope:

* :class:`ApiError` is the single error type routes raise for HTTP-level failures.
  ``api/app.py`` registers the handler that renders it as
  ``{"error": {"code": ..., "message": ...}}`` (AAP 0.5.3), with :func:`not_found` and
  :func:`conflict` for the two most common shapes.
* :data:`SessionDep` yields one :class:`~sqlalchemy.ext.asyncio.AsyncSession` per request.
  It never commits: mutating routes call ``await session.commit()`` themselves.
* :data:`NowDep`, :data:`SettingsDep`, :data:`ThresholdsDep` and :data:`LimiterDep` supply
  the clock, the settings, the loaded ``config/*.yaml`` and the login rate limiter, each
  replaceable through ``app.dependency_overrides`` in tests.
* :data:`UserDep` (:func:`require_user`) enforces the session cookie on every endpoint
  except ``POST /auth/login`` and ``GET /healthz``; :func:`require_csrf` enforces the
  ``X-Requested-With: ads-engine`` header on every mutating request (AAP 0.3.2, 0.5.3).

Nothing here creates a database engine, reads settings or loads configuration at import
time, so ``import ads_engine.api.deps`` works with no environment set. The module never
imports ``connectors``, ``sync``, ``simulation`` or ``api.routes``, and it logs no email,
secret or token: authentication events carry only the user id and a reason.

Example:
    A mutating route combines the dependencies like this::

        @router.post("/sources/{source}/sync", status_code=202)
        async def request_sync(
            source: SourceName, user: UserDep, session: SessionDep, now: NowDep
        ) -> SyncRequestView:
            row = SyncRequest(source=source)
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise conflict("sync_pending", f"A sync request for {source} is already pending")
            ...
"""

import threading
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.analysis.thresholds import Thresholds
from ads_engine.api import security
from ads_engine.db.models.ops import AppUser
from ads_engine.db.session import get_sessionmaker
from ads_engine.logging import get_logger
from ads_engine.settings import Settings, get_settings

__all__ = [
    "ApiError",
    "CurrentUser",
    "LimiterDep",
    "NowDep",
    "SessionDep",
    "SettingsDep",
    "ThresholdsDep",
    "UserDep",
    "conflict",
    "get_login_limiter",
    "get_now",
    "get_session",
    "get_settings_dep",
    "get_thresholds",
    "not_found",
    "require_csrf",
    "require_user",
]

_log = get_logger(__name__)

# Guards the lazy, once-per-application initialisation of `app.state.thresholds` and
# `app.state.login_limiter`. Synchronous dependencies run in FastAPI's thread pool, so two
# first requests can race; the lock makes every request of one app share one object (one
# limiter in particular, or the login limit would be split between two counters).
_app_state_lock: Final = threading.Lock()

_UNAUTHENTICATED_CODE: Final = "unauthenticated"
_UNAUTHENTICATED_MESSAGE: Final = "Sign in required"
_CSRF_CODE: Final = "csrf_header_missing"
_CSRF_MESSAGE: Final = f"Missing {security.CSRF_HEADER}: {security.CSRF_VALUE} header"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """The single error type routes raise for HTTP-level failures.

    ``api/app.py`` registers the handler that renders it with :attr:`status_code`, the
    body ``{"error": {"code": code, "message": message}}`` (AAP 0.5.3) and
    :attr:`headers`. Routes never raise ``HTTPException`` directly. Typical uses:

    * 401 ``unauthenticated`` (:func:`require_user`) and 403 ``csrf_header_missing``
      (:func:`require_csrf`);
    * 404 ``not_found`` (:func:`not_found`);
    * 409 conflicts (:func:`conflict`) such as ``run_in_progress``, ``sync_pending`` and
      ``subject_retired``;
    * 422 ``feedback_not_allowed`` for feedback on a C0 item;
    * 429 ``rate_limited`` on login, with a ``Retry-After`` header.

    The message is shown to the user, so it never contains an email, a password, a secret
    or a token.

    Example::

        raise ApiError(
            429,
            "rate_limited",
            "Too many login attempts; try again later",
            headers={"Retry-After": "42"},
        )

    Args:
        status_code: The HTTP status, a client or server error (400-599).
        code: The machine-readable error code, for example ``not_found``.
        message: The human-readable explanation.
        headers: Extra response headers; copied, so later changes to the argument do
            not affect the error.

    Attributes:
        status_code: The HTTP status of the response.
        code: The ``error.code`` of the response body.
        message: The ``error.message`` of the response body; also ``str(error)``.
        headers: The extra response headers, or ``None``.

    Raises:
        ValueError: When ``status_code`` is outside 400-599, or ``code`` or ``message``
            is empty.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(status_code, bool) or not 400 <= status_code <= 599:
            raise ValueError(f"status_code must be an HTTP error status (400-599): {status_code}")
        if not code:
            raise ValueError("code must not be empty")
        if not message:
            raise ValueError("message must not be empty")
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers: dict[str, str] | None = None if headers is None else dict(headers)

    def __repr__(self) -> str:
        """Render the status, code and message (never the headers' values)."""
        return f"{type(self).__name__}({self.status_code}, {self.code!r}, {self.message!r})"


def not_found(what: str) -> ApiError:
    """Build the 404 error for a missing resource.

    Example::

        run = await session.get(AnalysisRun, run_id)
        if run is None:
            raise not_found(f"Run {run_id}")

    Args:
        what: The resource as it reads in a sentence, for example ``"Run 12"``.

    Returns:
        ``ApiError(404, "not_found", "{what} not found")``.
    """
    return ApiError(404, "not_found", f"{what} not found")


def conflict(code: str, message: str) -> ApiError:
    """Build a 409 conflict error.

    Args:
        code: The conflict's machine code, for example ``run_in_progress``,
            ``sync_pending`` or ``subject_retired``.
        message: The human-readable explanation.

    Returns:
        ``ApiError(409, code, message)``.
    """
    return ApiError(409, code, message)


# ---------------------------------------------------------------------------
# Database session
# ---------------------------------------------------------------------------


async def _rollback_after_failure(session: AsyncSession) -> None:
    """Roll back the session of a failed request without masking the request's error.

    A database error from the rollback itself (for example on a connection the server has
    already closed) is logged with its type only and swallowed here, because the exception
    the caller must see is the one that failed the request. Closing the session afterwards
    returns the connection to the pool, which resets it, so nothing uncommitted survives
    even when this rollback fails or is cancelled.

    Args:
        session: The request's session.
    """
    try:
        await session.rollback()
    except SQLAlchemyError as exc:
        _log.warning("api.session_rollback_failed", error=type(exc).__name__)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield one database session for the request, rolled back on error and always closed.

    The session comes from :func:`ads_engine.db.session.get_sessionmaker`, so it keeps
    loaded attributes after commit. FastAPI caches the dependency per request, so a route
    and :func:`require_user` share the same session.

    This dependency **never commits**. Mutating routes call ``await session.commit()``
    themselves before returning, so a commit failure (for example the ``sync_request``
    partial unique index) surfaces inside the route, which maps it to the right 409.
    That holds whenever FastAPI runs this teardown: with the default request scope it
    runs after the response has been sent, too late to change the response. An exception
    raised by the route or a later dependency is thrown in here first: the session is
    rolled back and the exception re-raised unchanged for the error handlers. A session
    that ends without a commit is closed, which discards anything uncommitted.

    Yields:
        A new :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

    Raises:
        ValueError: When ``DATABASE_URL`` is malformed, on the first session of the process.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except BaseException:
            # BaseException, so a cancelled request is rolled back too; always re-raised.
            await _rollback_after_failure(session)
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]
"""The request's database session (:func:`get_session`)."""


# ---------------------------------------------------------------------------
# Clock, settings, thresholds and the login limiter
# ---------------------------------------------------------------------------


def get_now() -> datetime:
    """Return the current instant in UTC, the request's single clock.

    Session age, feedback ``until`` and the current-state overlay all read the time
    through this dependency, so a test fixes or advances it with
    ``app.dependency_overrides[get_now] = lambda: fixed_instant``.

    Returns:
        ``datetime.now(UTC)``, timezone-aware.
    """
    return datetime.now(UTC)


NowDep = Annotated[datetime, Depends(get_now)]
"""The current instant, timezone-aware (:func:`get_now`)."""


def get_settings_dep(request: Request) -> Settings:
    """Return the settings the application was built with.

    ``create_app(settings)`` stores its settings on ``app.state.settings``, and those are
    returned here, so a route reads the same settings the session middleware and the
    startup checks used, including in a test that builds the app with explicit settings.
    An application without stored settings falls back to the process-wide
    :func:`ads_engine.settings.get_settings`, which is the same cached object
    ``create_app()`` uses by default.

    Args:
        request: The current request.

    Returns:
        The application's :class:`~ads_engine.settings.Settings`.

    Raises:
        pydantic.ValidationError: When falling back to the environment and a value
            cannot be parsed.
    """
    # The request parameter goes beyond a bare `get_settings()` call on purpose: without it,
    # an app built with explicit settings would sign cookies with one configuration while
    # its routes read another from the environment.
    configured = getattr(request.app.state, "settings", None)
    if isinstance(configured, Settings):
        return configured
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
"""The application's settings (:func:`get_settings_dep`)."""


def get_thresholds(request: Request) -> Thresholds:
    """Return the loaded ``config/*.yaml``, loading it on first use for the application.

    The API lifespan loads the configuration eagerly into ``app.state.thresholds``, so
    under uvicorn a broken file fails at boot. A request never depends on the lifespan
    having run, though: ``httpx.ASGITransport``, the tests' client, runs no lifespan
    events, so when nothing is stored yet this dependency loads the files with
    :meth:`Thresholds.load` (directory from
    :func:`ads_engine.analysis.thresholds.resolve_config_dir`) once, under a lock, and
    stores the result for every later request.

    Thresholds are read once per process. The config files are bind-mounted read-only and
    a change takes effect on restart, matching the worker's startup detection of changed
    threshold files (AAP 0.3.2). A load failure is not cached; the next request tries again.

    Args:
        request: The current request.

    Returns:
        The application's :class:`~ads_engine.analysis.thresholds.Thresholds`.

    Raises:
        ads_engine.analysis.thresholds.ConfigError: When the config directory or a file is
            missing or invalid; ``api/app.py`` renders it as 500 ``config_error``.
        TypeError: When ``app.state.thresholds`` holds something other than
            :class:`~ads_engine.analysis.thresholds.Thresholds`.
    """
    state = request.app.state
    loaded = getattr(state, "thresholds", None)
    if loaded is None:
        with _app_state_lock:
            loaded = getattr(state, "thresholds", None)
            if loaded is None:
                loaded = Thresholds.load()
                state.thresholds = loaded
                _log.info("api.thresholds_loaded", config_hash=loaded.config_hash)
    if not isinstance(loaded, Thresholds):
        raise TypeError(
            f"app.state.thresholds must be a Thresholds instance, not {type(loaded).__name__}"
        )
    return loaded


ThresholdsDep = Annotated[Thresholds, Depends(get_thresholds)]
"""The loaded ``config/*.yaml`` (:func:`get_thresholds`)."""


def get_login_limiter(request: Request) -> security.LoginRateLimiter:
    """Return the application's login rate limiter.

    ``create_app`` creates one :class:`~ads_engine.api.security.LoginRateLimiter` per
    application on ``app.state.login_limiter``, so every test app starts with clean
    counters. An application assembled without ``create_app`` gets one created here on
    first use, under a lock, so the 5-attempts-per-minute limit always holds and every
    request of the app counts against the same limiter.

    Args:
        request: The current request.

    Returns:
        The application's limiter.

    Raises:
        TypeError: When ``app.state.login_limiter`` holds something other than a
            :class:`~ads_engine.api.security.LoginRateLimiter`.
    """
    state = request.app.state
    limiter = getattr(state, "login_limiter", None)
    if limiter is None:
        with _app_state_lock:
            limiter = getattr(state, "login_limiter", None)
            if limiter is None:
                limiter = security.LoginRateLimiter()
                state.login_limiter = limiter
    if not isinstance(limiter, security.LoginRateLimiter):
        raise TypeError(
            "app.state.login_limiter must be a LoginRateLimiter instance, "
            f"not {type(limiter).__name__}"
        )
    return limiter


LimiterDep = Annotated[security.LoginRateLimiter, Depends(get_login_limiter)]
"""The application's login rate limiter (:func:`get_login_limiter`)."""


# ---------------------------------------------------------------------------
# Authentication and CSRF
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The signed-in user, as verified by :func:`require_user`.

    Attributes:
        id: ``app_user.id``.
        email: The user's current stored email. Routes may return it to the user (as
            ``GET /auth/me`` does) but never log it.
    """

    id: int
    email: str


def _unauthenticated() -> ApiError:
    """Build the 401 every authentication failure returns, whatever its cause."""
    return ApiError(401, _UNAUTHENTICATED_CODE, _UNAUTHENTICATED_MESSAGE)


async def require_user(request: Request, session: SessionDep, now: NowDep) -> CurrentUser:
    """Require a valid session cookie and return its user.

    Every endpoint except ``POST /auth/login`` and ``GET /healthz`` declares this
    dependency through :data:`UserDep` (AAP 0.5.3). A session is accepted when
    :func:`ads_engine.api.security.read_session` validates it at ``now``, which enforces
    the absolute 12 h lifetime from sign-in, and its user still exists with the same
    email and the same session generation. Replacing the admin with ``ads-engine
    ensure-admin`` (a new row, or a changed email) therefore invalidates every earlier
    session, and so does signing out, which increments ``app_user.session_generation``:
    a copy of the cookie saved before the sign-out no longer matches. In each case the
    session is cleared, so the response also expires the cookie.

    Every failure returns the same 401, so the response never reveals why a session was
    refused. The log records only the reason and, where known, the user id: a warning
    ``invalid_signature`` for a session cookie ``SessionMiddleware`` could not verify
    (:func:`ads_engine.api.security.session_cookie_unverified`), an info
    ``invalid_or_expired`` for a verified session that is expired, issued in the future
    or malformed, and a warning ``user_missing``, ``user_replaced`` or
    ``session_revoked`` with the user id. A request without a session cookie is routine
    and not logged.

    Args:
        request: The current request; ``SessionMiddleware`` must be installed.
        session: The request's database session.
        now: The current instant.

    Returns:
        The verified user.

    Raises:
        ApiError: 401 ``unauthenticated`` when there is no valid session, its user no
            longer matches or it was revoked by a sign-out.
    """
    if security.session_cookie_unverified(request):
        # SessionMiddleware could not verify the cookie (tampered, forged, signed with
        # another secret or older than its signature limit) and left the session empty,
        # so read_session would see no cookie at all. Neither the cookie value nor the
        # client address is logged.
        _log.warning("auth.session_rejected", reason="invalid_signature")
        raise _unauthenticated()
    had_session = bool(request.session)
    claims = security.read_session(request, now=now)
    if claims is None:
        if had_session:
            # A cookie was presented but is expired, issued in the future or malformed;
            # read_session has cleared it. An absent cookie is routine and not logged.
            _log.info("auth.session_rejected", reason="invalid_or_expired")
        raise _unauthenticated()
    user = await session.get(AppUser, claims.user_id)
    if user is None or user.email != claims.email:
        security.end_session(request)
        _log.warning(
            "auth.session_rejected",
            reason="user_missing" if user is None else "user_replaced",
            user_id=claims.user_id,
        )
        raise _unauthenticated()
    if user.session_generation != claims.generation:
        # A sign-out has incremented the generation since this cookie was issued.
        security.end_session(request)
        _log.warning("auth.session_rejected", reason="session_revoked", user_id=claims.user_id)
        raise _unauthenticated()
    return CurrentUser(id=user.id, email=user.email)


UserDep = Annotated[CurrentUser, Depends(require_user)]
"""The signed-in user; 401 ``unauthenticated`` without a valid session (:func:`require_user`)."""


def require_csrf(request: Request) -> None:
    """Require ``X-Requested-With: ads-engine`` on every mutating request.

    ``api/app.py`` attaches this guard to every router
    (``include_router(..., dependencies=[Depends(require_csrf)])``), so every mutating
    route, ``POST /auth/login`` and ``POST /auth/logout`` included, needs the header (AAP
    0.5.3: "Mutating requests also require the ``X-Requested-With: ads-engine`` header").
    ``GET``, ``HEAD`` and ``OPTIONS`` pass untouched. The rule itself lives in
    :func:`ads_engine.api.security.csrf_ok`.

    Args:
        request: The current request.

    Raises:
        ApiError: 403 ``csrf_header_missing`` when a mutating request lacks the header or
            carries another value.
    """
    if not security.csrf_ok(request):
        _log.warning("auth.csrf_rejected", method=request.method, path=request.url.path)
        raise ApiError(403, _CSRF_CODE, _CSRF_MESSAGE)
