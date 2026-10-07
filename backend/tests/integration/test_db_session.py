"""Integration tests for ``ads_engine.db.session``.

The pure helpers (the ``jsonb`` serialiser, URL normalisation and the libpq ``options``
builder) are tested without a database. Everything else runs against the shared
PostgreSQL 18 at ``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``): engine caching,
reset and disposal, the UTC session time zone, the ``jsonb`` round trip, the pre-pinged
pool, ``session_scope`` commit, rollback and cancellation, and the advisory-lock release
guaranteed by ``dedicated_connection``. An unreachable database fails the test; it is never
skipped.

No table or other database object is created. Commit and rollback are observed through
``pg_xact_status`` of the scope's own transaction id, and advisory locks are observed from a
separate psycopg connection (the probe). Every advisory-lock key is derived from this file's
own namespace, so no other test or process sharing the database can collide with it.

Every test that touches the engine builds its own and disposes it on teardown, so no pooled
connection ever crosses event loops, and the settings environment is isolated with
``monkeypatch`` because the host shell exports real credentials.
"""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import bindparam, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.exc import InterfaceError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import QueuePool

from ads_engine.db import session as db_session
from ads_engine.db.session import (
    dedicated_connection,
    dispose_engine,
    get_engine,
    get_sessionmaker,
    json_dumps,
    reset_engine,
    session_scope,
)
from ads_engine.settings import Settings, get_settings

pytestmark = pytest.mark.integration

type ProbeConnection = psycopg.AsyncConnection[tuple[Any, ...]]
type LogEvent = tuple[str, str, dict[str, object]]

# Namespace of every advisory-lock key taken here; unique to this file.
_LOCK_NAMESPACE = "ads_engine:backend/tests/integration/test_db_session.py"
# How long to wait for the server to end a terminated or closed backend.
_SERVER_WAIT_SECONDS = 10.0

_LOCK = text("SELECT pg_advisory_lock(:key)")
_XID = text("SELECT pg_current_xact_id()::text")
_PID = text("SELECT pg_backend_pid()")
_UNLOCK_ALL_SQL = "SELECT pg_advisory_unlock_all()"

# Read once at import, before any fixture removes DATABASE_URL from the environment.
_CONFIGURED_TEST_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _lock_key(label: str) -> int:
    """Return a signed 64-bit advisory-lock key unique to this file and ``label``."""
    digest = hashlib.blake2b(f"{_LOCK_NAMESPACE}:{label}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _hidden(url: str) -> str:
    """Render a database URL with its password hidden, for failure messages."""
    return make_url(url).render_as_string(hide_password=True)


def _test_database_url() -> str:
    """Return the test database URL, failing the test when none is configured."""
    if not _CONFIGURED_TEST_URL:
        pytest.fail(
            "TEST_DATABASE_URL is not set (nor DATABASE_URL): point it at the shared "
            "PostgreSQL 18 test database, for example "
            "postgresql+psycopg://ads:ads@127.0.0.1:55432/ads_engine_test_cN",
            pytrace=False,
        )
    return _CONFIGURED_TEST_URL


def _errors(events: list[LogEvent]) -> list[LogEvent]:
    """Return the ``exception``-level events, the ones a failed cleanup logs."""
    return [logged for logged in events if logged[0] == "exception"]


def _with_query(url: str, **query: str) -> str:
    """Return ``url`` with the given query parameters set, password kept."""
    return make_url(url).update_query_dict(query).render_as_string(hide_password=False)


def _use_database_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Point the settings at ``url`` the way the module documents for tests."""
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    reset_engine()


async def _ping_or_fail(engine: AsyncEngine, url: str) -> None:
    """Open one connection through ``engine``, failing the test when the database is down."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.fail(
            f"PostgreSQL test database {_hidden(url)} is unreachable "
            f"({type(exc).__name__}: {getattr(exc, 'orig', exc)}). "
            "Start the shared ads-engine-pg container (docker start ads-engine-pg) or point "
            "TEST_DATABASE_URL at a reachable PostgreSQL 18.",
            pytrace=False,
        )


async def _xact_status(probe: ProbeConnection, xid: str) -> str:
    """Return ``pg_xact_status`` of a transaction id, as seen from the probe."""
    cur = await probe.execute("SELECT pg_xact_status(%s::xid8)", (xid,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _probe_can_lock(probe: ProbeConnection, key: int) -> bool:
    """Try the session-level lock ``key`` from the probe, releasing it again if taken."""
    cur = await probe.execute("SELECT pg_try_advisory_lock(%s)", (key,))
    row = await cur.fetchone()
    acquired = bool(row is not None and row[0])
    if acquired:
        await probe.execute("SELECT pg_advisory_unlock(%s)", (key,))
    return acquired


async def _backend_alive(probe: ProbeConnection, pid: int) -> bool:
    """Return whether the server still runs the backend ``pid``."""
    cur = await probe.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,))
    row = await cur.fetchone()
    return row is not None and row[0] == 1


