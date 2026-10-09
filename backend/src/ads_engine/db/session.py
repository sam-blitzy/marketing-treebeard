"""Async engine, transactional session scope and the dedicated advisory-lock connection.

The API, the worker and the ``ads-engine`` CLI all reach PostgreSQL 18 through this module,
using SQLAlchemy 2.1 async sessions on psycopg 3. Nothing connects at import time: the
engine is a lazy, process-wide singleton built from ``DATABASE_URL`` on first use.

These entry points cover every caller:

* :func:`session_scope` is the shared unit-of-work helper for requests and jobs.
  Repository functions never commit, so everything written inside one scope (for example
  a decision and the ``analysis_request`` it enqueues) commits atomically or not at all::

      async with session_scope() as session:
          session.add(decision)
          await enqueue_analysis_request(session, reason="decision")

* :func:`use_session` backs the interfaces whose ``session`` is optional: it joins the
  caller's session untouched, or runs the call in a :func:`session_scope` of its own.

* :func:`run_to_completion` runs a cleanup coroutine to completion even when its caller is
  cancelled. The scopes here close and release through it, and the analysis runner records
  a failed run's terminal state through it.

* :func:`dedicated_connection` checks out one physical connection in ``AUTOCOMMIT`` mode
  for holding session-level advisory locks (the ``analysis`` lock and the scheduler lock).
  The lock keys and the lock statements belong to their owners, ``analysis/lock.py`` and
  ``sync/scheduler.py``; this module only supplies the connection and guarantees that no
  advisory lock survives the block.
* :func:`ping_database` makes one ``SELECT 1`` round trip on a fresh connection outside
  the pool. Its caller bounds it with :func:`asyncio.timeout`, so ``GET /healthz`` (and any
  caller that must tell a reachable database from a hung one) answers within its own
  deadline even when every pooled connection is stuck on a server that stopped answering.
* :func:`dispose_engine` and :func:`reset_engine` end or forget the engine at process
  shutdown and between tests.
* :func:`normalise_database_url` is the rule ``DATABASE_URL`` must meet (psycopg 3, plain
  ``postgresql://`` and ``postgres://`` rewritten to it); the engine applies it, and the API
  applies it at startup so that a URL the engine would refuse stops the API before it serves.

Every new connection carries libpq's ``connect_timeout`` (10 s, unless ``DATABASE_URL``'s
query string sets its own), and a checkout waits at most 10 s for a free pooled connection,
so a database that accepts TCP but never answers fails a connection attempt with an error
instead of holding the caller for psycopg's 130 s default. No ``statement_timeout`` is set:
the engine also runs the worker's long upserts and purges, and a frozen server could not
enforce one anyway.

The URL is a secret (it carries the database password). It is read from
``Settings.database_url`` only where the engine is built and where the API checks it at
startup, and the URL itself is never logged: only an allowlisted descriptor of its driver,
host, port and database name is, never its credentials or query parameters.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from ads_engine.logging import get_logger
from ads_engine.settings import get_settings

__all__ = [
    "dedicated_connection",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "json_dumps",
    "normalise_database_url",
    "ping_database",
    "reset_engine",
    "run_to_completion",
    "session_scope",
    "use_session",
]

_log = get_logger(__name__)

# The plan's only driver: psycopg 3. One URL serves both the sync dialect (Alembic) and the
# async dialect, which create_async_engine selects automatically for this driver name.
_DRIVERNAME: Final = "postgresql+psycopg"
# Plain schemes that carry no driver and are normalised to psycopg. `postgres://` is the
# form many hosting providers emit; SQLAlchemy itself no longer accepts it.
_PLAIN_SCHEMES: Final = frozenset({"postgresql", "postgres"})

# Pool sizing per process: the API and the worker each hold at most 15 connections (5 kept
# open plus 10 overflow), dedicated lock connections included, since they are drawn from
# the same pool.
_POOL_SIZE: Final = 5
_MAX_OVERFLOW: Final = 10
# Seconds a checkout waits for a free pooled connection before SQLAlchemy raises its
# TimeoutError, instead of its 30 s default, so an exhausted pool fails a request or a job
# with an error well inside the API's request deadline.
_POOL_TIMEOUT_S: Final = 10

# libpq `connect_timeout` of every new connection, in seconds. psycopg's own default is
# 130 s, so without it a server that accepts TCP but never answers the startup handshake (a
# frozen or partitioned PostgreSQL) holds the caller for over two minutes. A
# `connect_timeout` given in DATABASE_URL's query string is kept instead.
_CONNECT_TIMEOUT_S: Final = 10
_CONNECT_TIMEOUT_PARAM: Final = "connect_timeout"

# Every session runs in UTC, so `timestamptz` values come back tz-aware in UTC and no SQL
# depends on the server's zone. Native-day semantics are owned by the application.
_UTC_SESSION_OPTION: Final = "-c TimeZone=UTC"

# Releases every session-level advisory lock held by this database session. Run on each
# dedicated connection before it returns to the pool.
_UNLOCK_ALL: Final = text("SELECT pg_advisory_unlock_all()")

# The round trip ping_database makes.
_PING: Final = text("SELECT 1")

# Process-wide lazy singletons. Guarded by a re-entrant lock so that two threads (for
# example FastAPI's threadpool and the event loop thread) never build two engines, and so
# that get_sessionmaker() may call get_engine() while holding it. The probe engine behind
# ping_database has no pool, so it holds no connection that could cross event loops.
_state_lock: Final = threading.RLock()
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_probe_engine: AsyncEngine | None = None


# ---------------------------------------------------------------------------
# JSON serialisation for jsonb columns
# ---------------------------------------------------------------------------


def _json_default(value: object) -> object:
    """Convert a value the standard JSON encoder cannot handle into a JSON-native one.

    Args:
        value: The object :func:`json.dumps` could not encode.

    Returns:
        ``Decimal`` as an exact fixed-point string with no exponent (``Decimal("1E+2")``
        gives ``"100"``); an aware ``datetime`` or a ``date`` as its ISO 8601 string; a
        ``set`` or ``frozenset`` as a sorted list, whose items are encoded in turn.

    Raises:
        ValueError: For a non-finite ``Decimal`` (``NaN``, ``Infinity``) or a naive
            ``datetime``, neither of which has an exact, unambiguous JSON form.
        TypeError: For any other type, naming it.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite Decimal {value} cannot be stored in jsonb")
        return format(value, "f")
    # datetime is a subclass of date, so it is checked first.
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime cannot be stored in jsonb; instants must be tz-aware")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable for jsonb")


