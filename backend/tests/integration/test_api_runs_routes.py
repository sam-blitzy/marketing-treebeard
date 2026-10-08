"""HTTP tests for the ``/runs`` routes: run selection, custom runs, the lock and window bounds.

AAP 0.5.3 (``GET /runs/latest``, ``GET /runs/{id}``, ``GET /runs?kind=custom&limit=``,
``POST /runs``; "Run selection and the analysis lock") and 0.5.4.1 (window bounds):

* ``GET /runs/latest`` is 404 ``not_found`` before the first default run, and afterwards
  returns the latest succeeded default run: a manual 90-day custom run, or a custom run over
  exactly the default window, never stands in for it;
* ``POST /runs`` computes a ``kind=custom``, ``trigger=manual`` run from stored data and
  answers 201; ``GET /runs/{id}`` and ``GET /runs?kind=custom`` return it;
* while another session holds the analysis lock, ``POST /runs`` answers 409
  ``run_in_progress`` and writes no run;
* an end after the default end, an end before the start or a span above ``window.max_days``
  is a 422 carrying ``InvalidWindow``'s code, and no run is written;
* a window date sent as a UNIX timestamp or a datetime string is a 422 ``validation_error``
  naming the field, and no run is written, while the same dates as ``YYYY-MM-DD`` compute it;
* a changed ``window.default_days`` shows in ``Run.default_days`` and in the default run;
* every route needs a session (401) and ``POST /runs`` the CSRF header (403).

The application is assembled as ``api/app.py`` assembles it for these routes: the auth and
runs routers under ``/api/v1`` behind the CSRF guard, Starlette's ``SessionMiddleware`` with
the product's cookie name, and handlers rendering ``ApiError``, ``RunInProgress`` (409),
``InvalidWindow`` (422), request validation failures and anything else (500) in the AAP
error shape. The request clock is fixed through ``app.dependency_overrides``.

The runner commits through the application's own engine, so the tests point
``DATABASE_URL`` at a private schema of the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``); the request sessions come from the
same engine, exactly as in production. The schema is dropped with ``CASCADE`` at teardown.
An unreachable database fails the tests; it never skips them.
"""

import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.middleware.sessions import SessionMiddleware

import ads_engine.db.models  # noqa: F401 - registers every table on the metadata
from ads_engine.analysis import runner
from ads_engine.analysis.lock import RunInProgress, analysis_lock
from ads_engine.analysis.thresholds import Thresholds, resolve_config_dir
from ads_engine.analysis.window import InvalidWindow
from ads_engine.api import deps, security
from ads_engine.api.deps import ApiError
from ads_engine.api.routes import auth, runs
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount
from ads_engine.db.models.analysis import AnalysisRun
from ads_engine.db.models.ops import AppUser, SourceDataset
from ads_engine.db.session import dispose_engine, reset_engine, session_scope
from ads_engine.settings import Settings, get_settings

pytestmark = pytest.mark.integration

# Read once at import, before any fixture removes DATABASE_URL from the environment.
_CONFIGURED_TEST_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The request clock; yesterday is 2026-10-06 both in New York and in UTC."""

DEFAULT_END = date(2026, 10, 6)
"""The default window end at :data:`NOW`."""

GOOGLE_TZ = "America/New_York"
DISPLAY_TZ = "Europe/Berlin"
SIGNING_KEY = "test-signing-key"

ADMIN_PASSWORD = secrets.token_urlsafe(24)
"""The seeded admin's password, generated per run so no credential is written down."""

CSRF = {security.CSRF_HEADER: security.CSRF_VALUE}
"""The header every mutating request carries (AAP 0.5.3)."""

API = "/api/v1"


