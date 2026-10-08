"""The ``ads-engine`` command-line interface: setup, sync, analysis and contract captures.

Commands (``ads-engine <command> --help`` for each):

=====================  ==========================================================================
``hash-password``      Read a password from stdin and print its argon2id hash, nothing else. Loads
                       no settings and touches no database, so ``make init`` can run it in the
                       backend image before ``.env`` exists.
``migrate``            Upgrade the database to the newest Alembic revision; idempotent. Run from
                       ``backend/`` on the host, or ``/app`` in the image.
``ensure-admin``       Create or update the single admin login from ``APP_ADMIN_EMAIL`` and
                       ``APP_ADMIN_PASSWORD_HASH``, removing any other login; idempotent.
``sync --source S``    Run one source's sync jobs now, against its active dataset, and print the
                       report as JSON.
``analyze``            Compute a custom-window analysis run from stored data
                       (``--start YYYY-MM-DD --end YYYY-MM-DD``) under the shared analysis lock.
``seed-simulated``     Build, backfill and activate simulated datasets for every source, then
                       compute the default-window run.
``capture-fixtures``   Record one redacted live request and response per contract surface of a
                       source into ``tests/contracts/{source}/captured/``.
=====================  ==========================================================================

Exit codes: :data:`EXIT_OK` (0) success; :data:`EXIT_FAILURE` (1) a failure, including every
expected domain error (configuration, settings validation, decision rejections and the
connector errors ``AccessDenied``, ``TransientError`` and ``QueryDefect``); :data:`EXIT_USAGE`
(2) a usage error, argparse's own or ours (an unparsable date, a window out of range, a
missing ``alembic.ini`` or contracts directory); :data:`EXIT_LOCK_HELD` (3) another process
holds the shared ``analysis`` lock.

Streams: structured JSON logs go to **stderr** (``configure_logging(stream=sys.stderr)``), so
stdout carries only command output: the password hash, JSON reports and written file paths.
Neither stream ever carries a secret, a token or an email address.

Imports: the analysis, sync, connector, simulation and Alembic modules are imported inside the
command that needs them, so ``hash-password`` runs in a minimal environment and ``--help``
stays fast. The CLI only reads from the platforms (the product is recommend-only) and holds
no Google OAuth flow: Google is reached solely through the n8n webhook.

Example::

    $ printf '%s' 'correct horse' | ads-engine hash-password
    $argon2id$v=19$m=65536,t=3,p=4$...
    $ cd backend && ads-engine migrate && ads-engine ensure-admin
    $ ads-engine analyze --start 2026-09-01 --end 2026-09-28
    {"kind": "custom", "run_id": 41, "status": "succeeded", ...}
"""

import argparse
import asyncio
import dataclasses
import getpass
import json
import re
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast
from urllib.parse import parse_qsl, unquote, urlencode

import httpx
from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError
from pydantic import ValidationError

from ads_engine import __version__
from ads_engine.db.models.ops import SOURCES, AppUser
from ads_engine.logging import EMAIL_RE, configure_logging, get_logger, redact
from ads_engine.settings import SOURCES as SETTINGS_SOURCES
from ads_engine.settings import ConfigurationError, Settings, Source, get_settings

if TYPE_CHECKING:
    from ads_engine.analysis.thresholds import Thresholds
    from ads_engine.connectors.google_n8n.client import CallBudget
    from ads_engine.connectors.linkedin.client import LinkedInClient
    from ads_engine.simulation.payloads import JSONValue, RequestShape

__all__ = [
    "EXIT_FAILURE",
    "EXIT_LOCK_HELD",
    "EXIT_OK",
    "EXIT_USAGE",
    "build_parser",
    "main",
]

log = get_logger(__name__)

# ---------------------------------------------------------------------------------------------
# Exit codes and shared constants
# ---------------------------------------------------------------------------------------------

EXIT_OK: Final = 0
"""The command succeeded."""

EXIT_FAILURE: Final = 1
"""The command failed: an expected domain error (one line on stderr) or an unexpected one
(logged as ``cli_failed`` with its traceback, redacted)."""

EXIT_USAGE: Final = 2
"""A usage error: argparse's own, or a bad date, an out-of-range window, or a missing
``alembic.ini`` or contracts directory."""

EXIT_LOCK_HELD: Final = 3
"""Another process holds the shared ``analysis`` lock (AAP 0.5.3: the CLI exits with 3)."""

_PROG: Final = "ads-engine"
_LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_ALEMBIC_INI: Final = "alembic.ini"
_ARGON2_PREFIX: Final = "$argon2"
_NO_ADMIN_HASH_MESSAGE: Final = (
    "APP_ADMIN_PASSWORD_HASH is not set; run `make init ADMIN_PASSWORD=…`"
)

if tuple(SOURCES) != tuple(SETTINGS_SOURCES):  # pragma: no cover - guarded by the model tests
    # `sync --source` and `capture-fixtures --source` offer the schema's sources and narrow
    # them to the settings' Source type; a renamed source must fail at import, not at run time.
    raise RuntimeError("ads_engine.db.models.ops.SOURCES and ads_engine.settings.SOURCES differ")


class _UsageError(Exception):
    """A usage error the CLI detects after argparse: the command exits with :data:`EXIT_USAGE`."""


# Expected domain errors, by defining module and class name. The CLI prints one line for them
# and exits with EXIT_FAILURE instead of logging a traceback. They are looked up in
# sys.modules rather than imported: an exception class whose module was never loaded cannot
# have been raised, so recognising them costs no import. That keeps the heavy connector and
# analysis packages out of `hash-password` and `--help`, and reaches the decision errors that
# sync's baseline writes can raise without making the CLI import the decisions module.
_LAZY_DOMAIN_ERRORS: Final[tuple[tuple[str, str], ...]] = (
    ("ads_engine.connectors.errors", "ConnectorError"),
    ("ads_engine.analysis.thresholds", "ConfigError"),
    ("ads_engine.decisions", "DecisionError"),
)
_EAGER_DOMAIN_ERRORS: Final[tuple[type[Exception], ...]] = (ConfigurationError,)

type _Handler = Callable[[argparse.Namespace], int]


# ---------------------------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------------------------


def _json_default(value: object) -> object:
    """Encode the non-JSON values command reports carry: dates, instants and decimals.

    Raises:
        TypeError: For any other type, naming it.
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _emit_json(payload: object) -> None:
    """Print one JSON report to stdout: sorted keys, two-space indent, trailing newline."""
    print(json.dumps(payload, indent=2, sort_keys=True, default=_json_default), flush=True)


def _error(message: str) -> None:
    """Print one line to stderr: ``ads-engine: <message>``, whitespace collapsed.

    Messages the CLI writes itself are printed as written; text that comes from an exception
    or a platform passes through :func:`_redacted` first.
    """
    print(f"{_PROG}: {' '.join(message.split())}", file=sys.stderr, flush=True)


def _redacted(text: str) -> str:
    """Redact text that did not originate in the CLI: exception and platform messages.

    :func:`ads_engine.logging.redact` removes email addresses, URL passwords, Bearer tokens
    and values assigned to credential keys, so none reaches the terminal even when a message
    carried one.
    """
    return str(redact(text))


def _domain_error_message(exc: Exception) -> str | None:
    """Return the one-line message of an expected domain error, or ``None`` for any other.

    Args:
        exc: The exception a command raised.

    Returns:
        ``"<ClassName>: <message>"`` for an expected error. A settings ``ValidationError``
        names the invalid fields only, never their values.
    """
    if isinstance(exc, ValidationError):
        if exc.title != Settings.__name__:
            # Only the environment's validation is a user error; any other model's is a defect.
            return None
        fields = sorted(
            {
                ".".join(str(part) for part in error["loc"]) or "settings"
                for error in exc.errors(include_url=False, include_input=False)
            }
        )
        count = exc.error_count()
        return (
            f"invalid settings: {', '.join(fields)} ({count} error{'s' if count != 1 else ''}); "
            "check the environment variables of those names"
        )
    expected = isinstance(exc, _EAGER_DOMAIN_ERRORS)
    if not expected:
        for module_name, class_name in _LAZY_DOMAIN_ERRORS:
            module = sys.modules.get(module_name)
            error_type: object = getattr(module, class_name, None) if module is not None else None
            if isinstance(error_type, type) and isinstance(exc, error_type):
                expected = True
                break
    if not expected:
        return None
    return f"{type(exc).__name__}: {exc}"


def _system_exit_code(exc: SystemExit) -> int:
    """Translate argparse's ``SystemExit`` into the code :func:`main` returns."""
    code = exc.code
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    return EXIT_USAGE


