"""FastAPI application factory of the Ads Action Engine API, mounted at ``/api/v1``.

uvicorn serves the factory, never a module-level application::

    uvicorn ads_engine.api.app:create_app --factory --host 0.0.0.0 --port 8000

:func:`create_app` assembles the whole API:

* **Startup checks**: :meth:`Settings.check_api_startup` refuses a plain-``http``
  ``APP_BASE_URL`` on any host other than ``localhost`` or ``127.0.0.1``, a missing
  ``SESSION_SECRET``, and one too weak to sign cookies (shorter than 32 characters, of fewer
  than 8 distinct characters, or a repeated run);
  :func:`~ads_engine.db.session.normalise_database_url`, the engine's own rule, refuses a
  ``DATABASE_URL`` that is malformed or names a driver other than psycopg 3, without
  connecting. That :class:`~ads_engine.settings.ConfigurationError`, or
  a :class:`pydantic.ValidationError` from an unparseable environment value, is logged as
  one ``api.startup_failed`` ERROR event naming the error class, the settings at fault and
  what is wrong, never their values, and marked as logged. A ``ValidationError`` is told as
  the worker and the CLI tell it: an ``errors`` list of ``{variable, problem}``, each
  problem from the value-free allowlist
  :func:`~ads_engine.settings.settings_error_problem`. It then propagates, so uvicorn
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
  running then, typically one waiting on a database that stopped answering, is answered
  503 ``request_timeout`` in the error envelope at the deadline itself, and logged as one
  ``api.request_timeout`` WARNING naming its method and path. It is cancelled, and its
  cleanup finishes in the background without delaying the answer.
* **No caching**: every HTTP response carries exactly one ``Cache-Control: no-store``, so no
  browser or shared cache keeps an API answer, signed-in evidence, decisions and identity
  included, after sign-out (CWE-525). :class:`_NoStoreMiddleware`, the outermost user
  middleware, sets it on every answer, the request deadline's 503 included; the error
  envelope carries it as well, so Starlette's server-error 500, sent outside every user
  middleware, has it too.
* **Routers**: one per route module, in the order of the endpoint table, each under
  ``/api/v1`` and behind :func:`~ads_engine.api.deps.require_csrf`, which requires
  ``X-Requested-With: ads-engine`` on mutating methods and passes safe methods untouched.
* **Error envelope**: every error the API returns, a domain rejection, a validation
  failure, an unknown path or an unexpected crash, is rendered as
  :class:`~ads_engine.api.schemas.ErrorResponse`, ``{"error": {"code", "message"}}``.
* **Lifespan**: loads ``config/*.yaml`` at boot, so a broken file stops uvicorn instead of
  failing the first request, and starts the analysis process in which ``POST /runs``
  computes (:func:`~ads_engine.analysis.runner.start_analysis_process`); one that cannot
  start is logged and the API serves on, the next ``POST /runs`` spawning it. On shutdown
  it waits, within a bound, for the requests the deadline answered to finish their cleanup,
  stops the analysis process, then disposes the database pool.

No middleware beyond the session cookie, the HEAD handling, the request deadline and the
no-store cache policy is installed. The SPA is served same-origin by nginx, which also
proxies ``/api``, so CORS and compression have no role here. Swagger UI and ReDoc are
disabled: they load CDN assets that nginx's ``default-src 'self'`` Content-Security-Policy
blocks. The OpenAPI document, the contract ``web/src/api/types.ts`` is written against, is
served at ``/api/v1/openapi.json`` to a signed-in session only. FastAPI's own schema route is
disabled, because it sits outside the guarded routers and would answer without a session.

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
from typing import Any, Final

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ads_engine import __version__
from ads_engine.analysis import runner
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
from ads_engine.logging import configure_logging, get_logger, logged_as, mark_logged, redact_emails
from ads_engine.settings import ConfigurationError, Settings, get_settings, settings_error_problem

__all__ = ["API_PREFIX", "create_app"]

_log = get_logger(__name__)

API_PREFIX: Final = "/api/v1"
"""Base path of every route (AAP 0.5.3); route modules declare paths relative to it."""

APP_TITLE: Final = "Ads Action Engine API"
"""Title of the OpenAPI document."""

_OPENAPI_PATH: Final = f"{API_PREFIX}/openapi.json"
"""Where :func:`_serve_openapi` serves the OpenAPI document to a signed-in session."""

_REQUEST_TIMEOUT_S: Final = 45.0
"""Seconds an HTTP request may run before it is answered 503 ``request_timeout`` and cancelled.

