"""HTTP tests for ``POST /auth/login``, ``GET /decisions`` and ``POST /decisions`` bounds.

A request value the database cannot hold must be a 422 validation error in the AAP 0.5.3
error shape, never a database error rendered as 500:

* an id above the largest PostgreSQL ``bigint`` (``schemas.BIGINT_MAX``) in
  ``subject.id``, ``recommendation_id`` or the ``subject_id`` query is refused before any
  SQL runs, while the largest ``bigint`` itself still reaches the database: an unknown
  ``subject_id`` lists nothing and an unknown ``recommendation_id`` is a 404;
* text PostgreSQL ``text`` cannot hold, U+0000 or an unpaired surrogate, in the login
  email or a decision note is refused before any SQL runs, and the message never repeats
  it, while an ordinary note, non-ASCII included, is stored exactly as sent.

One stored amount also has one rendering: ``POST /decisions`` answers from the committed
row at its ``numeric(18,6)`` scale, exactly as a later ``POST`` carries it over and
``GET /decisions`` lists it, while ``patch`` echoes the amount as submitted.

The application is assembled here as ``api/app.py`` assembles it: the auth and decisions
routers under ``/api/v1`` behind the CSRF guard, Starlette's ``SessionMiddleware`` with the
product's cookie name, one login limiter on ``app.state``, and handlers that render
``ApiError``, domain ``DecisionError`` and request validation failures in the error shape.
A catch-all handler answers any other exception with 500, and the transport is told not to
re-raise it, so a regression shows as a 500 response rather than a test crash.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema holding every table of the model metadata, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. An unreachable database fails the tests; it
never skips them.
"""

import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.middleware.sessions import SessionMiddleware

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.api import deps, schemas, security
from ads_engine.api.deps import ApiError
from ads_engine.api.routes import auth, decisions
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Campaign
from ads_engine.db.models.decisions import Decision
from ads_engine.db.models.ops import AppUser, SourceDataset
from ads_engine.decisions import DecisionError

type Sessions = async_sessionmaker[AsyncSession]

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
"""The request clock: sign-in and decision instant."""

BASELINE_AT = NOW - timedelta(days=1)
"""When the seeded campaign's baseline decision was recorded."""

SIGNING_KEY = "test-signing-key"

ADMIN_PASSWORD = secrets.token_urlsafe(24)
"""The seeded admin's password, generated per run so no credential is written down."""

CSRF = {security.CSRF_HEADER: security.CSRF_VALUE}
"""The header every mutating request carries (AAP 0.5.3)."""

OVERSIZE_ID = 2**63
"""One above the largest PostgreSQL ``bigint``."""


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
            "for the decisions route tests"
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
    schema = f"decisions_routes_test_{uuid.uuid4().hex}"
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
    """Yield a session factory on an engine created and disposed inside this test's loop."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------
# Scenario: one active LinkedIn campaign with its baseline, and the admin
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    """The seeded rows a test addresses.

    Attributes:
        dataset_id: The active LinkedIn primary dataset.
        campaign_id: Its one ``own_budget`` campaign, decided ``active`` at baseline.
        baseline_id: The campaign's baseline decision.
        email: The admin's email, unique to the test.
    """

    dataset_id: int
    campaign_id: int
    baseline_id: int
    email: str


@pytest.fixture
async def world(sessions: Sessions) -> World:
    """Commit an active LinkedIn dataset, its campaign and baseline, and an admin user.

    The source's previous active primary dataset is retired first, because at most one may
    be active per source; earlier tests' rows therefore drop out of ``GET /decisions``.
    """
    async with sessions() as session, session.begin():
        await session.execute(
            update(SourceDataset)
            .where(
                SourceDataset.source == "linkedin",
                SourceDataset.state == "active",
                SourceDataset.role == "primary",
            )
            .values(state="retired", retired_at=BASELINE_AT)
        )
        dataset = SourceDataset(source="linkedin", mode="simulated", state="active", role="primary")
        session.add(dataset)
        await session.flush()
        account = AdAccount(
            source="linkedin",
            dataset_id=dataset.id,
            external_id="508",
            name="Acme LinkedIn",
            currency="USD",
            timezone="UTC",
        )
        session.add(account)
        await session.flush()
        campaign = Campaign(
            source="linkedin",
            dataset_id=dataset.id,
            account_id=account.id,
            external_id="urn:li:sponsoredCampaign:61",
            name="White paper A",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
            daily_budget=Decimal("25.00"),
        )
        session.add(campaign)
        await session.flush()
        baseline = Decision(
            source="linkedin",
            dataset_id=dataset.id,
            subject_type="campaign",
            subject_id=campaign.id,
            shape="own_budget",
            status="active",
            daily_budget=Decimal("25.00"),
            total_budget=None,
            budget_id=None,
            decided_at=BASELINE_AT,
            decided_by=None,
            origin="baseline",
            patch={},
            note=None,
        )
        email = f"admin-{uuid.uuid4().hex}@example.com"
        session.add_all(
            [baseline, AppUser(email=email, password_hash=security.hash_password(ADMIN_PASSWORD))]
        )
        await session.flush()
        return World(
            dataset_id=dataset.id,
            campaign_id=campaign.id,
            baseline_id=baseline.id,
            email=email,
        )


async def _decision_count(sessions: Sessions, campaign_id: int) -> int:
    """Return how many decision rows the campaign has."""
    async with sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Decision)
            .where(Decision.subject_type == "campaign", Decision.subject_id == campaign_id)
        )
        return int(count or 0)


# ---------------------------------------------------------------------------------------
# The application under test
# ---------------------------------------------------------------------------------------


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """Return ``{"error": {"code", "message"}}`` with ``status_code`` (AAP 0.5.3)."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


