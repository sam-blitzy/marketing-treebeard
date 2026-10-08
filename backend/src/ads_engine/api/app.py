"""FastAPI application factory of the Ads Action Engine API, mounted at ``/api/v1``.

uvicorn serves the factory, never a module-level application::

    uvicorn ads_engine.api.app:create_app --factory --host 0.0.0.0 --port 8000

:func:`create_app` assembles the whole API:

* **Startup checks**: :meth:`Settings.check_api_startup` refuses a plain-``http``
  ``APP_BASE_URL`` on any host other than ``localhost`` or ``127.0.0.1``, and a missing
  ``SESSION_SECRET``. That :class:`~ads_engine.settings.ConfigurationError`, or a
  :class:`pydantic.ValidationError` from an unparseable environment value, is first logged
  as one ``api.startup_failed`` ERROR event naming the error class and the settings at
  fault, never their values. It then propagates, so uvicorn exits before it binds a socket.
* **Session cookie**: Starlette's ``SessionMiddleware`` signs ``ads_engine_session`` with
  ``SESSION_SECRET``. The cookie is always HttpOnly, ``SameSite=Lax`` and lives 12 h; it is
  ``Secure`` exactly when ``APP_BASE_URL`` is ``https://``.
* **Routers**: one per route module, in the order of the endpoint table, each under
  ``/api/v1`` and behind :func:`~ads_engine.api.deps.require_csrf`, which requires
  ``X-Requested-With: ads-engine`` on mutating methods and passes safe methods untouched.
* **Error envelope**: every error the API returns, a domain rejection, a validation
  failure, an unknown path or an unexpected crash, is rendered as
  :class:`~ads_engine.api.schemas.ErrorResponse`, ``{"error": {"code", "message"}}``.
* **Lifespan**: loads ``config/*.yaml`` at boot, so a broken file stops uvicorn instead of
  failing the first request, and disposes the database pool on shutdown.

No other middleware is installed. The SPA is served same-origin by nginx, which also proxies
``/api``, so CORS and compression have no role here. Swagger UI and ReDoc are disabled: they
load CDN assets that nginx's ``default-src 'self'`` Content-Security-Policy blocks. The
OpenAPI document, the contract ``web/src/api/types.ts`` is written against, is served at
``/api/v1/openapi.json`` to a signed-in session only. FastAPI's own schema route is
disabled, because it sits outside the guarded routers and would answer without a session.

The API reads stored results only. Neither this module nor any module it imports loads
``ads_engine.connectors``, ``ads_engine.sync`` or ``ads_engine.simulation``: all external I/O
belongs to the worker. Importing this module builds no application, reads no settings and
creates no database engine.
"""

import http
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from ads_engine import __version__
from ads_engine.analysis.lock import RunInProgress
from ads_engine.analysis.thresholds import (
    CONFIG_FILES,
    CRM_BINDINGS_DOCUMENT,
    ConfigError,
    Thresholds,
)
from ads_engine.analysis.window import InvalidWindow
from ads_engine.api import security
from ads_engine.api.deps import ApiError, require_csrf, require_user
from ads_engine.api.routes import (
    auth,
    config,
    decisions,
    discrepancies,
    health,
    recommendations,
    runs,
    sources,
)
from ads_engine.api.schemas import ErrorDetail, ErrorResponse
from ads_engine.db.repositories import DatasetNotWritable
from ads_engine.db.session import dispose_engine
from ads_engine.decisions import DecisionError, SubjectRetired
from ads_engine.logging import configure_logging, get_logger
from ads_engine.settings import ConfigurationError, Settings, get_settings

__all__ = ["API_PREFIX", "create_app"]

_log = get_logger(__name__)

API_PREFIX: Final = "/api/v1"
"""Base path of every route (AAP 0.5.3); route modules declare paths relative to it."""

APP_TITLE: Final = "Ads Action Engine API"
"""Title of the OpenAPI document."""

_OPENAPI_PATH: Final = f"{API_PREFIX}/openapi.json"
"""Where :func:`_serve_openapi` serves the OpenAPI document to a signed-in session."""