def json_dumps(value: object) -> str:
    """Serialise a value for a ``jsonb`` column; the engine's ``json_serializer``.

    Evidence, decision patches, raw payloads and configuration bindings carry ``Decimal``,
    ``date`` and ``datetime`` values, which the standard encoder rejects. Money is stored and
    served as decimal strings, so amounts stay exact end to end; that matches the API's
    serialisation of money as decimal strings. Reading back uses :func:`json.loads`, so a
    stored amount returns as its string, never as a binary float.

    Output is compact (no spaces), keeps non-ASCII text as is, and rejects ``NaN`` and
    infinite floats instead of writing the non-standard ``NaN`` token PostgreSQL refuses.

    Example::

        >>> json_dumps({"a": Decimal("1.500000"), "d": date(2026, 10, 1)})
        '{"a":"1.500000","d":"2026-10-01"}'

    Args:
        value: Any JSON-compatible value, possibly containing ``Decimal``, ``date``, aware
            ``datetime``, ``set`` or ``frozenset`` items.

    Returns:
        The JSON text.

    Raises:
        ValueError: For ``NaN`` or infinite floats, a non-finite ``Decimal`` or a naive
            ``datetime``.
        TypeError: For a value of any other unsupported type.
    """
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Engine and sessionmaker
# ---------------------------------------------------------------------------