async def _render_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Render a request validation failure as 422 ``validation_error``.

    The message names each failing location and pydantic's message, never the submitted
    input, which may be an email or a note.
    """
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
    """Assemble the auth and decisions routes as the product's application does.

    Args:
        sessions: The factory each request's database session comes from.

    Returns:
        The application, with ``get_session`` and ``get_now`` overridden.
    """
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


def _assert_validation_error(response: httpx.Response, location: str) -> None:
    """Assert a 422 ``validation_error`` in the AAP error shape naming ``location``."""
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == "validation_error"
    assert location in body["error"]["message"]


# ---------------------------------------------------------------------------------------
# Ids beyond the largest bigint
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_oversize_subject_id_in_the_body_is_a_422_not_a_database_error(
    sessions: Sessions, world: World, client: httpx.AsyncClient
) -> None:
    """``subject.id`` of 2**63 is a 422 naming the field, and no decision is written."""
    await _sign_in(client, world)
    response = await client.post(
        "/api/v1/decisions",
        json={"subject": {"type": "campaign", "id": OVERSIZE_ID}, "patch": {"status": "paused"}},
        headers=CSRF,
    )
    _assert_validation_error(response, "body.subject.id")
    assert str(OVERSIZE_ID) not in response.text
    assert await _decision_count(sessions, world.campaign_id) == 1


@pytest.mark.anyio
async def test_an_oversize_recommendation_id_is_a_422_not_a_database_error(
    sessions: Sessions, world: World, client: httpx.AsyncClient
) -> None:
    """``recommendation_id`` of 2**63 is a 422 before the item lookup; nothing is written."""
    await _sign_in(client, world)
    response = await client.post(
        "/api/v1/decisions",
        json={
            "subject": {"type": "campaign", "id": world.campaign_id},
            "patch": {"status": "paused"},
            "recommendation_id": OVERSIZE_ID,
        },
        headers=CSRF,
    )
    _assert_validation_error(response, "body.recommendation_id")
    assert await _decision_count(sessions, world.campaign_id) == 1


@pytest.mark.anyio
async def test_the_largest_bigint_recommendation_id_reaches_the_lookup_and_is_not_found(
    sessions: Sessions, world: World, client: httpx.AsyncClient
) -> None:
    """``recommendation_id`` of 2**63-1 passes validation and is looked up: 404."""
    await _sign_in(client, world)
    response = await client.post(
        "/api/v1/decisions",
        json={
            "subject": {"type": "campaign", "id": world.campaign_id},
            "patch": {"status": "paused"},
            "recommendation_id": schemas.BIGINT_MAX,
        },
        headers=CSRF,
    )
    assert response.status_code == 404, response.text
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": f"Recommendation {schemas.BIGINT_MAX} not found",
        }
    }
    assert await _decision_count(sessions, world.campaign_id) == 1


@pytest.mark.anyio
async def test_an_oversize_subject_id_query_is_a_422_not_a_database_error(
    world: World, client: httpx.AsyncClient
) -> None:
    """``GET /decisions?subject_id=2**63`` is a 422 naming the query parameter."""
    await _sign_in(client, world)
    response = await client.get(
        "/api/v1/decisions", params={"subject_type": "campaign", "subject_id": OVERSIZE_ID}
    )
    _assert_validation_error(response, "query.subject_id")


@pytest.mark.anyio
async def test_the_largest_bigint_subject_id_query_lists_nothing(
    world: World, client: httpx.AsyncClient
) -> None:
    """``subject_id`` of 2**63-1 is compared in SQL and lists nothing; a known id lists it."""
    await _sign_in(client, world)
    unknown = await client.get(
        "/api/v1/decisions",
        params={"subject_type": "campaign", "subject_id": schemas.BIGINT_MAX},
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json() == []
    # The same filter on the seeded campaign lists its baseline, so the empty answer above
    # came from the database comparison, not from a short cut.
    known = await client.get(
        "/api/v1/decisions",
        params={"subject_type": "campaign", "subject_id": world.campaign_id},
    )
    assert known.status_code == 200, known.text
    assert [row["id"] for row in known.json()] == [world.baseline_id]


def test_the_subject_id_query_documents_its_bigint_bounds_in_openapi() -> None:
    """The OpenAPI schema of ``subject_id`` carries ``exclusiveMinimum`` 0 and the bigint max."""
    app = FastAPI()
    app.include_router(decisions.router, prefix="/api/v1")
    parameters = app.openapi()["paths"]["/api/v1/decisions"]["get"]["parameters"]
    subject_id: dict[str, Any] = next(
        parameter for parameter in parameters if parameter["name"] == "subject_id"
    )
    integer = next(
        option for option in subject_id["schema"]["anyOf"] if option.get("type") == "integer"
    )
    assert integer["exclusiveMinimum"] == 0
    assert integer["maximum"] == schemas.BIGINT_MAX


# ---------------------------------------------------------------------------------------
# Text PostgreSQL cannot store
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_with_a_nul_in_the_email_is_a_422_not_a_database_error(
    world: World, client: httpx.AsyncClient
) -> None:
    """A NUL in the login email is a 422 that repeats neither credential; no cookie is set."""
    email = "adm\x00in@example.com"
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": ADMIN_PASSWORD},
        headers=CSRF,
    )
    _assert_validation_error(response, "body.email")
    assert "NUL (U+0000)" in response.json()["error"]["message"]
    assert "example.com" not in response.text
    assert ADMIN_PASSWORD not in response.text
    assert client.cookies.get(security.SESSION_COOKIE) is None


def _note_body(world: World, note: str) -> str:
    """Return a ``POST /decisions`` body pausing the campaign with ``note``, as JSON text.

    ``json.dumps`` escapes every non-ASCII character, so an unpaired surrogate travels as
    the ``\\ud800`` escape a browser's ``JSON.stringify`` sends; httpx's own ``json=``
    encoder would fail to encode it as UTF-8 before the request left the client.
    """
    return json.dumps(
        {
            "subject": {"type": "campaign", "id": world.campaign_id},
            "patch": {"status": "paused"},
            "note": note,
        }
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("note", "message"),
    [
        ("a\x00b", "NUL (U+0000)"),
        ("Paused \ud800 for review", "unicode string"),
    ],
    ids=["nul", "unpaired-surrogate"],
)
async def test_a_decision_note_postgresql_cannot_store_is_a_422_and_nothing_is_written(
    sessions: Sessions, world: World, client: httpx.AsyncClient, note: str, message: str
) -> None:
    """A note holding NUL or an unpaired surrogate is a 422 without the note; nothing is written."""
    await _sign_in(client, world)
    response = await client.post(
        "/api/v1/decisions",
        content=_note_body(world, note),
        headers={**CSRF, "Content-Type": "application/json"},
    )
    _assert_validation_error(response, "body.note")
    assert message in response.json()["error"]["message"]
    assert "Paused" not in response.text
    assert await _decision_count(sessions, world.campaign_id) == 1


@pytest.mark.anyio
async def test_an_ordinary_note_with_non_ascii_text_is_stored_exactly(
    sessions: Sessions, world: World, client: httpx.AsyncClient
) -> None:
    """An ordinary note with non-ASCII text is answered 201 and stored exactly as sent."""
    note = "Paused for review — café ✓"
    await _sign_in(client, world)
    response = await client.post(
        "/api/v1/decisions",
        content=_note_body(world, note),
        headers={**CSRF, "Content-Type": "application/json"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["note"], body["status"], body["origin"], body["shape"]) == (
        note,
        "paused",
        "user",
        "own_budget",
    )
    assert body["subject"]["id"] == world.campaign_id
    async with sessions() as session:
        stored = await session.get(Decision, body["id"])
    assert stored is not None
    assert (stored.note, stored.decided_by, stored.patch) == (
        note,
        world.email,
        {"status": "paused"},
    )
    assert await _decision_count(sessions, world.campaign_id) == 2


# ---------------------------------------------------------------------------------------
# One stored amount, one rendering
# ---------------------------------------------------------------------------------------


def _patch_body(world: World, patch: dict[str, str]) -> dict[str, Any]:
    """Return a ``POST /decisions`` body applying ``patch`` to the seeded campaign."""
    return {"subject": {"type": "campaign", "id": world.campaign_id}, "patch": patch}


@pytest.mark.anyio
async def test_a_recorded_amount_renders_at_the_stored_scale_in_every_response(
    world: World, client: httpx.AsyncClient
) -> None:
    """``POST`` answers "187.650000" like the carried-over row and the log; ``patch`` echoes."""
    await _sign_in(client, world)
    budget = await client.post(
        "/api/v1/decisions",
        json=_patch_body(world, {"daily_budget": "187.65"}),
        headers=CSRF,
    )
    assert budget.status_code == 201, budget.text
    recorded = budget.json()
    assert (recorded["daily_budget"], recorded["total_budget"], recorded["patch"]) == (
        "187.650000",
        None,
        {"daily_budget": "187.65"},
    )

    status = await client.post(
        "/api/v1/decisions", json=_patch_body(world, {"status": "paused"}), headers=CSRF
    )
    assert status.status_code == 201, status.text
    carried = status.json()
    assert (carried["status"], carried["daily_budget"], carried["patch"]) == (
        "paused",
        "187.650000",
        {"status": "paused"},
    )

    log = await client.get(
        "/api/v1/decisions",
        params={"subject_type": "campaign", "subject_id": world.campaign_id},
    )
    assert log.status_code == 200, log.text
    listed = {row["id"]: row for row in log.json()}
    assert list(listed) == [carried["id"], recorded["id"], world.baseline_id]
    # Every field of each POST answer, the amount included, reads exactly as the log lists it.
    assert listed[recorded["id"]] == recorded
    assert listed[carried["id"]] == carried
    assert listed[world.baseline_id]["daily_budget"] == "25.000000"


@pytest.mark.anyio
async def test_the_answer_without_a_read_back_row_keeps_the_stored_scale(
    world: World, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the post-commit read finds no row, the row in hand answers at the stored scale."""

    async def no_rows(session: AsyncSession, statement: object) -> list[schemas.Decision]:
        """Stand in for a read that runs after the decision's dataset was purged."""
        return []

    await _sign_in(client, world)
    monkeypatch.setattr(decisions, "_read_views", no_rows)
    response = await client.post(
        "/api/v1/decisions",
        json=_patch_body(world, {"daily_budget": "187.65"}),
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["daily_budget"], body["total_budget"], body["patch"]) == (
        "187.650000",
        None,
        {"daily_budget": "187.65"},
    )
    assert (body["status"], body["subject"]["id"], body["subject"]["name"]) == (
        "active",
        world.campaign_id,
        None,
    )
