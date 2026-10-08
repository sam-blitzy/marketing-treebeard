"""HTTP tests for ``POST /decisions`` recorded from an item (``recommendation_id``).

A decision recorded from an item is stored with ``origin='recommendation'`` and the item's
id, so the route holds the request to two guarantees before the domain writes anything:

* **The item offers this decision** (AAP 0.5.2, 0.5.5). An item stores the campaign or
  shared budget its "Record decision" writes as ``decision_subject``, and ``NULL`` on every
  item without that action. A request naming another campaign, the other subject type with
  the same numeric id, or a campaign of an item that offers a shared-budget decision is a
  422 ``subject_mismatch``; an item storing no decision subject, or one that does not
  decode, is a 422 ``decision_not_offered``. Both come before any lock or write.
* **The item rests on current data until the decision commits** (AAP 0.3.2). An item whose
  provenance names a dataset of another source (a live LinkedIn campaign's CRM evidence
  read from HubSpot's primary dataset) or a companion (a simulated LinkedIn campaign's CRM
  evidence) is refused with 409 ``subject_retired`` when that dataset is already retired,
  and also when the activation retiring it is still uncommitted as the request arrives: the
  request waits for the activation's locks and is then refused. A request already holding
  its locks makes the activation wait until the decision commits. Unverifiable provenance
  and a subject outside the active datasets are refused the same way.

Every refused request writes no decision and queues no ``analysis_request``. The checks run
in the order 404, 422, 409. Blocking is observed, never assumed: a waiting transaction is
seen in ``pg_stat_activity`` as blocked by the holder's backend (``pg_blocking_pids``)
before the holder commits.

The application is assembled as ``api/app.py`` assembles it: the auth and decisions routers
under ``/api/v1`` behind the CSRF guard, Starlette's ``SessionMiddleware`` with the
product's cookie name, one login limiter on ``app.state``, and handlers that render
``ApiError``, domain ``DecisionError`` and request validation failures in the AAP 0.5.3
error shape. A catch-all handler answers any other exception with 500, so a regression shows
as a 500 response rather than a test crash. Datasets are begun, freshened and activated
through the real :class:`~ads_engine.sync.datasets.DatasetManager`.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema holding every table of the model metadata, points every connection's ``search_path``
at it alone, and drops it with ``CASCADE`` at teardown, so the ``public`` schema and every
other schema stay untouched. Each test starts from empty dataset, run and user tables. An
unreachable database fails the tests; it never skips them.
"""

import asyncio
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, func, null, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.middleware.sessions import SessionMiddleware

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.api import deps, security
from ads_engine.api.deps import ApiError
from ads_engine.api.routes import auth, decisions
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Budget, Campaign
from ads_engine.db.models.analysis import AnalysisRequest, AnalysisRun, Recommendation
from ads_engine.db.models.decisions import Decision
from ads_engine.db.models.ops import AppUser, SourceDataset
from ads_engine.decisions import DecisionError
from ads_engine.sync.datasets import Activation, DatasetManager
from ads_engine.sync.freshness import FreshnessOutcome, record_freshness

type Sessions = async_sessionmaker[AsyncSession]

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
"""The request clock: sign-in and decision instant."""

T0 = NOW - timedelta(days=1)
"""When the datasets were built and the baselines recorded."""

SIGNING_KEY = "test-signing-key"

ADMIN_PASSWORD = secrets.token_urlsafe(24)
"""The seeded admin's password, generated per run so no credential is written down."""

CSRF = {security.CSRF_HEADER: security.CSRF_VALUE}
"""The header every mutating request carries (AAP 0.5.3)."""

HISTORICAL = "Computed from data that has since been replaced"
"""The overlay's historical-only reason, which the 409 repeats (AAP 0.3.2)."""

NOTE = "Paused per the pipeline review with the regional lead"
"""A note the requests carry; no error message may repeat it."""

WAIT_LIMIT = 30.0
"""Seconds a released transaction may take to finish before the test fails."""