# Routers in the order of the endpoint table. The order fixes the order of the OpenAPI
# paths, and no two modules share a path, so it never decides which route matches.
_ROUTERS: Final[tuple[APIRouter, ...]] = (
    auth.router,
    runs.router,
    recommendations.router,
    decisions.router,
    discrepancies.router,
    sources.router,
    config.router,
    health.router,
)

# Error codes of the envelope that this module, rather than the raising code, chooses.
_INVALID_WINDOW_CODE: Final = "invalid_window"
_SUBJECT_RETIRED_CODE: Final = "subject_retired"
_CONFIG_ERROR_CODE: Final = "config_error"
_VALIDATION_ERROR_CODE: Final = "validation_error"
_INTERNAL_ERROR_CODE: Final = "internal_error"
_INTERNAL_ERROR_MESSAGE: Final = "Internal server error"
_HTTP_ERROR_CODE: Final = "http_error"

# Codes for HTTP errors that Starlette and FastAPI raise themselves, such as an unknown path
# (404) or an unsupported method (405). Routes raise ApiError instead, with their own codes.
_HTTP_STATUS_CODES: Final[Mapping[int, str]] = {
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "rate_limited",
}

# A validation message names at most this many failures, so one malformed body cannot
# produce an unbounded message; the remainder is counted, never listed.
_MAX_VALIDATION_DETAILS: Final = 3
_VALIDATION_FALLBACK_MESSAGE: Final = "Invalid request"

