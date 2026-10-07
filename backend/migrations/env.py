"""Alembic environment of the Ads Action Engine: async SQLAlchemy 2.1 on psycopg 3.

Alembic loads this module by path for every command that uses ``backend/alembic.ini``: the
standalone ``alembic`` CLI (``uv run alembic upgrade head`` from ``backend/``), and
``alembic.command.upgrade(cfg, "head")`` called by ``ads-engine migrate`` and by the test
fixtures. The schema itself is the single revision ``versions/0001_initial_schema.py``.

Database URL:
    ``DATABASE_URL``, read through ``ads_engine.settings.get_settings()`` and handed to the
    engine as a parsed :class:`sqlalchemy.engine.URL`. It never passes through the Alembic
    ``Config``: ``alembic.ini`` deliberately has no ``sqlalchemy.url``, and this module never
    calls ``set_main_option``, because ConfigParser ``%`` interpolation corrupts URL-encoded
    passwords such as ``p%25w%40rd``. The URL is never logged, printed or placed in an
    exception message.

Metadata:
    Importing ``ads_engine.db.models`` registers every table on
    ``ads_engine.db.base.Base.metadata``, which autogenerate and ``alembic check`` compare
    with the database. The metadata carries the project's constraint naming convention.

Caller attributes (``Config.attributes``):
    ``configure_logger``: when falsy, the ini file's ``[loggers]`` sections are not applied,
    so the structlog JSON logging that ``ads-engine migrate`` and the tests configure stays
    in charge. The standalone ``alembic`` CLI leaves it unset and gets the ini's logging.

    ``connection``: a synchronous :class:`sqlalchemy.engine.Connection` to migrate through
    instead of opening one. Async callers obtain it inside ``AsyncConnection.run_sync``
    (Alembic's "connection sharing with asyncio" pattern). The caller owns that connection
    and its transaction: when it is already in a transaction, Alembic joins it and does not
    commit, so the caller must. Any other non-``None`` value, an ``AsyncConnection``
    included, raises :class:`TypeError`.

Event-loop constraint:
    Without a ``connection`` attribute, online mode drives the async engine with
    :func:`asyncio.run`, so it must be invoked outside a running event loop.
    ``ads-engine migrate`` calls ``alembic.command.upgrade`` synchronously. Code that is
    already inside an event loop passes the ``connection`` attribute, or runs the command
    in a worker thread with :func:`asyncio.to_thread`; otherwise :class:`RuntimeError` is
    raised before any connection is opened.

Example:
    Migrating on a shared connection from async code, as ``tests/conftest.py`` does::

        from alembic import command
        from alembic.config import Config


        def _upgrade(sync_connection: Connection, cfg: Config) -> None:
            cfg.attributes["connection"] = sync_connection
            command.upgrade(cfg, "head")


        cfg = Config("alembic.ini")
        cfg.attributes["configure_logger"] = False
        async with engine.begin() as async_connection:
            await async_connection.run_sync(_upgrade, cfg)
"""

import asyncio
from logging.config import fileConfig
from typing import Final, TypedDict

from alembic import context
from sqlalchemy import MetaData
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Imported for its side effect: defining the model classes registers every table on
# Base.metadata, so autogenerate and `alembic check` see the complete schema.
import ads_engine.db.models  # noqa: F401
from ads_engine.db.base import Base
from ads_engine.settings import get_settings

# The project's only driver. One URL serves the sync dialect (offline rendering, the shared
# `connection` attribute) and the async dialect, which create_async_engine selects for it.
_DRIVERNAME: Final = "postgresql+psycopg"
# Driverless schemes rewritten to psycopg 3, exactly as ads_engine.db.session does for the
# api and worker, so `ads-engine migrate` accepts every DATABASE_URL the processes accept.
_PLAIN_SCHEMES: Final = frozenset({"postgresql", "postgres"})

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


class _SharedOptions(TypedDict):
    """Keyword arguments of ``context.configure`` shared by offline and online runs."""

    target_metadata: MetaData
    compare_type: bool
    compare_server_default: bool
    transaction_per_migration: bool