async def _eventually(check: Callable[[], Awaitable[bool]], what: str) -> None:
    """Poll ``check`` until it holds, failing after the server wait budget."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SERVER_WAIT_SECONDS
    while not await check():
        if loop.time() > deadline:
            pytest.fail(f"timed out after {_SERVER_WAIT_SECONDS}s waiting until {what}")
        await asyncio.sleep(0.05)


async def _terminate(probe: ProbeConnection, pid: int) -> None:
    """Terminate backend ``pid`` from the probe and wait until the server has ended it."""
    await probe.execute("SELECT pg_terminate_backend(%s)", (pid,))

    async def gone() -> bool:
        return not await _backend_alive(probe, pid)

    await _eventually(gone, f"backend {pid} has ended")


async def _advisory_locks_held(probe: ProbeConnection, pid: int) -> int:
    """Count the advisory locks granted to backend ``pid``."""
    cur = await probe.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND pid = %s",
        (pid,),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


class _LogRecorder:
    """Stand-in for the module logger that records ``info`` and ``exception`` calls."""

    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def info(self, event_name: str, **fields: object) -> None:
        """Record an ``info`` event."""
        self.events.append(("info", event_name, fields))

    def exception(self, event_name: str, **fields: object) -> None:
        """Record an ``exception`` event."""
        self.events.append(("exception", event_name, fields))


class _NoOffset(tzinfo):
    """A tzinfo whose ``utcoffset`` is unknown, which makes a datetime naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        """Return no offset."""
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        """Return no DST offset."""
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        """Return no name."""
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run every async test on asyncio, the loop psycopg's async driver uses here."""
    return "asyncio"


@pytest.fixture
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every settings variable inherited from the host shell and clear the caches.

    The shell exports real credentials, so none of them reaches a test; the settings and
    engine caches are cleared before and after, so each test reads its own environment.
    """
    for key in list(os.environ):
        if key.lower() in Settings.model_fields:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    reset_engine()
    yield
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
async def engine_state(isolated_settings: None) -> AsyncIterator[None]:
    """Dispose whatever engine the test built, on the test's own event loop."""
    try:
        yield
    finally:
        await dispose_engine()
        reset_engine()


@pytest.fixture
def database_url(isolated_settings: None, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point ``DATABASE_URL`` at the test database and return that URL."""
    url = _test_database_url()
    _use_database_url(monkeypatch, url)
    return url


@pytest.fixture
async def engine(database_url: str, engine_state: None) -> AsyncEngine:
    """Build the process engine from the test database URL and check it is reachable."""
    built = get_engine()
    await _ping_or_fail(built, database_url)
    return built


@pytest.fixture
async def probe(database_url: str) -> AsyncIterator[ProbeConnection]:
    """Open an independent autocommit psycopg connection that observes the engine's work."""
    conninfo = make_url(database_url).set(drivername="postgresql")
    try:
        conn = await psycopg.AsyncConnection.connect(
            conninfo.render_as_string(hide_password=False), autocommit=True
        )
    except psycopg.Error as exc:
        pytest.fail(
            f"PostgreSQL test database {_hidden(database_url)} is unreachable for the probe "
            f"({type(exc).__name__}: {exc}). Start the shared ads-engine-pg container or "
            "point TEST_DATABASE_URL at a reachable PostgreSQL 18.",
            pytrace=False,
        )
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def log_events(monkeypatch: pytest.MonkeyPatch) -> list[LogEvent]:
    """Record what the module logs for the duration of one test."""
    recorder = _LogRecorder()
    monkeypatch.setattr(db_session, "_log", recorder)
    return recorder.events


# ---------------------------------------------------------------------------
# json_dumps: the jsonb serialiser
# ---------------------------------------------------------------------------


def test_json_dumps_matches_its_documented_example() -> None:
    """The docstring example serialises exactly as documented."""
    assert json_dumps({"a": Decimal("1.500000"), "d": date(2026, 10, 1)}) == (
        '{"a":"1.500000","d":"2026-10-01"}'
    )


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("1E+2"), '"100"'),
        (Decimal("1.5E-7"), '"0.00000015"'),
        (Decimal("-0.000001"), '"-0.000001"'),
        (Decimal("123456789012.345678"), '"123456789012.345678"'),
        (Decimal("0"), '"0"'),
    ],
)
def test_json_dumps_writes_decimals_as_exact_fixed_point_strings(
    amount: Decimal, expected: str
) -> None:
    """A Decimal becomes a fixed-point string with no exponent, keeping every digit."""
    assert json_dumps(amount) == expected


