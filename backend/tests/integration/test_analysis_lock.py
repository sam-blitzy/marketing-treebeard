"""Integration tests for ``ads_engine.analysis.lock``, the shared ``analysis`` lock.

AAP 0.5.3 "One lock for every entry point", "Lock unavailable" and "Crash recovery";
AAP 0.2.2 "Only one run executes at a time, whichever process starts it". The API, CLI and
worker behaviours built on :class:`RunInProgress` (409, exit code 3, pending requests) are
covered by ``test_runs_and_decisions.py``; this module covers the lock itself:

* the key is deterministic, fits ``bigint`` and differs from the scheduler's key;
* while one holder has the lock, another ``analysis_lock()`` raises ``RunInProgress``,
  whether the holder is a task of this process or a separate database session;
* the lock is released after the block exits normally, raises, is cancelled, or loses its
  connection, and the block's own exception always propagates unchanged;
* a ``running`` row left by a crashed holder is marked ``failed`` by the next holder, and
  is never touched while the lock is unavailable.

Everything runs against the shared PostgreSQL 18 at ``TEST_DATABASE_URL`` (falling back to
``DATABASE_URL``), in a private schema holding only ``analysis_run``; an unreachable database
fails the tests, never skips them. Advisory locks are database-scoped, so the real
``ANALYSIS_LOCK_KEY`` is used and checked to be free before each test. Lock state is
observed from independent psycopg connections (probes).
"""

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from ads_engine.analysis import lock as lock_module
from ads_engine.analysis.lock import ANALYSIS_LOCK_KEY, RunInProgress, analysis_lock
from ads_engine.db.base import Base
from ads_engine.db.models.analysis import AnalysisRun
from ads_engine.db.session import dispose_engine, reset_engine
from ads_engine.settings import Settings, get_settings

pytestmark = pytest.mark.integration

type ProbeConnection = psycopg.AsyncConnection[tuple[Any, ...]]
type LogEvent = tuple[str, str, dict[str, object]]

# How long to wait for the server to end a terminated backend and free its locks.
_SERVER_WAIT_SECONDS = 10.0

# Read once at import, before any fixture removes DATABASE_URL from the environment.
_CONFIGURED_TEST_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    if not _CONFIGURED_TEST_URL:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database, "
            "for example postgresql+psycopg://ads:ads@127.0.0.1:55432/ads_engine_test_cN",
            pytrace=False,
        )
    try:
        url = make_url(_CONFIGURED_TEST_URL)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL", pytrace=False)
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be PostgreSQL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _search_path_option(schema: str) -> str:
    """Return the libpq ``options`` value that pins a connection to ``schema``."""
    return f"-c search_path={schema}"


def _libpq_conninfo(url: URL) -> str:
    """Return the plain libpq URL of the test database, for psycopg probes."""
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


async def _connect_probe(url: URL, schema: str) -> ProbeConnection:
    """Open an autocommit psycopg connection pinned to ``schema``, or fail the test."""
    try:
        # `options` is passed as a keyword: libpq reads `+` in a URL query literally.
        return await psycopg.AsyncConnection.connect(
            _libpq_conninfo(url), autocommit=True, options=_search_path_option(schema)
        )
    except psycopg.Error as exc:
        pytest.fail(
            f"PostgreSQL test database {url.render_as_string(hide_password=True)} is "
            f"unreachable ({type(exc).__name__}). Start ads-engine-pg or fix TEST_DATABASE_URL.",
            pytrace=False,
        )


