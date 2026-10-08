"""Tests for the ``source_connection`` writes of ``ads_engine.sync.mode``: the rerun event.

AAP 0.3.2 ("Events that trigger a rerun") lists a source-mode change among the events that
each enqueue a default-window ``analysis_request``. Covered, through the controller:

* a stored live connection switched to simulated by :meth:`SourceModeController.resolve`
  (forced simulation, or ``auto`` without credentials) queues exactly one pending
  ``mode_changed:<source>`` request, and repeated resolutions queue no more;
* a newly created connection row, and a resolution that changes only the status or the
  reason, queue nothing;
* an ``AccessDenied`` on live data (:meth:`SourceModeController.on_error`) queues one
  ``mode_changed:<source>`` request next to the activation's own ``dataset_activated``
  request, a repeated denial adds none, and transient or defective calls add none;
* the request commits or rolls back with the mode change in the caller's transaction.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds every table of the model metadata in it, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. Each test starts from empty dataset,
connection and request tables in that schema. An unreachable database fails the tests; it
never skips them.
"""

import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.connectors.errors import AccessDenied, QueryDefect, TransientError
from ads_engine.db.base import Base
from ads_engine.db.models.analysis import AnalysisRequest
from ads_engine.db.models.ops import SourceConnection
from ads_engine.settings import Settings, Source
from ads_engine.sync.datasets import DatasetManager
from ads_engine.sync.freshness import FreshnessOutcome, record_freshness
from ads_engine.sync.mode import REASON_FORCED, REASON_NO_CREDENTIALS, SourceModeController

type Sessions = async_sessionmaker[AsyncSession]

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The fixed instant of every controller and dataset manager in this module."""

SOURCES: tuple[Source, ...] = ("google", "linkedin", "hubspot")

DENIAL_REASON = "contacts batch read: HTTP 403 MISSING_SCOPES"
"""A stored automatic-fallback reason, as a denial records it."""


def _fake_hubspot_token() -> SecretStr:
    """Return a HubSpot token that is no real credential, assembled at run time."""
    return SecretStr("-".join(("test", "hubspot", "token", uuid.uuid4().hex[:8])))


def _forced() -> Settings:
    """Return settings forcing every source to simulated data."""
    return Settings(data_mode="simulated")


def _auto_without_hubspot() -> Settings:
    """Return ``auto`` settings without HubSpot's credentials, whatever the environment."""
    return Settings(data_mode="auto", hubspot_access_token=None)


def _auto_with_hubspot() -> Settings:
    """Return ``auto`` settings carrying a fake HubSpot token, so HubSpot may run live."""
    fake = _fake_hubspot_token()
    return Settings(data_mode="auto", hubspot_access_token=fake)


def _denied(source: Source = "hubspot") -> AccessDenied:
    """Return the access denial a live HubSpot contacts read raises."""
    return AccessDenied(
        "HubSpot refused the token",
        source=source,
        endpoint="contacts batch read",
        code="MISSING_SCOPES",
        http_status=403,
    )


# ---------------------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the source-connection tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _connect_args(schema: str) -> dict[str, str]:
    """Pin every connection to ``schema`` alone, in UTC like the application's sessions."""
    return {"options": f"-c search_path={schema} -c TimeZone=UTC"}


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding every table of the metadata; drop it afterwards."""
    url = _database_url()
    schema = f"mode_connection_test_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection)
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield url, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
async def sessions(database: tuple[URL, str]) -> AsyncIterator[Sessions]:
    """Yield a session factory on empty dataset, connection and request tables.

    The engine is created and disposed inside this test's loop.
    """
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE source_dataset, source_connection, analysis_request CASCADE")
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _controller(settings: Settings, sessions: Sessions) -> SourceModeController:
    """Return a controller on the private schema with a fixed clock and no network."""
    return SourceModeController(settings, sessionmaker=sessions, clock=lambda: T0)


# ---------------------------------------------------------------------------------------
# Scenario builders and readers
# ---------------------------------------------------------------------------------------


async def _store_connection(
    sessions: Sessions,
    source: Source,
    *,
    mode: str,
    status: str = "ok",
    reason: str | None = None,
) -> None:
    """Commit a ``source_connection`` row as an earlier process left it."""
    async with sessions() as session, session.begin():
        session.add(SourceConnection(source=source, mode=mode, status=status, reason=reason))


async def _connection(sessions: Sessions, source: Source) -> SourceConnection | None:
    """Return a source's committed ``source_connection`` row, if any."""
    async with sessions() as session:
        return await session.scalar(
            select(SourceConnection).where(SourceConnection.source == source)
        )