@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_json_dumps_rejects_non_finite_decimals(amount: Decimal) -> None:
    """A non-finite Decimal has no exact JSON form and raises ValueError."""
    with pytest.raises(ValueError, match="non-finite Decimal"):
        json_dumps({"amount": amount})


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_json_dumps_rejects_non_finite_floats(number: float) -> None:
    """NaN and infinite floats raise instead of writing a token PostgreSQL refuses."""
    with pytest.raises(ValueError, match="Out of range float values"):
        json_dumps([number])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 10, 7, 12, 30, tzinfo=UTC), '"2026-10-07T12:30:00+00:00"'),
        (
            datetime(2026, 10, 7, 8, 30, 15, 250, tzinfo=timezone(timedelta(hours=-4))),
            '"2026-10-07T08:30:15.000250-04:00"',
        ),
        (date(2026, 10, 1), '"2026-10-01"'),
    ],
)
def test_json_dumps_writes_aware_datetimes_and_dates_as_iso_8601(
    value: date, expected: str
) -> None:
    """An aware datetime keeps its offset and a date is written as YYYY-MM-DD."""
    assert json_dumps(value) == expected


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 10, 7, 12, 30), datetime(2026, 10, 7, 12, 30, tzinfo=_NoOffset())],  # noqa: DTZ001
)
def test_json_dumps_rejects_naive_datetimes(value: datetime) -> None:
    """A naive datetime, including one whose tzinfo gives no offset, raises ValueError."""
    with pytest.raises(ValueError, match="naive datetime"):
        json_dumps({"at": value})


def test_json_dumps_writes_sets_as_sorted_lists_encoding_each_item() -> None:
    """Sets and frozensets become sorted lists whose items are encoded in turn."""
    payload = {"terms": {"b", "c", "a"}, "amounts": frozenset({Decimal("2"), Decimal("1.5")})}
    assert json_dumps(payload) == '{"terms":["a","b","c"],"amounts":["1.5","2"]}'


@pytest.mark.parametrize(
    ("value", "type_name"), [(object(), "object"), (b"raw", "bytes"), (1 + 2j, "complex")]
)
def test_json_dumps_rejects_other_types_naming_them(value: object, type_name: str) -> None:
    """Any other unsupported type raises TypeError naming the type."""
    with pytest.raises(TypeError, match=f"Object of type {type_name} is not JSON serializable"):
        json_dumps({"value": value})


def test_json_dumps_is_compact_and_keeps_non_ascii_text() -> None:
    """Output has no spaces and keeps non-ASCII characters unescaped."""
    assert json_dumps({"ids": [1, 2], "name": "Zürich — 東京"}) == (
        '{"ids":[1,2],"name":"Zürich — 東京"}'
    )


# ---------------------------------------------------------------------------
# _normalise_url and _connect_options
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://ads:pw@db.internal:5433/ads_engine",
        "postgres://ads:pw@db.internal:5433/ads_engine",
        "postgresql+psycopg://ads:pw@db.internal:5433/ads_engine",
    ],
)
def test_normalise_url_pins_the_psycopg_driver(raw: str) -> None:
    """Plain schemes are rewritten to postgresql+psycopg; everything else is kept."""
    url = db_session._normalise_url(raw)
    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.password, url.host, url.port, url.database) == (
        "ads",
        "pw",
        "db.internal",
        5433,
        "ads_engine",
    )


def test_normalise_url_keeps_the_query_string() -> None:
    """Query parameters such as ``options`` survive normalisation."""
    url = db_session._normalise_url("postgres://h/db?options=-c%20search_path%3Dx")
    assert url.drivername == "postgresql+psycopg"
    assert url.query == {"options": "-c search_path=x"}


@pytest.mark.parametrize(
    ("raw", "driver"),
    [
        ("postgresql+asyncpg://ads:hunter2-secret@h/db", "postgresql+asyncpg"),
        ("postgresql+psycopg2://ads:hunter2-secret@h/db", "postgresql+psycopg2"),
        ("mysql+pymysql://ads:hunter2-secret@h/db", "mysql+pymysql"),
        ("sqlite:///ads.db", "sqlite"),
    ],
)
def test_normalise_url_rejects_any_other_driver_without_the_url(raw: str, driver: str) -> None:
    """Another backend or driver raises ValueError naming the driver, never the URL."""
    with pytest.raises(ValueError, match="must use the postgresql\\+psycopg driver") as excinfo:
        db_session._normalise_url(raw)
    message = str(excinfo.value)
    assert repr(driver) in message
    assert "hunter2-secret" not in message