def normalise_database_url(raw: str) -> URL:
    """Parse ``DATABASE_URL`` and pin it to the psycopg 3 driver.

    This is the one rule for ``DATABASE_URL``: :func:`get_engine` applies it when it builds
    the engine, and ``api/app.py:create_app()`` applies it at startup, so the API refuses a
    URL the engine would refuse before it serves a request. It parses only and never
    connects, so a well-formed URL of an unreachable server passes.

    Example::

        normalise_database_url("postgres://ads:ads@db:5432/ads_engine").drivername
        # 'postgresql+psycopg'

    Args:
        raw: The secret URL text from ``Settings.database_url``.

    Returns:
        The URL with driver name ``postgresql+psycopg``. The plain schemes
        ``postgresql://`` and ``postgres://`` are rewritten to it.

    Raises:
        ValueError: When the text is not a SQLAlchemy URL, or names any other backend or
            driver (for example ``postgresql+asyncpg``). The message names ``DATABASE_URL``
            and at most the driver name, never the URL, and the parse error is not chained,
            because the URL carries the password.
    """
    try:
        url = make_url(raw)
    except ArgumentError, ValueError:
        # ValueError covers a malformed port, which make_url reports from int().
        raise ValueError("DATABASE_URL is not a valid SQLAlchemy database URL") from None
    if url.drivername in _PLAIN_SCHEMES:
        return url.set(drivername=_DRIVERNAME)
    if url.drivername != _DRIVERNAME:
        raise ValueError(
            f"DATABASE_URL must use the {_DRIVERNAME} driver (psycopg 3); got {url.drivername!r}"
        )
    return url


def _connect_options(url: URL) -> str:
    """Build the libpq ``options`` value that sets the session time zone to UTC.

    ``connect_args`` replace a same-named URL query parameter, so any ``options`` given in
    the URL (for example a ``search_path``) are kept and the UTC setting is appended last,
    where it takes precedence over an earlier ``TimeZone``.

    Args:
        url: The normalised database URL.

    Returns:
        The space-separated ``-c name=value`` options.
    """
    existing = url.query.get("options")
    given = [existing] if isinstance(existing, str) else list(existing or ())
    return " ".join([*(option for option in given if option.strip()), _UTC_SESSION_OPTION])


def _connect_timeout(url: URL) -> int:
    """Return the libpq ``connect_timeout`` every new connection uses, in seconds.

    ``connect_args`` replace a same-named URL query parameter, so a ``connect_timeout``
    given in the URL is read here and passed on: the last non-blank one, as libpq keeps the
    last of repeated keywords. Without one, :data:`_CONNECT_TIMEOUT_S` applies.

    Args:
        url: The normalised database URL.

    Returns:
        The URL's value, or :data:`_CONNECT_TIMEOUT_S`. libpq reads zero or less as no
        limit, which psycopg then caps at its own 130 s default.

    Raises:
        ValueError: When the URL's value is not a whole number of seconds, the only form
            libpq documents. The message never contains the URL.
    """
    existing = url.query.get(_CONNECT_TIMEOUT_PARAM)
    given = [existing] if isinstance(existing, str) else list(existing or ())
    present = [value for value in given if value.strip()]
    if not present:
        return _CONNECT_TIMEOUT_S
    try:
        return int(present[-1])
    except ValueError:
        raise ValueError(
            "DATABASE_URL's connect_timeout must be a whole number of seconds"
        ) from None


def _connect_args(url: URL) -> dict[str, Any]:
    """Build the psycopg connect arguments shared by the pooled engine and the probe engine.

    Args:
        url: The normalised database URL.

    Returns:
        ``options`` from :func:`_connect_options` and ``connect_timeout`` from
        :func:`_connect_timeout`.

    Raises:
        ValueError: When the URL's ``connect_timeout`` is not a whole number of seconds.
    """
    return {
        "options": _connect_options(url),
        _CONNECT_TIMEOUT_PARAM: _connect_timeout(url),
    }