The deadline must clear the slowest legitimate request: ``POST /runs`` computes a
custom-window run synchronously, which the product's performance budget allows up to 30 s.
It must also sit inside nginx's ``proxy_read_timeout``, after which nginx gives up on an
upstream with a bare 504 that has neither the error envelope nor an API log line. The 503 is
sent at the deadline itself, so 45 s is inside the 120 s ``web/nginx.conf`` sets for
``/api/`` and inside nginx's 60 s default as well. The cancelled request's cleanup finishes in
the background after the answer: on a server that stopped answering, cancelling a query in
flight costs psycopg up to about 10 s (a cancel request to the server, then a wait for it)
before it closes the connection, which :data:`_REQUEST_UNWIND_DRAIN_S` bounds at shutdown.
"""

_REQUEST_UNWIND_DRAIN_S: Final = 15.0
"""Seconds the shutdown waits for requests answered at the deadline to finish their cleanup.

Such a request keeps unwinding after its 503: psycopg spends up to 5 s on a cancel request to
a server that stopped answering and 5 s more waiting for the query to end before it closes
the connection, then the session's rollback and close run. 15 s covers that worst case of
about 10 s with room to spare. The database pool is disposed once every cleanup has
finished, or once the bound passes with some still running, which is then logged as
``api.request_unwinds_pending``.
"""

_REQUEST_TIMEOUT_CODE: Final = "request_timeout"
_REQUEST_TIMEOUT_MESSAGE: Final = "The request did not complete in time"

# The cache policy of every response, set by _NoStoreMiddleware and by _error_response. API
# bodies are the signed-in operator's evidence, decisions and identity, which no browser or
# shared cache may keep after sign-out (CWE-525); `no-cache` would still let a cache store them.
_CACHE_CONTROL_HEADER: Final = "Cache-Control"
_CACHE_CONTROL_NO_STORE: Final = "no-store"

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

    Every error envelope carries ``Cache-Control: no-store``. :class:`_NoStoreMiddleware`
    sets it on every answer that passes through it, but Starlette's server-error middleware
    sends the catch-all 500 straight to the server, outside every user middleware, so the
    envelope carries the header itself.

    Args:
        status_code: The HTTP status of the response.
        code: The machine-readable error code.
        message: The human-readable message; callers never pass an email, a password, a
            secret or a token.
        headers: Extra response headers, for example ``Retry-After`` on a 429 or ``Allow``
            on a 405; a ``Cache-Control`` among them is replaced by ``no-store``.

    Returns:
        The JSON response, built through :class:`~ads_engine.api.schemas.ErrorResponse` so
        every error body has exactly the published shape, with ``headers`` and exactly one
        ``Cache-Control: no-store``.
    """
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    merged = {
        name: value
        for name, value in (headers or {}).items()
        if name.lower() != _CACHE_CONTROL_HEADER.lower()
    }
    merged[_CACHE_CONTROL_HEADER] = _CACHE_CONTROL_NO_STORE
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=merged)


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
        ``start_before_earliest``, ``too_long`` or ``too_short``), or ``invalid_window``
        when it carries none.
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
# Cache policy
# ---------------------------------------------------------------------------


class _NoStoreMiddleware:
    """Mark every HTTP response ``Cache-Control: no-store``, so no cache keeps an API answer.

    Every body the API sends belongs to the signed-in operator: recommendation and CRM
    evidence, decisions, the admin's identity. Logout revokes the session, so a saved cookie
    cannot be replayed, but it cannot reach a copy a browser or a shared cache already stored;
    nothing may be stored at all (CWE-525). ``no-cache`` would not do, because it still lets a
    cache store the body and only makes it revalidate before reuse.

    A pure ASGI middleware, installed as the outermost user middleware. On an ``http`` scope's
    ``http.response.start`` it sets exactly one ``Cache-Control: no-store``, replacing any
    value the application sent, then forwards the message; the status, the body and every
    other header pass unchanged. It covers every routed answer and every error the handlers
    render, ``HEAD`` answers, login and logout with their ``Set-Cookie``, and the request
    deadline's 503 ``request_timeout``. Starlette's server-error middleware, which renders the
    catch-all 500 ``internal_error``, sits outside it and sends straight to the server; that
    answer carries the header because :func:`_error_response` adds it to every error envelope.
    Lifespan and websocket scopes pass through untouched.

    Args:
        app: The ASGI application it wraps.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one ASGI connection, marking its response ``Cache-Control: no-store``.

        Args:
            scope: The connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            """Forward ``message``, setting a response start's ``Cache-Control`` to ``no-store``."""
            if message["type"] == "http.response.start":
                # ASGI makes `headers` optional; MutableHeaders edits the message's own list,
                # and its assignment replaces every existing Cache-Control with one value.
                message.setdefault("headers", [])
                MutableHeaders(scope=message)[_CACHE_CONTROL_HEADER] = _CACHE_CONTROL_NO_STORE
            await send(message)

        await self.app(scope, receive, send_no_store)