_SHARED_OPTIONS: Final[_SharedOptions] = {
    "target_metadata": target_metadata,
    # Autogenerate and `alembic check` also compare column types and server defaults, so a
    # model that drifts from the frozen 0001 revision in either is reported.
    "compare_type": True,
    "compare_server_default": True,
    # Each revision runs and records its version in its own transaction, so a failure in a
    # later revision leaves the earlier ones applied and recorded.
    "transaction_per_migration": True,
}


def _database_url() -> URL:
    """Return ``DATABASE_URL`` from the settings, parsed and pinned to psycopg 3.

    The secret text is parsed here instead of being handed to the engine as a string, so a
    malformed URL never reaches an exception message, and the driverless schemes the api
    and worker accept are accepted by migrations too.

    Returns:
        The parsed URL with driver name ``postgresql+psycopg``. ``postgresql://`` and
        ``postgres://`` are rewritten to it. Percent-encoded credentials are decoded once,
        by SQLAlchemy, and never pass through ConfigParser.

    Raises:
        ValueError: When the text is not a SQLAlchemy URL, or names another backend or
            driver. The message never contains the URL, and the parse error is not chained,
            because the URL carries the database password.
        pydantic.ValidationError: When the environment cannot be parsed into settings.
    """
    raw = get_settings().database_url.get_secret_value()
    try:
        url = make_url(raw)
    except ArgumentError, ValueError:
        # ValueError covers a malformed port, whose message would echo part of the URL.
        raise ValueError("DATABASE_URL is not a valid SQLAlchemy database URL") from None
    if url.drivername in _PLAIN_SCHEMES:
        return url.set(drivername=_DRIVERNAME)
    if url.drivername != _DRIVERNAME:
        raise ValueError(
            f"DATABASE_URL must use the {_DRIVERNAME} driver (psycopg 3); got {url.drivername!r}"
        )
    return url


def _event_loop_running() -> bool:
    """Report whether the calling thread is already running an asyncio event loop.

    Returns:
        ``True`` inside a running loop, where :func:`asyncio.run` cannot be used.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def run_migrations_offline() -> None:
    """Render the migrations as SQL (``alembic ... --sql``) without connecting.

    The URL only selects the PostgreSQL dialect and the psycopg driver; no connection is
    opened, so this works with an unreachable host. Bound values are rendered inline.
    """
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_SHARED_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations on a synchronous connection.

    Called inside ``AsyncConnection.run_sync`` by :func:`run_async_migrations`, or directly
    with the connection a caller passed in ``Config.attributes["connection"]``.

    Args:
        connection: The connection to migrate through. When it is already in a
            transaction, Alembic joins that transaction and leaves the commit to its owner.
    """
    context.configure(connection=connection, **_SHARED_OPTIONS)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open one connection on a pool-less async engine and run the migrations on it.

    ``NullPool`` closes the connection as soon as it is released, and the engine is
    disposed even when a migration fails, so no connection outlives the command.
    """
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    """Run the migrations against the database.

    Uses the caller's ``Config.attributes["connection"]`` when one is given; otherwise
    connects to ``DATABASE_URL`` through the async engine.

    Raises:
        TypeError: When the ``connection`` attribute is not a synchronous
            :class:`~sqlalchemy.engine.Connection`, for example an ``AsyncConnection``.
        RuntimeError: When no ``connection`` attribute is given and the caller is already
            inside a running event loop.
    """
    shared_connection = config.attributes.get("connection")
    if shared_connection is not None:
        if not isinstance(shared_connection, Connection):
            # Only the type's name: the repr of an engine or connection can show the URL.
            raise TypeError(
                "Config.attributes['connection'] must be a synchronous "
                "sqlalchemy.engine.Connection, such as the one AsyncConnection.run_sync() "
                f"passes to its callable; got {type(shared_connection).__qualname__}"
            )
        do_run_migrations(shared_connection)
        return
    if _event_loop_running():
        raise RuntimeError(
            "Alembic online migrations cannot start their own event loop inside a running "
            "one; pass a synchronous Connection from AsyncConnection.run_sync() in "
            "Config.attributes['connection'], or run the command with asyncio.to_thread()"
        )
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