def _create_engine() -> AsyncEngine:
    """Build the process engine from the current settings.

    Returns:
        A new :class:`AsyncEngine` with a pre-pinged pool, the pool and connect timeouts and
        the ``jsonb`` serialiser. Building it opens no connection.

    Raises:
        ValueError: When ``DATABASE_URL`` is malformed, names another driver or carries a
            ``connect_timeout`` that is not a whole number of seconds.
        pydantic.ValidationError: When the environment cannot be parsed into settings.
    """
    url = normalise_database_url(get_settings().database_url.get_secret_value())
    connect_args = _connect_args(url)
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=_POOL_SIZE,
        max_overflow=_MAX_OVERFLOW,
        pool_timeout=_POOL_TIMEOUT_S,
        json_serializer=json_dumps,
        json_deserializer=json.loads,
        connect_args=connect_args,
    )
    # The URL is never logged, not even rendered with its password hidden: its query string
    # can carry credentials of its own (libpq `password`, `sslpassword`, key paths), which
    # password masking leaves in place. Only these allowlisted fields are written.
    _log.info(
        "db_engine_created",
        driver=url.drivername,
        host=url.host,
        port=url.port,
        database=url.database,
        pool_size=_POOL_SIZE,
        max_overflow=_MAX_OVERFLOW,
        pool_timeout_s=_POOL_TIMEOUT_S,
        connect_timeout_s=connect_args[_CONNECT_TIMEOUT_PARAM],
    )
    return engine


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, building it on first call.

    The engine uses psycopg 3's async dialect, a pre-pinged pool of 5 connections plus 10
    overflow whose checkout waits at most 10 s, a 10 s libpq ``connect_timeout`` (unless
    ``DATABASE_URL`` sets its own), :func:`json_dumps` and :func:`json.loads` for ``jsonb``,
    and a UTC session time zone. Building it opens no connection; the first connection is
    made on first use.

    Returns:
        The cached :class:`AsyncEngine`.

    Raises:
        ValueError: When ``DATABASE_URL`` is malformed, names a driver other than
            ``postgresql+psycopg`` or carries a ``connect_timeout`` that is not a whole
            number of seconds.
    """
    global _engine
    engine = _engine
    if engine is not None:
        return engine
    with _state_lock:
        if _engine is None:
            _engine = _create_engine()
        return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory bound to :func:`get_engine`.

    Sessions keep loaded attributes after commit (``expire_on_commit=False``), so objects
    returned from a :func:`session_scope` stay readable once the scope has committed,
    without an implicit refresh that async code could not await.

    Returns:
        The cached :class:`async_sessionmaker`.
    """
    global _sessionmaker
    factory = _sessionmaker
    if factory is not None:
        return factory
    with _state_lock:
        if _sessionmaker is None:
            _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
        return _sessionmaker


def _get_probe_engine() -> AsyncEngine:
    """Return the engine behind :func:`ping_database`, building it on first call.

    It is built from the same normalised URL and connect arguments as :func:`get_engine`,
    with no pool (:class:`~sqlalchemy.pool.NullPool`): every checkout opens a new
    connection and closing it ends that connection. It runs in ``AUTOCOMMIT``, so the ping
    sends no ``BEGIN`` and no ``ROLLBACK`` around its ``SELECT 1``. Building it opens no
    connection and logs nothing.

    Returns:
        The cached probe :class:`AsyncEngine`.

    Raises:
        ValueError: When ``DATABASE_URL`` is malformed, names another driver or carries a
            ``connect_timeout`` that is not a whole number of seconds.
    """
    global _probe_engine
    engine = _probe_engine
    if engine is not None:
        return engine
    with _state_lock:
        if _probe_engine is None:
            url = normalise_database_url(get_settings().database_url.get_secret_value())
            _probe_engine = create_async_engine(
                url,
                poolclass=NullPool,
                isolation_level="AUTOCOMMIT",
                connect_args=_connect_args(url),
            )
        return _probe_engine


async def ping_database() -> None:
    """Make one ``SELECT 1`` round trip on a fresh connection outside the pool.

    The connection is opened outside the process pool and closed afterwards, so neither a
    pooled connection stuck on a server that stopped answering nor an exhausted pool can
    stall the check. ``GET /healthz`` uses it, and so can any caller that must tell a
    reachable database from an unreachable one. The caller bounds it with
    :func:`asyncio.timeout`, whose expiry it reports as :class:`TimeoutError`::

        try:
            async with asyncio.timeout(3.0):
                await ping_database()
        except TimeoutError, SQLAlchemyError, OSError:
            ...  # the database is unreachable

    A server that accepts TCP but never answers (paused or partitioned) is caught while
    connecting, and the cancel ends that attempt at once on the caller's deadline. Even with
    no caller deadline, libpq's ``connect_timeout`` (10 s, unless ``DATABASE_URL`` sets its
    own) bounds the connect. A cancel that lands mid-query, when the server stops answering
    between the handshake and the ``SELECT 1``, costs psycopg up to about 10 s more: it
    first asks the server to cancel the query and waits for it, so the caller's
    ``TimeoutError`` then arrives up to about 10 s after its deadline. Nothing is logged:
    the caller owns the consequence, and no message names the URL.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: When the database refused or failed the attempt,
            for example a refused connection, failed authentication or libpq's
            ``connect_timeout`` expiring; psycopg's error is wrapped as an
            :class:`~sqlalchemy.exc.OperationalError`.
        OSError: When the attempt failed in the socket layer outside psycopg.
        ValueError: When ``DATABASE_URL`` is malformed, for example naming another driver
            or carrying a ``connect_timeout`` that is not a whole number of seconds.
    """
    engine = _get_probe_engine()
    async with engine.connect() as conn:
        await conn.execute(_PING)