# ---------------------------------------------------------------------------
# Request deadline
# ---------------------------------------------------------------------------


class _RequestDeadlineMiddleware:
    """Bound every HTTP request by a deadline, so none hangs without an answer or a log line.

    A pure ASGI middleware. The wrapped application runs in a task of its own, which the
    middleware waits on for at most the deadline:

    * **Finished in time**: its outcome propagates unchanged. A ``TimeoutError`` the
      application raised for another reason stays an ordinary error, and a cancellation of
      its own stays a cancellation.
    * **Deadline passed**: a request still running then, for example one waiting on a pooled
      connection to a database that stopped answering, is let go. Its send channel is closed
      first, so anything it sends afterwards is dropped, and it is cancelled. It is logged at
      once as one ``api.request_timeout`` WARNING with the method, the path without its query
      string, the deadline and whether the response had already started. When no response
      has started, it is answered 503 ``request_timeout`` in the error envelope at once,
      without waiting for the cancellation to unwind: psycopg spends up to about 10 s
      cancelling a query on a server that stopped answering. A response already under way is
      never started a second time; the server then closes the connection.
    * **Cancelled from outside** (a client disconnect, a server shutdown): the request is
      cancelled and waited for until it has finished unwinding, exactly as if it had run in
      the caller's task, so a graceful shutdown still covers its cleanup; then the
      cancellation propagates, and the middleware sends nothing of its own.

    A request the middleware stops waiting for, past the deadline or on a second outside
    cancellation, is held in ``unwinding`` until it has finished, so it is neither garbage
    collected mid-cleanup nor forgotten by the lifespan's drain. Its outcome is always
    retrieved: a cancellation is the expected end and stays silent, and any other exception
    is logged once as ``api.request_unwind_failed`` with the method, the path and the error
    class, never the exception's text, at ERROR level, or as a WARNING naming the event that
    already logged it (``logged_as``). Lifespan and websocket scopes pass through untouched.

    Args:
        app: The ASGI application it wraps.
        timeout: The deadline in seconds.
        unwinding: The set holding every request still unwinding after the middleware let it
            go; :func:`create_app` shares it with the lifespan through ``app.state``, which
            waits for it before disposing the database pool. A set of its own when omitted.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        timeout: float,
        unwinding: set[asyncio.Task[None]] | None = None,
    ) -> None:
        self.app = app
        self.timeout = timeout
        self.unwinding: set[asyncio.Task[None]] = set() if unwinding is None else unwinding

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one ASGI connection, answering 503 ``request_timeout`` past the deadline.

        Args:
            scope: The connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.

        Raises:
            asyncio.CancelledError: When the request was cancelled from outside, once the
                application has finished unwinding, or at once on a second cancellation.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False
        send_closed = False

        async def send_tracked(message: Message) -> None:
            """Forward ``message``, noting a response start; drop it once the channel closed."""
            nonlocal response_started
            if send_closed:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def serve() -> None:
            """Run the wrapped application on the tracked send channel."""
            await self.app(scope, receive, send_tracked)

        request = asyncio.create_task(serve())
        try:
            await asyncio.wait({request}, timeout=self.timeout)
        except asyncio.CancelledError:
            # Held before the wait, so a second cancellation that cuts the wait short still
            # leaves the request tracked and its outcome retrieved.
            self._hold_until_unwound(request, scope)
            request.cancel()
            await asyncio.wait({request})
            raise
        if request.done():
            # Finished in time, a finish on the deadline's own boundary included: the outcome
            # propagates unchanged.
            await request
            return
        send_closed = True
        request.cancel()
        self._hold_until_unwound(request, scope)
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

    def _hold_until_unwound(self, request: asyncio.Task[None], scope: Scope) -> None:
        """Hold ``request`` in :attr:`unwinding` until it ends, then retrieve its outcome.

        Args:
            request: The task running the wrapped application, cancelled or about to be.
            scope: Its connection scope, whose method and path name a failure in the log.
        """
        method: object = scope.get("method")
        path: object = scope.get("path")
        unwinding = self.unwinding

        def settle(ended: asyncio.Task[None]) -> None:
            """Release ``ended``, logging an exception other than its cancellation once."""
            unwinding.discard(ended)
            if ended.cancelled():
                return
            error = ended.exception()
            if error is None:
                return
            owner = logged_as(error)
            if owner is None:
                _log.error(
                    "api.request_unwind_failed",
                    method=method,
                    path=path,
                    error_type=type(error).__name__,
                )
                mark_logged(error, "api.request_unwind_failed")
            else:
                _log.warning(
                    "api.request_unwind_failed",
                    method=method,
                    path=path,
                    error_type=type(error).__name__,
                    logged_as=owner,
                )

        unwinding.add(request)
        request.add_done_callback(settle)


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


async def _drain_request_unwinds(app: FastAPI) -> None:
    """Wait for the requests the deadline let go to finish unwinding, within a bound.

    uvicorn's shutdown waits only for its own request tasks, and a request answered 503 at the
    deadline has already left its task while its cleanup (psycopg's query cancellation, the
    session's rollback and close) runs on. Waiting here keeps that cleanup ahead of
    :func:`~ads_engine.db.session.dispose_engine`. After :data:`_REQUEST_UNWIND_DRAIN_S` the
    wait gives up: the number still running is logged as one ``api.request_unwinds_pending``
    WARNING and the shutdown proceeds. An application without the shared set, or with none
    pending, returns at once.

    Args:
        app: The application being shut down; its ``app.state.request_unwinds`` is the set
            :func:`create_app` gave the request deadline.
    """
    held: object = getattr(app.state, "request_unwinds", None)
    if not isinstance(held, set) or not held:
        return
    _, pending = await asyncio.wait(set(held), timeout=_REQUEST_UNWIND_DRAIN_S)
    if pending:
        _log.warning(
            "api.request_unwinds_pending",
            pending=len(pending),
            timeout_s=_REQUEST_UNWIND_DRAIN_S,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the configuration and start the analysis process at boot; drain and stop on shutdown.

    Under uvicorn a missing or invalid ``config/*.yaml`` therefore fails at boot, before
    any request is served. A request never depends on this having run:
    ``httpx.ASGITransport`` runs no lifespan events, and
    :func:`~ads_engine.api.deps.get_thresholds` then loads the files on first use.
    Configuration already stored on ``app.state.thresholds``, for example by a test, is
    kept rather than reloaded.

    The analysis process, in which every ``POST /runs`` computes, is spawned once the
    configuration loaded (:func:`~ads_engine.analysis.runner.start_analysis_process`), so
    the first custom run does not wait for it. One that cannot start is logged as
    ``api.analysis_process_unavailable`` with its error class only, and the API serves on:
    the next ``POST /runs`` spawns it.

    On shutdown the requests answered 503 at the request deadline are given up to
    :data:`_REQUEST_UNWIND_DRAIN_S` to finish their cleanup (:func:`_drain_request_unwinds`).
    The analysis process is stopped after that (a run still computing there is awaited),
    and the database pool is disposed last; each step runs even when the one before it is
    cut short.

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
        await runner.start_analysis_process()
    except BaseException as exc:
        # Interpreter exits and cancellation propagate; any other failure leaves the API
        # serving, and the next POST /runs spawns the analysis process.
        if not isinstance(exc, Exception):
            raise
        _log.warning("api.analysis_process_unavailable", error=type(exc).__name__)
    try:
        yield
    finally:
        _log.info("api.stopping")
        try:
            await _drain_request_unwinds(app)
        finally:
            try:
                await runner.stop_analysis_process()
            finally:
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


def _startup_failure_details(exc: ConfigurationError | ValidationError) -> dict[str, object]:
    """Return what a startup refusal tells the operator: what is wrong, never a value.

    A :class:`~ads_engine.settings.ConfigurationError` gives only a ``message``, its own
    text: a fixed sentence per check such as "SESSION_SECRET is not set; run make init", or
    the ``DATABASE_URL`` rule's, which names at most the driver.

    A :class:`pydantic.ValidationError` gives the summary ``message``
    ``invalid settings (<N> error[s])``, ``error_count`` and ``errors``: per error, in
    pydantic's order, the ``variable`` (the error's ``loc`` joined with dots and upper-cased,
    as the environment spells it; ``SETTINGS`` for an error of the whole model) and a
    ``problem`` from :func:`~ads_engine.settings.settings_error_problem`, the value-free
    allowlist the worker and the CLI report with, stripped of the example email addresses a
    validator's message names. Each problem has a key of its own and never follows its
    variable's name in prose: the log pipeline reads text after an email-named key such as
    ``APP_ADMIN_EMAIL:`` as that key's value and redacts it, which would garble the problem.

    Example::

        _startup_failure_details(error)
        # {'message': 'invalid settings (1 error)', 'error_count': 1, 'errors': [
        #     {'variable': 'APP_ADMIN_EMAIL', 'problem': 'Value error, APP_ADMIN_EMAIL must
        #      be an email address such as [REDACTED_EMAIL]'}]}

    Args:
        exc: The refusal ``create_app`` caught.

    Returns:
        The ``api.startup_failed`` event's fields beyond ``error_type`` and ``fields``.
    """
    if isinstance(exc, ConfigurationError):
        return {"message": str(exc)}
    errors = [
        {
            "variable": (".".join(str(part) for part in error["loc"]) or "settings").upper(),
            "problem": redact_emails(settings_error_problem(error)),
        }
        for error in exc.errors(include_url=False, include_input=False)
    ]
    count = exc.error_count()
    return {
        "message": f"invalid settings ({count} error{'s' if count != 1 else ''})",
        "error_count": count,
        "errors": errors,
    }


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
       or ``127.0.0.1``, a missing ``SESSION_SECRET`` or one too weak to sign cookies
       (shorter than 32 characters, of fewer than 8 distinct characters, or a repeated
       run), or a malformed or non-psycopg ``DATABASE_URL`` (``fields``
       ``["database_url"]``) raises here, so uvicorn refuses to start; a well-formed URL
       of an unreachable server does not. That error, or an environment value that fails
       validation, is logged as one ``api.startup_failed`` ERROR event before it
       propagates: ``error_type``, the names of the ``fields`` at fault and a ``message``
       saying what is wrong, never a value. For a validation failure the ``message`` is the
       summary ``invalid settings (<N> error[s])``, and ``error_count`` and ``errors``, one
       ``{variable, problem}`` per error in pydantic's order, say what is wrong with each
       variable, from the allowlist :func:`~ads_engine.settings.settings_error_problem`.
       The error is marked as logged (:func:`~ads_engine.logging.mark_logged`), so when it
       ends the uvicorn process (exit status 1) nothing else is written about it.
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
    6. Install the request deadline around that: an HTTP request still running after
       :data:`_REQUEST_TIMEOUT_S` is answered 503 ``request_timeout`` at once, logged as
       ``api.request_timeout`` and cancelled. Its cleanup unwinds in the background, held in
       the set stored as ``app.state.request_unwinds``, which the lifespan drains before it
       disposes the database pool.
    7. Install the cache policy around that (:class:`_NoStoreMiddleware`), inside only
       Starlette's own server-error middleware: every HTTP response carries exactly one
       ``Cache-Control: no-store``, the deadline's 503 included. The error envelope carries
       it too, so the catch-all 500 that the server-error middleware renders has it as well.
    8. Store the login rate limiter and the settings on ``app.state``. They are created
       here rather than in the lifespan, because test transports run no lifespan.
    9. Include every router under ``/api/v1`` behind the CSRF guard, then the OpenAPI
       document's ``GET`` route.
    10. Register the error handlers of the ``{"error": {"code", "message"}}`` envelope; a
        405's ``Allow`` that names ``GET`` names ``HEAD`` too.

    Example::

        app = create_app()  # settings from the environment
        app = create_app(
            Settings(session_secret=secrets.token_hex(32), app_base_url="https://ads.example.com")
        )

    Args:
        settings: The settings to build with; :func:`~ads_engine.settings.get_settings`
            (the cached environment settings) when ``None``.

    Returns:
        The configured application. Each call returns a new application with its own
        login rate limiter.

    Raises:
        ads_engine.settings.ConfigurationError: When the base URL is unsafe,
            ``SESSION_SECRET`` is not set or is too weak (shorter than 32 characters, of
            fewer than 8 distinct characters, or a repeated run), or ``DATABASE_URL`` is
            malformed or names a driver other than ``postgresql+psycopg``, after
            ``api.startup_failed`` is logged and the error marked as logged.
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
            **_startup_failure_details(exc),
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
    # Around the HEAD handling and inside only the cache policy: its deadline covers the HEAD
    # handling, the session cookie, the routers and their error handlers. The requests it
    # lets go unwind in a set the lifespan drains on shutdown.
    request_unwinds: set[asyncio.Task[None]] = set()
    app.state.request_unwinds = request_unwinds
    app.add_middleware(
        _RequestDeadlineMiddleware, timeout=_REQUEST_TIMEOUT_S, unwinding=request_unwinds
    )
    # Added last, so it is the outermost user middleware: its Cache-Control: no-store covers
    # every answer inside it, the deadline's 503 included. Only Starlette's server-error
    # middleware, which renders the catch-all 500, sits outside it; _error_response puts the
    # header on that answer.
    app.add_middleware(_NoStoreMiddleware)
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