def _run_async[T](factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one command coroutine in a fresh event loop and close the database pool after it.

    The process-wide engine is created lazily inside the loop on first use, and its async
    connections belong to that loop, so it is disposed before the loop closes.

    Args:
        factory: Builds the coroutine to run.

    Returns:
        The coroutine's result.
    """
    from ads_engine.db.session import dispose_engine

    async def run() -> T:
        try:
            return await factory()
        finally:
            await dispose_engine()

    return asyncio.run(run())


def _as_source(value: str) -> Source:
    """Narrow a validated ``--source`` choice to :data:`ads_engine.settings.Source`.

    Raises:
        _UsageError: When ``value`` names no source.
    """
    for source in SETTINGS_SOURCES:
        if value == source:
            return source
    raise _UsageError(f"unknown source {value!r}; expected one of {', '.join(SETTINGS_SOURCES)}")


def _iso_date(text: str) -> date:
    """Parse an argparse ``YYYY-MM-DD`` value with :meth:`datetime.date.fromisoformat`.

    Raises:
        argparse.ArgumentTypeError: When the text is not an ISO calendar date; argparse then
            exits with :data:`EXIT_USAGE`.
    """
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date in YYYY-MM-DD form") from None


# ---------------------------------------------------------------------------------------------
# hash-password
# ---------------------------------------------------------------------------------------------


def _read_password() -> str:
    """Read the password: all of stdin, less exactly one trailing newline (LF or CRLF).

    On an interactive terminal the password is read with :func:`getpass.getpass` instead, so
    it is not echoed; the prompt goes to the terminal, never to stdout.

    Raises:
        _UsageError: When the password is empty.
    """
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
    else:
        password = sys.stdin.read()
        if password.endswith("\r\n"):
            password = password[:-2]
        elif password.endswith("\n"):
            password = password[:-1]
    if not password:
        raise _UsageError("the password read from stdin is empty")
    return password


def _cmd_hash_password(_args: argparse.Namespace) -> int:
    """``hash-password``: print the argon2id hash of the password read from stdin.

    The hash uses argon2-cffi's defaults (argon2id, RFC 9106 low-memory profile), the same
    parameters ``api/security.py`` verifies with, so a freshly initialised admin never needs
    a rehash. Only the hash and a newline reach stdout; ``make init`` captures it into
    ``.env`` as ``APP_ADMIN_PASSWORD_HASH``. No settings are loaded and no database is used.
    """
    password = _read_password()
    print(PasswordHasher().hash(password), flush=True)
    return EXIT_OK


# ---------------------------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------------------------


def _cmd_migrate(_args: argparse.Namespace) -> int:
    """``migrate``: upgrade the database to the newest revision (``alembic upgrade head``).

    ``alembic.ini`` is read from the working directory: ``backend/`` on the host, ``/app`` in
    the image. ``migrations/env.py`` takes the URL from ``get_settings().database_url`` (it is
    never placed in the Alembic config) and drives the async engine with ``asyncio.run``, so
    this runs synchronously, outside any event loop. ``configure_logger`` is switched off, so
    the ini's logging sections never replace the structlog JSON logging. A database already
    at head is left unchanged, which makes the command idempotent.

    Raises:
        _UsageError: When ``alembic.ini`` is not in the working directory.
    """
    ini = Path.cwd() / _ALEMBIC_INI
    if not ini.is_file():
        raise _UsageError(
            f"{_ALEMBIC_INI} not found in the working directory; "
            "run from backend/ (or /app in the image)"
        )
    from alembic import command
    from alembic.config import Config

    config = Config(str(ini))
    config.attributes["configure_logger"] = False
    log.info("migrate_started", target="head")
    command.upgrade(config, "head")
    log.info("migrate_finished", target="head")
    return EXIT_OK


# ---------------------------------------------------------------------------------------------
# ensure-admin
# ---------------------------------------------------------------------------------------------


def _admin_hash(settings: Settings) -> str | None:
    """Return the configured admin hash once it is a well-formed argon2 hash.

    The hash is checked, never shown: a missing, non-argon2 or malformed value prints why to
    stderr and yields ``None``, so a truncated value can never lock the admin out.
    """
    secret = settings.app_admin_password_hash
    if secret is None:
        _error(_NO_ADMIN_HASH_MESSAGE)
        return None
    value = secret.get_secret_value().strip()
    if not value.startswith(_ARGON2_PREFIX):
        _error(
            "APP_ADMIN_PASSWORD_HASH is not an argon2 hash (it must start with $argon2); "
            "write it with `ads-engine hash-password` or `make init ADMIN_PASSWORD=…`"
        )
        return None
    try:
        extract_parameters(value)
    except InvalidHashError:
        _error(
            "APP_ADMIN_PASSWORD_HASH is not a valid argon2 hash; write it again with "
            "`ads-engine hash-password`"
        )
        return None
    return value


async def _ensure_admin() -> int:
    """Upsert the admin by email and remove any other login, in one transaction.

    ``INSERT … ON CONFLICT (email) DO UPDATE SET password_hash = excluded.password_hash``
    makes the command idempotent. The product has a single admin login, so rows with another
    email (an admin whose ``APP_ADMIN_EMAIL`` was changed) are deleted in the same unit of
    work: the replaced login can no longer sign in, and its sessions stop validating because
    their user no longer exists (``api/deps.py``). No other table references ``app_user``.
    """
    from sqlalchemy import delete
    from sqlalchemy.dialects.postgresql import insert

    from ads_engine.db.session import session_scope

    settings = get_settings()
    password_hash = _admin_hash(settings)
    if password_hash is None:
        return EXIT_FAILURE
    email = settings.app_admin_email
    insert_admin = insert(AppUser).values(email=email, password_hash=password_hash)
    upsert = insert_admin.on_conflict_do_update(
        index_elements=[AppUser.email],
        set_={"password_hash": insert_admin.excluded.password_hash},
    ).returning(AppUser.id)
    async with session_scope() as session:
        user_id: int = (await session.execute(upsert)).scalar_one()
        removal = delete(AppUser).where(AppUser.email != email).returning(AppUser.id)
        replaced = (await session.execute(removal)).scalars().all()
    # The email is not logged: logs never carry addresses (the redaction would only blank it).
    log.info("admin_ensured", user_id=user_id, replaced_logins=len(replaced))
    return EXIT_OK


def _cmd_ensure_admin(_args: argparse.Namespace) -> int:
    """``ensure-admin``: create or update the admin from the environment; idempotent."""
    return _run_async(_ensure_admin)


# ---------------------------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------------------------


async def _sync(source: Source) -> int:
    """Run ``source``'s jobs through the scheduler's entry point and print the report.

    ``run_source_jobs`` resolves the active dataset's mode (live or simulated), runs the
    source's jobs now, records ``dataset_freshness`` and queues an ``analysis_request`` for a
    job that succeeded on an active dataset. The report carries jobs, rows, completeness,
    mode and reason, never a credential.

    Returns:
        :data:`EXIT_FAILURE` when a job failed on a ``QueryDefect``; :data:`EXIT_OK`
        otherwise, including when transient errors left datasets stale (the report says so).
    """
    from ads_engine.sync.jobs import run_source_jobs

    report = await run_source_jobs(source, trigger="manual")
    _emit_json(report.to_json())
    if report.has_defect:
        _error(f"sync {source}: a job failed on a query defect; see the report and the log")
        return EXIT_FAILURE
    return EXIT_OK


def _cmd_sync(args: argparse.Namespace) -> int:
    """``sync --source S``: run one source's sync jobs now."""
    source = _as_source(args.source)
    return _run_async(lambda: _sync(source))


# ---------------------------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------------------------


class _RunRow(Protocol):
    """The ``analysis_run`` columns a command reports; ``AnalysisRun`` satisfies it."""

    @property
    def id(self) -> int:
        """The run id."""
        ...

    @property
    def kind(self) -> str:
        """``default`` or ``custom``."""
        ...

    @property
    def status(self) -> str:
        """``running``, ``succeeded`` or ``failed``."""
        ...

    @property
    def window_start(self) -> date:
        """The first native date of the window."""
        ...

    @property
    def window_end(self) -> date:
        """The last native date of the window."""
        ...


def _run_summary(run: _RunRow) -> dict[str, object]:
    """The JSON summary of a persisted ``analysis_run``."""
    return {
        "run_id": run.id,
        "kind": run.kind,
        "status": run.status,
        "window_start": run.window_start,
        "window_end": run.window_end,
    }


async def _analyze(start: date, end: date) -> int:
    """Compute and persist one custom-window run from stored data.

    The window is validated as ``POST /runs`` validates it (``AnalysisWindow.custom`` through
    ``analysis.runner.custom_window``): the end no later than the default window end and a
    span of 1 to ``window.max_days`` days, from ``Thresholds.load()``. The run is ``custom``
    even when the window equals the default. ``run_analysis`` takes the shared ``analysis``
    lock itself, the one lock the API, the worker and the CLI share, so it is never taken here.

    Returns:
        :data:`EXIT_OK` with the run summary printed; :data:`EXIT_USAGE` for an invalid
        window; :data:`EXIT_LOCK_HELD` while another process holds the lock.
    """
    from ads_engine.analysis.lock import RunInProgress
    from ads_engine.analysis.runner import custom_window, run_analysis
    from ads_engine.analysis.thresholds import Thresholds
    from ads_engine.analysis.window import InvalidWindow

    thresholds = Thresholds.load()
    try:
        window = await custom_window(start, end, thresholds=thresholds)
    except InvalidWindow as exc:
        _error(f"invalid window ({exc.code}): {exc.message}")
        return EXIT_USAGE
    try:
        run = await run_analysis(window, "custom", "manual", thresholds=thresholds)
    except RunInProgress:
        _error("an analysis run is in progress")
        return EXIT_LOCK_HELD
    _emit_json(_run_summary(run))
    return EXIT_OK


def _cmd_analyze(args: argparse.Namespace) -> int:
    """``analyze --start --end``: compute a custom-window run."""
    start: date = args.start
    end: date = args.end
    return _run_async(lambda: _analyze(start, end))


# ---------------------------------------------------------------------------------------------
# seed-simulated
# ---------------------------------------------------------------------------------------------


async def _seed_simulated() -> int:
    """Give every source an active simulated dataset, then compute the default-window run.

    ``bootstrap_sources(settings, force_simulated=True)`` builds, backfills and activates a
    simulated primary dataset per source (with the companion CRM dataset for LinkedIn and
    the baseline decisions of the first entity sync); a source already active and simulated
    is kept. It is safe while the worker runs, because ``DatasetManager.activate`` serialises
    activations per source. The default-window run then reads the seeded data.

    Returns:
        :data:`EXIT_LOCK_HELD` when the analysis lock is held (the seeding stands);
        :data:`EXIT_FAILURE` when a source failed to bootstrap (the others are seeded and
        analysed); :data:`EXIT_OK` otherwise.
    """
    from ads_engine.analysis.lock import RunInProgress
    from ads_engine.analysis.runner import default_window, run_analysis
    from ads_engine.analysis.thresholds import Thresholds
    from ads_engine.sync.mode import bootstrap_sources

    settings = get_settings()
    report = await bootstrap_sources(settings, force_simulated=True)
    sources: dict[str, dict[str, object]] = {}
    for source in SETTINGS_SOURCES:
        resolved = report.modes.get(source)
        active = report.active.get(source)
        sources[source] = {
            "mode": None if resolved is None else resolved.mode,
            "reason": None if resolved is None else resolved.reason,
            "dataset_id": None if active is None else active.dataset_id,
            "dataset_mode": None if active is None else active.mode,
            "companion_id": None if active is None else active.companion_id,
            "failure": report.failures.get(source),
        }
    output: dict[str, object] = {"sources": sources, "run": None}
    thresholds = Thresholds.load()
    window = await default_window(thresholds=thresholds)
    try:
        run = await run_analysis(window, "default", "manual", thresholds=thresholds)
    except RunInProgress:
        _emit_json(output)
        _error("an analysis run is in progress; the simulated datasets are seeded")
        return EXIT_LOCK_HELD
    output["run"] = _run_summary(run)
    _emit_json(output)
    if report.failures:
        _error(f"seed-simulated: bootstrap failed for {', '.join(sorted(report.failures))}")
        return EXIT_FAILURE
    return EXIT_OK


def _cmd_seed_simulated(_args: argparse.Namespace) -> int:
    """``seed-simulated``: seed simulated datasets for every source and analyse them."""
    return _run_async(_seed_simulated)


# ---------------------------------------------------------------------------------------------
# capture-fixtures: recording transport
# ---------------------------------------------------------------------------------------------

# Hop-by-hop and framing headers: they describe the connection, not the contract, and the
# Host header would name the n8n workspace of the webhook.
_REQUEST_FRAMING_HEADERS: Final = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}
)
# The recorded body is decoded, so the replayed response must not claim an encoding or length.
_RESPONSE_FRAMING_HEADERS: Final = frozenset(
    {"content-encoding", "content-length", "transfer-encoding"}
)
_RESPONSE_EXTENSIONS: Final[tuple[str, ...]] = ("http_version", "reason_phrase")


