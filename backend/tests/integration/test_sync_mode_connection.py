"""Tests for the ``source_connection`` writes of ``ads_engine.sync.mode``: rerun and logs.

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
* the request commits or rolls back with the mode change in the caller's transaction;
* a job's connector error is logged once, as ``on_error``'s ``sync.classification`` naming
  the job, never again by the job; a classification outside a job names none.

AAP 0.11.1 ("Logging points": every classification and mode change with its reason) and
AAP 0.3.3 ("Granularity": the reason names the failing endpoint). Covered, from the
structlog events (``structlog.testing.capture_logs``):

* ``sync.mode.change`` and ``sync.connection`` carry an ``endpoint`` field: the failing
  call's endpoint for a denial, a transient or a defective call, a failed probe and a
  failed live build; ``None`` for forced simulation, missing credentials, activations and
  a return to ``ok`` (a cleared denial, or a ``stale`` or ``error`` connection whose data
  has caught up);
* levels: a first start into simulated data, a return to ``ok`` and a recovery to live are
  ``info``; a switch from live to simulated, a denial, and a ``stale`` or ``error``
  status are ``warning``;
* ``sync.probe`` is a ``warning`` when live calls were sent and a check failed (HubSpot
  answering 403), ``info`` when no request was sent (``DATA_MODE=simulated``, no
  credentials); ``ProbeReport.endpoint`` names the first failing check's endpoint, and
  the automatic fallback the failed probe causes logs it on ``sync.fallback``.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds every table of the model metadata in it, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. Each test starts from empty dataset,
connection and request tables in that schema. An unreachable database fails the tests; it
never skips them.
"""

import os
import sys
import types
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import structlog
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema
from structlog.testing import capture_logs

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.connectors.errors import AccessDenied, ConnectorError, QueryDefect, TransientError
from ads_engine.connectors.http import RetryPolicy
from ads_engine.db.base import Base
from ads_engine.db.models.analysis import AnalysisRequest
from ads_engine.db.models.ops import SourceConnection
from ads_engine.settings import Settings, Source
from ads_engine.sync import jobs
from ads_engine.sync.datasets import DatasetManager, Mode
from ads_engine.sync.freshness import FreshnessOutcome, record_freshness
from ads_engine.sync.mode import (
    REASON_FORCED,
    REASON_NO_CREDENTIALS,
    REASON_RECOVERED,
    REASON_SEEDED,
    ProbeCheck,
    ProbeReport,
    SourceModeController,
)

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


def _auto_with_google() -> Settings:
    """Return ``auto`` settings carrying fake n8n webhook credentials, so Google may run live."""
    fake = SecretStr("-".join(("test", "webhook", "secret", uuid.uuid4().hex[:8])))
    return Settings(
        data_mode="auto",
        n8n_google_ads_webhook_url="https://n8n.example.test/webhook/google-ads",
        n8n_webhook_secret=fake,
        google_ads_customer_id="1234567890",
    )


PROBE_ENDPOINT = "objects_batch_read contacts"
"""The endpoint of the HubSpot probe's first call, the one :func:`_forbidden` refuses."""


def _forbidden(request: httpx.Request) -> httpx.Response:
    """Answer every HubSpot call as a portal refusing the app's scopes (HTTP 403)."""
    return httpx.Response(
        403,
        json={"status": "error", "message": "This app lacks scopes", "category": "MISSING_SCOPES"},
        request=request,
    )


async def _no_sleep(_seconds: float) -> None:
    """Skip a retry backoff wait."""


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


def _refused_controller(sessions: Sessions) -> SourceModeController:
    """Return a live-capable HubSpot controller whose portal answers every call with 403."""
    return SourceModeController(
        _auto_with_hubspot(),
        sessionmaker=sessions,
        clock=lambda: T0,
        transports={"hubspot": httpx.MockTransport(_forbidden)},
        retry=RetryPolicy(sleep=_no_sleep),
    )