@pytest.mark.parametrize(
    "raw",
    ["", "not a url", "://ads:hunter2-secret@h/db", "postgresql://ads:hunter2-secret@h:port/db"],
)
def test_normalise_url_rejects_malformed_text_without_leaking_it(raw: str) -> None:
    """A malformed URL or port raises a fixed message with the parse error not chained."""
    with pytest.raises(ValueError, match="DATABASE_URL is not a valid") as excinfo:
        db_session._normalise_url(raw)

    assert str(excinfo.value) == "DATABASE_URL is not a valid SQLAlchemy database URL"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert "hunter2-secret" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql+psycopg://h/db", "-c TimeZone=UTC"),
        (
            "postgresql+psycopg://h/db?options=-c%20search_path%3Dads",
            "-c search_path=ads -c TimeZone=UTC",
        ),
        (
            "postgresql+psycopg://h/db?options=-c%20TimeZone%3DEurope/Paris",
            "-c TimeZone=Europe/Paris -c TimeZone=UTC",
        ),
        (
            "postgresql+psycopg://h/db?options=-c%20a%3D1&options=-c%20b%3D2",
            "-c a=1 -c b=2 -c TimeZone=UTC",
        ),
        ("postgresql+psycopg://h/db?options=%20%20", "-c TimeZone=UTC"),
    ],
)
def test_connect_options_keeps_url_options_and_appends_utc_last(raw: str, expected: str) -> None:
    """Existing ``options`` are kept, blank ones dropped, and the UTC setting comes last."""
    assert db_session._connect_options(make_url(raw)) == expected


# ---------------------------------------------------------------------------
# Engine and sessionmaker lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_engine_builds_without_connecting_and_logs_only_an_allowlisted_descriptor(
    engine_state: None, monkeypatch: pytest.MonkeyPatch, log_events: list[LogEvent]
) -> None:
    """Building the engine opens no connection and logs driver, host, port and database only."""
    password = "s3cret-session-test"
    _use_database_url(monkeypatch, f"postgres://ads:{password}@127.0.0.1:1/nowhere")

    built = get_engine()

    assert built.url.drivername == "postgresql+psycopg"
    assert built.dialect.driver == "psycopg"
    assert built.pool.checkedout() == 0
    assert built.pool.checkedin() == 0
    assert built.pool.size() == 5
    assert log_events == [
        (
            "info",
            "db_engine_created",
            {
                "driver": "postgresql+psycopg",
                "host": "127.0.0.1",
                "port": 1,
                "database": "nowhere",
                "pool_size": 5,
                "max_overflow": 10,
            },
        )
    ]
    assert password not in repr(log_events)


@pytest.mark.anyio
async def test_get_engine_never_logs_query_credentials_or_the_user(
    engine_state: None, monkeypatch: pytest.MonkeyPatch, log_events: list[LogEvent]
) -> None:
    """Credentials in the URL's query string, and the user name, never reach the log."""
    # Assembled at run time so the secret scan never sees a literal credential.
    username = "-".join(("svc", "reader", "x41"))
    password = "-".join(("pw", "authority", "q8z"))
    ssl_password = "-".join(("key", "phrase", "m3v"))
    query_password = "-".join(("pw", "query", "k7d"))
    url = make_url("postgresql+psycopg://127.0.0.1:1/nowhere").set(
        username=username,
        password=password,
        query={
            "sslpassword": ssl_password,
            "password": query_password,
            "sslmode": "require",
            "application_name": "x",
        },
    )
    _use_database_url(monkeypatch, url.render_as_string(hide_password=False))

    built = get_engine()

    assert built.url.query["sslpassword"] == ssl_password
    assert isinstance(built.pool, QueuePool)
    assert built.pool.checkedout() == 0
    assert [(level, name) for level, name, _ in log_events] == [("info", "db_engine_created")]
    fields = log_events[0][2]
    assert set(fields) == {"driver", "host", "port", "database", "pool_size", "max_overflow"}
    assert fields == {
        "driver": "postgresql+psycopg",
        "host": "127.0.0.1",
        "port": 1,
        "database": "nowhere",
        "pool_size": 5,
        "max_overflow": 10,
    }
    logged = repr(log_events)
    for leaked in (password, ssl_password, query_password, username, "sslpassword"):
        assert leaked not in logged


@pytest.mark.anyio
@pytest.mark.db
async def test_get_engine_and_get_sessionmaker_are_cached_process_singletons(
    engine: AsyncEngine, log_events: list[LogEvent]
) -> None:
    """Repeated calls return the same engine and factory; the factory keeps objects loaded."""
    log_events.clear()
    factory = get_sessionmaker()

    assert get_engine() is engine
    assert get_engine() is engine
    assert get_sessionmaker() is factory
    assert factory.kw["bind"] is engine
    assert factory.kw["expire_on_commit"] is False
    assert log_events == []