SCENARIOS = ("crm_primary", "companion")
"""How the LinkedIn campaign's CRM evidence was read.

``crm_primary``: a live LinkedIn dataset reads HubSpot's active primary, a dataset of
another source that a HubSpot activation retires. ``companion``: a simulated LinkedIn
dataset reads its companion CRM dataset, which retires with its parent.
"""


class Clock:
    """A settable, timezone-aware clock for :class:`DatasetManager`."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


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
            "for the decisions-from-items route tests"
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
    schema = f"decisions_from_items_test_{uuid.uuid4().hex}"
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
    """Yield a session factory on empty dataset, run and user tables of the private schema.

    The engine is created and disposed inside this test's loop. Truncating
    ``source_dataset`` and ``analysis_run`` cascades to every dataset-scoped table, the
    stored items and the decisions; ``source_connection``, ``analysis_request`` and
    ``app_user`` are emptied with them.
    """
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE source_dataset, source_connection, analysis_request, "
                    "analysis_run, app_user CASCADE"
                )
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def manager(sessions: Sessions) -> DatasetManager:
    """Return a dataset manager on the private schema and a clock standing at ``T0``."""
    return DatasetManager(sessions, clock=Clock(T0))


# ---------------------------------------------------------------------------------------
# Lock observation
# ---------------------------------------------------------------------------------------


async def _backend_pid(session: AsyncSession) -> int:
    """Return the PostgreSQL backend pid of ``session``'s connection (begins its transaction)."""
    pid = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(pid, int)
    return pid


async def _wait_until_blocked(sessions: Sessions, blocker_pid: int) -> None:
    """Wait until a backend waits on a lock held by ``blocker_pid``, or fail the test."""
    async with sessions() as session:
        for _ in range(800):
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"
                ),
                {"pid": blocker_pid},
            )
            await session.rollback()
            if waiting is not None and waiting >= 1:
                return
            await asyncio.sleep(0.025)
    pytest.fail(f"no transaction ever waited on the locks of backend {blocker_pid}")


# ---------------------------------------------------------------------------------------
# Scenario: active datasets built by the real manager, two campaigns, a shared budget
# ---------------------------------------------------------------------------------------


async def _make_fresh(manager: DatasetManager, sessions: Sessions, dataset_id: int) -> None:
    """Record every freshness row the dataset requires as a complete success at ``T0``."""
    outcome = FreshnessOutcome.success(T0, covered_from=None, covered_through=None, complete=True)
    async with sessions() as session, session.begin():
        for name in await manager.required_freshness(dataset_id, session=session):
            await record_freshness(session, dataset_id, name, outcome)


async def _ready_build(
    manager: DatasetManager, sessions: Sessions, source: str, mode: str = "simulated"
) -> tuple[int, int | None]:
    """Begin and freshen a primary (and a simulated LinkedIn one's companion), unactivated."""
    dataset_id = await manager.begin(source, mode, reason="credentials not configured")
    companion_id: int | None = None
    if source == "linkedin" and mode == "simulated":
        companion_id = await manager.begin("hubspot", "simulated", "companion", dataset_id)
        await _make_fresh(manager, sessions, companion_id)
    await _make_fresh(manager, sessions, dataset_id)
    return dataset_id, companion_id


async def _activated(
    manager: DatasetManager, sessions: Sessions, source: str, mode: str = "simulated"
) -> tuple[int, int | None]:
    """Begin, freshen and activate a primary; return it and its companion (or ``None``)."""
    dataset_id, companion_id = await _ready_build(manager, sessions, source, mode)
    await manager.activate(dataset_id)
    return dataset_id, companion_id