@dataclasses.dataclass(frozen=True, slots=True)
class _Exchange:
    """One live request and its response, as sent and received (unredacted, in memory only).

    Attributes:
        method: The HTTP method.
        url: The full URL, query included.
        headers: The request headers in send order.
        body: The raw request body (``b""`` when there is none).
        status: The response status.
        content: The decoded response body.
        captured_at: When the response arrived, in UTC.
    """

    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    status: int
    content: bytes
    captured_at: datetime

    @property
    def ok(self) -> bool:
        """Whether the platform answered 2xx (HubSpot's 207 multi-status included)."""
        return 200 <= self.status < 300

    def header(self, name: str) -> str | None:
        """A request header by case-insensitive name, or ``None``."""
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None

    def shape(self) -> RequestShape:
        """The request as the contract router reads it (``payloads.RequestShape``)."""
        from ads_engine.simulation.payloads import RequestShape

        return RequestShape(
            method=self.method, url=self.url, headers=dict(self.headers), body=self.body
        )


class _RecordingTransport(httpx.AsyncBaseTransport):
    """An httpx transport that sends through ``inner`` and records every exchange.

    The connector clients run unchanged on top of it: ``build_client(source,
    transport=_RecordingTransport(httpx.AsyncHTTPTransport()))``. Each response body is read
    in full, recorded, and handed back to the client as an equivalent in-memory response, so
    retries, error classification and parsing behave exactly as on the plain network
    transport. Recorded exchanges hold raw values (tokens, emails) and live only in memory;
    nothing is written before :func:`_redact_capture` has run on it.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self._exchanges: list[_Exchange] = []

    @property
    def exchanges(self) -> tuple[_Exchange, ...]:
        """Every recorded exchange, in the order the responses arrived."""
        return tuple(self._exchanges)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` through the inner transport and record the exchange.

        Args:
            request: The request the client built.

        Returns:
            An in-memory response with the same status, headers (less framing headers) and
            decoded body as the network response.
        """
        body = await request.aread()
        response = await self._inner.handle_async_request(request)
        try:
            content = await response.aread()
        finally:
            await response.aclose()
        self._exchanges.append(
            _Exchange(
                method=request.method,
                url=str(request.url),
                headers=tuple(request.headers.multi_items()),
                body=body,
                status=response.status_code,
                content=content,
                captured_at=datetime.now(UTC),
            )
        )
        headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in _RESPONSE_FRAMING_HEADERS
        ]
        extensions: dict[str, Any] = {
            key: response.extensions[key]
            for key in _RESPONSE_EXTENSIONS
            if key in response.extensions
        }
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            content=content,
            extensions=extensions,
        )

    async def aclose(self) -> None:
        """Close the inner transport."""
        await self._inner.aclose()