# A config_error message is built from these parts only, never from the error's text: the
# loader's details quote disk paths and YAML source lines, which stay in the server log.
_CONFIG_ERROR_MESSAGE: Final = "Configuration error"
_CONFIG_ERROR_HINT: Final = "Check the API logs for details."
# The logical names a ConfigError's `file` may be named by in a response. Any other value,
# such as the disk path of a missing config directory, is left out of the message.
_PUBLIC_CONFIG_FILES: Final[frozenset[str]] = frozenset((*CONFIG_FILES, CRM_BINDINGS_DOCUMENT))
# A dotted key is named only when it is short and made of ASCII identifiers and `[n]` indexes,
# such as `window.presets` or `assets[1].lead_forms[0]`. A key echoing arbitrary YAML text is
# left out of the message.
_PUBLIC_CONFIG_KEY_MAX_LENGTH: Final = 120
_PUBLIC_CONFIG_KEY_RE: Final = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]*|\[[0-9]{1,9}\])(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]{1,9}\])*"
)


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def _error_response(
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Render ``{"error": {"code": code, "message": message}}`` with ``status_code``.

    Args:
        status_code: The HTTP status of the response.
        code: The machine-readable error code.
        message: The human-readable message; callers never pass an email, a password, a
            secret or a token.
        headers: Extra response headers, for example ``Retry-After`` on a 429.

    Returns:
        The JSON response, built through :class:`~ads_engine.api.schemas.ErrorResponse` so
        every error body has exactly the published shape.
    """
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(),
        headers=None if headers is None else dict(headers),
    )


def _expect[E: Exception](exc: Exception, kind: type[E]) -> E:
    """Narrow the exception a handler received to the class it was registered for.

    Starlette dispatches on the exception's MRO, so the check always passes; it exists to
    give the handler a typed exception without an unchecked cast.

    Args:
        exc: The exception Starlette passed to the handler.
        kind: The class the handler was registered for.

    Returns:
        ``exc``, typed as ``kind``.

    Raises:
        TypeError: When the handler was registered for another class, a wiring defect;
            the catch-all handler then answers 500 ``internal_error``.
    """
    if isinstance(exc, kind):
        return exc
    raise TypeError(f"handler for {kind.__name__} received {type(exc).__name__}") from exc


async def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an :class:`~ads_engine.api.deps.ApiError` with its status, code and headers.

    Args:
        request: The failed request.
        exc: The raised :class:`~ads_engine.api.deps.ApiError`.

    Returns:
        The error envelope with ``exc.status_code`` and ``exc.headers``, for example
        ``Retry-After`` on a 429 ``rate_limited`` login.
    """
    error = _expect(exc, ApiError)
    return _error_response(error.status_code, error.code, error.message, error.headers)


async def _handle_decision_error(request: Request, exc: Exception) -> JSONResponse:
    """Render a domain rejection from ``ads_engine.decisions`` with its fixed status.

    Each subclass fixes its status: ``PatchRejected`` and ``InvalidResolution`` 422,
    ``SubjectRetired`` 409 ``subject_retired``, ``SubjectNotFound`` 404, and
    ``DiscrepancyClosed`` and ``PlatformStateUnknown`` 409.

    Args:
        request: The failed request.
        exc: The raised :class:`~ads_engine.decisions.DecisionError`.

    Returns:
        The error envelope with the exception's status, code and message.
    """
    error = _expect(exc, DecisionError)
    return _error_response(error.status_code, error.code, error.message)


async def _handle_run_in_progress(request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`~ads_engine.analysis.lock.RunInProgress` as 409 ``run_in_progress``.

    Another process (the worker, the CLI or another request) holds the shared ``analysis``
    lock; the request does not wait for it (AAP 0.5.3).

    Args:
        request: The failed request.
        exc: The raised :class:`~ads_engine.analysis.lock.RunInProgress`.

    Returns:
        409 with ``run_in_progress`` and "An analysis run is already in progress".
    """
    error = _expect(exc, RunInProgress)
    return _error_response(http.HTTPStatus.CONFLICT, error.code, error.message)


async def _handle_invalid_window(request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`~ads_engine.analysis.window.InvalidWindow` as 422 with its own code.

    Args:
        request: The failed request.
        exc: The raised :class:`~ads_engine.analysis.window.InvalidWindow`.

    Returns:
        422 with the window's code (``end_before_start``, ``end_after_default_end``,
        ``too_long`` or ``too_short``), or ``invalid_window`` when it carries none.
    """
    error = _expect(exc, InvalidWindow)
    # Read defensively: an InvalidWindow unpickled or built without its code still renders.
    raw_code: object = getattr(error, "code", None)
    code = raw_code if isinstance(raw_code, str) and raw_code else _INVALID_WINDOW_CODE
    raw_message: object = getattr(error, "message", None)
    message = raw_message if isinstance(raw_message, str) and raw_message else str(error)
    return _error_response(http.HTTPStatus.UNPROCESSABLE_CONTENT, code, message)


async def _handle_dataset_not_writable(request: Request, exc: Exception) -> JSONResponse:
    """Render a write on a retired or missing dataset as 409 ``subject_retired``.

    AAP 0.3.2: a decision, resolution or feedback write whose subject belongs to a dataset
    that is no longer active is refused. The repository raises this when a dataset retires
    between the route's own check and its write; the user sees the same answer as the
    route's ``SubjectRetired``, so the screen shows one message for both.

    Args:
        request: The failed request.
        exc: The raised :class:`~ads_engine.db.repositories.DatasetNotWritable`.

    Returns:
        409 with ``subject_retired`` and the message of
        :class:`~ads_engine.decisions.SubjectRetired`.
    """
    error = _expect(exc, DatasetNotWritable)
    _log.warning(
        "api.dataset_not_writable",
        dataset_id=error.dataset_id,
        state=error.state,
        path=request.url.path,
        method=request.method,
    )
    return _error_response(
        http.HTTPStatus.CONFLICT, _SUBJECT_RETIRED_CODE, SubjectRetired.default_message
    )


async def _handle_config_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an invalid ``config/*.yaml`` or stored CRM binding as 500 ``config_error``.

    The error's full text, with its file, dotted key and detail, is logged as
    ``api.config_error`` through the redacting log pipeline, and is never returned. The
    detail can quote disk paths, YAML parser diagnostics and source lines, and the
    ``file`` of a missing config directory is itself a disk path (CWE-209).

    The response message is built from logical parts only: ``Configuration error``, then
    ``in <file>`` when the file is one of the three config files or
    ``source_dataset.crm_bindings``, then ``at <key>`` when the dotted key is a short run of
    ASCII identifiers and ``[n]`` indexes, then a fixed pointer to the API logs. For
    example: ``Configuration error in thresholds.yaml at window.presets. Check the API logs
    for details.``

    Args:
        request: The failed request.
        exc: The raised :class:`~ads_engine.analysis.thresholds.ConfigError`.

    Returns:
        500 with ``config_error`` and the message built from the logical parts.
    """
    error = _expect(exc, ConfigError)
    # Read defensively: a ConfigError built without its attributes still renders.
    raw_file: object = getattr(error, "file", None)
    raw_key: object = getattr(error, "key", None)
    _log.error(
        "api.config_error",
        error=str(error),
        file=raw_file,
        key=raw_key,
        path=request.url.path,
        method=request.method,
    )
    message = _CONFIG_ERROR_MESSAGE
    if isinstance(raw_file, str) and raw_file in _PUBLIC_CONFIG_FILES:
        message += f" in {raw_file}"
    if (
        isinstance(raw_key, str)
        and len(raw_key) <= _PUBLIC_CONFIG_KEY_MAX_LENGTH
        and _PUBLIC_CONFIG_KEY_RE.fullmatch(raw_key) is not None
    ):
        message += f" at {raw_key}"
    return _error_response(
        http.HTTPStatus.INTERNAL_SERVER_ERROR,
        _CONFIG_ERROR_CODE,
        f"{message}. {_CONFIG_ERROR_HINT}",
    )


def _validation_message(errors: Sequence[Any]) -> str:
    """Summarise request validation failures as ``loc: msg`` pairs, never input values.

    Pydantic's ``input`` and ``ctx`` entries are ignored, because the input can be a
    password; only the location and the message template's text are used.

    Args:
        errors: ``RequestValidationError.errors()``.

    Returns:
        Up to three ``loc: msg`` pairs joined by ``"; "``, with a count of the rest, or
        "Invalid request" when nothing usable is reported.
    """
    details: list[str] = []
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        raw_loc: object = error.get("loc", ())
        parts = raw_loc if isinstance(raw_loc, (list, tuple)) else (raw_loc,)
        loc = ".".join(str(part) for part in parts)
        msg = str(error.get("msg", "")).strip() or "invalid"
        details.append(f"{loc}: {msg}" if loc else msg)
    if not details:
        return _VALIDATION_FALLBACK_MESSAGE
    message = "; ".join(details[:_MAX_VALIDATION_DETAILS])
    remaining = len(details) - _MAX_VALIDATION_DETAILS
    if remaining > 0:
        message += f" (and {remaining} more)"
    return message


async def _handle_request_validation(request: Request, exc: Exception) -> JSONResponse:
    """Render a request that fails its schema as 422 ``validation_error``.

    Args:
        request: The failed request.
        exc: The raised :class:`fastapi.exceptions.RequestValidationError`.

    Returns:
        422 with ``validation_error`` and up to three ``loc: msg`` pairs.
    """
    error = _expect(exc, RequestValidationError)
    return _error_response(
        http.HTTPStatus.UNPROCESSABLE_CONTENT,
        _VALIDATION_ERROR_CODE,
        _validation_message(error.errors()),
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Render an HTTP error raised by Starlette or FastAPI in the error envelope.

    These come from the framework, not from the routes: an unknown path (404), a method a
    path does not support (405, with its ``Allow`` header) and similar. The code follows
    the status; the message is the exception's detail, or the status phrase.

    Args:
        request: The failed request.
        exc: The raised :class:`starlette.exceptions.HTTPException`.

    Returns:
        The error envelope with the exception's status and headers.
    """
    error = _expect(exc, StarletteHTTPException)
    code = _HTTP_STATUS_CODES.get(error.status_code, _HTTP_ERROR_CODE)
    detail: object = error.detail
    if isinstance(detail, str) and detail:
        message = detail
    else:
        try:
            message = http.HTTPStatus(error.status_code).phrase
        except ValueError:
            message = "HTTP error"
    return _error_response(error.status_code, code, message, error.headers)


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Answer any other exception with 500 ``internal_error``, logging its traceback.

    The response names nothing about the failure. The log carries the traceback through
    the redaction processors of :mod:`ads_engine.logging`, which strip secrets, tokens and
    emails, plus the path and method, never the body.

    Args:
        request: The failed request.
        exc: The unhandled exception.

    Returns:
        500 with ``internal_error`` and "Internal server error".
    """
    _log.exception(
        "api.unhandled_error",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
    )
    return _error_response(
        http.HTTPStatus.INTERNAL_SERVER_ERROR, _INTERNAL_ERROR_CODE, _INTERNAL_ERROR_MESSAGE
    )


def _register_error_handlers(app: FastAPI) -> None:
    """Install every handler of the error envelope on ``app``.

    Starlette picks the handler of the nearest class in the exception's MRO, so the
    ``ValueError`` subclasses ``InvalidWindow`` and ``ConfigError`` keep their own codes,
    and every ``DecisionError`` subclass shares one handler.

    Args:
        app: The application being built.
    """
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(DecisionError, _handle_decision_error)
    app.add_exception_handler(RunInProgress, _handle_run_in_progress)
    app.add_exception_handler(InvalidWindow, _handle_invalid_window)
    app.add_exception_handler(DatasetNotWritable, _handle_dataset_not_writable)
    app.add_exception_handler(ConfigError, _handle_config_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the configuration at boot and dispose the database pool on shutdown.

    Under uvicorn a missing or invalid ``config/*.yaml`` therefore fails at boot, before
    any request is served. A request never depends on this having run:
    ``httpx.ASGITransport`` runs no lifespan events, and
    :func:`~ads_engine.api.deps.get_thresholds` then loads the files on first use.
    Configuration already stored on ``app.state.thresholds``, for example by a test, is
    kept rather than reloaded.

    Args:
        app: The application being served.

    Yields:
        Control to the server while the application serves requests.

    Raises:
        ads_engine.analysis.thresholds.ConfigError: When the config directory or a file is
            missing or invalid; uvicorn then exits.
    """
    loaded: object = getattr(app.state, "thresholds", None)
    if isinstance(loaded, Thresholds):
        thresholds = loaded
    else:
        thresholds = Thresholds.load()
        app.state.thresholds = thresholds
    configured: object = getattr(app.state, "settings", None)
    secure_cookies = configured.secure_cookies if isinstance(configured, Settings) else None
    _log.info(
        "api.started",
        version=__version__,
        config_hash=thresholds.config_hash,
        secure_cookies=secure_cookies,
    )
    try:
        yield
    finally:
        _log.info("api.stopping")
        await dispose_engine()


# ---------------------------------------------------------------------------
# OpenAPI document
# ---------------------------------------------------------------------------


async def _serve_openapi(request: Request) -> JSONResponse:
    """Serve the generated OpenAPI document at ``GET`` and ``HEAD /api/v1/openapi.json``.

    The document is the contract ``web/src/api/types.ts`` is written against. FastAPI's own
    schema route is disabled, because FastAPI registers it outside the routers, where it
    would answer without a session (AAP 0.5.3 exempts only ``POST /auth/login`` and
    ``GET /healthz``). :func:`create_app` registers this route instead, behind the routers'
    CSRF guard and :func:`~ads_engine.api.deps.require_user`, and leaves it out of the
    document, so the published paths stay exactly the endpoint table.

    Args:
        request: The current request; its application generates the document, which
            FastAPI caches until the routes change.

    Returns:
        The OpenAPI document, the same one ``app.openapi()`` returns.

    Raises:
        ApiError: 401 ``unauthenticated`` without a valid session (from ``require_user``).
    """
    return JSONResponse(request.app.openapi())


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the Ads Action Engine API application.

    uvicorn calls it once per process through ``--factory``::

        uvicorn ads_engine.api.app:create_app --factory --host 0.0.0.0 --port 8000

    Steps, in order:

    1. Configure JSON logging on stdout, redacting the secrets of ``settings`` (idempotent).
    2. Resolve the settings and run :meth:`Settings.check_api_startup`. A plain-``http``
       ``APP_BASE_URL`` on a host other than ``localhost`` or ``127.0.0.1``, or a missing
       ``SESSION_SECRET``, raises here, so uvicorn refuses to start. That error, or an
       environment value that fails validation, is logged as one ``api.startup_failed``
       ERROR event (``error_type`` and the names of the ``fields`` at fault, never their
       values) before it propagates.
    3. Create the application with no Swagger UI, no ReDoc and no FastAPI schema route,
       which would answer without a session. The OpenAPI document is served at
       ``/api/v1/openapi.json`` once the routers are included, behind their CSRF guard and
       the session check, so only a signed-in session reads it.
    4. Install ``SessionMiddleware``: cookie ``ads_engine_session``, HttpOnly,
       ``SameSite=Lax``, ``Max-Age`` 12 h, path ``/``, and ``Secure`` exactly when
       ``APP_BASE_URL`` is ``https://``.
    5. Store the login rate limiter and the settings on ``app.state``. They are created
       here rather than in the lifespan, because test transports run no lifespan.
    6. Include every router under ``/api/v1`` behind the CSRF guard.
    7. Register the error handlers of the ``{"error": {"code", "message"}}`` envelope.

    Example::

        app = create_app()  # settings from the environment
        app = create_app(Settings(session_secret="...", app_base_url="https://ads.example.com"))

    Args:
        settings: The settings to build with; :func:`~ads_engine.settings.get_settings`
            (the cached environment settings) when ``None``.

    Returns:
        The configured application. Each call returns a new application with its own
        login rate limiter.

    Raises:
        ads_engine.settings.ConfigurationError: When the base URL is unsafe or
            ``SESSION_SECRET`` is not set, after ``api.startup_failed`` is logged.
        pydantic.ValidationError: When ``settings`` is ``None`` and an environment value
            cannot be parsed, after ``api.startup_failed`` is logged.
    """
    configure_logging(settings=settings)
    try:
        if settings is None:
            settings = get_settings()
        settings.check_api_startup()
        session_secret = settings.session_secret
        # check_api_startup has already refused a missing or blank SESSION_SECRET; this
        # repeats it so the signing key is typed as present and can never be an empty string.
        if session_secret is None or not session_secret.get_secret_value():
            raise ConfigurationError(
                "SESSION_SECRET is not set; run make init", fields=("session_secret",)
            )
    except (ConfigurationError, ValidationError) as exc:
        # uvicorn reports a factory failure only as a raw traceback, so the refusal is
        # logged first. Field names only: a validation message can echo the input.
        if isinstance(exc, ValidationError):
            fields = sorted(
                {
                    ".".join(str(part) for part in error["loc"])
                    for error in exc.errors(include_url=False, include_input=False)
                }
            )
        else:
            fields = sorted(exc.fields)
        _log.error("api.startup_failed", error_type=type(exc).__name__, fields=fields)
        raise

    app = FastAPI(
        title=APP_TITLE,
        version=__version__,
        lifespan=_lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret.get_secret_value(),
        session_cookie=security.SESSION_COOKIE,
        max_age=security.SESSION_MAX_AGE,
        path="/",
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    app.state.login_limiter = security.LoginRateLimiter()
    app.state.settings = settings

    csrf_guard = [Depends(require_csrf)]
    for router in _ROUTERS:
        app.include_router(router, prefix=API_PREFIX, dependencies=csrf_guard)
    app.add_api_route(
        _OPENAPI_PATH,
        _serve_openapi,
        methods=["GET", "HEAD"],
        dependencies=[*csrf_guard, Depends(require_user)],
        include_in_schema=False,
    )

    _register_error_handlers(app)
    # Counted on the routers themselves: FastAPI includes a router lazily, so `app.routes`
    # holds one entry per included router rather than one per path operation.
    _log.info(
        "api.app_created",
        routes=sum(len(router.routes) for router in _ROUTERS),
        secure_cookies=settings.secure_cookies,
    )
    return app
