"""Integration tests for ``ads_engine.api.routes.auth``: the session-management contract.

AAP 0.5.3, Authentication: ``POST /auth/login``, ``POST /auth/logout`` and ``GET /auth/me``
each answer ``{email}``; every endpoint except login requires the session cookie (401
``unauthenticated``); mutating requests also require ``X-Requested-With: ads-engine`` (403
``csrf_header_missing``); errors use ``{"error": {"code", "message"}}``.

The routes run in a minimal FastAPI app assembled the way ``create_app`` mounts them:
Starlette's ``SessionMiddleware`` with the product's cookie name and 12 h lifetime (not
Secure, as on local HTTP), one login limiter on ``app.state``, the auth router under
``/api/v1`` behind the CSRF guard, and an ``ApiError`` handler rendering the AAP error shape.
Requests go through ``httpx.ASGITransport``, so no port is bound.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private schema
holding ``app_user``, points every connection's ``search_path`` at it alone, and drops it
with ``CASCADE`` at teardown, so ``public`` and every other schema stay untouched. The
request's database session comes from that schema through ``app.dependency_overrides``. An
unreachable database fails the database tests; it never skips them.
"""

import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, cast

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ads_engine.api import security
from ads_engine.api.deps import ApiError, get_session, require_csrf
from ads_engine.api.routes import auth
from ads_engine.db.base import Base
from ads_engine.db.models.ops import AppUser

pytestmark = pytest.mark.integration

type Sessions = async_sessionmaker[AsyncSession]

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "auth-routes-test-password"
SIGNING_KEY = "auth-routes-test-signing-key"

LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"

CSRF = {security.CSRF_HEADER: security.CSRF_VALUE}
EXPIRED_COOKIE = "expires=Thu, 01 Jan 1970 00:00:00 GMT"
UNAUTHENTICATED = {"error": {"code": "unauthenticated", "message": "Sign in required"}}
CSRF_MISSING = {
    "error": {
        "code": "csrf_header_missing",
        "message": f"Missing {security.CSRF_HEADER}: {security.CSRF_VALUE} header",
    }
}


# ---------------------------------------------------------------------------------------
# Application under test
# ---------------------------------------------------------------------------------------


async def _render_api_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an ``ApiError`` as ``{"error": {"code": ..., "message": ...}}`` with its headers."""
    error = cast(ApiError, exc)
    return JSONResponse(
        {"error": {"code": error.code, "message": error.message}},
        status_code=error.status_code,
        headers=error.headers,
    )


def _session_override(sessions: Sessions) -> Callable[[], AsyncIterator[AsyncSession]]:
    """Build a ``get_session`` replacement yielding sessions bound to the private schema."""

    async def override() -> AsyncIterator[AsyncSession]:
        """Yield one session for the request; closing it discards anything uncommitted."""
        async with sessions() as session:
            yield session

    return override


def _auth_app(sessions: Sessions | None = None) -> FastAPI:
    """Build a FastAPI app serving the auth router as ``create_app`` mounts it.

    Args:
        sessions: The session factory of the private schema; ``None`` for a test that only
            reads the OpenAPI document and sends no request.

    Returns:
        The app, with a fresh login limiter so every test starts with clean counters.
    """
    app = FastAPI()
    app.add_exception_handler(ApiError, _render_api_error)
    app.include_router(auth.router, prefix="/api/v1", dependencies=[Depends(require_csrf)])
    app.add_middleware(
        SessionMiddleware,
        secret_key=SIGNING_KEY,
        session_cookie=security.SESSION_COOKIE,
        max_age=security.SESSION_MAX_AGE,
        same_site="lax",
        https_only=False,
    )
    app.state.login_limiter = security.LoginRateLimiter()
    if sessions is not None:
        app.dependency_overrides[get_session] = _session_override(sessions)
    return app


class _LogRecorder:
    """Stand-in for the auth module's structlog logger that keeps every event it receives."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        """Record one info event with its structured fields."""
        self.events.append(("info", event, fields))

    def warning(self, event: str, **fields: object) -> None:
        """Record one warning event with its structured fields."""
        self.events.append(("warning", event, fields))

    def text(self) -> str:
        """Return every recorded event and field as one string, for leak checks."""
        return " ".join(f"{level} {event} {fields!r}" for level, event, fields in self.events)