# ---------------------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    if not _CONFIGURED_TEST_URL:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the runs route tests",
            pytrace=False,
        )
    try:
        url = make_url(_CONFIGURED_TEST_URL)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL", pytrace=False)
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be PostgreSQL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding every table of the metadata; drop it afterwards."""
    url = _database_url()
    schema = f"runs_routes_test_{uuid.uuid4().hex}"
    engine = create_engine(
        url, poolclass=NullPool, connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection)
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
async def app_db(database: tuple[URL, str], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """Point the application's settings and engine at the private schema, emptied.

    Every settings variable inherited from the host shell is removed first, so no host
    credential reaches the application, and ``DISPLAY_TIMEZONE`` is set to a zone other than
    UTC so the run views prove they read it from the settings.

    Yields:
        The application database URL (with the schema's ``search_path``).
    """
    url, schema = database
    for key in list(os.environ):
        if key.lower() in Settings.model_fields:
            monkeypatch.delenv(key, raising=False)
    app_url = url.update_query_dict({"options": f"-c search_path={schema}"})
    rendered = app_url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", rendered)
    monkeypatch.setenv("DISPLAY_TIMEZONE", DISPLAY_TZ)
    get_settings.cache_clear()
    reset_engine()
    async with session_scope() as session:
        preparer = (await session.connection()).dialect.identifier_preparer
        tables = ", ".join(preparer.quote(table.name) for table in Base.metadata.sorted_tables)
        await session.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    try:
        yield rendered
    finally:
        await dispose_engine()
        reset_engine()
        get_settings.cache_clear()


async def _seed() -> str:
    """Seed an active live Google dataset in New York time and the admin; return its email."""
    email = f"admin-{uuid.uuid4().hex}@example.com"
    async with session_scope() as session:
        dataset = SourceDataset(
            source="google",
            mode="live",
            state="active",
            role="primary",
            parent_dataset_id=None,
            reason=None,
            activated_at=NOW - timedelta(days=30),
            retired_at=None,
            crm_bindings=None,
        )
        session.add(dataset)
        await session.flush()
        session.add_all(
            [
                AdAccount(
                    source="google",
                    dataset_id=dataset.id,
                    external_id="1234567890",
                    name="Main",
                    currency="USD",
                    timezone=GOOGLE_TZ,
                ),
                AppUser(email=email, password_hash=security.hash_password(ADMIN_PASSWORD)),
            ]
        )
    return email


async def _run_count() -> int:
    """Return how many ``analysis_run`` rows exist."""
    async with session_scope() as session:
        count = await session.scalar(select(func.count()).select_from(AnalysisRun))
    return int(count or 0)


async def _default_run(thresholds: Thresholds) -> AnalysisRun:
    """Compute the worker's default run at :data:`NOW` with ``thresholds``."""
    window = await runner.default_window(now=NOW, thresholds=thresholds)
    return await runner.run_analysis(window, "default", "scheduled", thresholds=thresholds, now=NOW)


async def _insert_running_run() -> int:
    """Insert a bare ``running`` custom run, as a run in flight leaves it; return its id."""
    async with session_scope() as session:
        run = AnalysisRun(
            kind="custom",
            window_start=DEFAULT_END - timedelta(days=13),
            window_end=DEFAULT_END,
            source_day_boundaries=[],
            trigger="manual",
            data_modes={},
            dataset_ids={},
            config_hash="0" * 64,
            status="running",
            started_at=NOW,
            finished_at=None,
        )
        session.add(run)
    return run.id


# ---------------------------------------------------------------------------------------
# The application under test
# ---------------------------------------------------------------------------------------


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """Return ``{"error": {"code", "message"}}`` with ``status_code`` (AAP 0.5.3)."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


async def _render_api_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an :class:`ApiError` with its status, code and message."""
    error = cast(ApiError, exc)
    return _error(error.status_code, error.code, error.message)


async def _render_run_in_progress(request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`RunInProgress` as 409 with its code, as ``api/app.py`` does."""
    error = cast(RunInProgress, exc)
    return _error(409, error.code, error.message)


async def _render_invalid_window(request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`InvalidWindow` as 422 with its own code, as ``api/app.py`` does."""
    error = cast(InvalidWindow, exc)
    return _error(422, error.code, error.message)


async def _render_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Render a request validation failure as 422 ``validation_error``."""
    errors = cast(RequestValidationError, exc).errors()
    message = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in errors
    )
    return _error(422, "validation_error", message or "Invalid request")


async def _render_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Answer any other exception with 500 ``internal_error``."""
    return _error(500, "internal_error", type(exc).__name__)


def _build_app(thresholds: Thresholds) -> FastAPI:
    """Assemble the auth and runs routes as the product's application does.

    Args:
        thresholds: The configuration stored on ``app.state.thresholds``.

    Returns:
        The application, with ``get_now`` fixed at :data:`NOW`.
    """
    app = FastAPI()
    guard = [Depends(deps.require_csrf)]
    app.include_router(auth.router, prefix=API, dependencies=guard)
    app.include_router(runs.router, prefix=API, dependencies=guard)
    app.add_middleware(
        SessionMiddleware,
        secret_key=SIGNING_KEY,
        session_cookie=security.SESSION_COOKIE,
        max_age=security.SESSION_MAX_AGE,
        same_site="lax",
        https_only=False,
    )
    app.state.login_limiter = security.LoginRateLimiter()
    app.state.thresholds = thresholds
    app.add_exception_handler(RequestValidationError, _render_validation_error)
    app.add_exception_handler(ApiError, _render_api_error)
    app.add_exception_handler(RunInProgress, _render_run_in_progress)
    app.add_exception_handler(InvalidWindow, _render_invalid_window)
    app.add_exception_handler(Exception, _render_unexpected)
    app.dependency_overrides[deps.get_now] = lambda: NOW
    return app


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    """The shipped ``config/`` directory, loaded once."""
    return Thresholds.load(resolve_config_dir())


async def _client(config: Thresholds) -> httpx.AsyncClient:
    """Return a client of a fresh application, whose 500 responses are returned, not raised."""
    transport = httpx.ASGITransport(app=_build_app(config), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _sign_in(client: httpx.AsyncClient, email: str) -> None:
    """Sign the seeded admin in, so the client carries the session cookie."""
    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": ADMIN_PASSWORD}, headers=CSRF
    )
    assert response.status_code == 200, response.text
    assert client.cookies.get(security.SESSION_COOKIE)


def _assert_error(response: httpx.Response, status_code: int, code: str) -> dict[str, Any]:
    """Assert the AAP error shape with ``status_code`` and ``code``; return the error."""
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    return cast(dict[str, Any], body["error"])


def _window(days: int, *, end: date = DEFAULT_END) -> dict[str, str]:
    """A ``POST /runs`` body of ``days`` days ending on ``end``."""
    return {
        "window_start": (end - timedelta(days=days - 1)).isoformat(),
        "window_end": end.isoformat(),
    }


# ---------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------


def test_routes_are_declared_with_latest_before_the_id_route() -> None:
    paths = [(route.path, sorted(route.methods)) for route in runs.router.routes]  # type: ignore[attr-defined]
    assert paths == [
        ("/runs/latest", ["GET"]),
        ("/runs/{run_id}", ["GET"]),
        ("/runs", ["GET"]),
        ("/runs", ["POST"]),
    ]
    assert runs.router.tags == ["runs"]


@pytest.mark.anyio
async def test_every_route_needs_a_session(app_db: str, thresholds: Thresholds) -> None:
    await _seed()
    async with await _client(thresholds) as client:
        for response in (
            await client.get(f"{API}/runs/latest"),
            await client.get(f"{API}/runs/1"),
            await client.get(f"{API}/runs"),
            await client.post(f"{API}/runs", json=_window(28), headers=CSRF),
        ):
            _assert_error(response, 401, "unauthenticated")
    assert await _run_count() == 0


@pytest.mark.anyio
async def test_post_without_the_csrf_header_is_refused(app_db: str, thresholds: Thresholds) -> None:
    email = await _seed()
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        response = await client.post(f"{API}/runs", json=_window(28))
        _assert_error(response, 403, "csrf_header_missing")
    assert await _run_count() == 0


@pytest.mark.anyio
async def test_latest_and_unknown_ids_are_not_found(app_db: str, thresholds: Thresholds) -> None:
    email = await _seed()
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        latest = _assert_error(await client.get(f"{API}/runs/latest"), 404, "not_found")
        assert latest["message"] == "No analysis run yet"
        unknown = _assert_error(await client.get(f"{API}/runs/999999"), 404, "not_found")
        assert unknown["message"] == "Run 999999 not found"
        # Ids no analysis_run row can carry are refused before any SQL runs.
        for bad in ("0", "-1", str(2**63), "latest-ish"):
            _assert_error(await client.get(f"{API}/runs/{bad}"), 422, "validation_error")
        listed = await client.get(f"{API}/runs")
        assert listed.status_code == 200, listed.text
        assert listed.json() == []


@pytest.mark.anyio
async def test_a_custom_run_is_created_and_never_becomes_latest(
    app_db: str, thresholds: Thresholds
) -> None:
    email = await _seed()
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        created = await client.post(f"{API}/runs", json=_window(90), headers=CSRF)
        assert created.status_code == 201, created.text
        run = created.json()
        assert run["kind"] == "custom"
        assert run["trigger"] == "manual"
        assert run["status"] == "succeeded"
        assert run["finished_at"] is not None
        assert run["window_start"] == (DEFAULT_END - timedelta(days=89)).isoformat()
        assert run["window_end"] == DEFAULT_END.isoformat()
        assert run["days"] == 90
        assert run["short_window"] is False
        assert run["default_days"] == thresholds.default_days
        assert run["trusted_min_days"] == thresholds.trusted_min_days
        assert run["display_timezone"] == DISPLAY_TZ
        assert run["config_hash"] == thresholds.config_hash
        boundaries = {day["source"]: day["boundary"] for day in run["source_days"]}
        assert boundaries == {"google": GOOGLE_TZ}
        assert run["data_modes"]["google"] == "live"

        # A custom run alone leaves /runs/latest empty: it never stands in for the default.
        _assert_error(await client.get(f"{API}/runs/latest"), 404, "not_found")
        fetched = await client.get(f"{API}/runs/{run['id']}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json() == run

        default = await _default_run(thresholds)
        # A custom run over exactly the default window is still a custom run.
        same = await client.post(
            f"{API}/runs",
            json={
                "window_start": default.window_start.isoformat(),
                "window_end": default.window_end.isoformat(),
            },
            headers=CSRF,
        )
        assert same.status_code == 201, same.text
        assert same.json()["kind"] == "custom"

        latest = await client.get(f"{API}/runs/latest")
        assert latest.status_code == 200, latest.text
        assert latest.json()["id"] == default.id
        assert latest.json()["kind"] == "default"
        assert latest.json()["days"] == thresholds.default_days

        customs = await client.get(f"{API}/runs", params={"kind": "custom"})
        assert customs.status_code == 200, customs.text
        assert [item["id"] for item in customs.json()] == [same.json()["id"], run["id"]]
        defaults = await client.get(f"{API}/runs", params={"kind": "default", "limit": 1})
        assert [item["id"] for item in defaults.json()] == [default.id]
        limited = await client.get(f"{API}/runs", params={"limit": 1})
        assert [item["id"] for item in limited.json()] == [same.json()["id"]]
    assert await _run_count() == 3


@pytest.mark.anyio
async def test_a_held_lock_answers_run_in_progress(app_db: str, thresholds: Thresholds) -> None:
    email = await _seed()
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        async with analysis_lock():
            response = await client.post(f"{API}/runs", json=_window(28), headers=CSRF)
        _assert_error(response, 409, "run_in_progress")
        assert await _run_count() == 0
        # Once the lock is released, the same request computes the run.
        retried = await client.post(f"{API}/runs", json=_window(28), headers=CSRF)
        assert retried.status_code == 201, retried.text
    assert await _run_count() == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        (_window(28, end=DEFAULT_END + timedelta(days=1)), "end_after_default_end"),
        (
            {
                "window_start": DEFAULT_END.isoformat(),
                "window_end": (DEFAULT_END - timedelta(days=1)).isoformat(),
            },
            "end_before_start",
        ),
        (_window(181), "too_long"),
        ({"window_start": DEFAULT_END.isoformat()}, "validation_error"),
        ({**_window(28), "kind": "default"}, "validation_error"),
        ({"window_start": "2026-02-30", "window_end": "2026-03-01"}, "validation_error"),
    ],
)
async def test_window_bounds_are_unprocessable(
    app_db: str, thresholds: Thresholds, body: dict[str, str], code: str
) -> None:
    email = await _seed()
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        _assert_error(await client.post(f"{API}/runs", json=body, headers=CSRF), 422, code)
    assert await _run_count() == 0


@pytest.mark.anyio
async def test_a_timestamp_or_datetime_window_date_is_a_validation_error(
    app_db: str, thresholds: Thresholds
) -> None:
    email = await _seed()
    start = DEFAULT_END - timedelta(days=22)
    midnight = datetime(start.year, start.month, start.day, tzinfo=UTC)
    # Every value names the start's UTC midnight, which lax date parsing would turn into
    # ``start`` and so into a valid 23-day window.
    refused: list[tuple[str, object]] = [
        ("window_start", int(midnight.timestamp())),
        ("window_start", str(int(midnight.timestamp()))),
        ("window_start", float(midnight.timestamp())),
        ("window_start", f"{start.isoformat()}T00:00:00"),
        ("window_start", f"{start.isoformat()}T00:00:00Z"),
        ("window_end", f"{DEFAULT_END.isoformat()}T00:00:00"),
    ]
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        for field, value in refused:
            body: dict[str, object] = {
                "window_start": start.isoformat(),
                "window_end": DEFAULT_END.isoformat(),
                field: value,
            }
            response = await client.post(f"{API}/runs", json=body, headers=CSRF)
            error = _assert_error(response, 422, "validation_error")
            assert error["message"].startswith(f"body.{field}: "), (value, error)
            assert '"YYYY-MM-DD"' in error["message"], (value, error)
            assert str(value) not in error["message"], (value, error)
        assert await _run_count() == 0

        # The same dates as YYYY-MM-DD strings are a valid window, computed as usual.
        created = await client.post(
            f"{API}/runs",
            json={"window_start": start.isoformat(), "window_end": DEFAULT_END.isoformat()},
            headers=CSRF,
        )
        assert created.status_code == 201, created.text
        assert created.json()["window_start"] == start.isoformat()
        assert created.json()["window_end"] == DEFAULT_END.isoformat()
        assert created.json()["days"] == 23
    assert await _run_count() == 1


@pytest.mark.anyio
async def test_the_window_limits_themselves_are_accepted(
    app_db: str, thresholds: Thresholds
) -> None:
    email = await _seed()
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        one_day = await client.post(f"{API}/runs", json=_window(1), headers=CSRF)
        assert one_day.status_code == 201, one_day.text
        assert one_day.json()["days"] == 1
        assert one_day.json()["short_window"] is True
        longest = await client.post(f"{API}/runs", json=_window(thresholds.max_days), headers=CSRF)
        assert longest.status_code == 201, longest.text
        assert longest.json()["days"] == thresholds.max_days


@pytest.mark.anyio
async def test_changed_default_days_show_in_runs(app_db: str) -> None:
    config = Thresholds.load(
        resolve_config_dir(),
        overrides={"window.default_days": 21, "window.presets": [14, 21, 28, 56, 90]},
    )
    email = await _seed()
    default = await _default_run(config)
    async with await _client(config) as client:
        await _sign_in(client, email)
        latest = await client.get(f"{API}/runs/latest")
        assert latest.status_code == 200, latest.text
        assert latest.json()["id"] == default.id
        assert latest.json()["days"] == 21
        assert latest.json()["default_days"] == 21
        created = await client.post(f"{API}/runs", json=_window(21), headers=CSRF)
        assert created.status_code == 201, created.text
        assert created.json()["default_days"] == 21
        assert created.json()["config_hash"] == config.config_hash


@pytest.mark.anyio
async def test_list_query_bounds_are_unprocessable(app_db: str, thresholds: Thresholds) -> None:
    email = await _seed()
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        for params in ({"limit": 0}, {"limit": runs.MAX_LIMIT + 1}, {"kind": "nightly"}):
            _assert_error(await client.get(f"{API}/runs", params=params), 422, "validation_error")
        accepted = await client.get(f"{API}/runs", params={"limit": runs.MAX_LIMIT})
        assert accepted.status_code == 200, accepted.text


@pytest.mark.anyio
async def test_a_running_run_is_returned_as_it_stands(app_db: str, thresholds: Thresholds) -> None:
    email = await _seed()
    run_id = await _insert_running_run()
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        response = await client.get(f"{API}/runs/{run_id}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "running"
        assert response.json()["finished_at"] is None
        assert response.json()["data_modes"] == {"google": None, "linkedin": None, "hubspot": None}
        listed = await client.get(f"{API}/runs")
        assert [item["id"] for item in listed.json()] == [run_id]
        _assert_error(await client.get(f"{API}/runs/latest"), 404, "not_found")


class _LogRecorder:
    """Stand-in for the route module's logger that records every ``info`` event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


@pytest.mark.anyio
async def test_a_created_run_is_logged_without_the_email(
    app_db: str, thresholds: Thresholds, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = await _seed()
    recorder = _LogRecorder()
    monkeypatch.setattr(runs, "_log", recorder)
    async with await _client(thresholds) as client:
        await _sign_in(client, email)
        created = await client.post(f"{API}/runs", json=_window(30), headers=CSRF)
        assert created.status_code == 201, created.text
    assert len(recorder.events) == 1
    event, fields = recorder.events[0]
    assert event == "runs.custom_created"
    assert fields["run_id"] == created.json()["id"]
    assert fields["window_start"] == (DEFAULT_END - timedelta(days=29)).isoformat()
    assert fields["window_end"] == DEFAULT_END.isoformat()
    assert fields["days"] == 30
    assert isinstance(fields["user_id"], int)
    assert email not in repr(fields)