# ---------------------------------------------------------------------------------------
# Logging isolation and the backfill stand-in
# ---------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _uncached_loggers() -> Iterator[None]:
    """Keep every logger these tests use lazy, then restore structlog's configuration.

    ``configure_logging`` caches each logger's processor chain on its first use, and a
    later reconfiguration installs a new chain the cached logger never sees. The
    source-mode logger first used here would then write past
    ``structlog.testing.capture_logs`` in this module and in the modules that run after
    it. Nothing is cached while these tests run, and the configuration in force before
    each test is put back after it.
    """
    was_configured = structlog.is_configured()
    saved = structlog.get_config()
    structlog.configure(cache_logger_on_first_use=False)
    try:
        yield
    finally:
        if was_configured:
            structlog.configure(**saved)
        else:
            structlog.reset_defaults()


class _Backfill:
    """Stands in for ``ads_engine.sync.jobs.backfill_dataset(controller, dataset_id, *, now)``.

    Records every freshness row the dataset requires as a complete success, as a backfill
    that read everything does. With :attr:`live_failure` set, a live dataset's backfill
    raises it instead, as a backfill job lets out a classified error.

    Attributes:
        calls: The dataset of each call, in order.
        live_failure: The error a live dataset's backfill raises; ``None`` to succeed.
    """

    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions
        self.calls: list[int] = []
        self.live_failure: ConnectorError | None = None

    async def __call__(
        self, controller: SourceModeController, dataset_id: int, *, now: datetime | None = None
    ) -> None:
        """Backfill one building dataset (see the class docstring)."""
        self.calls.append(dataset_id)
        dataset = await controller.datasets.get(dataset_id)
        if self.live_failure is not None and dataset.mode == "live":
            raise self.live_failure
        await _freshen(self.sessions, controller.datasets, dataset_id)


@pytest.fixture
def backfill(monkeypatch: pytest.MonkeyPatch, sessions: Sessions) -> _Backfill:
    """Install :class:`_Backfill` as ``ads_engine.sync.jobs`` for this test."""
    stub = _Backfill(sessions)
    jobs = types.ModuleType("ads_engine.sync.jobs")
    jobs.backfill_dataset = stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ads_engine.sync.jobs", jobs)
    return stub


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


async def _freshen(sessions: Sessions, manager: DatasetManager, dataset_id: int) -> None:
    """Record every freshness row ``dataset_id`` requires as a complete success at T0."""
    async with sessions() as session, session.begin():
        for name in await manager.required_freshness(dataset_id):
            outcome = FreshnessOutcome.success(
                T0, covered_from=None, covered_through=None, complete=True
            )
            await record_freshness(session, dataset_id, name, outcome)


async def _active_dataset(sessions: Sessions, source: Source, mode: Mode, reason: str) -> int:
    """Build, freshen and activate a primary dataset of ``source`` in ``mode``; return its id."""
    manager = DatasetManager(sessions, clock=lambda: T0)
    dataset_id = await manager.begin(source, mode, reason=reason)
    await _freshen(sessions, manager, dataset_id)
    await manager.activate(dataset_id)
    return dataset_id


