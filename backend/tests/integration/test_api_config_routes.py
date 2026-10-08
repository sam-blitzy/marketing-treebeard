"""HTTP tests for ``GET /config``: the typed HubSpot mapping and CRM binding views.

AAP 0.5.3 (``GET /config``) and 0.5.4.7 (bindings). The route returns the configured
``config/hubspot_mapping.yaml`` and the CRM bindings stored on HubSpot's active primary and
companion datasets as ``schemas.HubSpotMappingView`` and ``schemas.CrmBindingsView``:

* the wire carries exactly ``HubSpotMapping.to_json`` and ``CrmBindings.to_json``, every
  nullable key present, with ``verified_at`` rendered as the same instant in UTC;
* a dataset without verified bindings, or no dataset at all, gives ``null``;
* a corrupt stored document is still decoded strictly and fails as 500 ``config_error``;
* the OpenAPI document publishes the five binding components and ``ConfigView`` refers to
  them, so contract tooling checks names such as ``crm_bindings.mql.label``.

The application is assembled as ``api/app.py`` assembles it for this route: the config
router under ``/api/v1``, the loaded shipped ``config/`` on ``app.state.thresholds``, the
signed-in user supplied by a dependency override, and handlers rendering ``ConfigError`` as
500 ``config_error`` and anything else as 500 ``internal_error``.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``) in a private schema holding every
table of the model metadata, dropped with ``CASCADE`` at teardown. An unreachable database
fails the tests; it never skips them.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.analysis.thresholds import (
    ConfigError,
    CrmBindings,
    GoogleCrmBinding,
    ResolvedBinding,
    Thresholds,
)
from ads_engine.api import deps, schemas
from ads_engine.api.deps import CurrentUser
from ads_engine.api.routes import config
from ads_engine.db.base import Base
from ads_engine.db.models.ops import SourceDataset

type Sessions = async_sessionmaker[AsyncSession]

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

VERIFIED_AT = datetime(2026, 10, 6, 2, 30, tzinfo=ZoneInfo("America/New_York"))
"""06:30 UTC, stamped in a zone other than UTC."""

LIVE_BINDINGS = CrmBindings(
    mql=ResolvedBinding(
        available=True,
        property="hs_v2_date_entered_1093456",
        stage_id="1093456",
        label="First meeting booked",
    ),
    ql=ResolvedBinding(available=False, reason="stage label 'Qualified Lead' not found"),
    google=GoogleCrmBinding(available=False, reason="HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not set"),
    verified_at=VERIFIED_AT,
)
"""The live portal's bindings: MQL resolved, QL and the Google link unavailable."""

COMPANION_BINDINGS = CrmBindings.unavailable(
    "CRM bindings not yet verified", at=datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
)
"""The simulated companion portal's bindings, all unavailable."""


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
            "for the config route tests"
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
    schema = f"config_routes_test_{uuid.uuid4().hex}"
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


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    """The shipped ``config/`` directory, loaded once."""
    return Thresholds.load(CONFIG_DIR)


async def _seed(
    sessions: Sessions,
    *,
    primary: dict[str, Any] | None,
    companion: dict[str, Any] | None,
    with_companion: bool,
) -> None:
    """Commit an active HubSpot primary dataset and, optionally, an active companion.

    Every earlier active LinkedIn and HubSpot dataset is retired first, because at most one
    primary is active per source and the route expects at most one active companion.

    Args:
        sessions: The session factory.
        primary: The primary dataset's stored ``crm_bindings`` document.
        companion: The companion dataset's stored ``crm_bindings`` document.
        with_companion: Whether to create the companion and its LinkedIn parent.
    """
    async with sessions() as session, session.begin():
        await session.execute(
            update(SourceDataset)
            .where(
                SourceDataset.source.in_(("hubspot", "linkedin")),
                SourceDataset.state == "active",
            )
            .values(state="retired", retired_at=VERIFIED_AT)
        )
        session.add(
            SourceDataset(
                source="hubspot", mode="live", state="active", role="primary", crm_bindings=primary
            )
        )
        if with_companion:
            parent = SourceDataset(
                source="linkedin", mode="simulated", state="active", role="primary"
            )
            session.add(parent)
            await session.flush()
            session.add(
                SourceDataset(
                    source="hubspot",
                    mode="simulated",
                    state="active",
                    role="companion",
                    parent_dataset_id=parent.id,
                    crm_bindings=companion,
                )
            )


# ---------------------------------------------------------------------------------------
# The application under test
# ---------------------------------------------------------------------------------------


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """Return ``{"error": {"code", "message"}}`` with ``status_code`` (AAP 0.5.3)."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


async def _render_config_error(request: Request, exc: Exception) -> JSONResponse:
    """Render a :class:`ConfigError` as 500 ``config_error``."""
    return _error(500, "config_error", str(cast(ConfigError, exc)))


async def _render_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Answer any other exception with 500 ``internal_error``."""
    return _error(500, "internal_error", type(exc).__name__)