@dataclass(frozen=True)
class World:
    """The seeded rows a test addresses.

    Attributes:
        scenario: One of :data:`SCENARIOS`.
        linkedin: The active LinkedIn primary dataset holding both campaigns.
        crm: The CRM dataset the LinkedIn campaigns' evidence read: HubSpot's active
            primary (``crm_primary``) or the LinkedIn dataset's companion (``companion``).
        google: The active Google primary dataset holding the shared budget.
        campaign_a: An ``own_budget`` LinkedIn campaign, decided ``active`` at baseline.
        campaign_b: A second such campaign.
        shared_budget: A Google explicitly shared budget.
        run_id: The default run the items belong to.
        email: The admin's email, unique to the test.
    """

    scenario: str
    linkedin: int
    crm: int
    google: int
    campaign_a: int
    campaign_b: int
    shared_budget: int
    run_id: int
    email: str


async def _linkedin_campaign(
    session: AsyncSession, dataset_id: int, account_id: int, external_id: str
) -> int:
    """Insert an ``own_budget`` LinkedIn campaign with its ``active`` / 25.00 baseline."""
    campaign = Campaign(
        source="linkedin",
        dataset_id=dataset_id,
        account_id=account_id,
        external_id=f"urn:li:sponsoredCampaign:{external_id}",
        name=f"White paper {external_id}",
        status="active",
        raw_status="ACTIVE",
        objective_or_channel="LEAD_GENERATION",
        daily_budget=Decimal("25.00"),
    )
    session.add(campaign)
    await session.flush()
    session.add(
        Decision(
            source="linkedin",
            dataset_id=dataset_id,
            subject_type="campaign",
            subject_id=campaign.id,
            shape="own_budget",
            status="active",
            daily_budget=Decimal("25.00"),
            total_budget=None,
            budget_id=None,
            decided_at=T0,
            decided_by=None,
            origin="baseline",
            patch={},
            note=None,
        )
    )
    await session.flush()
    return campaign.id


async def _world(sessions: Sessions, manager: DatasetManager, scenario: str) -> World:
    """Activate the scenario's datasets and commit the campaigns, budget, run and admin."""
    if scenario == "crm_primary":
        crm, _ = await _activated(manager, sessions, "hubspot", "live")
        linkedin, _ = await _activated(manager, sessions, "linkedin", "live")
    else:
        linkedin, companion = await _activated(manager, sessions, "linkedin")
        assert companion is not None
        crm = companion
    google, _ = await _activated(manager, sessions, "google")
    async with sessions() as session, session.begin():
        account = AdAccount(
            source="linkedin",
            dataset_id=linkedin,
            external_id="508",
            name="Acme LinkedIn",
            currency="USD",
            timezone="UTC",
        )
        session.add(account)
        await session.flush()
        campaign_a = await _linkedin_campaign(session, linkedin, account.id, "61")
        campaign_b = await _linkedin_campaign(session, linkedin, account.id, "62")
        budget = Budget(
            source="google",
            dataset_id=google,
            external_id="9001",
            amount=Decimal("300.00"),
            amount_micros=300_000_000,
            period="DAILY",
            explicitly_shared=True,
        )
        run = AnalysisRun(
            kind="default",
            window_start=(NOW - timedelta(days=28)).date(),
            window_end=(NOW - timedelta(days=1)).date(),
            source_day_boundaries=[],
            trigger="scheduled",
            data_modes={},
            dataset_ids={},
            config_hash="decisions-from-items-test",
            status="succeeded",
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=2) + timedelta(minutes=1),
        )
        email = f"admin-{uuid.uuid4().hex}@example.com"
        session.add_all(
            [
                budget,
                run,
                AppUser(email=email, password_hash=security.hash_password(ADMIN_PASSWORD)),
            ]
        )
        await session.flush()
        return World(
            scenario=scenario,
            linkedin=linkedin,
            crm=crm,
            google=google,
            campaign_a=campaign_a,
            campaign_b=campaign_b,
            shared_budget=budget.id,
            run_id=run.id,
            email=email,
        )