@pytest.mark.anyio
@pytest.mark.db
async def test_get_engine_accepts_a_plain_postgres_scheme(
    database_url: str, engine_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``postgres://`` DATABASE_URL is served through psycopg against the real database."""
    plain = make_url(database_url).set(drivername="postgres")
    _use_database_url(monkeypatch, plain.render_as_string(hide_password=False))

    built = get_engine()
    await _ping_or_fail(built, database_url)

    assert built.url.drivername == "postgresql+psycopg"
    async with session_scope() as session:
        assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.anyio
async def test_get_engine_rejects_another_driver_and_caches_nothing(
    engine_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL naming another driver raises ValueError on every call, never a cached engine."""
    _use_database_url(monkeypatch, "postgresql+asyncpg://ads:hunter2-secret@127.0.0.1:1/db")

    for build in (get_engine, get_sessionmaker):
        with pytest.raises(ValueError, match="postgresql\\+asyncpg") as excinfo:
            build()
        assert "hunter2-secret" not in str(excinfo.value)


@pytest.mark.anyio
@pytest.mark.db
async def test_reset_engine_forgets_the_engine_without_closing_its_connections(
    engine: AsyncEngine,
    database_url: str,
    probe: ProbeConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a settings switch and reset, a new engine serves the new URL; the old pool stays."""
    old_factory = get_sessionmaker()
    async with engine.connect() as conn:
        old_pid = await conn.scalar(_PID)
    try:
        _use_database_url(
            monkeypatch, _with_query(database_url, options="-c search_path=ads_session_reset")
        )

        assert engine.pool.checkedin() == 1
        assert await _backend_alive(probe, old_pid)
        rebuilt = get_engine()
        assert rebuilt is not engine
        assert get_sessionmaker() is not old_factory
        async with session_scope() as session:
            assert await session.scalar(text("SHOW search_path")) == "ads_session_reset"
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.db
async def test_dispose_engine_closes_the_pool_and_forgets_the_engine(
    log_events: list[LogEvent], engine: AsyncEngine, probe: ProbeConnection
) -> None:
    """Disposal ends every pooled backend, a second call does nothing, and a new engine follows."""
    async with engine.connect() as conn:
        pid = await conn.scalar(_PID)
    assert await _backend_alive(probe, pid)

    await dispose_engine()

    async def ended() -> bool:
        return not await _backend_alive(probe, pid)

    await _eventually(ended, f"the disposed engine's backend {pid} has ended")
    await dispose_engine()
    assert [name for _, name, _ in log_events if name == "db_engine_disposed"] == [
        "db_engine_disposed"
    ]
    rebuilt = get_engine()
    assert rebuilt is not engine
    async with session_scope() as session:
        assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.anyio
@pytest.mark.db
async def test_pool_pre_ping_replaces_a_pooled_connection_that_died(
    engine: AsyncEngine, probe: ProbeConnection
) -> None:
    """A pooled connection whose backend was terminated is replaced on the next checkout."""
    async with session_scope() as session:
        pid = await session.scalar(_PID)
    await _terminate(probe, pid)

    async with session_scope() as session:
        replacement = await session.scalar(_PID)

    assert replacement != pid


# ---------------------------------------------------------------------------
# UTC session time zone and jsonb round trip
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_sessions_and_dedicated_connections_run_in_utc(engine: AsyncEngine) -> None:
    """Every connection runs in UTC, so timestamptz values come back aware in UTC."""
    instant = text("SELECT CAST('2026-10-07 12:00:00-04' AS timestamptz)")
    async with session_scope() as session:
        assert await session.scalar(text("SHOW TimeZone")) == "UTC"
        value = await session.scalar(instant)
    async with dedicated_connection() as conn:
        assert await conn.scalar(text("SHOW TimeZone")) == "UTC"

    assert value == datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    assert value.utcoffset() == timedelta(0)


@pytest.mark.anyio
@pytest.mark.db
async def test_url_options_are_kept_and_utc_overrides_a_url_time_zone(
    database_url: str, engine_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL ``options`` search_path is applied, and its TimeZone loses to the UTC setting."""
    _use_database_url(
        monkeypatch,
        _with_query(
            database_url, options="-c search_path=ads_session_opts -c TimeZone=America/New_York"
        ),
    )
    await _ping_or_fail(get_engine(), database_url)

    async with session_scope() as session:
        assert await session.scalar(text("SHOW search_path")) == "ads_session_opts"
        assert await session.scalar(text("SHOW TimeZone")) == "UTC"


@pytest.mark.anyio
@pytest.mark.db
async def test_jsonb_values_are_written_with_json_dumps_and_read_with_json_loads(
    engine: AsyncEngine,
) -> None:
    """Decimals and dates bound as jsonb come back as their exact strings, never floats."""
    statement = text("SELECT :doc").bindparams(bindparam("doc", type_=JSONB))
    document = {"amount": Decimal("1.500000"), "day": date(2026, 10, 1), "ids": [1, 2]}

    async with session_scope() as session:
        stored = await session.scalar(statement, {"doc": document})

    assert stored == {"amount": "1.500000", "day": "2026-10-01", "ids": [1, 2]}


# ---------------------------------------------------------------------------
# session_scope
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_session_scope_commits_and_closes_on_success(
    engine: AsyncEngine, probe: ProbeConnection
) -> None:
    """A scope that exits normally commits its transaction and returns its connection."""
    async with session_scope() as session:
        assert isinstance(session, AsyncSession)
        assert session.bind is engine
        xid = await session.scalar(_XID)
        assert engine.pool.checkedout() == 1

    assert await _xact_status(probe, xid) == "committed"
    assert not session.in_transaction()
    assert engine.pool.checkedout() == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_session_scope_gives_each_unit_of_work_its_own_session(engine: AsyncEngine) -> None:
    """Every scope yields a new session from the cached factory."""
    async with session_scope() as first, session_scope() as second:
        assert first is not second
        assert await first.scalar(_PID) != await second.scalar(_PID)


@pytest.mark.anyio
@pytest.mark.db
async def test_session_scope_rolls_back_and_reraises_the_same_exception(
    engine: AsyncEngine, probe: ProbeConnection, log_events: list[LogEvent]
) -> None:
    """An error in the scope rolls back, closes and propagates unchanged."""
    failure = ValueError("unit of work failed")
    xids: list[str] = []

    async def unit_of_work() -> None:
        async with session_scope() as session:
            xids.append(await session.scalar(_XID))
            raise failure

    with pytest.raises(ValueError, match="unit of work failed") as excinfo:
        await unit_of_work()

    assert excinfo.value is failure
    assert await _xact_status(probe, xids[0]) == "aborted"
    assert engine.pool.checkedout() == 0
    assert _errors(log_events) == []


class _Abort(BaseException):
    """A BaseException that is not an Exception, like KeyboardInterrupt."""


@pytest.mark.anyio
@pytest.mark.db
async def test_session_scope_rolls_back_on_a_base_exception(
    engine: AsyncEngine, probe: ProbeConnection
) -> None:
    """A BaseException that is not an Exception is rolled back and re-raised too."""
    xids: list[str] = []

    async def unit_of_work() -> None:
        async with session_scope() as session:
            xids.append(await session.scalar(_XID))
            raise _Abort

    with pytest.raises(_Abort):
        await unit_of_work()

    assert await _xact_status(probe, xids[0]) == "aborted"
    assert engine.pool.checkedout() == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_session_scope_rolls_back_when_its_task_is_cancelled(
    engine: AsyncEngine, probe: ProbeConnection
) -> None:
    """Cancelling the task mid-scope rolls the transaction back and re-raises the cancel."""
    entered = asyncio.Event()
    xids: list[str] = []

    async def unit_of_work() -> None:
        async with session_scope() as session:
            xids.append(await session.scalar(_XID))
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(unit_of_work())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert await _xact_status(probe, xids[0]) == "aborted"
    assert engine.pool.checkedout() == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_session_scope_finishes_its_rollback_despite_repeated_cancellation(
    engine: AsyncEngine, probe: ProbeConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel arriving during the rollback is held until rollback and close have finished."""
    entered = asyncio.Event()
    rollback_started = asyncio.Event()
    release_rollback = asyncio.Event()
    rolled_back = asyncio.Event()
    xids: list[str] = []

    async def unit_of_work() -> None:
        async with session_scope() as session:
            xids.append(await session.scalar(_XID))
            real_rollback = session.rollback

            async def slow_rollback() -> None:
                rollback_started.set()
                await release_rollback.wait()
                await real_rollback()
                rolled_back.set()

            monkeypatch.setattr(session, "rollback", slow_rollback)
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(unit_of_work())
    await entered.wait()
    task.cancel()
    await rollback_started.wait()
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)

    assert not task.done()
    assert not rolled_back.is_set()
    assert await _xact_status(probe, xids[0]) == "in progress"

    release_rollback.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rolled_back.is_set()
    assert await _xact_status(probe, xids[0]) == "aborted"
    assert engine.pool.checkedout() == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_session_scope_keeps_the_original_error_when_the_connection_broke(
    engine: AsyncEngine, probe: ProbeConnection, log_events: list[LogEvent]
) -> None:
    """A failed rollback on a dead connection is logged; the scope's error still propagates."""
    failure = ValueError("work failed after the connection broke")
    pids: list[int] = []

    async def unit_of_work() -> None:
        async with session_scope() as session:
            pids.append(await session.scalar(_PID))
            await _terminate(probe, pids[0])
            raise failure

    with pytest.raises(ValueError, match="connection broke") as excinfo:
        await unit_of_work()

    assert excinfo.value is failure
    assert _errors(log_events) == [
        ("exception", "db_session_rollback_failed", {"error": "OperationalError"})
    ]
    assert engine.pool.checkedout() == 0
    async with session_scope() as session:
        assert await session.scalar(_PID) != pids[0]


@pytest.mark.anyio
@pytest.mark.db
async def test_session_scope_keeps_the_original_error_when_close_fails(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, log_events: list[LogEvent]
) -> None:
    """A database error from the close after a failure is logged and never replaces it."""
    failure = ValueError("work failed")

    async def unit_of_work() -> None:
        async with session_scope() as session:
            real_close = session.close

            async def close_then_fail() -> None:
                await real_close()
                raise InterfaceError("close", None, Exception("close reported a failure"))

            monkeypatch.setattr(session, "close", close_then_fail)
            await session.scalar(text("SELECT 1"))
            raise failure

    with pytest.raises(ValueError, match="work failed") as excinfo:
        await unit_of_work()

    assert excinfo.value is failure
    assert _errors(log_events) == [
        ("exception", "db_session_close_failed", {"error": "InterfaceError"})
    ]
    assert engine.pool.checkedout() == 0


# ---------------------------------------------------------------------------
# dedicated_connection
# ---------------------------------------------------------------------------


@pytest.fixture
def statements(engine: AsyncEngine) -> Iterator[list[str]]:
    """Record every SQL statement the engine sends, in order."""
    sent: list[str] = []

    def record(*args: Any) -> None:
        sent.append(str(args[2]))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield sent
    event.remove(engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.anyio
@pytest.mark.db
async def test_dedicated_connection_is_an_autocommit_checkout(
    engine: AsyncEngine, probe: ProbeConnection
) -> None:
    """The connection stays checked out in AUTOCOMMIT, never idle in a transaction."""
    async with dedicated_connection() as conn:
        options = conn.sync_connection.get_execution_options()
        assert options["isolation_level"] == "AUTOCOMMIT"
        pid = await conn.scalar(_PID)
        await conn.execute(_LOCK, {"key": _lock_key("autocommit")})
        cur = await probe.execute("SELECT state FROM pg_stat_activity WHERE pid = %s", (pid,))
        assert await cur.fetchone() == ("idle",)
        assert engine.pool.checkedout() == 1

    assert engine.pool.checkedout() == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_dedicated_connection_releases_every_advisory_lock_on_exit(
    engine: AsyncEngine, probe: ProbeConnection, statements: list[str]
) -> None:
    """Locks held in the block, stacked ones included, are gone before the connection pools."""
    first, second = _lock_key("exit-first"), _lock_key("exit-second")
    async with dedicated_connection() as conn:
        pid = await conn.scalar(_PID)
        for key in (first, first, second):
            await conn.execute(_LOCK, {"key": key})
        assert not await _probe_can_lock(probe, first)
        assert not await _probe_can_lock(probe, second)
        statements.clear()

    assert statements == [_UNLOCK_ALL_SQL]
    assert await _backend_alive(probe, pid)
    assert await _advisory_locks_held(probe, pid) == 0
    assert await _probe_can_lock(probe, first)
    assert await _probe_can_lock(probe, second)
    async with dedicated_connection() as reused:
        assert await reused.scalar(_PID) == pid


@pytest.mark.anyio
@pytest.mark.db
async def test_dedicated_connection_releases_locks_and_reraises_when_the_block_fails(
    engine: AsyncEngine, probe: ProbeConnection
) -> None:
    """A failing block still releases its lock, and its own exception propagates unchanged."""
    key = _lock_key("block-fails")
    failure = RuntimeError("run failed")

    async def run() -> None:
        async with dedicated_connection() as conn:
            await conn.execute(_LOCK, {"key": key})
            raise failure

    with pytest.raises(RuntimeError, match="run failed") as excinfo:
        await run()

    assert excinfo.value is failure
    assert await _probe_can_lock(probe, key)
    assert engine.pool.checkedout() == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_dedicated_connection_releases_locks_when_its_task_is_cancelled(
    engine: AsyncEngine, probe: ProbeConnection
) -> None:
    """Cancelling the lock holder releases its lock before the connection returns."""
    key = _lock_key("cancelled")
    entered = asyncio.Event()

    async def hold_lock() -> None:
        async with dedicated_connection() as conn:
            await conn.execute(_LOCK, {"key": key})
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_lock())
    await entered.wait()
    assert not await _probe_can_lock(probe, key)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _probe_can_lock(probe, key)
    assert engine.pool.checkedout() == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_dedicated_connection_invalidates_a_connection_whose_release_fails(
    engine: AsyncEngine,
    probe: ProbeConnection,
    monkeypatch: pytest.MonkeyPatch,
    log_events: list[LogEvent],
) -> None:
    """A failed release is logged and the connection discarded, which frees the lock."""
    key = _lock_key("release-fails")
    invalidated: list[BaseException | None] = []

    def record_invalidation(*args: Any) -> None:
        invalidated.append(args[2])

    event.listen(engine.sync_engine, "invalidate", record_invalidation)
    monkeypatch.setattr(db_session, "_UNLOCK_ALL", text("SELECT pg_advisory_unlock_all(1)"))
    try:
        async with dedicated_connection() as conn:
            pid = await conn.scalar(_PID)
            await conn.execute(_LOCK, {"key": key})
    finally:
        event.remove(engine.sync_engine, "invalidate", record_invalidation)

    assert _errors(log_events) == [
        (
            "exception",
            "db_advisory_unlock_failed",
            {"error": "ProgrammingError", "action": "invalidate"},
        )
    ]
    assert len(invalidated) == 1
    assert isinstance(invalidated[0], SQLAlchemyError)

    async def released() -> bool:
        return await _probe_can_lock(probe, key)

    async def ended() -> bool:
        return not await _backend_alive(probe, pid)

    await _eventually(released, "the discarded connection's lock is released")
    await _eventually(ended, f"the discarded connection's backend {pid} has ended")
    assert engine.pool.checkedout() == 0
    async with dedicated_connection() as replacement:
        assert await replacement.scalar(_PID) != pid


@pytest.mark.anyio
@pytest.mark.db
async def test_dedicated_connection_reraises_the_block_error_when_the_connection_broke(
    engine: AsyncEngine, probe: ProbeConnection, log_events: list[LogEvent]
) -> None:
    """On a broken connection the failed release is logged and the block's error kept."""
    key = _lock_key("broken")
    failure = RuntimeError("run failed after the connection broke")

    async def run() -> None:
        async with dedicated_connection() as conn:
            await conn.execute(_LOCK, {"key": key})
            await _terminate(probe, await conn.scalar(_PID))
            raise failure

    with pytest.raises(RuntimeError, match="connection broke") as excinfo:
        await run()

    assert excinfo.value is failure
    assert _errors(log_events) == [
        (
            "exception",
            "db_advisory_unlock_failed",
            {"error": "OperationalError", "action": "invalidate"},
        )
    ]
    assert await _probe_can_lock(probe, key)
    assert engine.pool.checkedout() == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_dedicated_connection_sends_nothing_on_an_invalidated_connection(
    engine: AsyncEngine,
    probe: ProbeConnection,
    statements: list[str],
    log_events: list[LogEvent],
) -> None:
    """An invalidated connection already ended its session, so no unlock is sent."""
    key = _lock_key("invalidated")
    async with dedicated_connection() as conn:
        await conn.execute(_LOCK, {"key": key})
        await conn.invalidate()
        assert conn.invalidated
        statements.clear()

    assert statements == []
    assert _errors(log_events) == []

    async def released() -> bool:
        return await _probe_can_lock(probe, key)

    await _eventually(released, "the invalidated connection's lock is released")
    assert engine.pool.checkedout() == 0


# ---------------------------------------------------------------------------
# _run_to_completion
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_to_completion_runs_the_cleanup_once() -> None:
    """A cleanup that succeeds is awaited exactly once and nothing is raised."""
    calls: list[str] = []

    async def cleanup() -> None:
        calls.append("cleanup")

    await db_session._run_to_completion(cleanup())

    assert calls == ["cleanup"]


@pytest.mark.anyio
async def test_run_to_completion_propagates_what_the_cleanup_raised() -> None:
    """An error raised by the cleanup reaches the caller."""

    async def cleanup() -> None:
        raise LookupError("cleanup failed")

    with pytest.raises(LookupError, match="cleanup failed"):
        await db_session._run_to_completion(cleanup())


@pytest.mark.anyio
async def test_run_to_completion_holds_cancellation_until_the_cleanup_finishes() -> None:
    """Cancels arriving mid-cleanup are re-raised only after the cleanup has completed."""
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[bool] = []

    async def cleanup() -> None:
        started.set()
        await release.wait()
        finished.append(True)

    task = asyncio.create_task(db_session._run_to_completion(cleanup()))
    await started.wait()
    for _ in range(2):
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)

    assert not task.done()
    assert finished == []

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert finished == [True]


@pytest.mark.anyio
async def test_run_to_completion_raises_when_the_cleanup_itself_is_cancelled() -> None:
    """A cleanup that ends cancelled surfaces as CancelledError to the caller."""

    async def cleanup() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await db_session._run_to_completion(cleanup())