class _LocalCallBudget:
    """A per-process Google call budget for captures made while the database is unreachable.

    It satisfies ``google_n8n.client.CallBudget``. The product's shared cap lives in
    ``source_connection``; without the database these calls cannot be counted there, so the
    capture is held to one call per GAQL template and a warning says the calls went unrecorded.
    """

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._used = 0

    async def acquire(self) -> None:
        """Admit one webhook POST attempt, or refuse it once the local cap is used.

        Raises:
            CallBudgetExhausted: When ``cap`` attempts were already admitted.
        """
        from ads_engine.connectors.errors import CallBudgetExhausted

        if self._used >= self._cap:
            raise CallBudgetExhausted(
                f"capture-fixtures local cap of {self._cap} webhook calls reached",
                endpoint="search",
            )
        self._used += 1


# ---------------------------------------------------------------------------------------------
# capture-fixtures: redaction
# ---------------------------------------------------------------------------------------------

_MARKER: Final = "<redacted>"
"""The redaction marker; root ``.gitleaks.toml`` allowlists it under ``backend/tests/contracts``."""

# The marker as one percent-encoded query value, which keeps the URL parseable.
_ENCODED_MARKER: Final = "%3Credacted%3E"

# The n8n webhook URL is a capability URL that is never logged or committed. Every Google
# request goes to it, so every Google capture gets this reserved placeholder (RFC 2606
# `.invalid`), which keeps the https scheme the search contract checks.
_REDACTED_WEBHOOK_URL: Final = "https://redacted.invalid/webhook/redacted"

# Credential values in request bodies and query strings, and in token responses.
_CREDENTIAL_KEYS: Final = frozenset({"access_token", "refresh_token", "client_secret", "client_id"})
# An OAuth authorization code is a request parameter only; HubSpot and LinkedIn error and
# multi-status bodies use `code` for error codes, which the contracts need intact.
_REQUEST_CREDENTIAL_KEYS: Final = _CREDENTIAL_KEYS | {"code"}
# Person-name and contact keys, in LinkedIn (camelCase) and HubSpot (lowercase) spelling.
_PERSON_KEYS: Final = frozenset(
    {"firstName", "lastName", "firstname", "lastname", "fullName", "phone", "mobilephone", "email"}
)
# LinkedIn lead-form answers: free-text personal data, blanked whole.
_ANSWER_DETAILS_KEY: Final = "answerDetails"
_AUTHORIZATION: Final = "authorization"
_FORM_MEDIA_TYPE: Final = "application/x-www-form-urlencoded"
# A configured secret at least this long is replaced wherever it occurs inside a string; a
# shorter one only where it is the whole string, so a short value never cuts ids or text apart.
_MIN_SUBSTRING_SECRET: Final = 8


def _blank(node: JSONValue, *, numbers: bool) -> JSONValue:
    """Replace every string leaf of ``node`` with the marker, keeping its structure.

    Keys stay, so a blanked answer or name object still has its contract shape.

    Args:
        node: The JSON value to blank.
        numbers: Also blank numeric leaves (a phone number sent as a number); booleans and
            ``null`` are kept either way.

    Returns:
        A new value with every string (and, with ``numbers``, every number) replaced.
    """
    if isinstance(node, dict):
        return {key: _blank(item, numbers=numbers) for key, item in node.items()}
    if isinstance(node, list):
        return [_blank(item, numbers=numbers) for item in node]
    if isinstance(node, str):
        return _MARKER
    if numbers and isinstance(node, int | float) and not isinstance(node, bool):
        return _MARKER
    return node