async def dispose_engine() -> None:
    """Close every pooled connection and forget the engines and session factory.

    ``api/app.py`` calls this from its lifespan on shutdown, and the worker on graceful
    exit. The caches are cleared before the pool is disposed, so a caller arriving during
    disposal builds a fresh engine instead of using the one being closed. The probe engine
    of :func:`ping_database` is disposed and forgotten as well. Calling it when no engine
    exists does nothing.
    """
    global _engine, _sessionmaker, _probe_engine
    with _state_lock:
        engine, _engine, _sessionmaker = _engine, None, None
        probe, _probe_engine = _probe_engine, None
    if probe is not None:
        await probe.dispose()
    if engine is not None:
        await engine.dispose()
        _log.info("db_engine_disposed")


def reset_engine() -> None:
    """Forget the engines and session factory without closing any connection.

    Tests call this after ``get_settings.cache_clear()`` when they switch ``DATABASE_URL``
    or event loops, so the next :func:`get_engine` or :func:`ping_database` call reads the
    new settings. Async connections belong to the event loop that opened them, so callers
    first ``await dispose_engine()`` while that loop is still running; this function only
    clears the caches, for a process or loop where disposal is no longer possible.
    """
    global _engine, _sessionmaker, _probe_engine
    with _state_lock:
        _engine = None
        _sessionmaker = None
        _probe_engine = None


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------


async def run_to_completion(cleanup: Coroutine[Any, Any, None]) -> None:
    """Run a cleanup coroutine to completion even if the calling task is cancelled.

    The cleanup runs in a task of its own behind :func:`asyncio.shield`. A cancellation
    received while waiting is held until the cleanup has finished and then re-raised, so
    a rollback, a lock release or a connection close is never abandoned halfway and two
    cleanups never run on one connection at the same time. This mirrors how SQLAlchemy's
    own async context managers close sessions and connections. The analysis runner records
    a failed run's terminal state through it as well.

    Args:
        cleanup: The coroutine to run; it is awaited exactly once.

    Raises:
        asyncio.CancelledError: When the caller was cancelled meanwhile, once the cleanup
            has completed, or when the cleanup task itself was cancelled.
        Exception: Whatever the cleanup raised.
    """
    task = asyncio.create_task(cleanup)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as err:
            if task.done():
                raise
            cancelled = err
        else:
            break
    if cancelled is not None:
        raise cancelled