# ---------------------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only, the loop psycopg's async driver uses here."""
    return "asyncio"


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the auth route tests"
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
    """Create a private schema holding ``app_user``; drop it afterwards."""
    url = _database_url()
    schema = f"auth_routes_test_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection, tables=[AppUser.__table__])
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
async def sessions(database: tuple[URL, str]) -> AsyncIterator[Sessions]:
    """Yield a session factory on an engine created and disposed inside this test's loop."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def admin_hash() -> str:
    """Hash the admin password once for the module; argon2 costs tens of milliseconds."""
    return security.hash_password(ADMIN_PASSWORD)


@pytest.fixture
async def admin_id(sessions: Sessions, admin_hash: str) -> int:
    """Store the single admin, replacing any row an earlier test left, and return its id."""
    async with sessions() as session:
        await session.execute(delete(AppUser))
        user = AppUser(email=ADMIN_EMAIL, password_hash=admin_hash)
        session.add(user)
        await session.commit()
        return user.id


@pytest.fixture
async def client(sessions: Sessions, admin_id: int) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client of a fresh auth app, with the admin stored and no session cookie."""
    transport = httpx.ASGITransport(app=_auth_app(sessions))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http


@pytest.fixture
def log_events(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """Replace the auth module's logger with a recorder for the duration of one test."""
    recorder = _LogRecorder()
    monkeypatch.setattr(auth, "_log", recorder)
    return recorder


async def _sign_in(client: httpx.AsyncClient) -> httpx.Response:
    """Sign the admin in with the CSRF header and assert the session cookie was issued."""
    response = await client.post(
        LOGIN, json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=CSRF
    )
    assert response.status_code == 200
    assert client.cookies.get(security.SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------------------
# Login, logout and the session
# ---------------------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.anyio
async def test_logout_returns_the_signed_out_email_and_expires_the_cookie(
    client: httpx.AsyncClient,
) -> None:
    """Login and logout both answer 200 ``{email}``; logout expires the cookie for good."""
    login = await _sign_in(client)
    assert login.json() == {"email": ADMIN_EMAIL}
    assert (await client.get(ME)).json() == {"email": ADMIN_EMAIL}

    logout = await client.post(LOGOUT, headers=CSRF)

    assert logout.status_code == 200
    assert logout.headers["content-type"] == "application/json"
    assert logout.json() == {"email": ADMIN_EMAIL}
    set_cookie = logout.headers["set-cookie"]
    assert set_cookie.startswith(f"{security.SESSION_COOKIE}=null;")
    assert EXPIRED_COOKIE in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert client.cookies.get(security.SESSION_COOKIE) is None

    after = await client.get(ME)
    assert after.status_code == 401
    assert after.json() == UNAUTHENTICATED


@pytest.mark.db
@pytest.mark.anyio
async def test_logout_without_a_session_is_401_unauthenticated(
    client: httpx.AsyncClient,
) -> None:
    """Logout requires the session cookie: without one it is 401 and sets no cookie."""
    response = await client.post(LOGOUT, headers=CSRF)

    assert response.status_code == 401
    assert response.json() == UNAUTHENTICATED
    assert "set-cookie" not in response.headers


@pytest.mark.db
@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [{}, {security.CSRF_HEADER: "XMLHttpRequest"}],
    ids=["header_missing", "header_wrong_value"],
)
async def test_logout_without_the_csrf_header_is_403_and_keeps_the_session(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    """Logout is mutating: without ``X-Requested-With: ads-engine`` it is 403, still signed in."""
    await _sign_in(client)

    response = await client.post(LOGOUT, headers=headers)

    assert response.status_code == 403
    assert response.json() == CSRF_MISSING
    assert "set-cookie" not in response.headers
    still_signed_in = await client.get(ME)
    assert still_signed_in.status_code == 200
    assert still_signed_in.json() == {"email": ADMIN_EMAIL}


@pytest.mark.db
@pytest.mark.anyio
async def test_logout_logs_the_user_id_and_never_the_email(
    client: httpx.AsyncClient, admin_id: int, log_events: _LogRecorder
) -> None:
    """The ``auth.logged_out`` event carries the user id only; no email reaches the log."""
    await _sign_in(client)

    assert (await client.post(LOGOUT, headers=CSRF)).status_code == 200

    assert ("info", "auth.logged_out", {"user_id": admin_id}) in log_events.events
    assert ADMIN_EMAIL not in log_events.text()


# ---------------------------------------------------------------------------------------
# OpenAPI declaration
# ---------------------------------------------------------------------------------------


def test_openapi_declares_me_as_the_200_response_of_login_logout_and_me() -> None:
    """Each auth operation documents 200 with the ``Me`` schema; logout declares no 204."""
    document: dict[str, Any] = _auth_app().openapi()
    operations = {
        LOGIN: document["paths"][LOGIN]["post"],
        LOGOUT: document["paths"][LOGOUT]["post"],
        ME: document["paths"][ME]["get"],
    }
    me_ref = {"$ref": "#/components/schemas/Me"}
    for path, operation in operations.items():
        ok = operation["responses"]["200"]
        assert ok["content"]["application/json"]["schema"] == me_ref, path
    logout_responses = operations[LOGOUT]["responses"]
    assert "204" not in logout_responses
    for status in ("401", "403"):
        assert logout_responses[status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
    assert document["components"]["schemas"]["Me"]["required"] == ["email"]