async def _scalar(probe: ProbeConnection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    """Run ``sql`` on the probe and return the first column of its first row."""
    cur = await probe.execute(sql.encode(), params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _probe_can_lock(probe: ProbeConnection) -> bool:
    """Try the analysis lock from the probe, releasing it again when taken."""
    acquired = bool(await _scalar(probe, "SELECT pg_try_advisory_lock(%s)", (ANALYSIS_LOCK_KEY,)))
    if acquired:
        await probe.execute(b"SELECT pg_advisory_unlock(%s)", (ANALYSIS_LOCK_KEY,))
    return acquired


async def _eventually(check: Callable[[], Awaitable[bool]], what: str) -> None:
    """Poll ``check`` until it holds, failing after the server wait budget."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SERVER_WAIT_SECONDS
    while not await check():
        if loop.time() > deadline:
            pytest.fail(f"timed out after {_SERVER_WAIT_SECONDS}s waiting until {what}")
        await asyncio.sleep(0.05)


async def _wait_lock_free(probe: ProbeConnection) -> None:
    """Wait until the probe can take the analysis lock (a terminated holder has ended)."""

    async def free() -> bool:
        return await _probe_can_lock(probe)

    await _eventually(free, "the analysis lock is free")


async def _terminate(probe: ProbeConnection, pid: int) -> None:
    """Terminate backend ``pid`` and wait until the server has ended it."""
    await probe.execute(b"SELECT pg_terminate_backend(%s)", (pid,))

    async def gone() -> bool:
        count = await _scalar(probe, "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,))
        return int(count) == 0

    await _eventually(gone, f"backend {pid} has ended")


async def _insert_run(probe: ProbeConnection, status: str) -> int:
    """Insert an ``analysis_run`` row with ``status`` and return its id."""
    finished = None if status == "running" else datetime(2026, 10, 2, tzinfo=UTC)
    run_id = await _scalar(
        probe,
        "INSERT INTO analysis_run (kind, window_start, window_end, source_day_boundaries, "
        "trigger, data_modes, dataset_ids, config_hash, default_days, trusted_min_days, "
        "status, started_at, finished_at) "
        "VALUES ('default', %s, %s, '[]'::jsonb, 'scheduled', '{}'::jsonb, '{}'::jsonb, "
        "'hash', 28, 14, %s, %s, %s) RETURNING id",
        (
            date(2026, 9, 4),
            date(2026, 10, 1),
            status,
            datetime(2026, 10, 2, tzinfo=UTC),
            finished,
        ),
    )
    return int(run_id)


async def _run_row(probe: ProbeConnection, run_id: int) -> tuple[str, datetime | None]:
    """Return ``(status, finished_at)`` of run ``run_id``."""
    cur = await probe.execute(
        b"SELECT status, finished_at FROM analysis_run WHERE id = %s", (run_id,)
    )
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), row[1]


class _LogRecorder:
    """Stand-in for the module logger that records every call by level."""

    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def _record(self, level: str, event_name: str, fields: dict[str, object]) -> None:
        self.events.append((level, event_name, fields))

    def debug(self, event_name: str, **fields: object) -> None:
        """Record a ``debug`` event."""
        self._record("debug", event_name, fields)

    def info(self, event_name: str, **fields: object) -> None:
        """Record an ``info`` event."""
        self._record("info", event_name, fields)

    def warning(self, event_name: str, **fields: object) -> None:
        """Record a ``warning`` event."""
        self._record("warning", event_name, fields)

    def exception(self, event_name: str, **fields: object) -> None:
        """Record an ``exception`` event."""
        self._record("exception", event_name, fields)

    def names(self) -> list[str]:
        """Return the recorded event names in order."""
        return [name for _, name, _ in self.events]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run every async test on asyncio, the loop psycopg's async driver uses here."""
    return "asyncio"


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding only ``analysis_run``; drop it afterwards.

    Yields:
        The database URL and the private schema's name.
    """
    url = _database_url()
    schema = f"analysis_lock_test_{uuid.uuid4().hex}"
    engine = create_engine(
        url, poolclass=NullPool, connect_args={"options": _search_path_option(schema)}
    )
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection, tables=[AnalysisRun.__table__])
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {type(exc).__name__}",
            pytrace=False,
        )
    try:
        yield url, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
async def app_database(
    database: tuple[URL, str], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Point the application's settings and engine at the private schema.

    Every settings variable inherited from the host shell is removed first (the shell
    exports real credentials), and the engine is disposed on this test's own loop.
    """
    url, schema = database
    for key in list(os.environ):
        if key.lower() in Settings.model_fields:
            monkeypatch.delenv(key, raising=False)
    app_url = url.update_query_dict({"options": _search_path_option(schema)})
    monkeypatch.setenv("DATABASE_URL", app_url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    reset_engine()
    try:
        yield
    finally:
        await dispose_engine()
        reset_engine()
        get_settings.cache_clear()


@pytest.fixture
async def probe(database: tuple[URL, str], app_database: None) -> AsyncIterator[ProbeConnection]:
    """An observer connection; the lock must be free and the table empty at the start."""
    url, schema = database
    conn = await _connect_probe(url, schema)
    try:
        if not await _probe_can_lock(conn):
            pytest.fail(
                "the analysis advisory lock is held by another session on the test database; "
                "use a database of your own (TEST_DATABASE_URL)",
                pytrace=False,
            )
        await conn.execute(b"DELETE FROM analysis_run")
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def holder(database: tuple[URL, str], app_database: None) -> AsyncIterator[ProbeConnection]:
    """A second independent connection, standing in for another process's lock session."""
    url, schema = database
    conn = await _connect_probe(url, schema)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def log_events(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """Record what the lock module logs for the duration of one test."""
    recorder = _LogRecorder()
    monkeypatch.setattr(lock_module, "_log", recorder)
    return recorder


# ---------------------------------------------------------------------------
# Key and exception
# ---------------------------------------------------------------------------


def test_key_is_the_documented_sha256_derivation_within_bigint() -> None:
    """The key is sha256("ads_engine:analysis")[:8], signed big-endian, inside bigint."""
    expected = int.from_bytes(
        hashlib.sha256(b"ads_engine:analysis").digest()[:8], "big", signed=True
    )
    assert expected == ANALYSIS_LOCK_KEY
    assert -(2**63) <= ANALYSIS_LOCK_KEY < 2**63


def test_key_differs_from_the_scheduler_key_derivation() -> None:
    """The scheduler's key, derived from "ads_engine:scheduler", never equals this key."""
    scheduler_key = int.from_bytes(
        hashlib.sha256(b"ads_engine:scheduler").digest()[:8], "big", signed=True
    )
    assert scheduler_key != ANALYSIS_LOCK_KEY


def test_run_in_progress_carries_code_and_message() -> None:
    """RunInProgress is a RuntimeError with code run_in_progress and the fixed message."""
    exc = RunInProgress()
    assert isinstance(exc, RuntimeError)
    assert exc.code == "run_in_progress"
    assert RunInProgress.code == "run_in_progress"
    assert str(exc) == "An analysis run is already in progress"
    assert exc.message == str(exc)
    custom = RunInProgress("worker run holds the lock")
    assert str(custom) == custom.message == "worker run holds the lock"
    assert custom.code == "run_in_progress"


def test_public_api_is_exported() -> None:
    """The module exports exactly the key, the exception and the context manager."""
    assert sorted(lock_module.__all__) == ["ANALYSIS_LOCK_KEY", "RunInProgress", "analysis_lock"]


# ---------------------------------------------------------------------------
# Contention and release
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_second_acquisition_raises_while_held_and_third_succeeds_after(
    probe: ProbeConnection, log_events: _LogRecorder
) -> None:
    """One holder at a time: a second analysis_lock() raises; after exit a third succeeds."""
    async with analysis_lock() as conn:
        assert await conn.scalar(text("SELECT 1")) == 1
        assert not await _probe_can_lock(probe)
        with pytest.raises(RunInProgress) as raised:
            async with analysis_lock():
                pytest.fail("a second holder entered the analysis lock")
        assert raised.value.code == "run_in_progress"
    assert await _probe_can_lock(probe)
    async with analysis_lock():
        assert not await _probe_can_lock(probe)
    assert await _probe_can_lock(probe)
    names = log_events.names()
    assert names.count("analysis_lock_acquired") == 2
    assert names.count("analysis_lock_released") == 2
    assert names.count("analysis_lock_unavailable") == 1


@pytest.mark.anyio
async def test_concurrent_tasks_contend_for_the_lock(probe: ProbeConnection) -> None:
    """Two tasks of one process contend like two processes: each call has its own session."""
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def hold() -> None:
        async with analysis_lock():
            entered.set()
            await leave.wait()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), _SERVER_WAIT_SECONDS)
    try:
        with pytest.raises(RunInProgress):
            async with analysis_lock():
                pytest.fail("a concurrent task entered the analysis lock")
    finally:
        leave.set()
        await task
    assert await _probe_can_lock(probe)


@pytest.mark.anyio
async def test_lock_held_by_another_session_raises_and_keeps_its_running_row(
    probe: ProbeConnection, holder: ProbeConnection
) -> None:
    """A live holder's running row is never marked failed by a refused acquisition."""
    assert await _scalar(holder, "SELECT pg_try_advisory_lock(%s)", (ANALYSIS_LOCK_KEY,))
    run_id = await _insert_run(holder, "running")
    with pytest.raises(RunInProgress):
        async with analysis_lock():
            pytest.fail("entered the analysis lock while another session held it")
    status, finished_at = await _run_row(probe, run_id)
    assert (status, finished_at) == ("running", None)
    await holder.execute(b"SELECT pg_advisory_unlock(%s)", (ANALYSIS_LOCK_KEY,))


@pytest.mark.anyio
async def test_body_exception_propagates_unchanged_and_releases(
    probe: ProbeConnection, log_events: _LogRecorder
) -> None:
    """The block's exception is re-raised as is, and the lock is free afterwards."""

    class BodyError(Exception):
        pass

    with pytest.raises(BodyError, match="rule failed"):
        async with analysis_lock():
            raise BodyError("rule failed")
    assert await _probe_can_lock(probe)
    assert "analysis_lock_released" in log_events.names()


@pytest.mark.anyio
async def test_cancelled_holder_releases_the_lock(probe: ProbeConnection) -> None:
    """Cancelling the task inside the block propagates and leaves the lock free."""
    entered = asyncio.Event()

    async def hold() -> None:
        async with analysis_lock():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), _SERVER_WAIT_SECONDS)
    assert not await _probe_can_lock(probe)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _probe_can_lock(probe)


