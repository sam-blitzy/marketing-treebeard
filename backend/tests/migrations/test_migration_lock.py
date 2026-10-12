"""Migration tests: ``migrations/env.py`` serialises concurrent runs with an advisory lock.

Two ``ads-engine migrate`` one-shots started together against an empty database used to
race: both found no ``alembic_version`` table, both created it, and the later one failed
with a unique violation on ``pg_type``. ``env.py`` now takes the advisory lock
``MIGRATION_LOCK_KEY`` on the migration connection before Alembic reads the current
revision. These tests show that:

* the key is derived from ``"ads_engine:migrate"`` the way the other lock keys are derived
  from their names, and differs from the analysis and scheduler keys;
* offline ``--sql`` rendering takes no lock;
* two CLI runs started together against one empty schema both exit 0 and leave it at
  revision ``0001`` with every table, whether they race or both wait on a held lock;
* on a caller's connection already in a transaction the lock is transaction-level, and
  the caller's transaction is never committed;
* on a caller's connection outside a transaction the lock is session-level, the revision
  is committed, and the lock is released after a success and after a failure, whose own
  error is the one raised.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). Each creates a private schema,
points every connection's ``search_path`` at it alone (the CLI through the ``options``
query parameter of its ``DATABASE_URL``), and drops it with ``CASCADE`` afterwards, so the
``public`` schema and every other schema stay untouched. An unreachable database fails
the tests; it never skips them.

Each concurrent run is a separate process, as in production: Alembic installs its
``alembic.context`` proxy as a module global, so two upgrades in threads of one process
would share it.
"""

import hashlib
import importlib.util
import io
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, ProgrammingError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.analysis.lock import ANALYSIS_LOCK_KEY
from ads_engine.db.base import Base
from ads_engine.sync.scheduler import SCHEDULER_LOCK_KEY

BACKEND: Final = Path(__file__).resolve().parents[2]
"""``backend/``: the working directory of the CLI, holding ``alembic.ini``."""

ALEMBIC_INI: Final = BACKEND / "alembic.ini"
"""``backend/alembic.ini``, whose ``script_location`` is resolved relative to the file."""

ENV_PY: Final = BACKEND / "migrations" / "env.py"
"""The Alembic environment under test."""

HEAD: Final = "0001"
"""The single revision of the schema, ``0001_initial_schema``."""

CLI_PROGRAM: Final = (
    "import sys; from ads_engine.cli import main; raise SystemExit(main(sys.argv[1:]))"
)
"""The console script's entry point, run in a fresh interpreter as ``tests/conftest.py`` does."""

CLI_TIMEOUT: Final = 120.0
"""Seconds one ``ads-engine migrate`` process may take once it holds the lock."""

WAITERS_TIMEOUT: Final = 60.0
"""Seconds both CLI processes may take to start and queue on a held lock."""

WAIT_LOG: Final = "waiting for it to finish"
"""Part of the line a run logs when it finds the migration lock held."""


@dataclass(frozen=True)
class Target:
    """A private, initially empty schema of the test database.

    Attributes:
        engine: An engine whose connections see only this schema.
        schema: The schema's name.
        cli_url: ``DATABASE_URL`` for the CLI, selecting the schema through ``search_path``.
    """

    engine: Engine
    schema: str
    cli_url: str


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the migration lock tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _search_path_option(schema: str) -> str:
    """The libpq ``options`` value that pins a session's ``search_path`` to ``schema``."""
    return f"-csearch_path={schema}"


def _no_steps(_heads: object, _context: MigrationContext) -> Iterable[object]:
    """An Alembic migration function that plans no revision steps."""
    return []