async def _requests(sessions: Sessions) -> list[AnalysisRequest]:
    """Return every committed analysis request, oldest first."""
    async with sessions() as session:
        return list(await session.scalars(select(AnalysisRequest).order_by(AnalysisRequest.id)))


async def _reasons(sessions: Sessions) -> list[str]:
    """Return the reasons of every committed analysis request, oldest first."""
    return [request.reason for request in await _requests(sessions)]


async def _mode_changes(sessions: Sessions, source: Source) -> int:
    """Count the committed ``mode_changed:<source>`` analysis requests."""
    return (await _reasons(sessions)).count(f"mode_changed:{source}")


async def _active_live_hubspot(sessions: Sessions) -> int:
    """Build, freshen and activate a live HubSpot primary; return its id."""
    manager = DatasetManager(sessions, clock=lambda: T0)
    dataset_id = await manager.begin("hubspot", "live", reason="live access verified")
    async with sessions() as session, session.begin():
        for name in await manager.required_freshness(dataset_id):
            outcome = FreshnessOutcome.success(
                T0, covered_from=None, covered_through=None, complete=True
            )
            await record_freshness(session, dataset_id, name, outcome)
    await manager.activate(dataset_id)
    return dataset_id


# ---------------------------------------------------------------------------------------
# resolve(): a mode change queues one rerun, a no-op queues none
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("source", SOURCES)
async def test_forced_simulation_of_a_live_connection_queues_one_rerun(
    sessions: Sessions, source: Source
) -> None:
    await _store_connection(sessions, source, mode="live")

    resolved = await _controller(_forced(), sessions).resolve(source)

    assert (resolved.mode, resolved.reason) == ("simulated", REASON_FORCED)
    connection = await _connection(sessions, source)
    assert connection is not None
    assert (connection.mode, connection.status, connection.reason) == (
        "simulated",
        "ok",
        REASON_FORCED,
    )
    requests = await _requests(sessions)
    assert [request.reason for request in requests] == [f"mode_changed:{source}"]
    assert (requests[0].picked_at, requests[0].run_id) == (None, None)


@pytest.mark.anyio
@pytest.mark.db
async def test_missing_credentials_on_a_live_connection_queue_one_rerun(
    sessions: Sessions,
) -> None:
    await _store_connection(sessions, "hubspot", mode="live")

    resolved = await _controller(_auto_without_hubspot(), sessions).resolve("hubspot")

    assert (resolved.mode, resolved.reason) == ("simulated", REASON_NO_CREDENTIALS)
    assert await _reasons(sessions) == ["mode_changed:hubspot"]


@pytest.mark.anyio
@pytest.mark.db
async def test_repeated_resolutions_queue_no_further_rerun(sessions: Sessions) -> None:
    await _store_connection(sessions, "linkedin", mode="live")
    controller = _controller(_forced(), sessions)

    for _ in range(3):
        resolved = await controller.resolve("linkedin")
        assert resolved.mode == "simulated"

    assert await _reasons(sessions) == ["mode_changed:linkedin"]


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("settings_factory", "expected_mode"),
    [
        pytest.param(_forced, "simulated", id="forced-simulation"),
        pytest.param(_auto_without_hubspot, "simulated", id="auto-without-credentials"),
        pytest.param(_auto_with_hubspot, "live", id="auto-with-credentials"),
    ],
)
async def test_creating_the_connection_row_queues_nothing(
    sessions: Sessions, settings_factory: Callable[[], Settings], expected_mode: str
) -> None:
    assert await _connection(sessions, "hubspot") is None

    resolved = await _controller(settings_factory(), sessions).resolve("hubspot")

    assert resolved.mode == expected_mode
    connection = await _connection(sessions, "hubspot")
    assert connection is not None
    assert connection.mode == expected_mode
    assert await _requests(sessions) == []


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("settings_factory", "stored_status", "stored_reason", "expected_reason"),
    [
        pytest.param(
            _forced, "denied", DENIAL_REASON, REASON_FORCED, id="forced-clears-a-stale-denial"
        ),
        pytest.param(
            _auto_without_hubspot,
            "ok",
            REASON_FORCED,
            REASON_NO_CREDENTIALS,
            id="reason-only",
        ),
    ],
)
async def test_status_or_reason_only_changes_queue_nothing(
    sessions: Sessions,
    settings_factory: Callable[[], Settings],
    stored_status: str,
    stored_reason: str,
    expected_reason: str,
) -> None:
    await _store_connection(
        sessions, "hubspot", mode="simulated", status=stored_status, reason=stored_reason
    )

    await _controller(settings_factory(), sessions).resolve("hubspot")

    connection = await _connection(sessions, "hubspot")
    assert connection is not None
    assert (connection.mode, connection.status, connection.reason) == (
        "simulated",
        "ok",
        expected_reason,
    )
    assert await _requests(sessions) == []