@pytest.mark.anyio
async def test_lock_released_by_the_block_logs_not_held(
    probe: ProbeConnection, log_events: _LogRecorder
) -> None:
    """A lock the block already released is reported at exit, without an error."""
    async with analysis_lock() as conn:
        released = await conn.scalar(
            text("SELECT pg_advisory_unlock(:key)"), {"key": ANALYSIS_LOCK_KEY}
        )
        assert released is True
    assert "analysis_lock_not_held_at_release" in log_events.names()
    assert "analysis_lock_released" not in log_events.names()
    assert await _probe_can_lock(probe)


@pytest.mark.anyio
async def test_invalidated_connection_skips_explicit_release(
    probe: ProbeConnection, log_events: _LogRecorder
) -> None:
    """An invalidated connection's closed session already freed the lock; nothing is sent."""
    async with analysis_lock() as conn:
        await conn.invalidate()
    await _wait_lock_free(probe)
    released = [fields for _, name, fields in log_events.events if name == "analysis_lock_released"]
    assert released
    assert released[-1]["via"] == "session_end"
    assert "analysis_lock_release_failed" not in log_events.names()


@pytest.mark.anyio
async def test_failed_release_is_logged_and_never_raised(
    probe: ProbeConnection, log_events: _LogRecorder
) -> None:
    """A lost lock connection makes the release fail: logged, not raised, lock still free."""
    async with analysis_lock() as conn:
        pid = int(await conn.scalar(text("SELECT pg_backend_pid()")))
        await _terminate(probe, pid)
    await _wait_lock_free(probe)
    failures = [
        fields for _, name, fields in log_events.events if name == "analysis_lock_release_failed"
    ]
    assert len(failures) == 1
    assert isinstance(failures[0]["error"], str)