async def _provenance(sessions: Sessions, *dataset_ids: int) -> list[dict[str, Any]]:
    """Return the frozen ``evidence.provenance`` of an item that read ``dataset_ids``."""
    async with sessions() as session:
        entries: list[dict[str, Any]] = []
        for dataset_id in dataset_ids:
            dataset = await session.get(SourceDataset, dataset_id)
            assert dataset is not None
            entries.append(
                {
                    "source": dataset.source,
                    "dataset_id": dataset.id,
                    "mode": dataset.mode,
                    "role": dataset.role,
                }
            )
        return entries


async def _item(
    sessions: Sessions,
    world: World,
    *,
    decision_subject: object,
    provenance: Sequence[Mapping[str, Any]] | None = None,
    evidence: object = None,
    rule_id: str = "C1b",
    section: str = "campaigns",
) -> int:
    """Commit one stored item of the world's run and return its id.

    ``provenance`` defaults to the campaign item's ``[LinkedIn dataset, CRM dataset]``;
    ``evidence``, when given, replaces the whole evidence block.
    """
    if evidence is None:
        if provenance is None:
            provenance = await _provenance(sessions, world.linkedin, world.crm)
        evidence = {
            "window": {"start": "2026-09-08", "end": "2026-10-05", "days": 28, "source_days": []},
            "metrics": [],
            "baselines": [],
            "thresholds": {},
            "datasets": [],
            "provenance": [dict(entry) for entry in provenance],
        }
    async with sessions() as session, session.begin():
        item = Recommendation(
            run_id=world.run_id,
            section=section,
            group="pause",
            rule_id=rule_id,
            entity_type="campaign",
            entity_id=str(world.campaign_a),
            rank=1,
            action="Pause campaign",
            reason="Spent with no pipeline output",
            evidence=evidence,
            flags=[],
            data_status="live",
            held=False,
            held_reason=None,
            required_datasets=[],
            decision_subject=decision_subject,
        )
        session.add(item)
        await session.flush()
        return item.id


def _campaign(campaign_id: int) -> dict[str, Any]:
    """Return a campaign decision subject, as stored and as a request names it."""
    return {"type": "campaign", "id": campaign_id}


def _budget(budget_id: int) -> dict[str, Any]:
    """Return a shared-budget decision subject, as stored and as a request names it."""
    return {"type": "budget", "id": budget_id}


@dataclass(frozen=True)
class Written:
    """What decision writes left behind: never baselines or activation reruns."""

    decisions: int
    reruns: int


async def _written(sessions: Sessions) -> Written:
    """Count the non-baseline decisions and the ``decision`` analysis requests."""
    async with sessions() as session:
        decided = await session.scalar(
            select(func.count()).select_from(Decision).where(Decision.origin != "baseline")
        )
        reruns = await session.scalar(
            select(func.count())
            .select_from(AnalysisRequest)
            .where(AnalysisRequest.reason == "decision")
        )
        return Written(decisions=int(decided or 0), reruns=int(reruns or 0))


async def _decisions_of(sessions: Sessions, campaign_id: int) -> list[Decision]:
    """Return the campaign's decisions, latest last."""
    async with sessions() as session:
        rows = await session.scalars(
            select(Decision)
            .where(Decision.subject_type == "campaign", Decision.subject_id == campaign_id)
            .order_by(Decision.decided_at, Decision.id)
        )
        return list(rows)


async def _replacement(manager: DatasetManager, sessions: Sessions, world: World) -> int:
    """Build, unactivated, the dataset whose activation retires the world's CRM dataset."""
    if world.scenario == "crm_primary":
        replacement, _ = await _ready_build(manager, sessions, "hubspot")
    else:
        replacement, _ = await _ready_build(manager, sessions, "linkedin")
    return replacement


def _retired_by_replacement(world: World) -> tuple[int, ...]:
    """Return the datasets the replacement's activation retires."""
    if world.scenario == "crm_primary":
        return (world.crm,)
    return (world.linkedin, world.crm)


# ---------------------------------------------------------------------------------------
# The application under test
# ---------------------------------------------------------------------------------------


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """Return ``{"error": {"code", "message"}}`` with ``status_code`` (AAP 0.5.3)."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


async def _render_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Render a request validation failure as 422 ``validation_error``."""
    errors = cast(RequestValidationError, exc).errors()
    message = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in errors
    )
    return _error(422, "validation_error", message or "Invalid request")