def _events(entries: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    """Return the captured log entries of one event, in order."""
    return [entry for entry in entries if entry.get("event") == event]


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


# ---------------------------------------------------------------------------------------
# A job's connector error is classified once, naming the job
# ---------------------------------------------------------------------------------------


def _lead_match_job(controller: SourceModeController, dataset_id: int) -> jobs.LeadMatchJob:
    """Return a lead-matching job on ``dataset_id``; ``_hand_over`` reads only these fields."""
    ctx = SimpleNamespace(controller=controller, source="hubspot", dataset_id=dataset_id)
    return jobs.LeadMatchJob(cast(jobs.JobContext, ctx))


@pytest.mark.anyio
@pytest.mark.db
async def test_a_job_error_is_one_classification_record_naming_the_job(
    sessions: Sessions,
) -> None:
    dataset_id = await _active_live_hubspot(sessions)
    controller = _controller(_auto_with_hubspot(), sessions)
    job = _lead_match_job(controller, dataset_id)
    transient = TransientError(
        "HubSpot returned 503", source="hubspot", endpoint="contacts batch read", http_status=503
    )
    elsewhere = TransientError(
        "LinkedIn returned 503", source="linkedin", endpoint="lead re-fetch", http_status=503
    )

    with capture_logs() as entries:
        own = await job._hand_over(transient, ("hubspot.contacts",))
        other = await job._hand_over(elsewhere, ("hubspot.contacts",))

    assert (own, other) == ("stale", "stale")
    # on_error's classification is the error's one record; the job adds none of its own.
    assert "sync.job.connector_error" not in {entry["event"] for entry in entries}
    first, second = (entry for entry in entries if entry["event"] == "sync.classification")
    assert first["log_level"] == "warning"
    assert (first["job"], first["source"], first["error_source"]) == (
        jobs.JOB_LEAD_MATCH,
        "hubspot",
        "hubspot",
    )
    assert (first["dataset_id"], first["datasets"], first["disposition"]) == (
        dataset_id,
        ["hubspot.contacts"],
        "stale",
    )
    assert first["reason"] == transient.reason
    # Another source's error is that source's, without this job's dataset, but still names it.
    assert (second["job"], second["source"], second["dataset_id"], second["datasets"]) == (
        jobs.JOB_LEAD_MATCH,
        "linkedin",
        None,
        [],
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_a_classification_outside_a_job_names_no_job(sessions: Sessions) -> None:
    dataset_id = await _active_live_hubspot(sessions)
    controller = _controller(_auto_with_hubspot(), sessions)
    defect = QueryDefect(
        "HubSpot rejected the request", source="hubspot", endpoint="deals search", http_status=400
    )

    with capture_logs() as entries:
        assert await controller.on_error("hubspot", defect, dataset_id=dataset_id) == "defect"

    (classification,) = (entry for entry in entries if entry["event"] == "sync.classification")
    assert (classification["log_level"], classification["job"]) == ("error", None)
    assert classification["query_id"] == defect.query_id


# ---------------------------------------------------------------------------------------
# Transition logs: the level and the failing endpoint
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("settings_factory", "reason", "missing"),
    [
        pytest.param(_forced, REASON_FORCED, [], id="forced-simulation"),
        pytest.param(
            _auto_without_hubspot,
            REASON_NO_CREDENTIALS,
            ["HUBSPOT_ACCESS_TOKEN"],
            id="missing-credentials",
        ),
    ],
)
async def test_a_first_start_into_simulated_data_logs_info_with_no_endpoint(
    sessions: Sessions,
    settings_factory: Callable[[], Settings],
    reason: str,
    missing: list[str],
) -> None:
    with capture_logs() as entries:
        await _controller(settings_factory(), sessions).resolve("hubspot")

    [change] = _events(entries, "sync.mode.change")
    assert (change["log_level"], change["from_mode"], change["to_mode"], change["reason"]) == (
        "info",
        None,
        "simulated",
        reason,
    )
    assert change["endpoint"] is None
    assert change["missing_credentials"] == missing


@pytest.mark.anyio
@pytest.mark.db
async def test_switching_a_live_connection_to_simulated_logs_a_warning(
    sessions: Sessions,
) -> None:
    await _store_connection(sessions, "linkedin", mode="live")

    with capture_logs() as entries:
        await _controller(_forced(), sessions).resolve("linkedin")

    [change] = _events(entries, "sync.mode.change")
    assert (change["log_level"], change["from_mode"], change["to_mode"]) == (
        "warning",
        "live",
        "simulated",
    )
    # Forced simulation is a configuration transition: no call failed.
    assert change["endpoint"] is None


@pytest.mark.anyio
@pytest.mark.db
async def test_access_denied_on_live_data_logs_a_warning_naming_the_endpoint(
    sessions: Sessions,
) -> None:
    dataset_id = await _active_live_hubspot(sessions)
    controller = _controller(_auto_with_hubspot(), sessions)

    with capture_logs() as entries:
        await controller.on_error("hubspot", _denied(), dataset_id=dataset_id)

    [change] = _events(entries, "sync.mode.change")
    assert (
        change["log_level"],
        change["from_mode"],
        change["to_mode"],
        change["status"],
        change["endpoint"],
    ) == ("warning", "live", "simulated", "denied", "contacts batch read")
    assert change["reason"] == _denied().reason
    assert _events(entries, "sync.connection") == []


@pytest.mark.anyio
@pytest.mark.db
async def test_transient_and_defective_calls_log_warnings_naming_the_endpoint(
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

    with capture_logs() as entries:
        await controller.on_error(
            "hubspot", transient, dataset_id=dataset_id, datasets=("hubspot.contacts",)
        )
        await controller.on_error("hubspot", defect, dataset_id=dataset_id)

    assert [
        (entry["log_level"], entry["from_status"], entry["status"], entry["endpoint"])
        for entry in _events(entries, "sync.connection")
    ] == [
        ("warning", "ok", "stale", "contacts batch read"),
        ("warning", "stale", "error", "deals search"),
    ]
    assert _events(entries, "sync.mode.change") == []


@pytest.mark.anyio
@pytest.mark.db
async def test_a_return_to_ok_logs_info_with_no_endpoint(sessions: Sessions) -> None:
    await _store_connection(
        sessions, "hubspot", mode="simulated", status="denied", reason=DENIAL_REASON
    )

    with capture_logs() as entries:
        await _controller(_forced(), sessions).resolve("hubspot")

    [connection] = _events(entries, "sync.connection")
    assert (
        connection["log_level"],
        connection["from_status"],
        connection["status"],
        connection["reason"],
        connection["endpoint"],
    ) == ("info", "denied", "ok", REASON_FORCED, None)
    assert _events(entries, "sync.mode.change") == []


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("stored_status", ["stale", "error"])
async def test_a_caught_up_connection_returning_to_ok_logs_info_with_no_endpoint(
    sessions: Sessions, stored_status: str
) -> None:
    await _active_dataset(sessions, "hubspot", "simulated", REASON_FORCED)
    # Activation points the connection at ``ok``; mark it as a failed call left it.
    async with sessions() as session, session.begin():
        await session.execute(
            update(SourceConnection)
            .where(SourceConnection.source == "hubspot")
            .values(status=stored_status)
        )

    with capture_logs() as entries:
        outcome = await _controller(_forced(), sessions).ensure_dataset("hubspot")

    assert outcome is None
    [connection] = _events(entries, "sync.connection")
    assert (connection["log_level"], connection["from_status"], connection["status"]) == (
        "info",
        stored_status,
        "ok",
    )
    # A return to ``ok`` follows no failing call: the field is present and null.
    assert "endpoint" in connection
    assert connection["endpoint"] is None
    assert _events(entries, "sync.mode.change") == []
    row = await _connection(sessions, "hubspot")
    assert row is not None
    assert row.status == "ok"


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("stored_mode", "stored_status", "built_mode", "reason", "level"),
    [
        pytest.param("simulated", "denied", "live", REASON_RECOVERED, "info", id="recovery"),
        pytest.param("live", "ok", "simulated", REASON_SEEDED, "warning", id="seeded-over-live"),
    ],
)
async def test_an_activation_that_changes_the_mode_logs_its_level_and_no_endpoint(
    sessions: Sessions,
    backfill: _Backfill,
    stored_mode: str,
    stored_status: str,
    built_mode: str,
    reason: str,
    level: str,
) -> None:
    await _store_connection(
        sessions,
        "google",
        mode=stored_mode,
        status=stored_status,
        reason=DENIAL_REASON if stored_status == "denied" else None,
    )

    with capture_logs() as entries:
        outcome = await _controller(_auto_with_google(), sessions).build_dataset(
            "google", built_mode, reason=reason
        )

    assert (outcome.activated, outcome.mode) == (True, built_mode)
    assert backfill.calls == [outcome.dataset_id]
    [change] = _events(entries, "sync.mode.change")
    assert (
        change["log_level"],
        change["from_mode"],
        change["to_mode"],
        change["reason"],
        change["dataset_id"],
    ) == (level, stored_mode, built_mode, reason, outcome.dataset_id)
    assert change["endpoint"] is None


@pytest.mark.anyio
@pytest.mark.db
async def test_a_failed_live_build_logs_its_failing_endpoint_on_the_fallback(
    sessions: Sessions, backfill: _Backfill
) -> None:
    await _store_connection(sessions, "google", mode="live")
    backfill.live_failure = TransientError(
        "n8n returned 503", source="google", endpoint="search Q-G2", http_status=503
    )

    with capture_logs() as entries:
        outcome = await _controller(_auto_with_google(), sessions).build_dataset(
            "google", "live", reason=REASON_RECOVERED
        )

    # The live build was discarded and the simulated build that followed activated.
    assert (outcome.activated, outcome.mode) == (True, "simulated")
    assert [
        (entry["log_level"], entry["from_status"], entry["status"], entry["endpoint"])
        for entry in _events(entries, "sync.connection")
    ] == [("warning", "ok", "stale", "search Q-G2")]
    [change] = _events(entries, "sync.mode.change")
    assert (
        change["log_level"],
        change["from_mode"],
        change["to_mode"],
        change["status"],
        change["endpoint"],
    ) == ("warning", "live", "simulated", "denied", "search Q-G2")
    [fallback] = _events(entries, "sync.fallback")
    assert (fallback["log_level"], fallback["endpoint"]) == ("warning", "search Q-G2")
    assert fallback["reason"].startswith("live build failed: ")


# ---------------------------------------------------------------------------------------
# Probe logs: a failed live probe is a warning, a configuration refusal is not
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_a_failed_live_probe_reports_and_logs_its_failing_endpoint(
    sessions: Sessions,
) -> None:
    with capture_logs() as entries:
        report = await _refused_controller(sessions).probe("hubspot")

    assert report.passed is False
    assert [(check.name, check.ok, check.endpoint) for check in report.checks] == [
        ("crm", False, PROBE_ENDPOINT)
    ]
    assert report.endpoint == PROBE_ENDPOINT
    [probe] = _events(entries, "sync.probe")
    assert (
        probe["log_level"],
        probe["passed"],
        probe["failed"],
        probe["reason"],
        probe["endpoint"],
    ) == ("warning", False, ["crm"], report.reason, PROBE_ENDPOINT)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_failed_live_probe_puts_the_source_in_fallback_naming_the_endpoint(
    sessions: Sessions,
) -> None:
    await _active_dataset(sessions, "hubspot", "simulated", REASON_NO_CREDENTIALS)
    controller = _refused_controller(sessions)

    with capture_logs() as entries:
        assert await controller.ensure_dataset("hubspot") is None

    assert await controller.in_fallback("hubspot")
    [probe] = _events(entries, "sync.probe")
    assert (probe["log_level"], probe["endpoint"]) == ("warning", PROBE_ENDPOINT)
    [connection] = _events(entries, "sync.connection")
    assert (
        connection["log_level"],
        connection["mode"],
        connection["from_status"],
        connection["status"],
        connection["endpoint"],
    ) == ("warning", "simulated", "ok", "denied", PROBE_ENDPOINT)
    [fallback] = _events(entries, "sync.fallback")
    assert (fallback["log_level"], fallback["endpoint"]) == ("warning", PROBE_ENDPOINT)


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("settings_factory", "check"),
    [
        pytest.param(
            _forced, ProbeCheck("data_mode", False, REASON_FORCED), id="forced-simulation"
        ),
        pytest.param(
            _auto_without_hubspot,
            ProbeCheck("credentials", False, REASON_NO_CREDENTIALS),
            id="missing-credentials",
        ),
    ],
)
async def test_a_probe_that_sends_no_request_logs_info(
    sessions: Sessions, settings_factory: Callable[[], Settings], check: ProbeCheck
) -> None:
    with capture_logs() as entries:
        report = await _controller(settings_factory(), sessions).probe("hubspot")

    assert report.checks == (check,)
    assert report.endpoint is None
    [probe] = _events(entries, "sync.probe")
    assert (probe["log_level"], probe["passed"], probe["endpoint"]) == ("info", False, None)


def test_the_probe_report_endpoint_is_the_first_failing_checks() -> None:
    passing = ProbeReport.of("linkedin", (ProbeCheck("adAccounts/1", True),))
    partial = ProbeReport.of(
        "linkedin",
        (
            ProbeCheck("adAccounts/1", True),
            ProbeCheck("leadForms 1", False, "linkedin leadForms: HTTP 403", "leadForms"),
            ProbeCheck("leadFormResponses 1", False, "linkedin: HTTP 403", "leadFormResponses"),
        ),
    )
    unconfigured = ProbeReport.of(
        "linkedin", (ProbeCheck("adAccounts", False, "no LinkedIn ad account configured"),)
    )

    assert passing.endpoint is None
    assert partial.endpoint == "leadForms"
    assert unconfigured.endpoint is None