@pytest.mark.anyio
async def test_failed_release_never_replaces_the_body_exception(
    probe: ProbeConnection, log_events: _LogRecorder
) -> None:
    """When the body raises after its connection died, the body's exception wins."""

    async def run_losing_connection() -> None:
        async with analysis_lock() as conn:
            pid = int(await conn.scalar(text("SELECT pg_backend_pid()")))
            await _terminate(probe, pid)
            raise LookupError("body")

    with pytest.raises(LookupError, match="body"):
        await run_losing_connection()
    await _wait_lock_free(probe)
    assert "analysis_lock_release_failed" in log_events.names()


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_crashed_running_row_is_marked_failed_by_next_holder(
    probe: ProbeConnection, holder: ProbeConnection, log_events: _LogRecorder
) -> None:
    """A holder that dies mid-run frees the lock; the next holder fails its running row."""
    assert await _scalar(holder, "SELECT pg_try_advisory_lock(%s)", (ANALYSIS_LOCK_KEY,))
    crashed = await _insert_run(holder, "running")
    succeeded = await _insert_run(probe, "succeeded")
    already_failed = await _insert_run(probe, "failed")
    holder_pid = int(await _scalar(holder, "SELECT pg_backend_pid()"))
    await _terminate(probe, holder_pid)
    await _wait_lock_free(probe)

    async with analysis_lock():
        status, finished_at = await _run_row(probe, crashed)
        assert status == "failed"
        assert finished_at is not None
        # The orphan is committed before the block runs, so a new run may start now.
        new_run = await _insert_run(probe, "running")

    assert await _run_row(probe, succeeded) == ("succeeded", datetime(2026, 10, 2, tzinfo=UTC))
    assert await _run_row(probe, already_failed) == ("failed", datetime(2026, 10, 2, tzinfo=UTC))
    assert (await _run_row(probe, new_run))[0] == "running"
    orphans = [
        fields for _, name, fields in log_events.events if name == "analysis_orphaned_runs_failed"
    ]
    assert orphans == [{"count": 1, "run_ids": [crashed]}]


@pytest.mark.anyio
async def test_no_orphans_logs_nothing_and_changes_nothing(
    probe: ProbeConnection, log_events: _LogRecorder
) -> None:
    """With no running row, acquisition writes nothing and logs no recovery."""
    succeeded = await _insert_run(probe, "succeeded")
    async with analysis_lock():
        pass
    assert (await _run_row(probe, succeeded))[0] == "succeeded"
    assert "analysis_orphaned_runs_failed" not in log_events.names()


@pytest.mark.anyio
async def test_single_running_row_guard_backs_the_lock(probe: ProbeConnection) -> None:
    """The partial unique index allows at most one running row, as the second guard."""
    await _insert_run(probe, "running")
    with pytest.raises(psycopg.errors.UniqueViolation):
        await _insert_run(probe, "running")
