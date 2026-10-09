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
                       report as JSON; exits 0 only when every job ran and succeeded.
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
missing ``alembic.ini`` or contracts directory, or an empty, non-UTF-8 or never-entered
``hash-password`` password); :data:`EXIT_LOCK_HELD` (3) another process holds the shared
``analysis`` lock. ``sync`` exits 0 only when every job ran and succeeded,
and 1 when no job ran (no active dataset, or a live dataset that may not be synced now) or
any job failed, transient staleness and access denials included; the report says which.

Streams: once a command runs, **stderr** carries only JSON lines, one object per line: the
structured logs (``configure_logging(stream=sys.stderr)``), one ``cli_error`` line per
failure message (``{"event": "cli_error", "level": "error", "logger": "ads_engine.cli",
"message": "ads-engine: …", "timestamp": "…Z"}``) and one ``cli_warning`` line, the same keys
with ``"level": "warning"``, per note that is not a failure (each surface ``capture-fixtures``
reports as not captured, which on its own leaves the exit 0). Both are written whatever
``--log-level`` is. stdout carries only command output: the password hash, JSON reports and
written file paths. Neither stream ever carries a secret, a token or an email address. The
boundary is argparse: its usage errors are written before logging is configured and stay plain
usage text, and they never repeat the supplied text: they name only the arguments, commands and
choices the parser defines.

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
import contextlib
import dataclasses
import errno
import getpass
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from secrets import token_hex
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, Protocol, cast, overload
from urllib.parse import parse_qsl, unquote, unquote_plus, urlencode

import httpx
from argon2 import Parameters, PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import ValidationError

from ads_engine import __version__
from ads_engine.db.models.ops import SOURCES, AppUser
from ads_engine.logging import EMAIL_RE, configure_logging, get_logger, logged_as, redact
from ads_engine.settings import SOURCES as SETTINGS_SOURCES
from ads_engine.settings import ConfigurationError, Settings, Source, get_settings

if TYPE_CHECKING:
    from ads_engine.analysis.thresholds import Thresholds
    from ads_engine.connectors.linkedin.client import LinkedInClient
    from ads_engine.simulation.payloads import JSONValue, RequestShape
    from ads_engine.sync.budget_guard import GoogleCallBudget

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
"""The command failed: an expected domain error (one ``cli_error`` JSON line on stderr) or an
unexpected one (logged as ``cli_failed`` with its traceback, redacted, or naming the event that
already logged that traceback, then one ``cli_error`` line)."""

EXIT_USAGE: Final = 2
"""A usage error: argparse's own (its diagnostic names the argument, never the supplied value),
or a bad date, an out-of-range window, a missing ``alembic.ini`` or contracts directory, or an
empty, non-UTF-8 or never-entered ``hash-password`` password."""

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