async def _render_api_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an :class:`ApiError` with its status, code, message and headers."""
    error = cast(ApiError, exc)
    response = _error(error.status_code, error.code, error.message)
    if error.headers:
        response.headers.update(error.headers)
    return response


async def _render_decision_error(request: Request, exc: Exception) -> JSONResponse:
    """Render a domain :class:`DecisionError` with the status its class fixes."""
    error = cast(DecisionError, exc)
    return _error(error.status_code, error.code, error.message)


async def _render_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Answer any other exception, a database error included, with 500 ``internal_error``."""
    return _error(500, "internal_error", type(exc).__name__)


def _build_app(sessions: Sessions) -> FastAPI:
    """Assemble the auth and decisions routes as the product's application does."""
    app = FastAPI()
    guard = [Depends(deps.require_csrf)]
    app.include_router(auth.router, prefix="/api/v1", dependencies=guard)
    app.include_router(decisions.router, prefix="/api/v1", dependencies=guard)
    app.add_middleware(
        SessionMiddleware,
        secret_key=SIGNING_KEY,
        session_cookie=security.SESSION_COOKIE,
        max_age=security.SESSION_MAX_AGE,
        same_site="lax",
        https_only=False,
    )
    app.state.login_limiter = security.LoginRateLimiter()
    app.add_exception_handler(RequestValidationError, _render_validation_error)
    app.add_exception_handler(ApiError, _render_api_error)
    app.add_exception_handler(DecisionError, _render_decision_error)
    app.add_exception_handler(Exception, _render_unexpected)

    async def session_override() -> AsyncIterator[AsyncSession]:
        """Yield one session per request, rolled back when the request fails."""
        async with sessions() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise

    app.dependency_overrides[deps.get_session] = session_override
    app.dependency_overrides[deps.get_now] = lambda: NOW
    return app


@pytest.fixture
async def client(sessions: Sessions) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of a fresh application, whose 500 responses are returned, not raised."""
    transport = httpx.ASGITransport(app=_build_app(sessions), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def _sign_in(client: httpx.AsyncClient, world: World) -> None:
    """Sign the seeded admin in, so the client carries the session cookie."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": world.email, "password": ADMIN_PASSWORD},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert client.cookies.get(security.SESSION_COOKIE)


async def _post(
    client: httpx.AsyncClient, subject: Mapping[str, Any], recommendation_id: int | None
) -> httpx.Response:
    """``POST /decisions`` pausing ``subject``, recorded from ``recommendation_id``."""
    body: dict[str, Any] = {"subject": dict(subject), "patch": {"status": "paused"}, "note": NOTE}
    if recommendation_id is not None:
        body["recommendation_id"] = recommendation_id
    return await client.post("/api/v1/decisions", json=body, headers=CSRF)


def _assert_error(response: httpx.Response, status_code: int, code: str) -> str:
    """Assert an AAP 0.5.3 error of ``status_code`` and ``code``; return its message."""
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    message = body["error"]["message"]
    assert isinstance(message, str)
    assert NOTE not in message
    return message