async def _abandon_session(session: AsyncSession) -> None:
    """Roll back and close a session whose unit of work failed.

    Runs while the original exception propagates. A database error (any
    :class:`~sqlalchemy.exc.SQLAlchemyError`, which wraps every psycopg error) from either
    step is logged and never replaces that exception, which is the one the caller has to
    see; closing still runs after a failed rollback, and a broken connection is discarded
    by the close.

    Args:
        session: The session of the failed :func:`session_scope`.
    """
    steps = (
        ("db_session_rollback_failed", session.rollback),
        ("db_session_close_failed", session.close),
    )
    for event, step in steps:
        try:
            await step()
        except SQLAlchemyError as exc:
            _log.exception(event, error=type(exc).__name__)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Run one unit of work in a session that commits on success and rolls back on error.

    This is the shared unit-of-work helper for requests and jobs. Repository functions add,
    upsert and flush but never commit, so every write inside one scope (a sync job's rows
    and its ``dataset_freshness`` update, or a decision and its ``analysis_request``) lands
    atomically::

        async with session_scope() as session:
            await upsert_facts(FactCampaignDaily, rows, dataset_id, session=session)

    Not every commit passes through a scope. ``GoogleCallBudget.acquire`` and
    ``GoogleCallBudget.used_24h`` (and so ``remaining``) in ``sync/budget_guard.py`` run
    independent short transactions of their own and commit them outside any caller's
    scope, so the row lock that enforces the call cap across processes is held for
    milliseconds rather than for a whole job. A call counted inside a job therefore stays
    counted even when that job's scope rolls back.

    On normal exit the session is committed and closed. On any exception, including
    cancellation, the session is rolled back and closed and the exception is re-raised
    unchanged; nothing is ever swallowed.

    Yields:
        A new :class:`AsyncSession` from :func:`get_sessionmaker`.
    """
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except BaseException:
        await run_to_completion(_abandon_session(session))
        raise
    await run_to_completion(session.close())


@asynccontextmanager
async def use_session(session: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
    """Join the caller's session, or run a unit of work of its own.

    The binding behind the session-optional public interfaces, for example
    ``record_decision(subject, patch, origin)`` and ``upsert_facts(table, rows,
    dataset_id)``. With a session, that session is yielded untouched: it is neither
    committed, rolled back nor closed, so the work lands in the caller's transaction.
    Without one, a :func:`session_scope` is opened, committed when the block exits
    normally and rolled back when it raises::

        async with use_session(session) as unit:
            await upsert_entities(unit, Campaign, rows, dataset_id)

    Args:
        session: The caller's session, or ``None`` for a transaction owned by the block.

    Yields:
        The caller's session, or a new one inside its own transaction.
    """
    if session is not None:
        yield session
        return
    async with session_scope() as owned:
        yield owned


async def _release_advisory_locks(conn: AsyncConnection) -> None:
    """Release every session-level advisory lock before the connection is pooled again.

    An invalidated connection has already closed its database session, which released its
    locks, so nothing is sent. When the release itself fails with a database error, for
    example on a broken connection, the error is logged and the connection is invalidated,
    so the pool discards it rather than hand a connection that may still hold a lock to an
    unrelated caller. Closing the physical connection ends its database session, which
    releases any lock it held.

    Args:
        conn: The dedicated connection being returned.
    """
    if conn.invalidated:
        return
    try:
        await conn.execute(_UNLOCK_ALL)
    except SQLAlchemyError as exc:
        _log.exception("db_advisory_unlock_failed", error=type(exc).__name__, action="invalidate")
        await conn.invalidate(exc)


@asynccontextmanager
async def dedicated_connection() -> AsyncIterator[AsyncConnection]:
    """Check out one physical connection in ``AUTOCOMMIT`` mode for session-level locks.

    The yielded connection stays checked out for the whole block, so a session-level
    advisory lock taken on it is held until it is released or the block ends.
    ``AUTOCOMMIT`` keeps a lock held for a whole analysis run, or for the life of the
    scheduler, from leaving the connection idle in a transaction.

    Consumers own their keys and statements, for example ``analysis/lock.py`` with
    ``ANALYSIS_LOCK_KEY`` and ``sync/scheduler.py`` with ``SCHEDULER_LOCK_KEY``::

        async with dedicated_connection() as conn:
            acquired = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": ANALYSIS_LOCK_KEY}
            )
            if not acquired:
                raise RunInProgress
            try:
                ...  # the whole run
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": ANALYSIS_LOCK_KEY}
                )

    On exit, whether the block succeeded, raised or was cancelled, the connection runs
    ``SELECT pg_advisory_unlock_all()`` before it returns to the pool, so a pooled
    connection never carries a stale advisory lock to another caller. If that statement
    fails, the connection is invalidated and the failure logged; the block's own exception,
    if any, is re-raised unchanged. If the process crashes, PostgreSQL ends its database
    session and releases the lock, which is what lets the next lock holder mark a
    ``running`` analysis row left behind by the crash as ``failed``.

    Yields:
        The dedicated :class:`AsyncConnection`, in ``AUTOCOMMIT`` isolation.
    """
    async with get_engine().connect() as checked_out:
        conn = await checked_out.execution_options(isolation_level="AUTOCOMMIT")
        try:
            yield conn
        finally:
            await run_to_completion(_release_advisory_locks(conn))