def _load_env() -> ModuleType:
    """Execute ``env.py`` offline, planning no steps, and return it as a module.

    ``env.py`` reads ``alembic.context`` at import, so it is executed inside an
    ``EnvironmentContext``; offline mode with no steps renders nothing into a private
    buffer and opens no connection.
    """
    cfg = Config(str(ALEMBIC_INI), output_buffer=io.StringIO())
    cfg.attributes["configure_logger"] = False
    spec = importlib.util.spec_from_file_location("migrations_env_under_test", ENV_PY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with EnvironmentContext(
        cfg,
        ScriptDirectory.from_config(cfg),
        fn=_no_steps,
        as_sql=True,
        destination_rev="head",
    ):
        spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lock_key() -> int:
    """``MIGRATION_LOCK_KEY`` as ``env.py`` defines it."""
    key = _load_env().MIGRATION_LOCK_KEY
    assert isinstance(key, int)
    return key


@pytest.fixture
def target() -> Iterator[Target]:
    """Create an empty private schema; drop it, and everything in it, afterwards."""
    url = _database_url()
    schema = f"migration_lock_{uuid.uuid4().hex}"
    engine = sa.create_engine(
        url,
        poolclass=NullPool,
        connect_args={"options": f"{_search_path_option(schema)} -c TimeZone=UTC"},
    )
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    cli_url = url.update_query_dict({"options": _search_path_option(schema)})
    try:
        yield Target(
            engine=engine,
            schema=schema,
            cli_url=cli_url.render_as_string(hide_password=False),
        )
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


def _upgrade(connection: Connection) -> None:
    """Run ``alembic upgrade head`` through ``env.py`` on the caller's ``connection``."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["configure_logger"] = False
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


def _lock_parameters(key: int) -> dict[str, int]:
    """The ``pg_locks`` columns of a ``bigint`` advisory key: its high and low 32 bits."""
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    return {"classid": unsigned >> 32, "objid": unsigned & 0xFFFF_FFFF}


_HELD_HERE: Final = sa.text(
    "SELECT count(*) FROM pg_locks "
    "WHERE locktype = 'advisory' AND objsubid = 1 "
    "AND classid::bigint = :classid AND objid::bigint = :objid "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND granted AND pid = pg_backend_pid()"
)
"""Counts the advisory locks on ``:classid``/``:objid`` that this session holds."""

_WAITING: Final = sa.text(
    "SELECT count(*) FROM pg_locks "
    "WHERE locktype = 'advisory' AND objsubid = 1 "
    "AND classid::bigint = :classid AND objid::bigint = :objid "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND NOT granted"
)
"""Counts the sessions of this database waiting for the advisory lock ``:classid``/``:objid``."""


def _holds_lock(connection: Connection, key: int) -> bool:
    """Report whether the session of ``connection`` holds the advisory lock ``key``."""
    return bool(connection.scalar(_HELD_HERE, _lock_parameters(key)))


def _waiters(engine: Engine, key: int) -> int:
    """Count the sessions waiting for the advisory lock ``key``."""
    with engine.connect() as connection:
        return int(connection.scalar(_WAITING, _lock_parameters(key)) or 0)


def _await_waiters(
    engine: Engine, key: int, processes: list[subprocess.Popen[str]], count: int
) -> None:
    """Wait until ``count`` sessions queue for ``key``; fail when a process ends first."""
    deadline = time.monotonic() + WAITERS_TIMEOUT
    while _waiters(engine, key) < count:
        for process in processes:
            if process.poll() is not None:
                _stdout, stderr = process.communicate()
                pytest.fail(f"migrate exited {process.returncode} before waiting:\n{stderr}")
        if time.monotonic() > deadline:
            pytest.fail(f"{count} migrate processes did not queue on the migration lock")
        time.sleep(0.1)


def _start_migrate(cli_url: str) -> subprocess.Popen[str]:
    """Start ``ads-engine migrate`` against ``cli_url`` in ``backend/``."""
    environment = dict(os.environ)
    environment["DATABASE_URL"] = cli_url
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and program; argv from the test
        [sys.executable, "-c", CLI_PROGRAM, "migrate"],
        cwd=BACKEND,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _revisions(target: Target) -> list[str]:
    """The revisions recorded in the schema's ``alembic_version`` table."""
    with target.engine.connect() as connection:
        return list(connection.scalars(sa.text("SELECT version_num FROM alembic_version")))


def _tables(target: Target) -> set[str]:
    """The names of the tables in the private schema."""
    return set(sa.inspect(target.engine).get_table_names(schema=target.schema))


def test_the_migration_lock_key_is_derived_from_its_name_and_unique(lock_key: int) -> None:
    expected = int.from_bytes(
        hashlib.sha256(b"ads_engine:migrate").digest()[:8], "big", signed=True
    )
    assert lock_key == expected
    assert -(2**63) <= lock_key < 2**63
    assert lock_key != ANALYSIS_LOCK_KEY
    assert lock_key != SCHEDULER_LOCK_KEY


def test_offline_sql_rendering_takes_no_lock() -> None:
    buffer = io.StringIO()
    cfg = Config(str(ALEMBIC_INI), output_buffer=buffer)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE alembic_version" in sql
    assert "CREATE TABLE source_dataset" in sql
    assert "pg_advisory" not in sql
    assert "pg_try_advisory" not in sql


@pytest.mark.db
@pytest.mark.parametrize("held", [False, True], ids=["racing", "queued-on-a-held-lock"])
def test_two_migrate_runs_started_together_on_an_empty_schema_both_succeed(
    target: Target, lock_key: int, held: bool
) -> None:
    results: list[tuple[int, str]] = []
    with target.engine.connect() as holder:
        if held:
            holder.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
            holder.commit()
        processes = [_start_migrate(target.cli_url) for _ in range(2)]
        try:
            if held:
                _await_waiters(target.engine, lock_key, processes, count=2)
                holder.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                holder.commit()
            for process in processes:
                _stdout, stderr = process.communicate(timeout=CLI_TIMEOUT)
                results.append((process.returncode, stderr))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    for returncode, stderr in results:
        assert returncode == 0, stderr
        assert "IntegrityError" not in stderr
        assert "UniqueViolation" not in stderr
        if held:
            assert WAIT_LOG in stderr
    assert _revisions(target) == [HEAD]
    assert _tables(target) == set(Base.metadata.tables) | {"alembic_version"}


@pytest.mark.db
def test_a_caller_transaction_takes_a_transaction_lock_and_is_never_committed(
    target: Target, lock_key: int
) -> None:
    with target.engine.connect() as connection:
        transaction = connection.begin()
        _upgrade(connection)
        assert connection.in_transaction()
        assert _holds_lock(connection, lock_key)
        assert list(connection.scalars(sa.text("SELECT version_num FROM alembic_version"))) == [
            HEAD
        ]
        with target.engine.connect() as other:
            assert other.scalar(sa.text("SELECT to_regclass('alembic_version')")) is None
            busy = other.scalar(sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
            assert busy is False
        transaction.rollback()
        assert not _holds_lock(connection, lock_key)
    assert _tables(target) == set()


@pytest.mark.db
def test_a_caller_connection_outside_a_transaction_commits_and_releases_the_lock(
    target: Target, lock_key: int
) -> None:
    with target.engine.connect() as connection:
        _upgrade(connection)
        assert not connection.in_transaction()
        assert not _holds_lock(connection, lock_key)
        connection.rollback()
        _upgrade(connection)
        assert not connection.in_transaction()
    assert _revisions(target) == [HEAD]
    assert _tables(target) == set(Base.metadata.tables) | {"alembic_version"}


@pytest.mark.db
def test_a_failed_run_raises_its_own_error_and_releases_the_lock(
    target: Target, lock_key: int
) -> None:
    with target.engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE source_dataset (id integer)"))
    with target.engine.connect() as connection:
        with pytest.raises(ProgrammingError) as caught:
            _upgrade(connection)
        assert isinstance(caught.value.orig, psycopg.errors.DuplicateTable)
        assert not connection.in_transaction()
        assert not _holds_lock(connection, lock_key)
    assert _tables(target) == {"source_dataset"}