# ---------------------------------------------------------------------------------------
# The item must offer this decision (BD1-F02)
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_the_decision_an_item_offers_is_recorded_from_it(
    sessions: Sessions, manager: DatasetManager, client: httpx.AsyncClient, scenario: str
) -> None:
    world = await _world(sessions, manager, scenario)
    item = await _item(sessions, world, decision_subject=_campaign(world.campaign_a))
    await _sign_in(client, world)

    response = await _post(client, _campaign(world.campaign_a), item)

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["origin"], body["recommendation_id"], body["status"]) == (
        "recommendation",
        item,
        "paused",
    )
    latest = (await _decisions_of(sessions, world.campaign_a))[-1]
    assert (latest.id, latest.origin, latest.recommendation_id) == (
        body["id"],
        "recommendation",
        item,
    )
    assert latest.dataset_id == world.linkedin
    assert await _written(sessions) == Written(decisions=1, reruns=1)


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("offered", "requested"),
    [
        pytest.param("campaign_a", "campaign_b", id="another-campaign"),
        pytest.param("campaign_a", "budget_with_campaign_a_id", id="budget-with-the-same-id"),
        pytest.param("shared_budget", "campaign_a", id="campaign-of-a-shared-budget-item"),
        pytest.param("shared_budget", "campaign_with_budget_id", id="campaign-with-the-same-id"),
    ],
)
async def test_a_subject_the_item_does_not_offer_is_a_422_subject_mismatch(
    sessions: Sessions,
    manager: DatasetManager,
    client: httpx.AsyncClient,
    offered: str,
    requested: str,
) -> None:
    world = await _world(sessions, manager, "crm_primary")
    subjects: dict[str, dict[str, Any]] = {
        "campaign_a": _campaign(world.campaign_a),
        "campaign_b": _campaign(world.campaign_b),
        "shared_budget": _budget(world.shared_budget),
        "budget_with_campaign_a_id": _budget(world.campaign_a),
        "campaign_with_budget_id": _campaign(world.shared_budget),
    }
    stored, sent = subjects[offered], subjects[requested]
    provenance = (
        await _provenance(sessions, world.google)
        if stored["type"] == "budget"
        else await _provenance(sessions, world.linkedin, world.crm)
    )
    item = await _item(sessions, world, decision_subject=stored, provenance=provenance)
    await _sign_in(client, world)

    message = _assert_error(await _post(client, sent, item), 422, "subject_mismatch")

    assert message == (
        f"Recommendation {item} offers a decision on {stored['type']} {stored['id']}, "
        f"not on {sent['type']} {sent['id']}"
    )
    assert await _written(sessions) == Written(decisions=0, reruns=0)


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(lambda world: None, id="none"),
        pytest.param(lambda world: null(), id="sql-null"),
        pytest.param(lambda world: {"type": "campaign"}, id="no-id"),
        pytest.param(lambda world: {"id": world.campaign_a}, id="no-type"),
        pytest.param(lambda world: {"type": "keyword", "id": world.campaign_a}, id="unknown-type"),
        pytest.param(
            lambda world: {"type": "campaign", "id": str(world.campaign_a)},
            id="string-id",
        ),
        pytest.param(
            lambda world: {"type": "campaign", "id": float(world.campaign_a)},
            id="float-id",
        ),
        pytest.param(lambda world: {"type": "campaign", "id": True}, id="boolean-id"),
        pytest.param(lambda world: {"type": "campaign", "id": 0}, id="zero-id"),
        pytest.param(lambda world: ["campaign", world.campaign_a], id="array"),
        pytest.param(lambda world: "campaign", id="string"),
    ],
)
async def test_an_item_offering_no_decision_is_a_422_decision_not_offered(
    sessions: Sessions,
    manager: DatasetManager,
    client: httpx.AsyncClient,
    stored: Callable[[World], object],
) -> None:
    world = await _world(sessions, manager, "crm_primary")
    item = await _item(
        sessions, world, decision_subject=stored(world), rule_id="CR1", section="creative"
    )
    await _sign_in(client, world)

    message = _assert_error(
        await _post(client, _campaign(world.campaign_a), item), 422, "decision_not_offered"
    )

    assert message == (
        f"Recommendation {item} offers no decision to record; record the decision without "
        "recommendation_id"
    )
    assert await _written(sessions) == Written(decisions=0, reruns=0)