def _write_cli_line(level: Literal["error", "warning"], message: str) -> None:
    """Write one ``cli_<level>`` JSON line to stderr carrying ``ads-engine: <message>``.

    The line has the keys of the configured log lines, sorted: ``event`` (``cli_error`` or
    ``cli_warning``), ``level`` (``error`` or ``warning``), ``logger`` (``ads_engine.cli``),
    ``message`` (the program prefix and ``message``, whitespace collapsed) and an ISO-8601 UTC
    ``timestamp`` ending in ``Z``, so stderr stays one JSON object per line. It is written
    directly, not logged: the message is part of the command's outcome, so ``--log-level``
    never hides it, and the log redaction would rewrite the CLI's own hints (``make init
    ADMIN_PASSWORD=…`` reads as a credential assignment). Messages the CLI writes itself are
    therefore kept as written; text that comes from an exception or a platform passes through
    :func:`_redacted` at the call site first.

    Args:
        level: ``error`` for a failure (:func:`_error`), ``warning`` for a note
            (:func:`_warning`); the event is ``cli_`` followed by it.
        message: The text, without the program prefix.
    """
    line = {
        "event": f"cli_{level}",
        "level": level,
        # Spelled out: `python -m ads_engine.cli` runs this module as `__main__`.
        "logger": "ads_engine.cli",
        "message": f"{_PROG}: {' '.join(message.split())}",
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    print(json.dumps(line, sort_keys=True), file=sys.stderr, flush=True)


def _error(message: str) -> None:
    """Write one ``cli_error`` line (``level`` ``error``) to stderr: a failure of the command.

    Every call is followed by a non-zero exit. The line's shape and why it is written
    directly are in :func:`_write_cli_line`.

    Args:
        message: The failure, without the program prefix.
    """
    _write_cli_line("error", message)


def _warning(message: str) -> None:
    """Write one ``cli_warning`` line (``level`` ``warning``) to stderr: a note, not a failure.

    It reports something the command left undone that is not itself a failure: a surface
    ``capture-fixtures`` did not capture, with why (the platform had no data for it, or an
    earlier failure, reported in its own ``cli_error`` line, kept it from being sent). On its
    own it never makes the exit non-zero. The line has the shape of :func:`_error`'s and is
    written the same direct way (:func:`_write_cli_line`).

    Args:
        message: The note, without the program prefix.
    """
    _write_cli_line("warning", message)


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


_DATE_FORMAT_REASON: Final = "not a date in YYYY-MM-DD form"
"""Why :func:`_iso_date` rejected a value; argparse prefixes the argument name."""

_EXTENDED_CALENDAR_DATE_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
"""The one date form :func:`_iso_date` accepts; ``[0-9]`` refuses fullwidth and other digits."""


def _iso_date(text: str) -> date:
    """Parse an argparse date given in the ISO 8601 extended calendar form ``YYYY-MM-DD``.

    Only that form is accepted, with surrounding whitespace ignored. The text must match
    :data:`_EXTENDED_CALENDAR_DATE_RE` before :meth:`datetime.date.fromisoformat` reads it,
    because ``fromisoformat`` alone also accepts the basic form (``20260901``) and week dates
    (``2026-W37-4``). ``fromisoformat`` still rejects impossible dates such as ``2026-02-30``.

    Args:
        text: The value given to ``--start`` or ``--end``.

    Returns:
        The calendar date the text names.

    Raises:
        argparse.ArgumentTypeError: When the text is not a valid date in that form; argparse
            then exits with :data:`EXIT_USAGE`. The message is :data:`_DATE_FORMAT_REASON` and
            never repeats the text, which may be an email address or a pasted credential.
    """
    value = text.strip()
    if _EXTENDED_CALENDAR_DATE_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(_DATE_FORMAT_REASON)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(_DATE_FORMAT_REASON) from None


# ---------------------------------------------------------------------------------------------
# hash-password
# ---------------------------------------------------------------------------------------------


def _read_password() -> str:
    """Read the password: all of stdin, less exactly one trailing newline (LF or CRLF).

    On an interactive terminal the password is read with :func:`getpass.getpass` instead, so
    it is not echoed; the prompt goes to the terminal, never to stdout.

    The password must be valid UTF-8, the encoding its hash is computed over. Undecodable
    input either fails the read (a strict stream) or arrives as lone surrogates that cannot
    be encoded (``surrogateescape``: the C, POSIX and C.UTF-8 locales and Python's UTF-8
    mode); both are rejected here, before the hasher sees them. No message repeats any part
    of the password.

    Returns:
        The password, non-empty and encodable as UTF-8.

    Raises:
        _UsageError: When end of input arrives at the prompt before a line is entered, when
            the password is not valid UTF-8, or when it is empty.
    """
    if sys.stdin.isatty():
        not_utf8 = "the password entered is not valid UTF-8"
        try:
            password = getpass.getpass("Password: ")
        except EOFError:
            raise _UsageError("no password was entered") from None
        except UnicodeDecodeError:
            raise _UsageError(not_utf8) from None
    else:
        not_utf8 = "the password read from stdin is not valid UTF-8"
        try:
            password = sys.stdin.read()
        except UnicodeDecodeError:
            raise _UsageError(not_utf8) from None
        if password.endswith("\r\n"):
            password = password[:-2]
        elif password.endswith("\n"):
            password = password[:-1]
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        raise _UsageError(not_utf8) from None
    if not password:
        raise _UsageError("the password read from stdin is empty")
    return password


def _cmd_hash_password(_args: argparse.Namespace) -> int:
    """``hash-password``: print the argon2id hash of the password read from stdin.

    The hash uses argon2-cffi's defaults (argon2id, RFC 9106 low-memory profile), the same
    parameters ``api/security.py`` verifies with, so a freshly initialised admin never needs
    a rehash. Only the hash and a newline reach stdout; ``make init`` captures it into
    ``.env`` as ``APP_ADMIN_PASSWORD_HASH``. No settings are loaded and no database is used.

    Returns:
        :data:`EXIT_OK` once the hash is printed.

    Raises:
        _UsageError: From :func:`_read_password`, for an empty, non-UTF-8 or never-entered
            password; :func:`main` prints its message and exits with :data:`EXIT_USAGE`.
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
    at head is left unchanged, which makes the command idempotent. Concurrent runs against
    one database are serialised by ``migrations/env.py``'s advisory lock: a later run logs
    that it is waiting, then finds the database at head and changes nothing, so two
    ``migrate`` one-shots started together both exit 0.

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


# The argon2 parameters ensure-admin accepts. The minimums are libargon2's own (argon2.h), and
# argon2 refuses a hash below any of them at every sign-in. The caps bound the cost of every login
# verification, which recomputes the hash with its stored parameters, and of the verification
# ensure-admin runs to prove the hash readable: at most 10 passes over 2 GiB (2_097_152 KiB,
# RFC 9106's high-memory profile) in 16 lanes. `ads-engine hash-password` writes t=3, m=65536
# (64 MiB), p=4, a 16-byte salt and a 32-byte digest, well inside them.
_ARGON2_MIN_SALT_BYTES: Final = 8
_ARGON2_MIN_HASH_BYTES: Final = 4
_ARGON2_MIN_MEMORY_KIB_PER_LANE: Final = 8
_ARGON2_MAX_TIME_COST: Final = 10
_ARGON2_MAX_PARALLELISM: Final = 16
_ARGON2_MAX_MEMORY_KIB: Final = 2_097_152
_ARGON2_PROBE_SECRET_BYTES: Final = 32


def _argon2_parameter_problem(parameters: Parameters) -> str | None:
    """Name the first parameter of a parsed argon2 hash outside the range ensure-admin accepts.

    Args:
        parameters: What :func:`argon2.extract_parameters` read from the hash; it derives the
            salt and digest lengths from the length of their base64 text.

    Returns:
        The parameter and its accepted range, never its value, or ``None`` when every
        parameter is in range.
    """
    lanes = parameters.parallelism
    if parameters.salt_len < _ARGON2_MIN_SALT_BYTES:
        return f"the salt must be at least {_ARGON2_MIN_SALT_BYTES} bytes"
    if parameters.hash_len < _ARGON2_MIN_HASH_BYTES:
        return f"the digest must be at least {_ARGON2_MIN_HASH_BYTES} bytes"
    if not 1 <= parameters.time_cost <= _ARGON2_MAX_TIME_COST:
        return f"the time cost must be 1 to {_ARGON2_MAX_TIME_COST}"
    if not 1 <= lanes <= _ARGON2_MAX_PARALLELISM:
        return f"the parallelism must be 1 to {_ARGON2_MAX_PARALLELISM}"
    if parameters.memory_cost < _ARGON2_MIN_MEMORY_KIB_PER_LANE * lanes:
        return f"the memory cost must be at least {_ARGON2_MIN_MEMORY_KIB_PER_LANE} KiB per lane"
    if parameters.memory_cost > _ARGON2_MAX_MEMORY_KIB:
        return f"the memory cost must be at most {_ARGON2_MAX_MEMORY_KIB} KiB (2 GiB)"
    return None


def _argon2_verification_problem(value: str) -> str | None:
    """Verify a random secret against the hash ``value`` and return why argon2 could not.

    A password mismatch means libargon2 decoded the salt and digest and recomputed the hash
    with its parameters, which is what every sign-in does, so some password verifies against
    it. A decoding or parameter error, or a failed memory allocation, means none can. The
    secret is random, so a match is all but impossible; it proves the same and is accepted.

    Args:
        value: A printable-ASCII argon2 hash whose parameters are within the accepted range,
            so the verification's cost is bounded.

    Returns:
        libargon2's reason, a fixed message that never quotes the hash, or ``None`` when the
        hash verified.
    """
    try:
        PasswordHasher().verify(value, secrets.token_bytes(_ARGON2_PROBE_SECRET_BYTES))
    except VerifyMismatchError:
        return None
    except (InvalidHashError, VerificationError) as exc:
        return _redacted(str(exc)) or type(exc).__name__
    return None


def _admin_hash(settings: Settings) -> str | None:
    """Return the configured admin hash once argon2 can verify a password against it.

    The hash is checked, never shown: each failed check prints one line to stderr naming what
    is wrong, never any part of the value, and yields ``None``, so :func:`_ensure_admin` exits
    before it opens its transaction and the stored admin stays as it was. The checks, in
    order: the value is set; it starts with ``$argon2``; it is printable ASCII (argon2 reads a
    hash as an ASCII C string) and parses as an argon2 PHC string; its salt and digest lengths,
    time cost, parallelism and memory cost are within libargon2's minimums and the caps above;
    and verifying a random secret against it ends in a password mismatch, not in a decoding or
    parameter error.

    A value that passes is a hash some password verifies against. Whether that password is the
    admin's cannot be known without it, so a well-formed hash of any other password is accepted
    as a password change.
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
    parameters: Parameters | None = None
    if value.isascii() and value.isprintable():
        try:
            parameters = extract_parameters(value)
        except InvalidHashError:
            parameters = None
    if parameters is None:
        _error(
            "APP_ADMIN_PASSWORD_HASH is not a valid argon2 hash (it cannot be read as an argon2 "
            "PHC string); write it again with `ads-engine hash-password`"
        )
        return None
    problem = _argon2_parameter_problem(parameters)
    if problem is not None:
        _error(
            "APP_ADMIN_PASSWORD_HASH has argon2 parameters outside the supported range: "
            f"{problem}; write it again with `ads-engine hash-password`"
        )
        return None
    reason = _argon2_verification_problem(value)
    if reason is not None:
        _error(
            f"APP_ADMIN_PASSWORD_HASH cannot be verified by argon2 ({reason}); write it again "
            "with `ads-engine hash-password`"
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
    The hash is validated by :func:`_admin_hash` before the transaction opens, so an invalid
    one exits with :data:`EXIT_FAILURE` and changes nothing.
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
        :data:`EXIT_OK` only when every job ran and succeeded (``report.ok``). Otherwise
        :data:`EXIT_FAILURE`, with the report printed and one line on stderr saying why:

        * a job failed on a defect: a ``QueryDefect``, or a database integrity violation (the
          job's ``integrity`` in the report names its kind, SQLSTATE, table and constraint);
        * no job ran: the source has no active dataset yet, or its live dataset may not be
          synced now; the line carries the report's ``reason``;
        * jobs failed otherwise (a transient error left their datasets stale, access was
          denied, or the dataset could not be written): the line names each failed job and
          its disposition, never the error text, and the failed datasets keep their last
          good data.
    """
    from ads_engine.sync.jobs import run_source_jobs

    report = await run_source_jobs(source, trigger="manual")
    _emit_json(report.to_json())
    if report.ok:
        return EXIT_OK
    if report.has_defect:
        _error(
            f"sync {source}: a job failed on a defect (query or data integrity); "
            "see the report and the log"
        )
    elif report.reason is not None:
        _error(f"sync {source}: no job ran: {_redacted(report.reason)}; see the report")
    else:
        failed = [result for result in report.results if result.status != "succeeded"]
        total = len(report.results)
        # Job names and disposition codes only: a job's error text stays in the report.
        outcomes = ", ".join(f"{result.job}: {result.disposition or 'failed'}" for result in failed)
        _error(
            f"sync {source}: {len(failed)} of {total} job{'s' if total != 1 else ''} failed "
            f"({outcomes}); the failed datasets keep their last good data; "
            "see the report and the log"
        )
    return EXIT_FAILURE


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


class _CallBudgetUnavailableError(Exception):
    """The shared Google call budget in the database could not be read or reserved.

    Raised in place of a webhook POST that has no durable reservation, so that POST is never
    sent.

    Attributes:
        error: The class name of the database error. Its message is left out, because it can
            name the database host.
    """

    def __init__(self, error: str) -> None:
        super().__init__(f"the shared Google call budget in the database is unavailable ({error})")
        self.error = error


class _CaptureCallBudget:
    """The call budget of a Google capture: the product's shared ``GoogleCallBudget``.

    It satisfies ``google_n8n.client.CallBudget``. Every webhook POST attempt is reserved in
    ``source_connection`` first, so capture calls count in ``calls_24h`` against
    ``GOOGLE_DAILY_CALL_BUDGET`` exactly like the worker's and ``sync``'s, and a full window
    refuses them with ``CallBudgetExhausted``. It fails closed: no local count ever stands in
    for the database. The first database failure is remembered, and that attempt and every
    later one are refused with :class:`_CallBudgetUnavailableError` without touching the
    database again.

    Attributes:
        failure: The class name of the database error that made the budget unavailable, or
            ``None`` while the database answers.
    """

    __slots__ = ("_shared", "failure")

    def __init__(self, shared: GoogleCallBudget) -> None:
        self._shared = shared
        self.failure: str | None = None

    async def remaining(self) -> int:
        """Read how many calls the shared window still admits; sends nothing.

        Returns:
            The calls the sliding 24 h window admits before the cap.

        Raises:
            _CallBudgetUnavailableError: When the database cannot be read, now or earlier.
        """
        return await self._metered(self._shared.remaining, "preflight")

    async def acquire(self) -> None:
        """Reserve one webhook POST attempt in the shared budget, or refuse it.

        Raises:
            CallBudgetExhausted: When the window already holds ``GOOGLE_DAILY_CALL_BUDGET``
                calls; nothing is counted and no POST may be made.
            _CallBudgetUnavailableError: When this reservation or an earlier one failed on the
                database; no POST may be made.
        """
        await self._metered(self._shared.acquire, "reservation")

    async def _metered[T](self, call: Callable[[], Coroutine[Any, Any, T]], phase: str) -> T:
        """Run one call of the shared budget, failing closed on a database error.

        Args:
            call: The ``GoogleCallBudget`` method to await.
            phase: ``"preflight"`` or ``"reservation"``, for the log.

        Returns:
            The call's result.

        Raises:
            _CallBudgetUnavailableError: When the database failed on this call or an earlier
                one; after a failure ``call`` is not awaited.
        """
        from sqlalchemy.exc import SQLAlchemyError

        if self.failure is not None:
            raise _CallBudgetUnavailableError(self.failure)
        try:
            return await call()
        except (SQLAlchemyError, OSError) as exc:
            self.failure = type(exc).__name__
            log.warning(
                "capture_call_budget_unavailable",
                source="google",
                phase=phase,
                error=self.failure,
                detail="no webhook call is sent without a reservation in source_connection",
            )
            raise _CallBudgetUnavailableError(self.failure) from exc


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
# The text redaction itself writes. The leak check reads only what lies between these, so a
# short secret such as "red" is never found inside a marker.
_INSERTED_TEXT: Final = re.compile(
    "|".join(re.escape(text) for text in (_REDACTED_WEBHOOK_URL, _ENCODED_MARKER, _MARKER))
)
# The leak check percent-decodes each string with each decoder until it stops changing, so a
# secret encoded any number of times, or in a spelling the secret pattern does not match, is
# found.
_DECODERS: Final[tuple[Callable[[str], str], ...]] = (unquote, unquote_plus)
# One colon of a URN: literal, or percent-encoded any number of times in either hex case
# (`%3A`, `%3a`, `%253A`, …), as request URLs and `next` links carry it.
_URN_COLON: Final = r"(?::|%(?:25)*3[Aa])"
# A LinkedIn organization URN, `urn:li:organization:{id}`, with the other spellings of the
# same id space: `urn:li:organizationBrand:{id}` and `urn:li:company:{id}` (the other member
# of the campaign-group beneficiary union). Group 1 is the prefix as written, group 2 the id.
# The organization id is a host secret, yet no setting holds it, so it is found by this URN
# context only: a bare number equal to an id is a metric or another id and is kept.
_ORGANIZATION_URN: Final = re.compile(
    rf"(urn{_URN_COLON}li{_URN_COLON}(?:organization(?:Brand)?|company){_URN_COLON})([0-9]+)"
)


def _hex_digit(digit: str) -> str:
    """A regular expression matching one hex digit of a percent escape, in either case."""
    return digit if digit.isdigit() else f"[{digit.upper()}{digit.lower()}]"


def _secret_spelling(secret: str) -> str:
    """A regular expression matching ``secret`` in plain text or any percent-encoded spelling.

    Each character matches itself, the ``%XX`` escapes of its UTF-8 bytes with either case of
    each hex digit, and, for a space, ``+``, in any mix: ``abc`` is matched as ``%61%62%63``
    and ``a%62c``, and ``a/b+c`` as ``a%2fb%2bc``. Plain characters match case-sensitively.

    Args:
        secret: The plaintext of one configured secret.

    Returns:
        The pattern, without anchors or groups that capture.
    """
    characters: list[str] = []
    for character in secret:
        # surrogatepass: a lone surrogate (an undecodable environment byte) gets an escape
        # too, instead of failing the whole command.
        escape = "".join(
            "%" + "".join(_hex_digit(digit) for digit in f"{byte:02X}")
            for byte in character.encode("utf-8", errors="surrogatepass")
        )
        alternatives = [re.escape(character), escape]
        if character == " ":
            alternatives.append(re.escape("+"))
        characters.append(f"(?:{'|'.join(alternatives)})")
    return "".join(characters)


def _zero_organization_id(match: re.Match[str]) -> str:
    """Keep one :data:`_ORGANIZATION_URN` match's prefix as written; zero its id, same length.

    Zeros keep the URN well-formed, so a redacted capture still satisfies its contract.
    """
    return match.group(1) + "0" * len(match.group(2))


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

    Every configured secret is removed wherever it occurs, whatever its length: in any string
    or dict key, in plain text or in any percent-encoded spelling, and as a JSON number equal
    to it. Capture fidelity gives way to that guarantee, and :meth:`holds_secret` confirms it
    on each finished document, percent-decoded however many times it was encoded too, so a
    capture that still holds a secret is never written. The id of every LinkedIn
    organization URN (``urn:li:organization:``, ``urn:li:organizationBrand:`` and
    ``urn:li:company:``, plain or percent-encoded) becomes zeros of the same length in any
    string or dict key, whatever organization it names (:data:`_ORGANIZATION_URN`).

    Attributes:
        secrets: The plaintext of every configured secret, the n8n webhook URL and the
            LinkedIn client id, longest first.
        secret_pattern: One alternation of those secrets, longest plaintext first, each
            matching its plain text and any percent-encoded spelling (see
            :func:`_secret_spelling`), or ``None`` when nothing is configured. A single pass
            replaces every occurrence and never rescans the marker it wrote, so a secret
            such as ``red`` cannot loop or survive.
        header_names: Lower-cased request headers whose values are credentials:
            ``Authorization`` and the configured n8n secret header.
        identifiers: Patterns of the configured Google customer id and LinkedIn ad account
            ids, each with its same-length replacement of zeros. Those ids are host secrets
            that must never be committed; zeros keep every id, URN and resource name
            well-formed, so a capture still satisfies its contract.
        identifier_values: The same ids as text; a JSON number equal to one becomes ``0``.
    """

    secrets: tuple[str, ...]
    secret_pattern: re.Pattern[str] | None
    header_names: frozenset[str]
    identifiers: tuple[tuple[re.Pattern[str], str], ...]
    identifier_values: frozenset[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> _RedactionPolicy:
        """Build the policy from the configured secrets, header name and account ids."""
        secrets = set(settings.secret_values())
        if settings.n8n_google_ads_webhook_url:
            secrets.add(settings.n8n_google_ads_webhook_url)
        # A plain setting, so not among the secret values, but a LinkedIn OAuth credential.
        client_id = (settings.linkedin_client_id or "").strip()
        if client_id:
            secrets.add(client_id)
        ordered = tuple(sorted(secrets, key=len, reverse=True))
        alternation = "|".join(_secret_spelling(secret) for secret in ordered)
        ids = list(settings.linkedin_ad_account_ids)
        if settings.google_ads_customer_id:
            ids.append(settings.google_ads_customer_id)
        identifiers = tuple(
            (re.compile(rf"(?<![0-9]){re.escape(value)}(?![0-9])"), "0" * len(value))
            for value in sorted(set(ids), key=len, reverse=True)
        )
        return cls(
            secrets=ordered,
            secret_pattern=re.compile(alternation) if ordered else None,
            header_names=frozenset(
                {_AUTHORIZATION, settings.n8n_webhook_secret_header.strip().lower()}
            ),
            identifiers=identifiers,
            identifier_values=frozenset(ids),
        )

    def text(self, value: str) -> str:
        """Redact one string: secrets, account ids, organization URN ids, then email addresses.

        Every configured secret is replaced wherever it occurs, whatever its length: its exact
        plaintext by ``<redacted>``, and any percent-encoded spelling (either hex case, encoded
        or literal characters in any mix, ``+`` for a space) by ``%3Credacted%3E``, so a URL
        or form body stays parseable. Configured account ids, and the id of every LinkedIn
        organization URN, become zeros of the same length.
        """
        if self.secret_pattern is not None:
            value = self.secret_pattern.sub(self._secret_marker, value)
        for pattern, replacement in self.identifiers:
            value = pattern.sub(replacement, value)
        value = _ORGANIZATION_URN.sub(_zero_organization_id, value)
        return EMAIL_RE.sub(_MARKER, value)

    def _secret_marker(self, match: re.Match[str]) -> str:
        """The marker for one matched secret: plain for its plaintext, else percent-encoded."""
        return _MARKER if match.group(0) in self.secrets else _ENCODED_MARKER

    def holds_secret(self, node: JSONValue) -> bool:
        """Whether a redacted capture still holds a configured secret: the fail-closed check.

        Every dict key, every string and every number (by its JSON text) is searched for a
        configured secret, outside the markers and the webhook placeholder that redaction
        writes. Each stretch between them is searched for its plain text and every
        percent-encoded spelling and, independently of that pattern, percent-decoded until it
        stops changing, with and without ``+`` as a space, each decoded form being searched
        for any secret's plaintext, so a secret encoded any number of times is found too.
        Values redaction keeps as recorded, such as ``captured_at`` and header names, are
        searched too.

        Args:
            node: The redacted capture document, or a part of it.

        Returns:
            ``True`` when a configured secret survives; the capture must not be written.
        """
        if isinstance(node, dict):
            return any(self._holds(key) or self.holds_secret(item) for key, item in node.items())
        if isinstance(node, list):
            return any(self.holds_secret(item) for item in node)
        if isinstance(node, str):
            return self._holds(node)
        if isinstance(node, int | float) and not isinstance(node, bool):
            return self._holds(json.dumps(node))
        return False

    def _holds(self, text: str) -> bool:
        """Whether ``text`` holds a configured secret outside the text redaction inserted."""
        pattern = self.secret_pattern
        return pattern is not None and any(
            pattern.search(segment) is not None or self._decodes_to_secret(segment)
            for segment in _INSERTED_TEXT.split(text)
        )

    def _decodes_to_secret(self, segment: str) -> bool:
        """Whether percent-decoding ``segment`` reveals a configured secret's plaintext.

        Each decoder of ``_DECODERS`` is applied again and again until the text no longer
        changes, with no cap on the number of rounds, and every decoded form is searched.
        """
        for decode in _DECODERS:
            # No round cap is needed. A round that decodes any %XX escape shortens the text,
            # since each escape becomes at most one character (an invalid byte a single
            # U+FFFD). The only change that keeps the length is unquote_plus turning "+" into
            # spaces, which never become "+" again, and it leaves nothing to decode. So at most
            # len(segment) rounds change the text, and a segment with no "%" (nor, for
            # unquote_plus, "+") costs one call.
            decoded, previous = decode(segment), segment
            while decoded != previous:
                if any(secret in decoded for secret in self.secrets):
                    return True
                decoded, previous = decode(decoded), decoded
        return False

    def value(self, node: JSONValue, keys: frozenset[str]) -> JSONValue:
        """Redact a JSON body recursively.

        Values under ``keys`` (credentials and person keys) are blanked, numbers included;
        every ``answerDetails`` is blanked whole; dict keys and every other string go through
        :meth:`text`, which removes every configured secret wherever it occurs; a number equal
        to a configured account id becomes ``0``, and one whose JSON text equals a configured
        secret becomes the marker.

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
        if (
            isinstance(node, int | float)
            and not isinstance(node, bool)
            and json.dumps(node) in self.secrets
        ):
            # A digits-only secret echoed as a JSON number, blanked as a person number is.
            return _MARKER
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
    * any email address, and every configured secret (the LinkedIn client id included)
      wherever it occurs in a body string, dict key, URL or header value, whatever its length,
      in plain text or in any percent-encoded spelling (the latter as ``%3Credacted%3E``); a
      JSON number equal to a secret becomes the marker too;
    * the n8n webhook URL (a placeholder URL) and the configured Google customer id and
      LinkedIn ad account ids (same-length zeros);
    * the id of every LinkedIn organization URN (``urn:li:organization:``,
      ``urn:li:organizationBrand:`` and ``urn:li:company:``), plain or percent-encoded, in
      any body string, dict key, URL or header value (same-length zeros, prefix kept).

    :func:`_build_captures` then checks the result with
    :meth:`_RedactionPolicy.holds_secret` and never writes one that still holds a secret,
    however many times it was percent-encoded.

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
# Google's campaign days (Q-G3) are read for the 30 days ending yesterday, account-wide, so
# the keyword and search-term reads can be pointed at a campaign, day and ad group that
# delivered. Keywords and search terms exist only in Search campaigns, which are preferred.
_GOOGLE_LOOKBACK: Final = timedelta(days=30)
_SEARCH_CHANNEL: Final = "SEARCH"
# One demographic pivot is enough to record the segment surfaces (any of the three works).
_SEGMENT_PIVOT: Final = "MEMBER_SENIORITY"
# Capture-only bounds: each request reads one page (no further page token, next link or
# association continuation), and the LinkedIn segment fallback reads one delivering campaign,
# so every capture surface costs one request. Sync never passes these bounds.
_CAPTURE_PAGES: Final = 1
_CAPTURE_CAMPAIGNS: Final = 1
_CAPTURE_CONTINUATIONS: Final = 0
_LIFECYCLE_STAGE: Final = "lifecyclestage"
_CONTACTS: Final = "contacts"
_DEALS: Final = "deals"


def _capture_now() -> datetime:
    """The instant a capture's request windows end at: the current instant in UTC.

    Each source's capture reads it once, so all of its windows (Google's 30 days ending
    yesterday and the day read within them, LinkedIn's "yesterday", the 30-day lead and deal
    look-backs) end at the same instant. Tests replace it with the clock of the simulated
    world they serve, so the windows select the same records whatever the host clock reads.
    ``captured_at`` stays the response's arrival time.
    """
    return datetime.now(UTC)


@dataclasses.dataclass(slots=True)
class _CaptureRun:
    """The recorder of one source's capture and what went wrong on the way.

    Attributes:
        source: The source captured.
        recorder: The transport every call went through.
        failures: ``"<surface>: <reason>"`` for each call that failed, where reasons are the
            connectors' sanitised reasons, and for each capture left unwritten because a
            configured secret survived its redaction.
        skipped: Surfaces not called, or (Google) whose every answered query returned no
            rows, with why.
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

        Only the connectors' classified errors are caught; anything else propagates: a
        defect of the CLI, or the Google call budget becoming unavailable, which
        :func:`_capture_google` handles. A 401 stops the capture, because no later call can
        authenticate; a 403 on one surface (a scope the token lacks) only fails that surface.

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


def _row_id(row: Mapping[str, Any], resource: str) -> int | None:
    """``{resource}.id`` of one GoogleAdsRow (``campaign``, ``adGroup``), sent as a string.

    Returns:
        The id, or ``None`` when the row lacks it or it is not a plain decimal number.
    """
    entity = row.get(resource)
    if isinstance(entity, Mapping):
        raw = entity.get("id")
        if isinstance(raw, str | int) and not isinstance(raw, bool):
            text = str(raw)
            if text.isascii() and text.isdigit():
                return int(text)
    return None


def _first_id(rows: Sequence[Mapping[str, Any]], resource: str) -> int | None:
    """The id of the first GoogleAdsRow carrying ``resource`` (``campaign``, ``adGroup``)."""
    for row in rows:
        found = _row_id(row, resource)
        if found is not None:
            return found
    return None


def _impressions(row: Mapping[str, Any]) -> int:
    """``metrics.impressions`` of one GoogleAdsRow, used only to choose a capture's scope.

    The int64 arrives as a string. An absent or unreadable value counts as no impressions:
    it only ranks candidates, and nothing derived from it is stored or reported.
    """
    metrics = row.get("metrics")
    raw = metrics.get("impressions") if isinstance(metrics, Mapping) else None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(raw, 0)
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        return int(raw)
    return 0


def _row_day(row: Mapping[str, Any]) -> date | None:
    """``segments.date`` of one GoogleAdsRow, or ``None`` when absent or not an ISO date."""
    segments = row.get("segments")
    raw = segments.get("date") if isinstance(segments, Mapping) else None
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


@dataclasses.dataclass(slots=True)
class _GoogleTargets:
    """What a Google capture has read so far that points its later queries at data.

    Attributes:
        yesterday: The day before the capture instant (UTC); every window ends on it.
        campaign_id: The first Q-G2 campaign: Q-G2b's id, and the fallback campaign range.
        ad_group_id: The first Q-G4 ad group: the fallback ad-group range.
        channels: Q-G2's ``advertisingChannelType`` by campaign id.
        ad_group_campaigns: Q-G4's ``campaign.id`` by ad-group id, in the order listed.
        delivering: The campaign with the most impressions in Q-G3's 30 days, a Search
            campaign when any Search campaign had impressions; ``None`` when none had any.
        delivering_is_search: Whether ``delivering`` is a Search campaign.
        day: The latest day ``delivering`` had impressions.
        keyword_ad_group: The ad group with the most impressions in Q-G5's rows, taken from
            ``delivering``'s ad groups first, then from Search campaigns', then from any;
            the first row's ad group when none had impressions; ``None`` without a row.
        keyword_words: ``keyword_ad_group`` in words, without the day Q-G5 read.
    """

    yesterday: date
    campaign_id: int | None = None
    ad_group_id: int | None = None
    channels: dict[int, str] = dataclasses.field(default_factory=dict)
    ad_group_campaigns: dict[int, int] = dataclasses.field(default_factory=dict)
    delivering: int | None = None
    delivering_is_search: bool = False
    day: date | None = None
    keyword_ad_group: int | None = None
    keyword_words: str = ""

    def observe(self, query_id: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Take what the later queries are narrowed by from one answered query's rows."""
        if query_id == "Q-G2":
            self.campaign_id = _first_id(rows, "campaign")
            for row in rows:
                campaign_id, campaign = _row_id(row, "campaign"), row.get("campaign")
                if campaign_id is not None and isinstance(campaign, Mapping):
                    channel = campaign.get("advertisingChannelType")
                    if isinstance(channel, str):
                        self.channels[campaign_id] = channel
        elif query_id == "Q-G3":
            self._choose_campaign(rows)
        elif query_id == "Q-G4":
            self.ad_group_id = _first_id(rows, "adGroup")
            for row in rows:
                ad_group_id, campaign_id = _row_id(row, "adGroup"), _row_id(row, "campaign")
                if ad_group_id is not None and campaign_id is not None:
                    self.ad_group_campaigns.setdefault(ad_group_id, campaign_id)
        elif query_id == "Q-G5":
            self._choose_ad_group(rows)

    def top_campaign(self) -> str:
        """``delivering`` in words."""
        kind = "Search campaign" if self.delivering_is_search else "campaign"
        return f"the {kind} with the most impressions in the last {_GOOGLE_LOOKBACK.days} days"

    def keyword_day(self) -> tuple[date, str]:
        """The one day Q-G5 and Q-G6 read, with that day in words."""
        if self.delivering is not None and self.day is not None:
            return self.day, f"the latest day with impressions of {self.top_campaign()}"
        return self.yesterday, "yesterday"

    def first_ad_group_of(self, campaign_id: int | None) -> int | None:
        """The first Q-G4 ad group of ``campaign_id``, or ``None``."""
        if campaign_id is None:
            return None
        for ad_group_id, owner in self.ad_group_campaigns.items():
            if owner == campaign_id:
                return ad_group_id
        return None

    def _is_search(self, campaign_id: int | None) -> bool:
        """Whether Q-G2 listed ``campaign_id`` as a Search campaign."""
        return campaign_id is not None and self.channels.get(campaign_id) == _SEARCH_CHANNEL

    def _choose_campaign(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Choose ``delivering`` and ``day`` from Q-G3's campaign-day rows.

        Impressions are summed per campaign over the rows; a row without a campaign id or a
        date counts for nothing. Ties go to the lower campaign id.
        """
        totals: dict[int, int] = {}
        latest: dict[int, date] = {}
        for row in rows:
            campaign_id, day = _row_id(row, "campaign"), _row_day(row)
            impressions = _impressions(row)
            if campaign_id is None or day is None or impressions == 0:
                continue
            totals[campaign_id] = totals.get(campaign_id, 0) + impressions
            if campaign_id not in latest or day > latest[campaign_id]:
                latest[campaign_id] = day
        search = [campaign_id for campaign_id in totals if self._is_search(campaign_id)]
        candidates = search or list(totals)
        if not candidates:
            return
        chosen = max(candidates, key=lambda campaign_id: (totals[campaign_id], -campaign_id))
        self.delivering = chosen
        self.delivering_is_search = bool(search)
        self.day = latest[chosen]

    def _choose_ad_group(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Choose ``keyword_ad_group`` from Q-G5's keyword-day rows.

        With a delivering campaign Q-G5 reads every campaign, so the ad groups of
        ``delivering`` come first, then those of Search campaigns (owners from Q-G4, channels
        from Q-G2), then any; within the first of these sets that had impressions the most
        impressions win, ties going to the lower ad-group id.
        """
        totals: dict[int, int] = {}
        for row in rows:
            ad_group_id = _row_id(row, "adGroup")
            if ad_group_id is not None:
                totals[ad_group_id] = totals.get(ad_group_id, 0) + _impressions(row)
        if not totals:
            return
        owners = self.ad_group_campaigns
        tiers = (
            [a for a in totals if self.delivering is not None and owners.get(a) == self.delivering],
            [a for a in totals if self._is_search(owners.get(a))],
            list(totals),
        )
        for tier in tiers:
            delivered = [ad_group_id for ad_group_id in tier if totals[ad_group_id] > 0]
            if delivered:
                self.keyword_ad_group = max(delivered, key=lambda a: (totals[a], -a))
                self.keyword_words = "the ad group with the most keyword impressions"
                return
        self.keyword_ad_group = next(iter(totals))
        self.keyword_words = "the first ad group of the keyword rows read"


def _first_row(body: JSONValue) -> JSONValue | None:
    """The first ``results[]`` row of an n8n envelope (or its one-element array wrapper)."""
    envelope = body[0] if isinstance(body, list) and len(body) == 1 else body
    if isinstance(envelope, dict):
        results = envelope.get("results")
        if isinstance(results, list) and results:
            return results[0]
    return None


def _google_scope(query_id: str, targets: _GoogleTargets) -> tuple[dict[str, object], str] | None:
    """The scope one GAQL template is sent with, and that scope in words, never in ids.

    Derived from the template's grammar and from what the earlier queries returned
    (``targets``), so each query reads where data was seen:

    * Q-G2 and Q-G4 start their keyset at id 0; Q-G2b looks up the first Q-G2 campaign, and
      is skipped (``None``) when none was read. Q-G1 and Q-G8 are not narrowed.
    * Q-G3 reads every campaign over the 30 days ending yesterday. Its rows choose the
      delivering campaign (:meth:`_GoogleTargets.observe`).
    * Q-G5 reads every campaign on the latest day the delivering campaign had impressions.
      Its rows choose the keyword ad group.
    * Q-G6 reads that day for the keyword ad group, or else for the delivering campaign.
    * Q-G7 reads the keyword ad group, or else the delivering campaign's first Q-G4 ad group.
    * Without a delivering campaign, Q-G5 and Q-G6 read yesterday and every range falls back
      to the first Q-G4 ad group, then to the first Q-G2 campaign.

    Returns:
        ``(scope, words)`` for :meth:`QueryTemplate.render`, or ``None`` to skip the query.
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
    words: list[str] = []
    if LAST_ID in required:
        scope[LAST_ID] = 0
        words.append("the first keyset page")
    if IDS in required:
        if targets.campaign_id is None:
            return None
        scope[IDS] = [targets.campaign_id]
        words.append("the first campaign listed")
    dated = START in required
    day, day_words = targets.keyword_day()
    if query_id == "Q-G3" or (query_id == "Q-G5" and targets.delivering is not None):
        # Account-wide: Q-G3 to find the delivering campaign, Q-G5 to find its keywords.
        ranges: list[tuple[str, str, int | None, str]] = []
        words.append("every campaign")
    else:
        top = targets.top_campaign()
        # Undated (Q-G7), the keyword ad group is named with the day it was chosen on.
        keyword_words = targets.keyword_words
        if not dated:
            keyword_words += " yesterday" if day_words == "yesterday" else f" on {day_words}"
        ranges = [
            (AD_GROUP_LO, AD_GROUP_HI, targets.keyword_ad_group, keyword_words),
            (CAMPAIGN_LO, CAMPAIGN_HI, targets.delivering, top),
            (
                AD_GROUP_LO,
                AD_GROUP_HI,
                targets.first_ad_group_of(targets.delivering),
                f"the first ad group listed for {top}",
            ),
            (AD_GROUP_LO, AD_GROUP_HI, targets.ad_group_id, "the first ad group listed"),
            (CAMPAIGN_LO, CAMPAIGN_HI, targets.campaign_id, "the first campaign listed"),
        ]
    for low, high, entity_id, entity_words in ranges:
        if low in allowed and entity_id is not None:
            scope[low] = entity_id
            scope[high] = entity_id
            words.append(entity_words)
            if entity_id == targets.delivering and low == CAMPAIGN_LO:
                day_words = "its latest day with impressions"
            break
    if dated:
        if query_id == "Q-G3":
            day = targets.yesterday
            scope[START] = day - _GOOGLE_LOOKBACK + timedelta(days=1)
            day_words = f"the {_GOOGLE_LOOKBACK.days} days ending yesterday"
        else:
            scope[START] = day
        scope[END] = day
        words.append(day_words)
    return scope, ", ".join(words) or "the whole account"


async def _google_budget(settings: Settings) -> _CaptureCallBudget:
    """The call budget of a Google capture, read once before any request is built.

    The product's shared ``GoogleCallBudget``, so every capture call counts in
    ``source_connection.calls_24h`` against ``GOOGLE_DAILY_CALL_BUDGET`` like every other
    webhook call. The window is read first, so a database that cannot meter the calls
    refuses the capture before any request leaves.

    Raises:
        _CallBudgetUnavailableError: When the database cannot be read; no webhook call was sent.
    """
    from ads_engine.sync.budget_guard import GoogleCallBudget

    budget = _CaptureCallBudget(GoogleCallBudget(settings))
    remaining = await budget.remaining()
    log.info("capture_call_budget", source="google", remaining=remaining)
    return budget


async def _capture_google(settings: Settings, run: _CaptureRun) -> None:
    """One webhook ``search`` call per GAQL template (Q-G1 … Q-G8, Q-G2b), each one page.

    The first call becomes ``search.json``; the first row of each query becomes its
    ``row_*`` capture. Only the first page is read: a capture records shapes, not data.

    Each query is pointed at data the earlier ones returned (:func:`_google_scope`). Q-G3
    reads every campaign over the 30 days ending yesterday. The campaign with the most
    impressions there is the delivering campaign; a Search campaign is preferred whenever
    one had impressions, because keywords and search terms exist only there. Q-G5 reads
    every campaign on that campaign's latest day with impressions. Q-G6 reads that day, and
    Q-G7 reads the keywords, of the ad group with the most impressions in Q-G5's rows,
    preferring the delivering campaign's ad groups, then those of Search campaigns. Without
    a delivering campaign the reads fall back to yesterday and to the first ad group or
    campaign listed. Q-G2b looks up the first Q-G2 campaign.

    A row surface whose every answered query returned no rows is added to ``run.skipped`` as
    ``"<surface>: search <query> returned no rows (<scope in words>)"``. The scope is
    described, never quoted, so no id reaches the report. A query that failed or was not
    sent is reported as such and adds no such entry.

    Every POST attempt is reserved in the shared call budget first. When a reservation fails
    on the database, that query and every later one are not sent and the failure is
    recorded; the captures already made are kept, because their calls were counted.

    Raises:
        _CallBudgetUnavailableError: When the shared call budget cannot be read before the
            first call; no webhook call was sent.
    """
    from ads_engine.connectors.google_n8n.client import GoogleAdsWebhookClient
    from ads_engine.connectors.google_n8n.queries import QUERIES
    from ads_engine.connectors.http import build_client

    # One call per template, each reserved in the shared budget before its POST.
    budget = await _google_budget(settings)
    targets = _GoogleTargets(yesterday=_capture_now().date() - timedelta(days=1))
    # Row surfaces some answered query returned a row for, and the empty answers of the rest.
    with_rows: set[str] = set()
    empty: dict[str, list[str]] = {}
    async with build_client("google", run.recorder, settings=settings) as http:
        client = GoogleAdsWebhookClient.from_settings(settings, http, budget)
        for query_id, template in QUERIES.items():
            surface = f"search {query_id}"
            if budget.failure is not None:
                run.skipped.append(f"{surface}: not sent, the shared call budget is unavailable")
                continue
            chosen = _google_scope(query_id, targets)
            if chosen is None:
                run.skipped.append(f"{surface}: no campaign id was read to look up")
                continue
            scope, scope_words = chosen
            query = template.render(**scope)
            try:
                page = await run.step(surface, partial(client.search_page, query_id, query))
            except _CallBudgetUnavailableError as exc:
                run.failures.append(f"{surface}: {exc}; no further webhook call was sent")
                continue
            if page is None:
                continue
            # search_page returned, so the last exchange is this query's successful POST.
            run.rows.append((template.row_surface, run.recorder.exchanges[-1]))
            if page.rows:
                with_rows.add(template.row_surface)
            else:
                empty.setdefault(template.row_surface, []).append(
                    f"{surface} returned no rows ({scope_words})"
                )
            targets.observe(query_id, page.rows)
    for row_surface, answers in empty.items():
        if row_surface not in with_rows:
            run.skipped.append(f"{row_surface}: {'; '.join(answers)}")


async def _capture_linkedin(settings: Settings, run: _CaptureRun) -> None:
    """One minimal call per LinkedIn surface, for the first configured ad account.

    ``access_token`` (refresh-token trio only), ``ad_account``, ``ad_campaign_groups``
    (first page), ``ad_campaigns`` (first page), ``creatives`` (first campaign, first page),
    ``posts`` (first creative's post), ``ad_analytics`` (yesterday, DAILY, first response),
    ``ad_analytics_statistics`` (one pivot, ALL, first response), ``lead_forms``
    (``count=1``), ``lead_form_responses`` (``count=1``) and ``lead_form_responses_batch``
    (the lead just read, when there is one). Each call reads one page: no later page token
    or ``next`` link is followed, so the listings are incomplete ("page limit") by design.

    When LinkedIn rejects the Statistics Finder with HTTP 400, the segment call takes the
    client's live fallback with the same bounds: one ``ad_analytics_campaign_delivery``
    discovery request and one ``ad_analytics_member_segments`` request for the first
    delivering campaign, each captured under its own surface.
    """
    from ads_engine.connectors.http import build_client
    from ads_engine.connectors.linkedin.auth import LinkedInTokenProvider
    from ads_engine.connectors.linkedin.client import LinkedInClient

    account = settings.linkedin_ad_account_ids[0]
    now = _capture_now()
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
        groups = await run.step(
            "ad_campaign_groups",
            partial(client.list_campaign_groups, account, max_pages=_CAPTURE_PAGES),
        )
        if groups is not None:
            run.interrupted("ad_campaign_groups", groups.interrupted_by)
        campaigns = await run.step(
            "ad_campaigns", partial(client.list_campaigns, account, max_pages=_CAPTURE_PAGES)
        )
        campaign_urn: str | None = None
        if campaigns is not None:
            run.interrupted("ad_campaigns", campaigns.interrupted_by)
            campaign_urn = campaigns.rows[0].urn if campaigns.rows else None
        await _capture_linkedin_creatives(client, account, campaign_urn, run)
        analytics = await run.step(
            "ad_analytics",
            partial(
                client.analytics_daily,
                account,
                (day, day),
                expected_creatives=1,
                max_pages=_CAPTURE_PAGES,
            ),
        )
        if analytics is not None:
            run.interrupted("ad_analytics", analytics.interrupted_by)
        segments = await run.step(
            "ad_analytics_statistics",
            partial(
                client.analytics_segments,
                account,
                _SEGMENT_PIVOT,
                (day, day),
                max_pages=_CAPTURE_PAGES,
                max_campaigns=_CAPTURE_CAMPAIGNS,
            ),
        )
        if segments is not None:
            run.interrupted("ad_analytics_statistics", segments.interrupted_by)
        await _capture_linkedin_leads(client, account, now, run)


async def _capture_linkedin_creatives(
    client: LinkedInClient, account: str, campaign_urn: str | None, run: _CaptureRun
) -> None:
    """Capture ``creatives`` (one campaign, first page) and ``posts`` for one creative's post."""
    if campaign_urn is None:
        run.skipped.append("creatives: the account has no campaign")
        run.skipped.append("posts: no creative was read")
        return
    creatives = await run.step(
        "creatives",
        partial(client.list_creatives, account, [campaign_urn], max_pages=_CAPTURE_PAGES),
    )
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
    """Capture ``lead_forms``, ``lead_form_responses`` and ``lead_form_responses_batch``.

    The responses listing reads one response (``count=1``), so the batch re-fetch is skipped
    whenever that response yields no sponsored lead, with a reason that says which case held:
    nothing was read (the call failed or a transient error cut it short), the response read
    was a test lead (dropped at ingest and never re-fetched; a sponsored lead may still exist
    in the window), or the window held no response at all.
    """
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
        if responses.interrupted_by is not None:
            reason = "no lead response was read"
        elif responses.test_leads_skipped > 0:
            reason = "the one lead response read was a test lead, which is never re-fetched"
        else:
            reason = "no sponsored lead in the last 30 days"
        run.skipped.append(f"lead_form_responses_batch: {reason}")
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
    ``associations_batch_read`` (that deal's contacts, or one input with no deal; the initial
    call only, so an input with more pages is ``partial`` and its continuation is never sent)
    and ``objects_batch_read`` (that contact by id, or the probe email batch with zero
    matches).
    """
    from ads_engine.connectors.http import build_client
    from ads_engine.connectors.hubspot.client import PROBE_EMAIL, HubSpotClient
    from ads_engine.connectors.hubspot.search import deal_search_filter, search_body

    now = _capture_now()
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
                partial(
                    client.associations,
                    _DEALS,
                    _CONTACTS,
                    [deal_id],
                    max_continuations=_CAPTURE_CONTINUATIONS,
                ),
            )
            if fetch is not None and fetch.all_targets:
                contact_id = fetch.all_targets[0]
        else:
            # As the recovery probe does: one input with no deal, answered as a multi-status.
            await run.step(
                "associations_batch_read",
                partial(
                    client.associations,
                    _CONTACTS,
                    _DEALS,
                    ["0"],
                    max_continuations=_CAPTURE_CONTINUATIONS,
                ),
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


# The failure reported for a capture that fails closed; it never carries the value.
_FAIL_CLOSED_REASON: Final = "not written: a configured secret survived redaction"


def _build_captures(run: _CaptureRun, policy: _RedactionPolicy) -> dict[str, dict[str, JSONValue]]:
    """Choose the first 2xx JSON exchange per surface and turn each into a redacted capture.

    Surfaces are identified with ``payloads.identify_surface``, the router the simulator
    uses. For Google, the first row of each query's page is added as its ``row_*`` capture;
    a query that returned no rows adds none (:func:`_capture_google` reports its surface in
    ``run.skipped``).

    Each redacted capture is checked with :meth:`_RedactionPolicy.holds_secret`, failing
    closed: one that still holds a configured secret is left out, so it is never written, and
    ``"<surface>: not written: a configured secret survived redaction"`` is added to
    ``run.failures``, which makes the command exit 1. A surface is decided by its first
    exchange either way. A capture that only breaks its contract is kept.

    Returns:
        Redacted capture documents keyed by surface name, none holding a configured secret.
    """
    from ads_engine.simulation.payloads import UnknownSurface, identify_surface

    source = run.source
    documents: dict[str, dict[str, JSONValue]] = {}
    refused: set[str] = set()

    def admit(surface: str, raw: dict[str, JSONValue]) -> None:
        """Keep the redacted ``raw`` as ``surface``, unless a configured secret survived."""
        document = _redact_capture(raw, policy, source)
        if policy.holds_secret(document):
            refused.add(surface)
            run.failures.append(f"{surface}: {_FAIL_CLOSED_REASON}")
        else:
            documents[surface] = document

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
        if surface not in documents and surface not in refused:
            admit(surface, _capture_document(source, surface, exchange, body))
    for row_surface, exchange in run.rows:
        if row_surface in documents or row_surface in refused:
            continue
        body, is_json = _decode_json(exchange.content)
        row = _first_row(body) if is_json else None
        if row is None:
            continue
        admit(row_surface, _capture_document(source, row_surface, exchange, row))
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


# The output directory of a source's captures, under tests/contracts/{source}/.
_CAPTURED_DIR: Final = "captured"
# Directory opens and temporary-file creation never follow a symbolic link in the last path
# component, and O_DIRECTORY rejects any other non-directory. No descriptor reaches a child.
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_TEMPORARY_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_CAPTURE_FILE_MODE: Final = 0o644
_WRITE_PROBE: Final = "write-probe"


class _OutputRefusedError(Exception):
    """The capture output directory cannot be used safely; the message says which and why."""


def _directory_problem(exc: OSError, *, writing: bool) -> str:
    """Why an existing directory cannot be used, from the ``OSError`` an operation raised.

    Args:
        exc: The error of opening the directory, or of creating an entry in it.
        writing: Whether the operation created an entry, so a permission error means the
            directory is not writable rather than not accessible.

    Returns:
        A predicate for the directory's displayed path.
    """
    match exc.errno:
        case errno.EACCES | errno.EPERM if writing:
            return (
                f"is not writable by this user (uid {os.geteuid()}); in the backend image the "
                "runtime user owns only tests/contracts/*/captured/"
            )
        case errno.EACCES | errno.EPERM:
            return f"is not accessible to this user (uid {os.geteuid()})"
        case errno.EROFS:
            return "is on a read-only file system"
        case _:
            return f"cannot be used ({exc.strerror or type(exc).__name__})"


def _open_directory(parent_fd: int, name: str, path: Path) -> int:
    """Open the directory ``name`` under ``parent_fd`` without following a symbolic link.

    Linux reports a symbolic link met under ``O_NOFOLLOW | O_DIRECTORY`` as ``ENOTDIR``, the
    same as a regular file, so a failed open examines the entry itself to say which it is.

    Args:
        parent_fd: The open parent directory.
        name: The directory's name, one path component.
        path: The directory's full path, for the refusal.

    Returns:
        A read-only descriptor of the directory, which the caller closes.

    Raises:
        _OutputRefusedError: When ``name`` is a symbolic link, is not a directory or cannot be
            opened.
    """
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        reason = _directory_problem(exc, writing=False)
        with contextlib.suppress(OSError):
            mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                reason = "is a symbolic link; captures are written only into a real directory"
            elif not stat.S_ISDIR(mode):
                reason = "is not a directory"
        raise _OutputRefusedError(f"{_display_path(path)} {reason}") from None


def _create_temporary(dir_fd: int, stem: str) -> tuple[str, int]:
    """Create a new, empty temporary file ``.{stem}.{16 hex digits}.tmp`` in a directory.

    ``O_CREAT | O_EXCL | O_NOFOLLOW`` either creates the file or fails, so an entry already at
    that name, a symbolic link included, is never opened or written through. The random part
    keeps concurrent captures of one surface on separate files.

    Args:
        dir_fd: The open directory.
        stem: The name the temporary stands in for.

    Returns:
        The temporary's name, relative to ``dir_fd``, and a descriptor open for writing,
        which the caller closes.

    Raises:
        OSError: When the file cannot be created; ``FileExistsError`` when the name is taken.
    """
    name = f".{stem}.{token_hex(8)}.tmp"
    return name, os.open(name, _TEMPORARY_FLAGS, _CAPTURE_FILE_MODE, dir_fd=dir_fd)


class _CaptureOutput:
    """The verified ``tests/contracts/{source}/captured/`` directory of one capture, held open.

    :meth:`open` runs before any request is sent. Every later write goes through its open
    descriptor, so replacing a directory on the path afterwards cannot redirect a capture.

    Attributes:
        path: The directory's path; written files are displayed relative to the working
            directory from it.
    """

    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self.path = path

    @classmethod
    def open(cls, root: Path, source: Source) -> _CaptureOutput:
        """Open ``{root}/{source}/captured/`` and prove this user can write into it.

        The walk from ``root`` takes one component at a time with ``O_NOFOLLOW`` and
        ``O_DIRECTORY``, so a symbolic link or a non-directory at ``{source}`` or
        ``captured`` is refused instead of followed. A missing ``captured/`` is created.
        Writability is proved by creating and removing a real temporary file, because
        permission bits alone miss read-only mounts and ACLs.

        Args:
            root: The contracts directory, ``tests/contracts`` under the working directory.
            source: The source captured.

        Returns:
            The open output directory; the caller closes it.

        Raises:
            _OutputRefusedError: When ``{source}`` or ``captured`` is a symbolic link or not a
                directory, or ``captured/`` cannot be created or written by this user.
        """
        source_path = root / source
        path = source_path / _CAPTURED_DIR
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            reason = _directory_problem(exc, writing=False)
            raise _OutputRefusedError(f"{_display_path(root)} {reason}") from None
        try:
            source_fd = _open_directory(root_fd, source, source_path)
            try:
                try:
                    os.mkdir(_CAPTURED_DIR, dir_fd=source_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    reason = _directory_problem(exc, writing=True)
                    raise _OutputRefusedError(
                        f"{_display_path(path)} is missing and cannot be created: "
                        f"{_display_path(source_path)} {reason}"
                    ) from None
                fd = _open_directory(source_fd, _CAPTURED_DIR, path)
            finally:
                os.close(source_fd)
        finally:
            os.close(root_fd)
        output = cls(fd, path)
        try:
            output._probe()
        except BaseException:
            output.close()
            raise
        return output

    def _probe(self) -> None:
        """Create and remove a temporary file through :func:`_create_temporary`.

        Raises:
            _OutputRefusedError: When the temporary file cannot be created.
        """
        try:
            name, fd = _create_temporary(self._fd, _WRITE_PROBE)
        except OSError as exc:
            reason = _directory_problem(exc, writing=True)
            raise _OutputRefusedError(f"{_display_path(self.path)} {reason}") from None
        os.close(fd)
        os.unlink(name, dir_fd=self._fd)

    def write(self, name: str, content: bytes) -> Path:
        """Replace the file ``name`` in this directory with ``content``, atomically.

        The bytes go to a new temporary sibling from :func:`_create_temporary` and are synced
        to disk; the closed temporary is then renamed over ``name``. A reader sees the old file
        or the complete new one, concurrent writers each rename their own complete file, and a
        symbolic link at ``name`` is replaced, never followed. When any step fails, this
        call's own temporary is removed and the error propagates.

        Args:
            name: The file name, one path component.
            content: The complete file content.

        Returns:
            The file's path.

        Raises:
            OSError: When the file cannot be written or renamed into place.
        """
        temporary, fd = _create_temporary(self._fd, name)
        renamed = False
        try:
            try:
                view = memoryview(content)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            renamed = True
        finally:
            if not renamed:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=self._fd)
        return self.path / name

    def close(self) -> None:
        """Close the directory descriptor; a second call does nothing."""
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def _write_captures(
    output: _CaptureOutput, documents: Mapping[str, dict[str, JSONValue]]
) -> list[Path]:
    """Write each capture as ``{surface}.json`` in ``output``, replacing an older one atomically.

    Files are canonical JSON (sorted keys, two-space indent, UTF-8, trailing newline), the
    format of every fixture.

    Returns:
        The written paths, sorted by surface.
    """
    from ads_engine.simulation.payloads import canonical_json

    written: list[Path] = []
    for name in sorted(documents):
        content = canonical_json(documents[name]).encode("utf-8")
        written.append(output.write(f"{name}.json", content))
    return written


def _display_path(path: Path) -> str:
    """``path`` relative to the working directory when it lies under it."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


# The names Settings.missing_credentials("linkedin") reports around the refresh-token trio's
# unset members: the account ids first, and the static access token last whenever neither
# authentication alternative is complete. The trio is LINKEDIN_CLIENT_ID,
# LINKEDIN_CLIENT_SECRET and LINKEDIN_REFRESH_TOKEN.
_LINKEDIN_ACCOUNTS_NAME: Final = "LINKEDIN_AD_ACCOUNT_IDS"
_LINKEDIN_STATIC_NAME: Final = "LINKEDIN_ACCESS_TOKEN"
_LINKEDIN_REFRESH_TRIO_SIZE: Final = 3


def _missing_credentials_text(settings: Settings, source: Source) -> str:
    """Name the variables ``source`` still needs to run live, as the capture refusal says it.

    Every Google and HubSpot credential is required, so their unset names are listed as
    :meth:`Settings.missing_credentials` reports them. LinkedIn needs
    ``LINKEDIN_AD_ACCOUNT_IDS`` plus either the refresh-token trio or
    ``LINKEDIN_ACCESS_TOKEN``, so its text states the alternative and names only the trio
    members still unset, for example ``LINKEDIN_AD_ACCOUNT_IDS and either LINKEDIN_CLIENT_ID,
    LINKEDIN_CLIENT_SECRET and LINKEDIN_REFRESH_TOKEN, or LINKEDIN_ACCESS_TOKEN``. A list of
    any other shape is listed as it is. Only names appear, never a value.

    Args:
        settings: The settings to check.
        source: The source to check.

    Returns:
        The text that follows ``set`` in the refusal; empty when ``source`` is fully
        configured.
    """
    missing = settings.missing_credentials(source)
    listed = ", ".join(missing)
    if source != "linkedin":
        return listed
    authentication = [name for name in missing if name != _LINKEDIN_ACCOUNTS_NAME]
    trio = authentication[:-1]
    if not trio or authentication[-1] != _LINKEDIN_STATIC_NAME:
        return listed
    trio_text = trio[0] if len(trio) == 1 else f"{', '.join(trio[:-1])} and {trio[-1]}"
    if len(trio) < _LINKEDIN_REFRESH_TRIO_SIZE:
        trio_text += " (to complete the refresh-token trio)"
    alternatives = f"either {trio_text}, or {_LINKEDIN_STATIC_NAME}"
    if _LINKEDIN_ACCOUNTS_NAME in missing:
        return f"{_LINKEDIN_ACCOUNTS_NAME} and {alternatives}"
    return alternatives


def _cmd_capture_fixtures(args: argparse.Namespace) -> int:
    """``capture-fixtures --source S``: record redacted live contract captures.

    Refused (exit 1, nothing sent or written) under ``DATA_MODE=simulated``, without the
    source's credentials, when the output directory cannot be written safely:
    ``tests/contracts/{source}`` or its ``captured/`` is a symbolic link or not a directory,
    or this user cannot create files in ``captured/`` (:meth:`_CaptureOutput.open`; a missing
    ``captured/`` is created), and for Google when the shared call budget in the database
    cannot be read: every Google call is reserved in ``source_connection.calls_24h`` before
    it is sent, never counted locally. The output goes to
    ``tests/contracts/{source}/captured/`` under the working directory, so run it from
    ``backend/``.

    Returns:
        :data:`EXIT_OK` when every surface was either captured or reported on stderr as not
        captured with its data reason (such as no sponsored LinkedIn lead to re-fetch, or a
        Google query that answered with no rows), no call failed, no capture held a
        configured secret, and every capture satisfies its contract. A surface reported as
        not captured gets one ``cli_warning`` line (:func:`_warning`), not a ``cli_error``
        line, is counted in ``capture_fixtures_finished`` ``skipped`` and does not by itself
        make the exit non-zero. Every refusal and failure (a failed call, a capture left
        unwritten, a contract break, nothing captured) is one ``cli_error`` line
        (:func:`_error`).
        :data:`EXIT_FAILURE` when a refusal applies, a call failed (a Google call the shared
        budget refused or could not reserve included), nothing was captured, a capture still
        held a configured secret after redaction (that capture is not written), or a capture
        breaks its contract (the captures read are still written, because they are the live
        truth the contract tests must flag).

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
        missing = _missing_credentials_text(settings, source)
        _error(f"capture-fixtures {source}: live credentials are not configured; set {missing}")
        return EXIT_FAILURE
    root = contracts_root()
    source_dir = root / source
    if not source_dir.is_dir():
        raise _UsageError(
            f"{_display_path(source_dir)} not found; run from backend/ (or /app in the image)"
        )
    try:
        output = _CaptureOutput.open(root, source)
    except _OutputRefusedError as exc:
        _error(f"capture-fixtures {source}: {_redacted(str(exc))}; nothing was sent")
        return EXIT_FAILURE
    try:
        thresholds = Thresholds.load() if source == "hubspot" else None
        policy = _RedactionPolicy.from_settings(settings)
        try:
            run = _run_async(lambda: _capture(source, settings, thresholds))
        except _CallBudgetUnavailableError as exc:
            _error(f"capture-fixtures {source}: {exc}; no webhook call was sent")
            return EXIT_FAILURE
        documents = _build_captures(run, policy)
        problems = _contract_problems(documents, root)
        written = _write_captures(output, documents) if documents else []
    finally:
        output.close()
    for path in written:
        print(_display_path(path), flush=True)
    for failure in run.failures:
        _error(f"capture-fixtures {source}: {_redacted(failure)}")
    for skipped in run.skipped:
        _warning(f"capture-fixtures {source}: not captured: {skipped}")
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

# argparse reasons that carry no user input, passed through unchanged after the argument name.
_VALUE_FREE_REASONS: Final[frozenset[str]] = frozenset(
    {
        _DATE_FORMAT_REASON,
        "expected one argument",
        "expected at most one argument",
        "expected at least one argument",
    }
)
_EXPECTED_ARGUMENTS_RE: Final = re.compile(r"expected [0-9]+ arguments?")
_UNRECOGNIZED_COUNT_RE: Final = re.compile(r"unrecognized arguments \([0-9]+\)")
_REQUIRED_PREFIX: Final = "the following arguments are required: "


def _action_name(action: argparse.Action) -> str | None:
    """Name an argument as argparse's diagnostics do: ``-h/--help``, ``--start``, ``COMMAND``.

    Returns:
        The joined option strings, else a string metavar, else the destination; ``None`` for
        a suppressed one. A name that differs from argparse's own only makes
        :meth:`_RedactingArgumentParser.error` fall back to its generic message.
    """
    if action.option_strings:
        return "/".join(action.option_strings)
    if isinstance(action.metavar, str) and action.metavar != argparse.SUPPRESS:
        return action.metavar
    if action.dest != argparse.SUPPRESS:
        return action.dest
    return None


def _argument_reason(action: argparse.Action, reason: str) -> str | None:
    """Rewrite argparse's reason for rejecting one argument without the text it rejected.

    Args:
        action: The argument the reason concerns.
        reason: argparse's reason, which may quote the supplied value.

    Returns:
        A value-free reason that keeps the useful part (the choices, the expected type), or
        ``None`` when the reason is in no recognised form.
    """
    if reason in _VALUE_FREE_REASONS or _EXPECTED_ARGUMENTS_RE.fullmatch(reason):
        return reason
    if reason.startswith(("invalid choice: ", "unknown parser ")):
        choices = () if action.choices is None else tuple(str(choice) for choice in action.choices)
        return f"invalid choice (choose from {', '.join(choices)})" if choices else "invalid choice"
    if reason.startswith("ignored explicit argument "):
        return "ignored explicit argument"
    if reason.startswith("invalid ") and " value: " in reason:
        type_name = getattr(action.type, "__name__", None)
        if isinstance(type_name, str) and type_name.isidentifier():
            return f"invalid {type_name} value"
        return "invalid value"
    return None


class _RedactingArgumentParser(argparse.ArgumentParser):
    """An argument parser whose usage errors never repeat what was typed on the command line.

    argparse's diagnostics quote the offending text: an unparsable value, an invalid choice or
    command, unrecognized arguments, an option's ``=value``. The CLI parses before logging and
    its redaction are configured, so a pasted email address or credential would reach stderr
    verbatim, control characters included. :meth:`error` therefore rebuilds every diagnostic
    from the parser's own vocabulary (argument and command names, choices, the names of missing
    required arguments) and fails closed to a generic message for any form it does not
    recognise. Sub-parsers inherit the class, because :meth:`add_subparsers` defaults
    ``parser_class`` to the parent's type. Redaction covers the exiting path, ``exit_on_error``
    (the default, which :func:`build_parser` keeps).
    """

    @overload
    def parse_args(
        self, args: Iterable[str] | None = None, namespace: None = None
    ) -> argparse.Namespace: ...

    @overload
    def parse_args[N](self, args: Iterable[str] | None, namespace: N) -> N: ...

    @overload
    def parse_args[N](self, *, namespace: N) -> N: ...

    def parse_args(self, args: Iterable[str] | None = None, namespace: object = None) -> object:
        """Parse as :meth:`argparse.ArgumentParser.parse_args` does, counting unrecognized input.

        argparse quotes every unrecognized argument; this reports only how many there were.

        Args:
            args: The arguments; ``sys.argv[1:]`` when ``None``.
            namespace: The object to fill; a new :class:`argparse.Namespace` when ``None``.

        Returns:
            The filled namespace.

        Raises:
            argparse.ArgumentError: With ``exit_on_error`` off, when an argument is
                unrecognized.
        """
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            message = f"unrecognized arguments ({len(extras)})"
            if not self.exit_on_error:
                raise argparse.ArgumentError(None, message)
            self.error(message)
        return parsed

    def error(self, message: str) -> NoReturn:
        """Print the usage and a diagnostic free of user input to stderr; exit with ``2``.

        Args:
            message: argparse's diagnostic, which may quote what was typed; only its rewrite by
                :meth:`_diagnostic` is written.
        """
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {self._diagnostic(message)}\n")

    def _diagnostic(self, message: str) -> str:
        """Rewrite one argparse diagnostic so it names only this parser's own vocabulary.

        Args:
            message: argparse's diagnostic.

        Returns:
            ``argument NAME: REASON`` for one of this parser's arguments; a value-free
            message for missing required arguments, an ambiguous option or unrecognized
            arguments; and otherwise a generic message pointing at ``--help``.
        """
        generic = f"invalid usage; run '{self.prog} --help'"
        names = {
            name: action for action in self._actions if (name := _action_name(action)) is not None
        }
        for name in sorted(names, key=len, reverse=True):
            prefix = f"argument {name}: "
            if message.startswith(prefix):
                reason = _argument_reason(names[name], message.removeprefix(prefix))
                return prefix + (generic if reason is None else reason)
        if message.startswith(_REQUIRED_PREFIX):
            required = message.removeprefix(_REQUIRED_PREFIX).split(", ")
            return message if all(name in names for name in required) else generic
        if message.startswith("ambiguous option: "):
            _, separator, matches = message.rpartition(" could match ")
            options = matches.split(", ")
            known = {option for action in self._actions for option in action.option_strings}
            if separator and all(option in known for option in options):
                return f"ambiguous option (could match {', '.join(options)})"
            return "ambiguous option"
        if message.startswith("unrecognized arguments: "):
            return "unrecognized arguments"
        if _UNRECOGNIZED_COUNT_RE.fullmatch(message):
            return message
        return generic


# The `--help` epilogs: each command's environment, exit codes and an example. The parsers use
# argparse.RawDescriptionHelpFormatter, which prints descriptions and epilogs verbatim, so their
# line breaks and alignment hold; every line stays within 79 columns of an 80-column terminal.
_HELP_EPILOG: Final = """\
exit codes:
  0  success
  1  failure: the command failed or did no work; its message says why
  2  usage error: argparse's, or one the command detects (see its --help)
  3  another process holds the shared analysis lock (analyze, seed-simulated)

environment:
  DATABASE_URL  the PostgreSQL URL (postgresql+psycopg://...), read by every
                command except hash-password (capture-fixtures: google only)
  DATA_MODE     auto (the default) or simulated, which forces every source to
                simulated data
  live credentials, per source (a source without them is simulated):
    google    N8N_GOOGLE_ADS_WEBHOOK_URL, N8N_WEBHOOK_SECRET and
              GOOGLE_ADS_CUSTOMER_ID
    linkedin  LINKEDIN_AD_ACCOUNT_IDS plus either LINKEDIN_CLIENT_ID,
              LINKEDIN_CLIENT_SECRET and LINKEDIN_REFRESH_TOKEN, or
              LINKEDIN_ACCESS_TOKEN
    hubspot   HUBSPOT_ACCESS_TOKEN
  config/*.yaml are read from ./config, else ../config, relative to the
  working directory; no variable selects the directory.

output:
  stdout carries only command output: the password hash, JSON reports and
  written file paths. stderr carries JSON log lines, one object per line,
  including the failing command's one-line message (event "cli_error", its
  text in "message"). argparse usage errors are plain text.

examples:
  printf '%s' 'correct horse' | ads-engine hash-password
  cd backend && ads-engine migrate && ads-engine ensure-admin
  ads-engine sync --source google
  ads-engine analyze --start 2026-09-01 --end 2026-09-28

Run 'ads-engine COMMAND --help' for each command's environment, exit codes and
an example.
"""

_HASH_EPILOG: Final = """\
input:
  the password is read from stdin, less one trailing newline (LF or CRLF); on
  a terminal it is prompted for without echo

environment:
  none: no settings are loaded and no database is used, so make init can run
  it before .env exists and store the hash as APP_ADMIN_PASSWORD_HASH

exit codes:
  0  the hash was printed to stdout, and nothing else
  1  an unexpected error
  2  the password is empty, is not valid UTF-8, or none was entered at the
     prompt

example:
  printf '%s' 'correct horse' | ads-engine hash-password
"""

_MIGRATE_EPILOG: Final = """\
environment:
  DATABASE_URL  the database to upgrade

Run it where alembic.ini is: backend/ on the host, /app in the image. A
database already at head is left unchanged, so the command is idempotent.
Concurrent runs on one database are serialised: a later run waits for the
running one, then finds the schema at head and changes nothing.

exit codes:
  0  the schema is at the newest revision
  1  the upgrade failed (the database is unreachable, for example)
  2  alembic.ini is not in the working directory

example:
  cd backend && ads-engine migrate
"""

_ENSURE_ADMIN_EPILOG: Final = """\
environment:
  DATABASE_URL             the database holding the login
  APP_ADMIN_EMAIL          the admin's email (default admin@example.com)
  APP_ADMIN_PASSWORD_HASH  the argon2id hash written by make init, or printed
                           by 'ads-engine hash-password'

The hash is checked before anything is written, and it is never shown.

exit codes:
  0  the admin login matches the environment
  1  APP_ADMIN_PASSWORD_HASH is unset or not a usable argon2 hash, a setting
     is invalid, or the database write failed

example:
  cd backend && ads-engine migrate && ads-engine ensure-admin
"""

_SYNC_EPILOG: Final = """\
environment:
  DATABASE_URL   the database holding the source's active dataset
  DATA_MODE      a live dataset is synced only under auto (the default)
  config/*.yaml  read from ./config, else ../config
  credentials    the source's live credentials (see 'ads-engine --help'),
                 needed when its active dataset is live; google also reads
                 GOOGLE_DAILY_CALL_BUDGET (default 1000 webhook calls per
                 24 h) and N8N_WEBHOOK_SECRET_HEADER (default
                 X-Webhook-Secret)

The JSON report on stdout lists each job's outcome, or the reason none ran.

exit codes:
  0  every job ran and succeeded
  1  no job ran (the source has no active dataset yet, or its live dataset may
     not be synced now: DATA_MODE=simulated or credentials missing), or any
     job failed: on a transient error (the datasets stay stale with their last
     good data), access denied, a defect or otherwise; the report says which

example:
  ads-engine sync --source linkedin
"""

_ANALYZE_EPILOG: Final = """\
environment:
  DATABASE_URL   the database holding the synced data and the runs
  config/*.yaml  read from ./config, else ../config; window.max_days is in
                 config/thresholds.yaml

window limits:
  START and END are calendar dates in exactly YYYY-MM-DD form, applied to each
  source's native days. END is no later than the default window end: the
  earlier of yesterday in the Google account's time zone and yesterday UTC.
  The span, START to END inclusive, is 1 to window.max_days days (180 as
  shipped). The run is kind custom even when it equals the default window.

exit codes:
  0  the run succeeded; its summary is printed as JSON
  1  the run failed, or the configuration or database could not be read
  2  a date is not in YYYY-MM-DD form, or the window is out of range
  3  another process holds the shared analysis lock; no run was started

example:
  ads-engine analyze --start 2026-09-01 --end 2026-09-28
"""

_SEED_SIMULATED_EPILOG: Final = """\
environment:
  DATABASE_URL   the database to seed
  config/*.yaml  read from ./config, else ../config
  No credentials are needed: every source is seeded with simulated data,
  whatever DATA_MODE says.

A source whose active dataset is already simulated keeps it. The JSON report
on stdout gives each source's dataset and the default-window run.

exit codes:
  0  every source is seeded and the default-window run succeeded
  1  a source failed to bootstrap (the others are seeded and analysed), or
     the run failed
  3  another process holds the shared analysis lock; the datasets stay seeded
     and no run was computed

example:
  cd backend && ads-engine seed-simulated
"""

_CAPTURE_FIXTURES_EPILOG: Final = """\
environment:
  DATA_MODE      must not be simulated: only live traffic is recorded
  DATABASE_URL   google only: each webhook call is reserved in the shared call
                 budget (GOOGLE_DAILY_CALL_BUDGET, default 1000 per 24 h)
  config/*.yaml  hubspot only, read from ./config, else ../config
  the source's live credentials:
    google    N8N_GOOGLE_ADS_WEBHOOK_URL, N8N_WEBHOOK_SECRET and
              GOOGLE_ADS_CUSTOMER_ID (header name N8N_WEBHOOK_SECRET_HEADER,
              default X-Webhook-Secret)
    linkedin  LINKEDIN_AD_ACCOUNT_IDS plus either LINKEDIN_CLIENT_ID,
              LINKEDIN_CLIENT_SECRET and LINKEDIN_REFRESH_TOKEN, or
              LINKEDIN_ACCESS_TOKEN as the alternative
    hubspot   HUBSPOT_ACCESS_TOKEN

Run it from backend/ (or /app in the image). It sends one minimal live request
per contract surface, writes the redacted exchanges to
tests/contracts/{source}/captured/ and prints each written path on stdout.

exit codes:
  0  every surface was captured, or reported as not captured in a
     cli_warning line on stderr (it had no data to read, for example), and
     every capture satisfies its contract
  1  refused before sending (DATA_MODE=simulated, credentials missing, an
     unsafe output directory, or google's call budget unreadable), a call
     failed, nothing was captured, a capture still held a secret after
     redaction (it is not written), or a capture breaks its contract
  2  tests/contracts/{source}/ is not under the working directory

example:
  cd backend && DATA_MODE=auto ads-engine capture-fixtures --source hubspot
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the ``ads-engine`` argument parser: global options and the seven commands.

    Each sub-parser stores its handler in ``handler``; :func:`main` dispatches on it. Usage
    errors name arguments, commands and choices but never the supplied text
    (:class:`_RedactingArgumentParser`), and exit with :data:`EXIT_USAGE`.

    Every parser's ``--help`` ends with an epilog naming the environment it reads, its exit
    codes and an example (:data:`_HELP_EPILOG` and the per-command epilogs). Each parser uses
    :class:`argparse.RawDescriptionHelpFormatter`, which sub-parsers do not inherit, so the
    descriptions and epilogs print with their own line breaks; argument help is still wrapped.

    Returns:
        The parser.
    """
    parser = _RedactingArgumentParser(
        prog=_PROG,
        description=(
            "Ads Action Engine administration: password hashing, migrations, the admin\n"
            "login, syncs, analysis runs, simulated seeding and live contract captures."
        ),
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default="INFO",
        type=str.upper,
        help=(
            "level of the JSON log lines written to stderr (default: INFO); a failing "
            "command's cli_error line is written at every level"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    hash_password = commands.add_parser(
        "hash-password",
        help="print the argon2id hash of the password read from stdin",
        description="Read a password from stdin (one trailing newline is dropped) and print only\n"
        "its argon2id hash. Loads no settings and uses no database.",
        epilog=_HASH_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hash_password.set_defaults(handler=_cmd_hash_password)

    migrate = commands.add_parser(
        "migrate",
        help="upgrade the database schema to the newest revision",
        description="Run `alembic upgrade head` with backend/alembic.ini from the working\n"
        "directory (backend/ on the host, /app in the image). Idempotent.",
        epilog=_MIGRATE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    migrate.set_defaults(handler=_cmd_migrate)

    ensure_admin = commands.add_parser(
        "ensure-admin",
        help="create or update the admin login from the environment",
        description="Upsert the admin from APP_ADMIN_EMAIL and APP_ADMIN_PASSWORD_HASH and\n"
        "remove any other login. Idempotent.",
        epilog=_ENSURE_ADMIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ensure_admin.set_defaults(handler=_cmd_ensure_admin)

    sync = commands.add_parser(
        "sync",
        help="run one source's sync jobs now and print the report",
        description="Run the source's sync jobs against its active dataset (live or simulated)\n"
        "and print the JSON report.",
        epilog=_SYNC_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sync.add_argument("--source", required=True, choices=SOURCES, help="the source to sync")
    sync.set_defaults(handler=_cmd_sync)

    analyze = commands.add_parser(
        "analyze",
        help="compute a custom-window analysis run from stored data",
        description="Compute a custom run over [START, END] (calendar dates on each source's "
        "native\ndays) under the shared analysis lock. Exits 3 while another run holds it.",
        epilog=_ANALYZE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    analyze.add_argument("--start", required=True, type=_iso_date, help="first date, YYYY-MM-DD")
    analyze.add_argument("--end", required=True, type=_iso_date, help="last date, YYYY-MM-DD")
    analyze.set_defaults(handler=_cmd_analyze)

    seed = commands.add_parser(
        "seed-simulated",
        help="seed simulated datasets for every source and run the default analysis",
        description="Build, backfill and activate a simulated dataset per source (an active\n"
        "simulated one is kept), then compute the default-window run.",
        epilog=_SEED_SIMULATED_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    seed.set_defaults(handler=_cmd_seed_simulated)

    capture = commands.add_parser(
        "capture-fixtures",
        help="record redacted live contract captures for one source",
        description="Send one minimal live request per contract surface and write the redacted\n"
        "exchanges to tests/contracts/{source}/captured/. Run from backend/.",
        epilog=_CAPTURE_FIXTURES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        owner = logged_as(exc)
        if owner is None:
            log.exception("cli_failed", command=name, error=type(exc).__name__)
        else:
            # Its owner (an analysis run's analysis_run_failed) logged the traceback already.
            log.warning("cli_failed", command=name, error=type(exc).__name__, logged_as=owner)
        _error(f"{name} failed ({type(exc).__name__}); see the log on stderr")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
