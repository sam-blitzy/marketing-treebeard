"""FastAPI application factory of the Ads Action Engine API, mounted at ``/api/v1``.

uvicorn serves the factory, never a module-level application::

    uvicorn ads_engine.api.app:create_app --factory --host 0.0.0.0 --port 8000

:func:`create_app` assembles the whole API:

* **Startup checks**: :meth:`Settings.check_api_startup` refuses a plain-``http``
  ``APP_BASE_URL`` on any host other than ``localhost`` or ``127.0.0.1``, and a missing
  ``SESSION_SECRET``; :func:`~ads_engine.db.session.normalise_database_url`, the engine's
  own rule, refuses a ``DATABASE_URL`` that is malformed or names a driver other than
  psycopg 3, without connecting. That :class:`~ads_engine.settings.ConfigurationError`, or
  a :class:`pydantic.ValidationError` from an unparseable environment value, is logged as
  one ``api.startup_failed`` ERROR event naming the error class, the settings at fault and
  what is wrong, never their values, and marked as logged. It then propagates, so uvicorn
  exits with status 1 before it binds a socket, and the ``sys.excepthook`` that
  :func:`~ads_engine.logging.configure_logging` installs writes no raw traceback after it.
* **Session cookie**: Starlette's ``SessionMiddleware`` signs ``ads_engine_session`` with
  ``SESSION_SECRET``. The cookie is always HttpOnly, ``SameSite=Lax`` and lives 12 h; it is
  ``Secure`` exactly when ``APP_BASE_URL`` is ``https://``.
* **HEAD requests**: ``HEAD`` is answered wherever ``GET`` is, with ``GET``'s status and
  headers and no body (RFC 9110 §9.3.2). It is served as ``GET`` beneath the routes, so the
  OpenAPI document publishes no ``head`` operation, and a 405's ``Allow`` that names ``GET``
  names ``HEAD`` too.
* **Request deadline**: every HTTP request runs under a 45 s deadline. A request still
  running then, typically one waiting on a database that stopped answering, is cancelled
  and answered 503 ``request_timeout`` in the error envelope, and logged as one
  ``api.request_timeout`` WARNING naming its method and path.
* **Routers**: one per route module, in the order of the endpoint table, each under
  ``/api/v1`` and behind :func:`~ads_engine.api.deps.require_csrf`, which requires
  ``X-Requested-With: ads-engine`` on mutating methods and passes safe methods untouched.
* **Error envelope**: every error the API returns, a domain rejection, a validation
  failure, an unknown path or an unexpected crash, is rendered as
  :class:`~ads_engine.api.schemas.ErrorResponse`, ``{"error": {"code", "message"}}``.
* **Lifespan**: loads ``config/*.yaml`` at boot, so a broken file stops uvicorn instead of
  failing the first request, and disposes the database pool on shutdown.

No middleware beyond the session cookie, the HEAD handling and the request deadline is
installed. The SPA is served same-origin by nginx, which also proxies ``/api``, so CORS and
compression have no role here. Swagger UI and ReDoc are disabled: they load CDN assets that
nginx's ``default-src 'self'`` Content-Security-Policy blocks. The OpenAPI document, the
contract ``web/src/api/types.ts`` is written against, is served at ``/api/v1/openapi.json``
to a signed-in session only. FastAPI's own schema route is disabled, because it sits outside
the guarded routers and would answer without a session.

The API reads stored results only. Neither this module nor any module it imports loads
``ads_engine.connectors``, ``ads_engine.sync`` or ``ads_engine.simulation``: all external I/O
belongs to the worker. Importing this module builds no application, reads no settings and
creates no database engine.
"""

import asyncio
import http
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final, get_args

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pydantic_core.core_schema import ErrorType
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
from ads_engine.db.session import dispose_engine, normalise_database_url
from ads_engine.decisions import DecisionError, SubjectRetired
from ads_engine.logging import configure_logging, get_logger, logged_as, mark_logged
from ads_engine.settings import ConfigurationError, Settings, get_settings

__all__ = ["API_PREFIX", "create_app"]

_log = get_logger(__name__)

API_PREFIX: Final = "/api/v1"
"""Base path of every route (AAP 0.5.3); route modules declare paths relative to it."""

APP_TITLE: Final = "Ads Action Engine API"
"""Title of the OpenAPI document."""