@pytest.mark.anyio
@pytest.mark.db
async def test_the_checks_run_404_then_422_then_409(
    sessions: Sessions, manager: DatasetManager, client: httpx.AsyncClient
) -> None:
    world = await _world(sessions, manager, "crm_primary")
    item = await _item(sessions, world, decision_subject=_campaign(world.campaign_a))
    replacement = await _replacement(manager, sessions, world)
    await manager.activate(replacement)
    await _sign_in(client, world)

    # An unknown item is a 404 whatever the subject.
    _assert_error(await _post(client, _campaign(world.campaign_b), item + 1000), 404, "not_found")
    # A historical item naming another subject: the mismatch is reported first.
    _assert_error(await _post(client, _campaign(world.campaign_b), item), 422, "subject_mismatch")
    # The same historical item, its own subject: 409.
    message = _assert_error(
        await _post(client, _campaign(world.campaign_a), item), 409, "subject_retired"
    )
    assert message == HISTORICAL
    assert await _written(sessions) == Written(decisions=0, reruns=0)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_decision_without_an_item_is_recorded_as_a_user_decision(
    sessions: Sessions, manager: DatasetManager, client: httpx.AsyncClient
) -> None:
    world = await _world(sessions, manager, "crm_primary")
    await _sign_in(client, world)

    response = await _post(client, _campaign(world.campaign_b), None)

    assert response.status_code == 201, response.text
    assert (response.json()["origin"], response.json()["recommendation_id"]) == ("user", None)
    assert await _written(sessions) == Written(decisions=1, reruns=1)