@dataclasses.dataclass(frozen=True, slots=True)
class _RedactionPolicy:
    """What a capture must lose before it is written: secrets, tokens, people and accounts.

    Attributes:
        secrets: The plaintext of every configured secret and the n8n webhook URL, longest
            first.
        header_names: Lower-cased request headers whose values are credentials:
            ``Authorization`` and the configured n8n secret header.
        identifiers: Patterns of the configured Google customer id and LinkedIn ad account
            ids, each with its same-length replacement of zeros. Those ids are host secrets
            that must never be committed; zeros keep every id, URN and resource name
            well-formed, so a capture still satisfies its contract.
        identifier_values: The same ids as text; a JSON number equal to one becomes ``0``.
    """

    secrets: tuple[str, ...]
    header_names: frozenset[str]
    identifiers: tuple[tuple[re.Pattern[str], str], ...]
    identifier_values: frozenset[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> _RedactionPolicy:
        """Build the policy from the configured secrets, header name and account ids."""
        secrets = set(settings.secret_values())
        if settings.n8n_google_ads_webhook_url:
            secrets.add(settings.n8n_google_ads_webhook_url)
        ids = list(settings.linkedin_ad_account_ids)
        if settings.google_ads_customer_id:
            ids.append(settings.google_ads_customer_id)
        identifiers = tuple(
            (re.compile(rf"(?<![0-9]){re.escape(value)}(?![0-9])"), "0" * len(value))
            for value in sorted(set(ids), key=len, reverse=True)
        )
        return cls(
            secrets=tuple(sorted(secrets, key=len, reverse=True)),
            header_names=frozenset(
                {_AUTHORIZATION, settings.n8n_webhook_secret_header.strip().lower()}
            ),
            identifiers=identifiers,
            identifier_values=frozenset(ids),
        )

    def text(self, value: str) -> str:
        """Redact one string: configured secrets, account ids, then email addresses."""
        for secret in self.secrets:
            if value == secret:
                return _MARKER
            if len(secret) >= _MIN_SUBSTRING_SECRET and secret in value:
                value = value.replace(secret, _MARKER)
        for pattern, replacement in self.identifiers:
            value = pattern.sub(replacement, value)
        return EMAIL_RE.sub(_MARKER, value)

    def value(self, node: JSONValue, keys: frozenset[str]) -> JSONValue:
        """Redact a JSON body recursively.

        Values under ``keys`` (credentials and person keys) are blanked, numbers included;
        every ``answerDetails`` is blanked whole; dict keys and every other string go through
        :meth:`text`; a number equal to a configured account id becomes ``0``.

        Args:
            node: The decoded JSON body or a part of it.
            keys: The keys whose values are blanked.

        Returns:
            The redacted copy.
        """
        if isinstance(node, dict):
            redacted: dict[str, JSONValue] = {}
            for key, item in node.items():
                if key in keys:
                    redacted[self.text(key)] = _blank(item, numbers=True)
                elif key == _ANSWER_DETAILS_KEY:
                    redacted[key] = _blank(item, numbers=False)
                else:
                    redacted[self.text(key)] = self.value(item, keys)
            return redacted
        if isinstance(node, list):
            return [self.value(item, keys) for item in node]
        if isinstance(node, str):
            return self.text(node)
        if (
            isinstance(node, int)
            and not isinstance(node, bool)
            and str(node) in self.identifier_values
        ):
            # An ad account id sent as a JSON number (LinkedIn's adAccounts `id`).
            return 0
        return node

    def url(self, url: str, source: Source) -> str:
        """Redact a request URL: the webhook URL, credential query values, ids and secrets."""
        if source == "google":
            return _REDACTED_WEBHOOK_URL
        base, separator, query = url.partition("?")
        pairs: list[str] = []
        for pair in query.split("&") if separator else ():
            name, equals, _value = pair.partition("=")
            if equals and unquote(name) in _REQUEST_CREDENTIAL_KEYS:
                pairs.append(f"{name}={_ENCODED_MARKER}")
            else:
                pairs.append(pair)
        rebuilt = base + (separator + "&".join(pairs) if separator else "")
        return self.text(rebuilt)

    def headers(self, headers: Sequence[tuple[str, str]]) -> dict[str, JSONValue]:
        """Redact request headers; framing headers are dropped.

        ``Authorization`` keeps its scheme (``Bearer <redacted>``), because the contracts
        require a Bearer credential; the n8n secret header becomes the marker.
        """
        redacted: dict[str, JSONValue] = {}
        for name, value in headers:
            lowered = name.lower()
            if lowered in _REQUEST_FRAMING_HEADERS:
                continue
            if lowered == _AUTHORIZATION:
                scheme, space, _credential = value.strip().partition(" ")
                redacted[name] = f"{scheme} {_MARKER}" if space else _MARKER
            elif lowered in self.header_names:
                redacted[name] = _MARKER
            else:
                redacted[name] = self.text(value)
        return redacted

    def form(self, body: str) -> str:
        """Redact a form-encoded body (the LinkedIn OAuth token request)."""
        pairs = [
            (name, _MARKER if name in _REQUEST_CREDENTIAL_KEYS else self.text(value))
            for name, value in parse_qsl(body, keep_blank_values=True)
        ]
        return urlencode(pairs)


def _decode_json(content: bytes) -> tuple[JSONValue, bool]:
    """Decode a body as JSON.

    Returns:
        ``(value, True)``, or ``(None, False)`` when the body is not UTF-8 JSON.
    """
    try:
        return cast("JSONValue", json.loads(content)), True
    except ValueError:  # json.JSONDecodeError and UnicodeDecodeError both derive from it
        return None, False


def _media_type(headers: Mapping[str, JSONValue]) -> str:
    """The lower-cased media type of a recorded ``Content-Type`` header, or ``""``."""
    for name, value in headers.items():
        if name.lower() == "content-type" and isinstance(value, str):
            return value.split(";", 1)[0].strip().lower()
    return ""


def _request_body(exchange: _Exchange) -> JSONValue:
    """The recorded request body: ``null``, decoded JSON, or text (a form body)."""
    if not exchange.body:
        return None
    media = (exchange.header("content-type") or "").split(";", 1)[0].strip().lower()
    if media != _FORM_MEDIA_TYPE:
        value, is_json = _decode_json(exchange.body)
        if is_json:
            return value
    return exchange.body.decode("utf-8", errors="replace")


def _capture_document(
    source: Source, name: str, exchange: _Exchange, response_body: JSONValue
) -> dict[str, JSONValue]:
    """Assemble one unredacted capture: ``{surface, captured_at, request, response}``.

    The request is ``{method, url, headers, body}`` and the response ``{status, body}``, the
    shape ``payloads.validate_capture`` reads. The document still holds raw values; it is
    only ever passed to :func:`_redact_capture`.
    """
    headers: dict[str, JSONValue] = dict(exchange.headers)
    return {
        "surface": f"{source}.{name}",
        "captured_at": exchange.captured_at.isoformat(),
        "request": {
            "method": exchange.method,
            "url": exchange.url,
            "headers": headers,
            "body": _request_body(exchange),
        },
        "response": {"status": exchange.status, "body": response_body},
    }


def _redact_capture(
    capture: dict[str, JSONValue], policy: _RedactionPolicy, source: Source
) -> dict[str, JSONValue]:
    """Return the redacted copy of a capture document; nothing raw survives it.

    Redacted, with the marker ``<redacted>``:

    * request header values of ``Authorization`` (scheme kept) and the n8n secret header;
    * query and body values of ``access_token``, ``refresh_token``, ``client_secret``,
      ``client_id`` and, in requests, ``code``; token responses lose the first four too;
    * person-name and contact keys (``firstName``, ``lastName``, ``firstname``,
      ``lastname``, ``fullName``, ``phone``, ``mobilephone``, ``email``);
    * every LinkedIn ``formResponse.answers[].answerDetails`` whole, keys kept;
    * any email address and the plaintext of every configured secret, in any string;
    * the n8n webhook URL (a placeholder URL) and the configured Google customer id and
      LinkedIn ad account ids (same-length zeros).

    Args:
        capture: The document from :func:`_capture_document`.
        policy: The redaction policy of the current settings.
        source: The capture's source.

    Returns:
        A new, redacted document.
    """
    request = cast("dict[str, JSONValue]", capture["request"])
    response = cast("dict[str, JSONValue]", capture["response"])
    raw_headers = cast("dict[str, JSONValue]", request["headers"])
    body = request["body"]
    redacted_body: JSONValue
    if isinstance(body, str):
        is_form = _media_type(raw_headers) == _FORM_MEDIA_TYPE
        redacted_body = policy.form(body) if is_form else policy.text(body)
    else:
        redacted_body = policy.value(body, _REQUEST_CREDENTIAL_KEYS | _PERSON_KEYS)
    header_pairs = [(name, str(value)) for name, value in raw_headers.items()]
    return {
        "surface": capture["surface"],
        "captured_at": capture["captured_at"],
        "request": {
            "method": request["method"],
            "url": policy.url(str(request["url"]), source),
            "headers": policy.headers(header_pairs),
            "body": redacted_body,
        },
        "response": {
            "status": response["status"],
            "body": policy.value(response["body"], _CREDENTIAL_KEYS | _PERSON_KEYS),
        },
    }


# ---------------------------------------------------------------------------------------------
# capture-fixtures: one minimal call per surface
# ---------------------------------------------------------------------------------------------

# Lead responses and deals are looked for over the last 30 days; every call reads one page.
_LEAD_LOOKBACK: Final = timedelta(days=30)
_DEAL_LOOKBACK: Final = timedelta(days=30)
# One demographic pivot is enough to record the statistics surface (any of the three works).
_SEGMENT_PIVOT: Final = "MEMBER_SENIORITY"
_LIFECYCLE_STAGE: Final = "lifecyclestage"
_CONTACTS: Final = "contacts"
_DEALS: Final = "deals"


@dataclasses.dataclass(slots=True)
class _CaptureRun:
    """The recorder of one source's capture and what went wrong on the way.

    Attributes:
        source: The source captured.
        recorder: The transport every call went through.
        failures: ``"<surface>: <reason>"`` for each call that failed; reasons are the
            connectors' sanitised reasons.
        skipped: Surfaces not called, with why.
        rows: Google only: ``(row surface, exchange)`` of each GAQL query that answered.
        stopped: Set when the platform refused the credentials outright (HTTP 401, or a
            failed LinkedIn token exchange); later calls are skipped.
    """

    source: Source
    recorder: _RecordingTransport
    failures: list[str] = dataclasses.field(default_factory=list)
    skipped: list[str] = dataclasses.field(default_factory=list)
    rows: list[tuple[str, _Exchange]] = dataclasses.field(default_factory=list)
    stopped: bool = False

    async def step[T](self, surface: str, call: Callable[[], Coroutine[Any, Any, T]]) -> T | None:
        """Make one capture call, recording a connector failure instead of raising it.

        Only the connectors' classified errors are caught; anything else is a defect of the
        CLI and propagates. A 401 stops the capture, because no later call can authenticate;
        a 403 on one surface (a scope the token lacks) only fails that surface.

        Args:
            surface: The surface the call addresses, for the report.
            call: Makes the call.

        Returns:
            The call's result, or ``None`` when it failed or was skipped.
        """
        from ads_engine.connectors.errors import AccessDenied, ConnectorError

        if self.stopped:
            self.skipped.append(f"{surface}: skipped after the credentials were refused")
            return None
        try:
            return await call()
        except ConnectorError as exc:
            self.failures.append(f"{surface}: {exc.reason}")
            log.warning(
                "capture_surface_failed",
                source=self.source,
                surface=surface,
                error_kind=exc.kind,
                http_status=exc.http_status,
                reason=exc.reason,
            )
            if isinstance(exc, AccessDenied) and exc.is_unauthorized:
                self.stopped = True
            return None

    def interrupted(self, surface: str, error: Exception | None) -> None:
        """Record a listing that a transient error cut short (its ``interrupted_by``)."""
        if error is not None:
            self.failures.append(f"{surface}: {error}")


def _first_id(rows: Sequence[Mapping[str, Any]], resource: str) -> int | None:
    """The id of the first GoogleAdsRow carrying ``resource`` (``campaign``, ``adGroup``)."""
    for row in rows:
        entity = row.get(resource)
        if isinstance(entity, Mapping):
            raw = entity.get("id")
            if isinstance(raw, str | int) and not isinstance(raw, bool) and str(raw).isdigit():
                return int(raw)
    return None


def _first_row(body: JSONValue) -> JSONValue | None:
    """The first ``results[]`` row of an n8n envelope (or its one-element array wrapper)."""
    envelope = body[0] if isinstance(body, list) and len(body) == 1 else body
    if isinstance(envelope, dict):
        results = envelope.get("results")
        if isinstance(results, list) and results:
            return results[0]
    return None


def _google_scope(
    query_id: str, day: date, campaign_id: int | None, ad_group_id: int | None
) -> dict[str, object] | None:
    """The tiniest scope each GAQL template accepts, derived from the template's grammar.

    Keyset templates start at id 0, dated templates read one day, and ranges narrow to the
    first campaign or ad group already read. Q-G2b needs an id list, so without a campaign it
    is skipped (``None``).
    """
    from ads_engine.connectors.google_n8n.queries import (
        AD_GROUP_HI,
        AD_GROUP_LO,
        CAMPAIGN_HI,
        CAMPAIGN_LO,
        END,
        IDS,
        LAST_ID,
        QUERIES,
        START,
    )

    template = QUERIES[query_id]
    allowed = template.allowed_scope_keys
    required = template.required_scope_keys
    scope: dict[str, object] = {}
    if LAST_ID in required:
        scope[LAST_ID] = 0
    if IDS in required:
        if campaign_id is None:
            return None
        scope[IDS] = [campaign_id]
    if START in required:
        scope[START] = day
        scope[END] = day
    if AD_GROUP_LO in allowed and ad_group_id is not None:
        scope[AD_GROUP_LO] = ad_group_id
        scope[AD_GROUP_HI] = ad_group_id
    elif CAMPAIGN_LO in allowed and campaign_id is not None:
        scope[CAMPAIGN_LO] = campaign_id
        scope[CAMPAIGN_HI] = campaign_id
    return scope


async def _google_budget(settings: Settings, cap: int) -> CallBudget:
    """The call budget of a Google capture.

    The product's shared ``GoogleCallBudget`` when the database is reachable, so these calls
    count against ``GOOGLE_DAILY_CALL_BUDGET`` like every other; otherwise a local budget of
    ``cap`` calls, with a warning that ``source_connection.calls_24h`` will not show them.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from ads_engine.sync.budget_guard import GoogleCallBudget

    budget = GoogleCallBudget(settings)
    try:
        remaining = await budget.remaining()
    except (SQLAlchemyError, OSError) as exc:
        log.warning(
            "capture_call_budget_unrecorded",
            source="google",
            cap=cap,
            error=type(exc).__name__,
            detail=(
                "the database is unreachable: these webhook calls are capped locally and are "
                "not recorded in source_connection.calls_24h"
            ),
        )
        return _LocalCallBudget(cap)
    log.info("capture_call_budget", source="google", remaining=remaining)
    return budget


async def _capture_google(settings: Settings, run: _CaptureRun) -> None:
    """One webhook ``search`` call per GAQL template (Q-G1 … Q-G8, Q-G2b), each one page.

    The first call becomes ``search.json``; the first row of each query becomes its
    ``row_*`` capture. Only the first page is read: a capture records shapes, not data.
    """
    from ads_engine.connectors.google_n8n.client import GoogleAdsWebhookClient
    from ads_engine.connectors.google_n8n.queries import QUERIES
    from ads_engine.connectors.http import build_client

    # One call per template: 9 calls when the database cannot meter them.
    budget = await _google_budget(settings, len(QUERIES))
    day = datetime.now(UTC).date() - timedelta(days=1)
    campaign_id: int | None = None
    ad_group_id: int | None = None
    async with build_client("google", run.recorder, settings=settings) as http:
        client = GoogleAdsWebhookClient.from_settings(settings, http, budget)
        for query_id, template in QUERIES.items():
            surface = f"search {query_id}"
            scope = _google_scope(query_id, day, campaign_id, ad_group_id)
            if scope is None:
                run.skipped.append(f"{surface}: no campaign id was read to look up")
                continue
            query = template.render(**scope)
            page = await run.step(surface, partial(client.search_page, query_id, query))
            if page is None:
                continue
            # search_page returned, so the last exchange is this query's successful POST.
            run.rows.append((template.row_surface, run.recorder.exchanges[-1]))
            if query_id == "Q-G2":
                campaign_id = _first_id(page.rows, "campaign")
            elif query_id == "Q-G4":
                ad_group_id = _first_id(page.rows, "adGroup")


async def _capture_linkedin(settings: Settings, run: _CaptureRun) -> None:
    """One minimal call per LinkedIn surface, for the first configured ad account.

    ``access_token`` (refresh-token trio only), ``ad_account``, ``ad_campaign_groups``,
    ``ad_campaigns``, ``creatives`` (first campaign), ``posts`` (first creative's post),
    ``ad_analytics`` (yesterday, DAILY), ``ad_analytics_statistics`` (one pivot, ALL),
    ``lead_forms`` (``count=1``), ``lead_form_responses`` (``count=1``) and
    ``lead_form_responses_batch`` (the lead just read, when there is one).
    """
    from ads_engine.connectors.http import build_client
    from ads_engine.connectors.linkedin.auth import LinkedInTokenProvider
    from ads_engine.connectors.linkedin.client import LinkedInClient

    account = settings.linkedin_ad_account_ids[0]
    now = datetime.now(UTC)
    day = now.date() - timedelta(days=1)
    async with build_client("linkedin", run.recorder, settings=settings) as http:
        tokens = LinkedInTokenProvider(settings, http)
        if (
            settings.linkedin_uses_refresh_token
            and await run.step("access_token", tokens.get) is None
        ):
            # Without an access token no other call can authenticate.
            run.stopped = True
            return
        client = LinkedInClient(http, tokens)
        await run.step("ad_account", partial(client.get_ad_account, account))
        groups = await run.step("ad_campaign_groups", partial(client.list_campaign_groups, account))
        if groups is not None:
            run.interrupted("ad_campaign_groups", groups.interrupted_by)
        campaigns = await run.step("ad_campaigns", partial(client.list_campaigns, account))
        campaign_urn: str | None = None
        if campaigns is not None:
            run.interrupted("ad_campaigns", campaigns.interrupted_by)
            campaign_urn = campaigns.rows[0].urn if campaigns.rows else None
        await _capture_linkedin_creatives(client, account, campaign_urn, run)
        analytics = await run.step(
            "ad_analytics",
            partial(client.analytics_daily, account, (day, day), expected_creatives=1),
        )
        if analytics is not None:
            run.interrupted("ad_analytics", analytics.interrupted_by)
        segments = await run.step(
            "ad_analytics_statistics",
            partial(client.analytics_segments, account, _SEGMENT_PIVOT, (day, day)),
        )
        if segments is not None:
            run.interrupted("ad_analytics_statistics", segments.interrupted_by)
        await _capture_linkedin_leads(client, account, now, run)


async def _capture_linkedin_creatives(
    client: LinkedInClient, account: str, campaign_urn: str | None, run: _CaptureRun
) -> None:
    """Capture ``creatives`` for one campaign and ``posts`` for one creative's post."""
    if campaign_urn is None:
        run.skipped.append("creatives: the account has no campaign")
        run.skipped.append("posts: no creative was read")
        return
    creatives = await run.step("creatives", partial(client.list_creatives, account, [campaign_urn]))
    if creatives is None:
        run.skipped.append("posts: no creative was read")
        return
    run.interrupted("creatives", creatives.interrupted_by)
    post_urn = next((row.post_urn for row in creatives.rows if row.post_urn), None)
    if post_urn is None:
        run.skipped.append("posts: no creative references a post")
        return
    posts = await run.step("posts", partial(client.get_posts, [post_urn]))
    if posts is not None:
        run.interrupted("posts", posts.interrupted_by)


async def _capture_linkedin_leads(
    client: LinkedInClient, account: str, now: datetime, run: _CaptureRun
) -> None:
    """Capture ``lead_forms``, ``lead_form_responses`` and ``lead_form_responses_batch``."""
    forms = await run.step(
        "lead_forms", partial(client.list_lead_forms, account, page_size=1, max_pages=1)
    )
    if forms is not None:
        run.interrupted("lead_forms", forms.interrupted_by)
    responses = await run.step(
        "lead_form_responses",
        partial(
            client.list_lead_responses,
            account,
            (now - _LEAD_LOOKBACK, now),
            page_size=1,
            max_pages=1,
        ),
    )
    if responses is None:
        run.skipped.append("lead_form_responses_batch: no lead response was read")
        return
    run.interrupted("lead_form_responses", responses.interrupted_by)
    if not responses.leads:
        run.skipped.append("lead_form_responses_batch: no sponsored lead in the last 30 days")
        return
    batch = await run.step(
        "lead_form_responses_batch",
        partial(client.get_lead_responses, account, [responses.leads[0].external_id]),
    )
    if batch is not None:
        run.interrupted("lead_form_responses_batch", batch.interrupted_by)


async def _capture_hubspot(settings: Settings, run: _CaptureRun, thresholds: Thresholds) -> None:
    """One minimal call per HubSpot surface.

    ``properties`` (``contacts/lifecyclestage``), then the CRM binding verification that
    binds the contact properties, ``crm_search`` (deals of the last 30 days, ``limit=1``),
    ``associations_batch_read`` (that deal's contacts, or one input with no deal) and
    ``objects_batch_read`` (that contact by id, or the probe email batch with zero matches).
    """
    from ads_engine.connectors.http import build_client
    from ads_engine.connectors.hubspot.client import PROBE_EMAIL, HubSpotClient
    from ads_engine.connectors.hubspot.search import deal_search_filter, search_body

    now = datetime.now(UTC)
    async with build_client("hubspot", run.recorder, settings=settings) as http:
        client = HubSpotClient.from_settings(settings, http, live=True)
        await run.step("properties", partial(client.property, _CONTACTS, _LIFECYCLE_STAGE))
        bindings = await run.step(
            "properties (CRM bindings)",
            partial(client.verify_bindings, thresholds.mapping, settings),
        )
        body = search_body(deal_search_filter(), now - _DEAL_LOOKBACK, now, limit=1)
        page = await run.step("crm_search", partial(client.search_page, _DEALS, body))
        deal_id = str(page.results[0]["id"]) if page is not None and page.results else None
        contact_id: str | None = None
        if deal_id is not None:
            fetch = await run.step(
                "associations_batch_read",
                partial(client.associations, _DEALS, _CONTACTS, [deal_id]),
            )
            if fetch is not None and fetch.all_targets:
                contact_id = fetch.all_targets[0]
        else:
            # As the recovery probe does: one input with no deal, answered as a multi-status.
            await run.step(
                "associations_batch_read", partial(client.associations, _CONTACTS, _DEALS, ["0"])
            )
        if bindings is None:
            run.skipped.append("objects_batch_read: the contact properties could not be bound")
        elif contact_id is not None:
            await run.step("objects_batch_read", partial(client.contacts_by_id, [contact_id]))
        else:
            await run.step("objects_batch_read", partial(client.contacts_by_email, [PROBE_EMAIL]))


async def _capture(
    source: Source, settings: Settings, thresholds: Thresholds | None
) -> _CaptureRun:
    """Run one source's capture calls through a recording network transport."""
    run = _CaptureRun(source=source, recorder=_RecordingTransport(httpx.AsyncHTTPTransport()))
    match source:
        case "google":
            await _capture_google(settings, run)
        case "linkedin":
            await _capture_linkedin(settings, run)
        case "hubspot":
            if thresholds is None:
                raise ValueError("a HubSpot capture needs the loaded configuration")
            await _capture_hubspot(settings, run, thresholds)
    return run


# ---------------------------------------------------------------------------------------------
# capture-fixtures: selection, validation, writing
# ---------------------------------------------------------------------------------------------


def _build_captures(run: _CaptureRun, policy: _RedactionPolicy) -> dict[str, dict[str, JSONValue]]:
    """Choose the first 2xx JSON exchange per surface and turn each into a redacted capture.

    Surfaces are identified with ``payloads.identify_surface``, the router the simulator
    uses. For Google, the first row of each query's page is added as its ``row_*`` capture;
    queries that returned no rows are skipped.

    Returns:
        Redacted capture documents keyed by surface name.
    """
    from ads_engine.simulation.payloads import UnknownSurface, identify_surface

    source = run.source
    documents: dict[str, dict[str, JSONValue]] = {}
    for exchange in run.recorder.exchanges:
        if not exchange.ok:
            continue
        body, is_json = _decode_json(exchange.content)
        if not is_json:
            continue
        try:
            surface = identify_surface(source, exchange.shape())
        except UnknownSurface:
            log.warning("capture_unknown_surface", source=source, method=exchange.method)
            continue
        if surface not in documents:
            raw = _capture_document(source, surface, exchange, body)
            documents[surface] = _redact_capture(raw, policy, source)
    for row_surface, exchange in run.rows:
        if row_surface in documents:
            continue
        body, is_json = _decode_json(exchange.content)
        row = _first_row(body) if is_json else None
        if row is None:
            continue
        raw = _capture_document(source, row_surface, exchange, row)
        documents[row_surface] = _redact_capture(raw, policy, source)
    return documents


def _contract_problems(documents: Mapping[str, dict[str, JSONValue]], root: Path) -> list[str]:
    """Check every capture against its contract with ``payloads.validate_capture``.

    Returns:
        ``"<surface>: <violation>"`` for each capture that breaks its contract.
    """
    from ads_engine.simulation.payloads import (
        ContractNotFound,
        ContractViolation,
        UnknownSurface,
        validate_capture,
    )

    problems: list[str] = []
    for name, document in sorted(documents.items()):
        try:
            validate_capture(document, root=root)
        except (ContractViolation, UnknownSurface, ContractNotFound) as exc:
            problems.append(f"{name}: {exc}")
    return problems


def _write_captures(directory: Path, documents: Mapping[str, dict[str, JSONValue]]) -> list[Path]:
    """Write each capture as ``{directory}/{surface}.json``, replacing an older one atomically.

    Files are canonical JSON (sorted keys, two-space indent, UTF-8, trailing newline), the
    format of every fixture.

    Returns:
        The written paths, sorted by surface.
    """
    from ads_engine.simulation.payloads import canonical_json

    directory.mkdir(exist_ok=True)
    written: list[Path] = []
    for name in sorted(documents):
        path = directory / f"{name}.json"
        temporary = directory / f".{name}.json.tmp"
        temporary.write_text(canonical_json(documents[name]), encoding="utf-8")
        temporary.replace(path)
        written.append(path)
    return written


def _display_path(path: Path) -> str:
    """``path`` relative to the working directory when it lies under it."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _cmd_capture_fixtures(args: argparse.Namespace) -> int:
    """``capture-fixtures --source S``: record redacted live contract captures.

    Refused (exit 1, nothing sent or written) under ``DATA_MODE=simulated`` and without the
    source's credentials. The output goes to ``tests/contracts/{source}/captured/`` under the
    working directory, so run it from ``backend/``.

    Returns:
        :data:`EXIT_OK` when every surface was captured and satisfies its contract;
        :data:`EXIT_FAILURE` when a refusal applies, a call failed, nothing was captured or a
        capture breaks its contract (the captures read are still written, because they are
        the live truth the contract tests must flag).

    Raises:
        _UsageError: When ``tests/contracts/{source}/`` is not under the working directory.
    """
    from ads_engine.analysis.thresholds import Thresholds
    from ads_engine.simulation.payloads import contracts_root

    source = _as_source(args.source)
    settings = get_settings()
    if settings.data_mode == "simulated":
        _error(
            "capture-fixtures records live traffic only; it is refused under DATA_MODE=simulated"
        )
        return EXIT_FAILURE
    if not settings.has_credentials(source):
        missing = ", ".join(settings.missing_credentials(source))
        _error(f"capture-fixtures {source}: live credentials are not configured; set {missing}")
        return EXIT_FAILURE
    root = contracts_root()
    source_dir = root / source
    if not source_dir.is_dir():
        raise _UsageError(
            f"{_display_path(source_dir)} not found; run from backend/ (or /app in the image)"
        )
    thresholds = Thresholds.load() if source == "hubspot" else None
    policy = _RedactionPolicy.from_settings(settings)
    run = _run_async(lambda: _capture(source, settings, thresholds))
    documents = _build_captures(run, policy)
    problems = _contract_problems(documents, root)
    written = _write_captures(source_dir / "captured", documents) if documents else []
    for path in written:
        print(_display_path(path), flush=True)
    for failure in run.failures:
        _error(f"capture-fixtures {source}: {_redacted(failure)}")
    for skipped in run.skipped:
        _error(f"capture-fixtures {source}: not captured: {skipped}")
    for problem in problems:
        _error(f"capture-fixtures {source}: capture breaks its contract: {_redacted(problem)}")
    log.info(
        "capture_fixtures_finished",
        source=source,
        written=len(written),
        failed=len(run.failures),
        skipped=len(run.skipped),
        contract_violations=len(problems),
    )
    if not written:
        _error(f"capture-fixtures {source}: no surface could be captured")
        return EXIT_FAILURE
    return EXIT_FAILURE if run.failures or problems else EXIT_OK


# ---------------------------------------------------------------------------------------------
# Parser and entry point
# ---------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the ``ads-engine`` argument parser: global options and the seven commands.

    Each sub-parser stores its handler in ``handler``; :func:`main` dispatches on it.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Ads Action Engine administration: password hashing, migrations, the admin login, "
            "syncs, analysis runs, simulated seeding and live contract captures."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default="INFO",
        type=str.upper,
        help="level of the JSON log lines written to stderr (default: INFO)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    hash_password = commands.add_parser(
        "hash-password",
        help="print the argon2id hash of the password read from stdin",
        description="Read a password from stdin (one trailing newline is dropped) and print "
        "only its argon2id hash. Loads no settings and uses no database.",
    )
    hash_password.set_defaults(handler=_cmd_hash_password)

    migrate = commands.add_parser(
        "migrate",
        help="upgrade the database schema to the newest revision",
        description="Run `alembic upgrade head` with backend/alembic.ini from the working "
        "directory (backend/ on the host, /app in the image). Idempotent.",
    )
    migrate.set_defaults(handler=_cmd_migrate)

    ensure_admin = commands.add_parser(
        "ensure-admin",
        help="create or update the admin login from the environment",
        description="Upsert the admin from APP_ADMIN_EMAIL and APP_ADMIN_PASSWORD_HASH and "
        "remove any other login. Idempotent.",
    )
    ensure_admin.set_defaults(handler=_cmd_ensure_admin)

    sync = commands.add_parser(
        "sync",
        help="run one source's sync jobs now and print the report",
        description="Run the source's sync jobs against its active dataset (live or "
        "simulated) and print the JSON report.",
    )
    sync.add_argument("--source", required=True, choices=SOURCES, help="the source to sync")
    sync.set_defaults(handler=_cmd_sync)

    analyze = commands.add_parser(
        "analyze",
        help="compute a custom-window analysis run from stored data",
        description="Compute a custom run over [START, END] (calendar dates on each source's "
        "native days) under the shared analysis lock. Exits 3 while another run holds it.",
    )
    analyze.add_argument("--start", required=True, type=_iso_date, help="first date, YYYY-MM-DD")
    analyze.add_argument("--end", required=True, type=_iso_date, help="last date, YYYY-MM-DD")
    analyze.set_defaults(handler=_cmd_analyze)

    seed = commands.add_parser(
        "seed-simulated",
        help="seed simulated datasets for every source and run the default analysis",
        description="Build, backfill and activate a simulated dataset per source (an active "
        "simulated one is kept), then compute the default-window run.",
    )
    seed.set_defaults(handler=_cmd_seed_simulated)

    capture = commands.add_parser(
        "capture-fixtures",
        help="record redacted live contract captures for one source",
        description="Send one minimal live request per contract surface and write the "
        "redacted exchanges to tests/contracts/{source}/captured/. Run from backend/.",
    )
    capture.add_argument("--source", required=True, choices=SOURCES, help="the source to capture")
    capture.set_defaults(handler=_cmd_capture_fixtures)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``ads-engine`` command line; the console script calls ``sys.exit(main())``.

    Args:
        argv: The arguments after the program name; ``sys.argv[1:]`` when ``None``.

    Returns:
        The exit code: :data:`EXIT_OK`, :data:`EXIT_FAILURE`, :data:`EXIT_USAGE` or
        :data:`EXIT_LOCK_HELD`. ``--help`` and ``--version`` return 0, argparse usage errors 2.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(None if argv is None else list(argv))
    except SystemExit as exc:
        return _system_exit_code(exc)
    name: str = args.command
    handler: _Handler = args.handler
    level: str = args.log_level
    if name == "hash-password":
        # The documented defaults only: no environment is read, so `make init` needs none.
        configure_logging(stream=sys.stderr, level=level, settings=Settings.model_construct())
    else:
        configure_logging(stream=sys.stderr, level=level)
    try:
        return handler(args)
    except _UsageError as exc:
        _error(str(exc))
        return EXIT_USAGE
    except Exception as exc:
        message = _domain_error_message(exc)
        if message is not None:
            _error(f"{name}: {_redacted(message)}")
            return EXIT_FAILURE
        log.exception("cli_failed", command=name, error=type(exc).__name__)
        _error(f"{name} failed ({type(exc).__name__}); see the log on stderr")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