_OPENAPI_PATH: Final = f"{API_PREFIX}/openapi.json"
"""Where :func:`_serve_openapi` serves the OpenAPI document to a signed-in session."""

_REQUEST_TIMEOUT_S: Final = 45.0
"""Seconds an HTTP request may run before it is cancelled and answered 503 ``request_timeout``.

The deadline must clear the slowest legitimate request: ``POST /runs`` computes a
custom-window run synchronously, which the product's performance budget allows up to 30 s.
It must also leave room for the answer to reach the client: cancelling a query that is in
flight costs psycopg up to about 10 s more (a cancel request to the server, then a wait for
it) before the 503 can be sent, and nginx gives up on an upstream after its
``proxy_read_timeout`` with a bare 504 that has neither the error envelope nor an API log
line. 45 s plus that unwind lands at about 55 s, inside the 120 s ``web/nginx.conf`` sets for
``/api/`` and inside nginx's 60 s default as well.
"""

_REQUEST_TIMEOUT_CODE: Final = "request_timeout"
_REQUEST_TIMEOUT_MESSAGE: Final = "The request did not complete in time"

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
    ``file`` of a missing config directory is itself a disk path (CWE-209). An error the
    analysis runner already logged in full (a contradicting override fails the run as
    ``analysis_run_failed``) is logged here as a warning naming that event (``logged_as``);
    any other at error level.

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
    owner = logged_as(error)
    if owner is None:
        _log.error(
            "api.config_error",
            error=str(error),
            file=raw_file,
            key=raw_key,
            path=request.url.path,
            method=request.method,
        )
    else:
        _log.warning(
            "api.config_error",
            error=str(error),
            file=raw_file,
            key=raw_key,
            path=request.url.path,
            method=request.method,
            logged_as=owner,
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


def _allow_with_head(headers: Mapping[str, str]) -> dict[str, str]:
    """Return ``headers`` with ``HEAD`` added to an ``Allow`` that names ``GET`` but not ``HEAD``.

    A route's 405 names only the methods it declares, while :class:`_HeadAsGetMiddleware`
    serves ``HEAD`` wherever ``GET`` is served. Every other header, and an ``Allow`` that
    names no ``GET``, is returned unchanged.

    Example::

        _allow_with_head({"Allow": "GET"})  # {"Allow": "GET, HEAD"}
        _allow_with_head({"Allow": "POST"})  # {"Allow": "POST"}

    Args:
        headers: The headers of a 405 the framework raised.

    Returns:
        A new mapping with the same headers, the ``Allow`` value extended where needed.
    """
    adjusted = dict(headers)
    for name, value in headers.items():
        if name.lower() != "allow":
            continue
        methods = [part.strip().upper() for part in value.split(",") if part.strip()]
        if "GET" in methods and "HEAD" not in methods:
            adjusted[name] = ", ".join((*methods, "HEAD"))
    return adjusted


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Render an HTTP error raised by Starlette or FastAPI in the error envelope.

    These come from the framework, not from the routes: an unknown path (404), a method a
    path does not support (405, with its ``Allow`` header) and similar. The code follows
    the status; the message is the exception's detail, or the status phrase. A 405's
    ``Allow`` that names ``GET`` also names ``HEAD``, which is served wherever ``GET`` is.

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
    headers: Mapping[str, str] | None = error.headers
    if error.status_code == http.HTTPStatus.METHOD_NOT_ALLOWED and headers is not None:
        headers = _allow_with_head(headers)
    return _error_response(error.status_code, code, message, headers)


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Answer any other exception with 500 ``internal_error``, logging its traceback.

    The response names nothing about the failure. The log carries the traceback through
    the redaction processors of :mod:`ads_engine.logging`, which strip secrets, tokens and
    emails, plus the path and method, never the body. The exception is then marked logged
    (:func:`~ads_engine.logging.mark_logged`), so uvicorn's own record of it, made when
    Starlette re-raises it after this response, is dropped. An exception its owner already
    logged in full (the analysis runner's ``analysis_run_failed``) is logged as a warning
    naming that event (``logged_as``), without a second traceback.

    Args:
        request: The failed request.
        exc: The unhandled exception.

    Returns:
        500 with ``internal_error`` and "Internal server error".
    """
    owner = logged_as(exc)
    if owner is None:
        _log.exception(
            "api.unhandled_error",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
        )
        mark_logged(exc, "api.unhandled_error")
    else:
        _log.warning(
            "api.unhandled_error",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            logged_as=owner,
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
# Request deadline
# ---------------------------------------------------------------------------


class _RequestDeadlineMiddleware:
    """Bound every HTTP request by a deadline, so none hangs without an answer or a log line.

    A pure ASGI middleware: a request still running when the deadline passes, for example
    one waiting on a pooled connection to a database that stopped answering, is cancelled
    through :func:`asyncio.timeout`. When no response has started, it is answered 503
    ``request_timeout`` in the error envelope. It is logged either way as one
    ``api.request_timeout`` WARNING with the method, the path without its query string, the
    deadline and whether the response had already started. A response already under way is
    never started a second time; the server then closes the connection.

    Only the deadline's own expiry is handled. A ``TimeoutError`` the application raised for
    another reason, and any cancellation from outside (a client disconnect, a server
    shutdown), propagate unchanged. Lifespan and websocket scopes pass through untouched.

    Args:
        app: The ASGI application it wraps.
        timeout: The deadline in seconds.
    """

    def __init__(self, app: ASGIApp, *, timeout: float) -> None:
        self.app = app
        self.timeout = timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one ASGI connection, answering 503 ``request_timeout`` past the deadline.

        Args:
            scope: The connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def send_tracked(message: Message) -> None:
            """Forward ``message``, noting when the response has started."""
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        deadline = asyncio.timeout(self.timeout)
        try:
            async with deadline:
                await self.app(scope, receive, send_tracked)
        except TimeoutError:
            if not deadline.expired():
                raise
            _log.warning(
                "api.request_timeout",
                method=scope.get("method"),
                path=scope.get("path"),
                timeout_s=self.timeout,
                response_started=response_started,
            )
            if response_started:
                return
            response = _error_response(
                http.HTTPStatus.SERVICE_UNAVAILABLE,
                _REQUEST_TIMEOUT_CODE,
                _REQUEST_TIMEOUT_MESSAGE,
            )
            await response(scope, receive, send)


# ---------------------------------------------------------------------------
# HEAD requests
# ---------------------------------------------------------------------------


class _HeadAsGetMiddleware:
    """Answer ``HEAD`` wherever ``GET`` is served, with ``GET``'s status and headers, no body.

    RFC 9110 §9.3.2 has a server answer ``HEAD`` exactly as it would ``GET``, without the
    content, but FastAPI registers a path operation for its declared methods only, so every
    ``@router.get`` route would refuse ``HEAD`` with 405. Declaring ``HEAD`` on the routes
    would publish a second OpenAPI operation per path; this pure ASGI middleware serves it
    beneath the routes instead, so the document keeps exactly the endpoint table.

    For an ``http`` scope whose method is ``HEAD``, the wrapped application is called with a
    shallow copy of the scope whose method is ``GET``; the server's own scope is never
    modified and still says ``HEAD``. ``http.response.start`` is forwarded unchanged, so the
    status and every header (``Content-Type``, ``Content-Length``, ``Set-Cookie``, ``Allow``,
    ``Retry-After``) are ``GET``'s. Every body chunk is dropped, and the application's last
    body message is replaced by one empty final one. Any other message is forwarded as sent.

    Everything inside it, the session cookie, the CSRF guard, the routes and their error
    handlers, sees ``GET``. A ``HEAD`` therefore meets the session check exactly as a ``GET``
    does, and a ``HEAD`` on a path that serves no ``GET``, such as ``POST /auth/login``, is
    405 with that route's own ``Allow``. Responses rendered outside it keep their body: the
    request deadline's 503 ``request_timeout`` and Starlette's server-error 500
    ``internal_error``, which also log the client's real method. The HTTP server drops those
    bodies on the wire, because it reads the method from its own scope (uvicorn's h11 and
    httptools protocols send no content for ``HEAD``). Other methods, and lifespan and
    websocket scopes, pass through with the original receive and send channels.

    Args:
        app: The ASGI application it wraps.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one ASGI connection, answering ``HEAD`` as ``GET`` without its body.

        Args:
            scope: The connection scope; never modified.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != "http" or scope.get("method") != "HEAD":
            await self.app(scope, receive, send)
            return

        async def send_without_body(message: Message) -> None:
            """Forward ``message``, replacing the response body with one empty final message."""
            if message["type"] != "http.response.body":
                await send(message)
            elif not message.get("more_body", False):
                await send({"type": "http.response.body", "body": b"", "more_body": False})

        await self.app({**scope, "method": "GET"}, receive, send_without_body)


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
    """Serve the generated OpenAPI document at ``GET /api/v1/openapi.json``.

    The document is the contract ``web/src/api/types.ts`` is written against. FastAPI's own
    schema route is disabled, because FastAPI registers it outside the routers, where it
    would answer without a session (AAP 0.5.3 exempts only ``POST /auth/login`` and
    ``GET /healthz``). :func:`create_app` registers this route instead, behind the routers'
    CSRF guard and :func:`~ads_engine.api.deps.require_user`, and leaves it out of the
    document, so the published paths stay exactly the endpoint table. ``HEAD`` is answered
    as on every other ``GET`` route, by :class:`_HeadAsGetMiddleware`.

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

# Error types whose message pydantic renders from its own template, which never quotes the
# input. Any other type's message is a validator's own text (``value_error``,
# ``assertion_error`` or a custom type), which may quote its input. The Settings validators
# raise only fixed, value-free text (``settings.VALIDATOR_MESSAGES``); every quoted literal in
# such a message is withheld all the same, as a safeguard.
_TEMPLATE_ERROR_TYPES: Final[frozenset[str]] = frozenset(get_args(ErrorType)) - {
    "value_error",
    "assertion_error",
}
# A quoted literal in a validator's own message, as Python's repr quotes a string: single or
# double quotes, with backslash escapes. Each is withheld, since it may be the input.
_QUOTED_LITERAL_RE: Final = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")
_WITHHELD_VALUE: Final = "<value withheld>"


def _startup_failure_message(exc: ConfigurationError | ValidationError) -> str:
    """Return what a startup refusal tells the operator: what is wrong, never a value.

    A :class:`~ads_engine.settings.ConfigurationError` keeps its own text: a fixed sentence
    per check such as "SESSION_SECRET is not set; run make init", or the ``DATABASE_URL``
    rule's, which names at most the driver. A
    :class:`pydantic.ValidationError` gives one ``NAME: message`` per failure, joined by
    ``"; "``, where ``NAME`` is the environment variable (the field's upper-case name) and
    the message is pydantic's, built without the input, URL or context. In a validator's
    own message every quoted literal becomes ``<value withheld>``, since a validator may
    quote its input. The log pipeline then redacts the text like every other value.

    Example::

        _startup_failure_message(error)
        # 'GOOGLE_DAILY_CALL_BUDGET: Input should be a valid integer, unable to parse
        #  string as an integer'

    Args:
        exc: The refusal ``create_app`` caught.

    Returns:
        The message for the ``api.startup_failed`` event.
    """
    if isinstance(exc, ConfigurationError):
        return str(exc)
    details: list[str] = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        name = ".".join(str(part) for part in error["loc"]).upper() or "SETTINGS"
        message = error["msg"]
        if error["type"] not in _TEMPLATE_ERROR_TYPES:
            message = _QUOTED_LITERAL_RE.sub(_WITHHELD_VALUE, message)
        details.append(f"{name}: {message}")
    return "; ".join(details)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the Ads Action Engine API application.

    uvicorn calls it once per process through ``--factory``::

        uvicorn ads_engine.api.app:create_app --factory --host 0.0.0.0 --port 8000

    Steps, in order:

    1. Configure JSON logging on stdout, redacting the secrets of ``settings``, and the
       ``sys.excepthook`` that keeps an uncaught exception out of the stream as a raw
       traceback (idempotent).
    2. Resolve the settings, run :meth:`Settings.check_api_startup`, then parse
       ``DATABASE_URL`` with :func:`~ads_engine.db.session.normalise_database_url`, which
       never connects. A plain-``http`` ``APP_BASE_URL`` on a host other than ``localhost``
       or ``127.0.0.1``, a missing ``SESSION_SECRET``, or a malformed or non-psycopg
       ``DATABASE_URL`` (``fields`` ``["database_url"]``) raises here, so uvicorn refuses
       to start; a well-formed URL of an unreachable server does not. That error, or an
       environment value that fails validation, is logged as one ``api.startup_failed``
       ERROR event before it propagates: ``error_type``, the names of the ``fields`` at
       fault and a ``message`` saying what is wrong, never a value. The error is marked as
       logged (:func:`~ads_engine.logging.mark_logged`), so when it ends the uvicorn
       process (exit status 1) nothing else is written about it.
    3. Create the application with no Swagger UI, no ReDoc and no FastAPI schema route,
       which would answer without a session. The OpenAPI document is served at
       ``/api/v1/openapi.json`` once the routers are included, behind their CSRF guard and
       the session check, so only a signed-in session reads it.
    4. Install ``SessionMiddleware``: cookie ``ads_engine_session``, HttpOnly,
       ``SameSite=Lax``, ``Max-Age`` 12 h, path ``/``, and ``Secure`` exactly when
       ``APP_BASE_URL`` is ``https://``.
    5. Install the HEAD handling around it (:class:`_HeadAsGetMiddleware`): a ``HEAD`` is
       served from the session cookie inwards as ``GET``, and answered with ``GET``'s status
       and headers and no body.
    6. Install the request deadline around that, inside only Starlette's own server-error
       middleware: an HTTP request still running after :data:`_REQUEST_TIMEOUT_S` is
       cancelled, answered 503 ``request_timeout`` and logged as ``api.request_timeout``.
    7. Store the login rate limiter and the settings on ``app.state``. They are created
       here rather than in the lifespan, because test transports run no lifespan.
    8. Include every router under ``/api/v1`` behind the CSRF guard, then the OpenAPI
       document's ``GET`` route.
    9. Register the error handlers of the ``{"error": {"code", "message"}}`` envelope; a
       405's ``Allow`` that names ``GET`` names ``HEAD`` too.

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
        ads_engine.settings.ConfigurationError: When the base URL is unsafe,
            ``SESSION_SECRET`` is not set, or ``DATABASE_URL`` is malformed or names a
            driver other than ``postgresql+psycopg``, after ``api.startup_failed`` is
            logged and the error marked as logged.
        pydantic.ValidationError: When ``settings`` is ``None`` and an environment value
            cannot be parsed, after ``api.startup_failed`` is logged and the error marked
            as logged.
    """
    configure_logging(settings=settings)
    try:
        if settings is None:
            settings = get_settings()
        settings.check_api_startup()
        try:
            # Parsed only, never connected: an unreachable server still lets the API start.
            normalise_database_url(settings.database_url.get_secret_value())
        except ValueError as err:
            # The rule's message names DATABASE_URL and at most its driver, never the URL;
            # the parse error is not chained, because the URL carries the password.
            raise ConfigurationError(str(err), fields=("database_url",)) from None
        session_secret = settings.session_secret
        # check_api_startup has already refused a missing or blank SESSION_SECRET; this
        # repeats it so the signing key is typed as present and can never be an empty string.
        if session_secret is None or not session_secret.get_secret_value():
            raise ConfigurationError(
                "SESSION_SECRET is not set; run make init", fields=("session_secret",)
            )
    except (ConfigurationError, ValidationError) as exc:
        # uvicorn catches only a factory's TypeError, so this error leaves the process
        # through sys.excepthook. It is logged here in full, with a message that names what
        # is wrong and never a value, and marked as logged: the hook configure_logging
        # installed then writes nothing more, so no raw traceback follows this line.
        if isinstance(exc, ValidationError):
            fields = sorted(
                {
                    ".".join(str(part) for part in error["loc"])
                    for error in exc.errors(include_url=False, include_input=False)
                }
            )
        else:
            fields = sorted(exc.fields)
        _log.error(
            "api.startup_failed",
            error_type=type(exc).__name__,
            fields=fields,
            message=_startup_failure_message(exc),
        )
        mark_logged(exc, "api.startup_failed")
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
    # Around the session cookie and inside the deadline: everything from the session cookie
    # inwards sees a HEAD as GET, while the deadline's log names the client's real method.
    app.add_middleware(_HeadAsGetMiddleware)
    # Added last, so it is the outermost user middleware: its deadline covers the HEAD
    # handling, the session cookie, the routers and their error handlers. Only Starlette's
    # server-error middleware, which renders the catch-all 500, sits outside it.
    app.add_middleware(_RequestDeadlineMiddleware, timeout=_REQUEST_TIMEOUT_S)
    app.state.login_limiter = security.LoginRateLimiter()
    app.state.settings = settings

    csrf_guard = [Depends(require_csrf)]
    for router in _ROUTERS:
        app.include_router(router, prefix=API_PREFIX, dependencies=csrf_guard)
    app.add_api_route(
        _OPENAPI_PATH,
        _serve_openapi,
        methods=["GET"],
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