def _build_app(sessions: Sessions, thresholds: Thresholds) -> FastAPI:
    """Assemble the config route as the product's application does.

    Args:
        sessions: The factory each request's database session comes from.
        thresholds: The loaded configuration, stored as the lifespan stores it.

    Returns:
        The application, with ``get_session`` and ``require_user`` overridden.
    """
    app = FastAPI()
    app.include_router(config.router, prefix="/api/v1")
    app.state.thresholds = thresholds
    app.add_exception_handler(ConfigError, _render_config_error)
    app.add_exception_handler(Exception, _render_unexpected)

    async def session_override() -> AsyncIterator[AsyncSession]:
        """Yield one session per request."""
        async with sessions() as session:
            yield session

    app.dependency_overrides[deps.get_session] = session_override
    app.dependency_overrides[deps.require_user] = lambda: CurrentUser(
        id=1, email="admin@example.com"
    )
    return app


@pytest.fixture
async def client(sessions: Sessions, thresholds: Thresholds) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of a fresh application, whose 500 responses are returned, not raised."""
    transport = httpx.ASGITransport(
        app=_build_app(sessions, thresholds), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _assert_stored_document(served: dict[str, Any], bindings: CrmBindings) -> None:
    """Assert ``served`` is ``bindings.to_json()`` with ``verified_at`` as the same UTC instant."""
    stored = bindings.to_json()
    assert list(served) == list(stored)
    for key in ("mql", "ql", "google"):
        assert served[key] == stored[key], key
    assert served["verified_at"].endswith("Z")
    assert datetime.fromisoformat(served["verified_at"]) == bindings.verified_at
    assert CrmBindings.from_json(served) == bindings


# ---------------------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_config_serves_the_typed_mapping_and_both_portals_bindings(
    sessions: Sessions, client: httpx.AsyncClient, thresholds: Thresholds
) -> None:
    """The mapping and both stored documents reach the wire key for key."""
    await _seed(
        sessions,
        primary=LIVE_BINDINGS.to_json(),
        companion=COMPANION_BINDINGS.to_json(),
        with_companion=True,
    )
    response = await client.get("/api/v1/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["hubspot_mapping"] == thresholds.mapping.to_json()
    _assert_stored_document(body["crm_bindings"], LIVE_BINDINGS)
    _assert_stored_document(body["companion_crm_bindings"], COMPANION_BINDINGS)
    assert body["crm_bindings"]["verified_at"] == "2026-10-06T06:30:00Z"
    assert body["crm_bindings"]["mql"]["label"] == "First meeting booked"
    assert body["crm_bindings"]["ql"] == {
        "available": False,
        "property": None,
        "stage_id": None,
        "label": None,
        "reason": "stage label 'Qualified Lead' not found",
    }
    assert schemas.ConfigView.model_validate(body).model_dump(mode="json") == body


@pytest.mark.anyio
async def test_get_config_without_verified_bindings_serves_null(
    sessions: Sessions, client: httpx.AsyncClient
) -> None:
    """An unverified primary and an absent companion are both ``null``, never ``{}``."""
    await _seed(sessions, primary=None, companion=None, with_companion=False)
    response = await client.get("/api/v1/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["crm_bindings"], body["companion_crm_bindings"]) == (None, None)


@pytest.mark.anyio
async def test_get_config_with_a_corrupt_stored_document_is_a_config_error(
    sessions: Sessions, client: httpx.AsyncClient
) -> None:
    """A stored document missing a key is decoded strictly and fails as ``config_error``."""
    corrupt = {**LIVE_BINDINGS.to_json(), "mql": {"available": True}}
    await _seed(sessions, primary=corrupt, companion=None, with_companion=False)
    response = await client.get("/api/v1/config")
    assert response.status_code == 500, response.text
    error = response.json()["error"]
    assert error["code"] == "config_error"
    assert "source_dataset.crm_bindings" in error["message"]


def test_the_openapi_document_publishes_the_binding_components() -> None:
    """``ConfigView`` refers to named components instead of open objects."""
    app = FastAPI()
    app.include_router(config.router, prefix="/api/v1")
    components = app.openapi()["components"]["schemas"]
    properties = components["ConfigView"]["properties"]
    assert properties["hubspot_mapping"] == {"$ref": "#/components/schemas/HubSpotMappingView"}
    for name in ("crm_bindings", "companion_crm_bindings"):
        assert properties[name]["anyOf"] == [
            {"$ref": "#/components/schemas/CrmBindingsView"},
            {"type": "null"},
        ], name
    crm = components["CrmBindingsView"]["properties"]
    assert crm["mql"] == {"$ref": "#/components/schemas/ResolvedBindingView"}
    assert crm["google"] == {"$ref": "#/components/schemas/GoogleCrmBindingView"}
    stage = components["StageBindingView"]
    assert stage["required"] == ["mode", "stage_labels", "property"]
    assert stage["properties"]["mode"]["enum"] == ["stage_label", "property"]
    assert components["ResolvedBindingView"]["required"] == [
        "available",
        "property",
        "stage_id",
        "label",
        "reason",
    ]