# ---------------------------------------------------------------------------------------
# The item must rest on current data until the decision commits (BD1-F03)
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_an_item_whose_crm_dataset_was_retired_is_a_409(
    sessions: Sessions, manager: DatasetManager, client: httpx.AsyncClient, scenario: str
) -> None:
    world = await _world(sessions, manager, scenario)
    item = await _item(sessions, world, decision_subject=_campaign(world.campaign_a))
    replacement = await _replacement(manager, sessions, world)
    activation = await manager.activate(replacement)
    assert activation.retired == _retired_by_replacement(world)
    await _sign_in(client, world)

    message = _assert_error(
        await _post(client, _campaign(world.campaign_a), item), 409, "subject_retired"
    )

    assert message == HISTORICAL
    assert await _written(sessions) == Written(decisions=0, reruns=0)


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param({"metrics": []}, id="no-provenance"),
        pytest.param({"provenance": []}, id="empty-provenance"),
        pytest.param({"provenance": {"dataset_id": 1}}, id="provenance-not-a-list"),
        pytest.param(
            {"provenance": [{"source": "linkedin", "dataset_id": 1, "mode": "live"}]},
            id="entry-without-role",
        ),
        pytest.param([], id="evidence-not-an-object"),
    ],
)
async def test_an_item_with_unverifiable_provenance_is_a_409(
    sessions: Sessions, manager: DatasetManager, client: httpx.AsyncClient, evidence: object
) -> None:
    world = await _world(sessions, manager, "crm_primary")
    item = await _item(
        sessions, world, decision_subject=_campaign(world.campaign_a), evidence=evidence
    )
    await _sign_in(client, world)

    message = _assert_error(
        await _post(client, _campaign(world.campaign_a), item), 409, "subject_retired"
    )

    assert message == HISTORICAL
    assert await _written(sessions) == Written(decisions=0, reruns=0)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_subject_outside_the_active_datasets_is_a_409(
    sessions: Sessions, manager: DatasetManager, client: httpx.AsyncClient
) -> None:
    world = await _world(sessions, manager, "crm_primary")
    building, _ = await _ready_build(manager, sessions, "linkedin", "live")
    async with sessions() as session, session.begin():
        account = AdAccount(
            source="linkedin",
            dataset_id=building,
            external_id="508",
            name="Acme LinkedIn",
            currency="USD",
            timezone="UTC",
        )
        session.add(account)
        await session.flush()
        stray = await _linkedin_campaign(session, building, account.id, "61")
    item = await _item(sessions, world, decision_subject=_campaign(stray))
    await _sign_in(client, world)

    message = _assert_error(await _post(client, _campaign(stray), item), 409, "subject_retired")

    assert message == (
        f"Campaign {stray} does not belong to an active dataset; decisions apply only to the "
        "active dataset's subjects"
    )
    assert await _written(sessions) == Written(decisions=0, reruns=0)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_subject_that_does_not_exist_is_still_the_domains_404(
    sessions: Sessions, manager: DatasetManager, client: httpx.AsyncClient
) -> None:
    world = await _world(sessions, manager, "crm_primary")
    missing = world.campaign_b + 1000
    item = await _item(sessions, world, decision_subject=_campaign(missing))
    await _sign_in(client, world)

    _assert_error(await _post(client, _campaign(missing), item), 404, "not_found")

    assert await _written(sessions) == Written(decisions=0, reruns=0)


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_a_request_during_an_uncommitted_activation_waits_and_is_refused(
    sessions: Sessions, manager: DatasetManager, client: httpx.AsyncClient, scenario: str
) -> None:
    world = await _world(sessions, manager, scenario)
    item = await _item(sessions, world, decision_subject=_campaign(world.campaign_a))
    replacement = await _replacement(manager, sessions, world)
    await _sign_in(client, world)

    async with sessions() as retiring:
        holder = await _backend_pid(retiring)
        activation = await manager.activate(replacement, session=retiring)
        assert activation.retired == _retired_by_replacement(world)
        request = asyncio.create_task(_post(client, _campaign(world.campaign_a), item))
        try:
            await _wait_until_blocked(sessions, holder)
            assert not request.done()
            await retiring.commit()
        finally:
            await retiring.rollback()
            response = await asyncio.wait_for(request, timeout=WAIT_LIMIT)

    message = _assert_error(response, 409, "subject_retired")
    assert message == HISTORICAL
    assert [row.origin for row in await _decisions_of(sessions, world.campaign_a)] == ["baseline"]
    assert await _written(sessions) == Written(decisions=0, reruns=0)


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_a_request_holding_its_locks_makes_the_activation_wait(
    sessions: Sessions,
    manager: DatasetManager,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    world = await _world(sessions, manager, scenario)
    item = await _item(sessions, world, decision_subject=_campaign(world.campaign_a))
    replacement = await _replacement(manager, sessions, world)
    await _sign_in(client, world)

    # Pause the request after the route's currentness guard and before the domain call, so
    # the only locks its transaction holds are the guard's.
    record_decision = decisions.record_decision
    reached, release = asyncio.Event(), asyncio.Event()
    holders: list[int] = []

    async def paused(*args: Any, **kwargs: Any) -> Decision:
        holders.append(await _backend_pid(kwargs["session"]))
        reached.set()
        await release.wait()
        return await record_decision(*args, **kwargs)

    monkeypatch.setattr(decisions, "record_decision", paused)
    request = asyncio.create_task(_post(client, _campaign(world.campaign_a), item))
    activation: asyncio.Task[Activation] | None = None
    try:
        await asyncio.wait_for(reached.wait(), timeout=WAIT_LIMIT)
        activation = asyncio.create_task(manager.activate(replacement))
        await _wait_until_blocked(sessions, holders[0])
        assert not activation.done()
    finally:
        release.set()
        response = await asyncio.wait_for(request, timeout=WAIT_LIMIT)
        activated = None if activation is None else await asyncio.wait_for(activation, WAIT_LIMIT)

    # The decision committed while the item was current; the activation then went ahead.
    assert response.status_code == 201, response.text
    assert response.json()["origin"] == "recommendation"
    assert activated is not None
    assert activated.retired == _retired_by_replacement(world)
    latest = (await _decisions_of(sessions, world.campaign_a))[-1]
    assert (latest.origin, latest.recommendation_id) == ("recommendation", item)
    assert await _written(sessions) == Written(decisions=1, reruns=1)

    # The item is historical from now on.
    monkeypatch.setattr(decisions, "record_decision", record_decision)
    message = _assert_error(
        await _post(client, _campaign(world.campaign_a), item), 409, "subject_retired"
    )
    assert message == HISTORICAL
    assert await _written(sessions) == Written(decisions=1, reruns=1)