# ---------------------------------------------------------------------------------------
# on_error(): a denial on live data is a mode change; transient and defective calls are not
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_access_denied_on_live_data_queues_one_rerun(sessions: Sessions) -> None:
    dataset_id = await _active_live_hubspot(sessions)
    # The activation keeps its own rerun event.
    assert await _reasons(sessions) == ["dataset_activated:hubspot"]
    controller = _controller(_auto_with_hubspot(), sessions)

    disposition = await controller.on_error("hubspot", _denied(), dataset_id=dataset_id)

    assert disposition == "switched_to_simulated"
    connection = await _connection(sessions, "hubspot")
    assert connection is not None
    assert (connection.mode, connection.status) == ("simulated", "denied")
    assert await _mode_changes(sessions, "hubspot") == 1
    pending = [
        request for request in await _requests(sessions) if request.reason == "mode_changed:hubspot"
    ]
    assert [(request.picked_at, request.run_id) for request in pending] == [(None, None)]

    # The live dataset stays active until the simulated one activates, so a second denial
    # on it is handled again, but the connection's mode no longer changes.
    again = await controller.on_error("hubspot", _denied(), dataset_id=dataset_id)

    assert again == "switched_to_simulated"
    assert await _mode_changes(sessions, "hubspot") == 1


@pytest.mark.anyio
@pytest.mark.db
async def test_transient_and_defective_calls_on_live_data_queue_nothing(
    sessions: Sessions,
) -> None:
    dataset_id = await _active_live_hubspot(sessions)
    controller = _controller(_auto_with_hubspot(), sessions)
    transient = TransientError(
        "HubSpot returned 503", source="hubspot", endpoint="contacts batch read", http_status=503
    )
    defect = QueryDefect(
        "HubSpot rejected the request", source="hubspot", endpoint="deals search", http_status=400
    )

    stale = await controller.on_error(
        "hubspot", transient, dataset_id=dataset_id, datasets=("hubspot.contacts",)
    )
    assert stale == "stale"
    assert await controller.on_error("hubspot", defect, dataset_id=dataset_id) == "defect"

    connection = await _connection(sessions, "hubspot")
    assert connection is not None
    assert (connection.mode, connection.status) == ("live", "error")
    assert await _mode_changes(sessions, "hubspot") == 0


# ---------------------------------------------------------------------------------------
# The request shares the mode change's transaction
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_rerun_rolls_back_with_the_mode_change(sessions: Sessions) -> None:
    await _store_connection(sessions, "google", mode="live")
    controller = _controller(_forced(), sessions)

    async with sessions() as session:
        await session.begin()
        await controller._record_connection(
            "google", mode="simulated", reason=REASON_FORCED, session=session
        )
        await session.rollback()

    connection = await _connection(sessions, "google")
    assert connection is not None
    assert connection.mode == "live"
    assert await _requests(sessions) == []

    async with sessions() as session, session.begin():
        await controller._record_connection(
            "google", mode="simulated", reason=REASON_FORCED, session=session
        )

    connection = await _connection(sessions, "google")
    assert connection is not None
    assert connection.mode == "simulated"
    assert await _reasons(sessions) == ["mode_changed:google"]
