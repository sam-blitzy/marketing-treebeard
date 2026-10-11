"""Integration tests of the API's cross-cutting contract, on the real application.

Every request goes through ``create_app()`` and ``httpx.ASGITransport`` against the PostgreSQL 18
test database; nothing reaches the network. The guarantees proven here:

* **Authentication and session** (AAP 0.5.3 "Authentication", 0.3.2 "Security architecture"):
  login, ``/auth/me`` and logout; one 401 ``invalid_credentials`` for an unknown email and a
  wrong password; the signed session cookie is HttpOnly, ``SameSite=Lax`` and lives 12 h, and is
  ``Secure`` exactly when ``APP_BASE_URL`` is ``https://``; a session expires 12 h after sign-in.
* **Rate limit, CSRF and errors** (AAP 0.5.3 "Rate limits" and status codes): the 6th login
  attempt within a minute from one client IP is 429 with ``Retry-After``; every mutating request
  needs ``X-Requested-With: ads-engine`` (403); every endpoint except ``POST /auth/login`` and
  ``GET /healthz`` needs the session (401); every error status renders ``{"error": {"code",
  "message"}}`` and nothing else.
* **Bounded waits** (AAP 0.5.3 ``GET /healthz`` and error envelope): a database that accepts
  TCP and never answers, as a paused or partitioned PostgreSQL does, makes ``/healthz`` answer
  503 ``database_unavailable`` within its 3 s bound, logged as ``health.database_unavailable``;
  a request still running at the request deadline, a database-backed one included, is
  answered 503 ``request_timeout`` at the deadline, logged as ``api.request_timeout``, and
  cancelled, while its cleanup unwinds in the background: what it sends meanwhile is dropped,
  a failure of the unwind is logged once without its text, and the lifespan waits for the
  unwinds, within a bound, before it disposes the pool. A request that finishes in time, a
  ``TimeoutError`` of the application's own, a response already started and other ASGI scopes
  are left alone, and an outside cancellation propagates once the request has unwound.
* **HEAD requests** (RFC 9110 §9.3.2): ``HEAD`` answers every ``GET`` route, signed in or
  anonymous, with ``GET``'s status and headers and no body, while the OpenAPI document publishes
  no ``head`` operation; a 405's ``Allow`` that names ``GET`` names ``HEAD`` too, and ``HEAD`` on
  a ``POST``-only route is 405 ``Allow: POST``. The deadline's 503 and the catch-all 500 log the
  client's real method.
* **No caching** (CWE-525): every answer carries exactly one ``Cache-Control: no-store``, so no
  browser or shared cache keeps it after sign-out: a login, a failed login and the 6th attempt's
  429 (its ``Retry-After`` kept), ``/auth/me`` and logout; the signed-in evidence and decision
  reads and the OpenAPI document on ``GET`` and ``HEAD``; 401, 403, 404, 405 (its ``Allow``
  kept) and 422; ``/healthz``; the deadline's 503 and the catch-all 500. The middleware replaces
  any ``Cache-Control`` the application sent and leaves other ASGI scopes alone.
* **Startup guard** (AAP 0.3.2): plain ``http://`` is accepted only on ``localhost`` and
  ``127.0.0.1``, and the API refuses to start without ``SESSION_SECRET`` or on a ``DATABASE_URL``
  the engine would refuse (malformed, or a driver other than psycopg 3), while an unreachable
  database still starts it and ``/healthz`` answers 503. Each refusal, and an environment value
  that fails validation, is logged once as ``api.startup_failed`` with the error class, the
  fields at fault and what is wrong, never their values, before the error propagates; under
  uvicorn the process then exits 1 with no line that is not JSON.
* **Every route returns its schema** (AAP 0.5.3 endpoint table), on simulated data seeded the
  way the worker seeds a first start.
* **Feedback is idempotent per item** (AAP 0.5.3 ``POST /recommendations/{id}/feedback``,
  0.3.2 rerun requests): a repeat of the item's latest feedback in force, sequential or
  concurrent, is answered with it and writes no row and no rerun request; another kind or note
  is recorded; the C0 422 and the historical 409 still come first.
* **Boundaries and redaction** (AAP 0.3.2, 0.11.1): a failed login is logged without the email,
  the password or the session secret, and the API process never imports the connectors, the sync
  jobs or the simulator.
* **Configuration errors** (AAP 0.5.3 error envelope; CWE-209): a broken config file, a missing
  config directory or a corrupt stored CRM binding is 500 ``config_error`` whose message names at
  most the logical file and dotted key, never a disk path, a YAML diagnostic or the detail, which
  are logged as ``api.config_error`` instead.

Run selection, the analysis lock and the decision-patch rules are proven by the run and decision
route tests; here each route gets one schema check.

**Seeding.** ``tests/conftest.py`` empties every table before each test, so the simulated world
is built once per module, inside the first test that needs it, and copied into a private schema;
every later test restores that copy after the per-test truncation, which costs milliseconds
instead of a full simulated backfill. The seed is ``sync.mode.bootstrap_sources(...,
force_simulated=True)`` (the worker's first start, every source simulated), one user decision
pausing an active Google campaign, the ``status`` discrepancy the next entity sync's detection
would open for it, and one default-window run.
"""

import asyncio
import functools
import gc
import json
import logging
import os
import socket
import subprocess
import sys
import textwrap
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
import pytest
import structlog
import yaml
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine, inspect, text
from starlette.types import Message, Receive, Scope, Send

from ads_engine import logging as ads_logging
from ads_engine.analysis import runner
from ads_engine.analysis.thresholds import CONFIG_FILES, ConfigError, Thresholds
from ads_engine.api import app as api_app
from ads_engine.api import deps, schemas, security
from ads_engine.api.app import API_PREFIX, create_app
from ads_engine.api.routes import health
from ads_engine.db import session as db_session
from ads_engine.db.models.ops import DATASETS, SOURCES, AppUser
from ads_engine.settings import ConfigurationError, Settings, get_settings
from ads_engine.sync import mode

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

BACKEND_DIR: Final = Path(__file__).resolve().parents[2]
"""``backend/``, the working directory of the boundary subprocess."""

CONFIG_DIR: Final = BACKEND_DIR.parent / "config"
"""The repository's ``config/``, read here to cross-check ``GET /config``."""

SECTION_KEYS: Final = ("keywords", "campaigns", "budget", "creative", "white_papers")
"""The five sections, in the fixed order AAP 0.5.3 requires."""

DISCREPANCY_GROUP: Final = "discrepancies"
"""The Campaigns group C0 items are pinned in (AAP 0.5.4.4)."""

SIGN_IN: Final = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The request clock at sign-in in the session-expiry test."""

GOOGLE_CALL_CAP: Final = 1000
"""The documented default of ``GOOGLE_DAILY_CALL_BUDGET`` (AAP 0.3.3)."""

UNKNOWN_ID: Final = 2**62
"""An id in the ``bigint`` range that no row carries."""

# Every AAP 0.5.3 endpoint as OpenAPI names it: (method, path template under /api/v1).
AAP_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("post", "/auth/login"),
        ("post", "/auth/logout"),
        ("get", "/auth/me"),
        ("get", "/runs/latest"),
        ("get", "/runs/{run_id}"),
        ("get", "/runs"),
        ("post", "/runs"),
        ("get", "/recommendations"),
        ("post", "/recommendations/{rec_id}/feedback"),
        ("get", "/decisions"),
        ("post", "/decisions"),
        ("get", "/discrepancies"),
        ("post", "/discrepancies/{discrepancy_id}/resolve"),
        ("get", "/sources"),
        ("post", "/sources/{source}/sync"),
        ("get", "/config"),
        ("get", "/healthz"),
    }
)

UNAUTHENTICATED_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {("post", "/auth/login"), ("get", "/healthz")}
)
"""The two endpoints AAP 0.5.3 exempts from the session cookie."""


@dataclass(frozen=True, slots=True)
class ProtectedRequest:
    """One concrete request to a session-protected endpoint.

    Attributes:
        method: The HTTP method, upper case.
        template: The OpenAPI path template under ``/api/v1``.
        path: The concrete path (with query) under ``/api/v1``.
        body: The JSON body, a valid one where the route takes a body.
    """

    method: str
    template: str
    path: str
    body: Mapping[str, Any] | None = None


PROTECTED_REQUESTS: Final[tuple[ProtectedRequest, ...]] = (
    ProtectedRequest("GET", "/auth/me", "/auth/me"),
    ProtectedRequest("POST", "/auth/logout", "/auth/logout"),
    ProtectedRequest("GET", "/runs/latest", "/runs/latest"),
    ProtectedRequest("GET", "/runs/{run_id}", "/runs/1"),
    ProtectedRequest("GET", "/runs", "/runs?kind=custom&limit=5"),
    ProtectedRequest(
        "POST", "/runs", "/runs", {"window_start": "2026-09-01", "window_end": "2026-09-14"}
    ),
    ProtectedRequest("GET", "/recommendations", "/recommendations"),
    ProtectedRequest(
        "POST",
        "/recommendations/{rec_id}/feedback",
        "/recommendations/1/feedback",
        {"kind": "snooze"},
    ),
    ProtectedRequest("GET", "/decisions", "/decisions"),
    ProtectedRequest(
        "POST",
        "/decisions",
        "/decisions",
        {"subject": {"type": "campaign", "id": 1}, "patch": {"status": "paused"}},
    ),
    ProtectedRequest("GET", "/discrepancies", "/discrepancies?state=open"),
    ProtectedRequest(
        "POST",
        "/discrepancies/{discrepancy_id}/resolve",
        "/discrepancies/1/resolve",
        {"resolution": "platform_correct"},
    ),
    ProtectedRequest("GET", "/sources", "/sources"),
    ProtectedRequest("POST", "/sources/{source}/sync", "/sources/google/sync"),
    ProtectedRequest("GET", "/config", "/config"),
)
"""Every endpoint except ``POST /auth/login`` and ``GET /healthz``, each requested once."""

CSRF_PROTECTED_REQUESTS: Final[tuple[ProtectedRequest, ...]] = (
    ProtectedRequest("POST", "/auth/logout", "/auth/logout"),
    ProtectedRequest(
        "POST", "/runs", "/runs", {"window_start": "2026-09-01", "window_end": "2026-09-14"}
    ),
    ProtectedRequest(
        "POST",
        "/decisions",
        "/decisions",
        {"subject": {"type": "campaign", "id": 1}, "patch": {"status": "paused"}},
    ),
    ProtectedRequest("POST", "/sources/{source}/sync", "/sources/google/sync"),
    ProtectedRequest(
        "POST",
        "/recommendations/{rec_id}/feedback",
        "/recommendations/1/feedback",
        {"kind": "snooze"},
    ),
)
"""Mutating requests sent by a signed-in client without a valid CSRF header (login apart)."""

# Tables the seed copy leaves out: Alembic's bookkeeping, and the admin row, which the
# `admin_user` fixture writes for every test before the copy is restored.
NOT_SEEDED_TABLES: Final = frozenset({"alembic_version", "app_user"})

SEED_BACKFILL_DAYS: Final = "28"
"""``BACKFILL_DAYS`` of the seed: the default window's length, far below the 180-day default."""

SUBPROCESS_MARKER: Final = "TEST_API_BOUNDARY="
"""Prefix of the boundary subprocess's result line, which shares stdout with the JSON logs."""


# ---------------------------------------------------------------------------------------------
# Local views of conftest fixtures (tests cannot import conftest)
# ---------------------------------------------------------------------------------------------


class AdminIdentity(Protocol):
    """The ``test_identity`` fixture: the test admin and the request conventions."""

    @property
    def email(self) -> str:
        """The admin email."""
        ...

    @property
    def password(self) -> str:
        """The admin password."""
        ...

    @property
    def csrf_headers(self) -> Mapping[str, str]:
        """``X-Requested-With: ads-engine``."""
        ...

    @property
    def session_cookie(self) -> str:
        """The session cookie name."""
        ...


class CapturedLogs(Protocol):
    """The ``captured_logs`` fixture: the product's JSON log lines written meanwhile."""

    @property
    def text(self) -> str:
        """Everything written so far, raw."""
        ...

    def configure(self, *, settings: Settings | None = None, level: str = "INFO") -> None:
        """(Re)configure logging onto the buffer, redacting the secrets of ``settings``."""
        ...

    def clear(self) -> None:
        """Forget everything written so far."""
        ...

    def __call__(self) -> list[dict[str, Any]]:
        """Return every line written so far, parsed."""
        ...


type LoginFunction = Callable[..., Awaitable[httpx.Response]]
"""The ``login`` fixture: ``await login(client, email=..., password=...)``."""


# ---------------------------------------------------------------------------------------------
# Logging isolation
# ---------------------------------------------------------------------------------------------

CONFIGURED_LOGGERS: Final = (*ads_logging._QUIET_LOGGERS, *ads_logging._ROUTED_LOGGERS)
"""Loggers ``configure_logging`` changes besides the root logger."""


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the logging configuration of ``create_app()`` from outliving each test.

    ``create_app()`` (called here and by the conftest ``api_client``) and the ``captured_logs``
    fixture run ``configure_logging``, which points the root handler at the test's captured
    stdout, removes pytest's own root handlers and turns structlog's logger caching on. Left in
    place, a module logger first used under it would keep that configuration for the rest of
    the session and bypass ``structlog.testing.capture_logs`` in later modules, and a later
    module that restores logging would flush a stream pytest has closed. As
    ``tests/unit/test_cli.py`` does, every configuration made during the test runs with caching
    off, and the handlers, levels, structlog configuration, redactor, last-resort handler and
    ``sys.excepthook`` are put back afterwards.
    """
    real_configure = ads_logging.configure_logging

    def configure_without_cache(**kwargs: Any) -> None:
        """Configure logging as the product does, but with structlog's logger caching off."""
        real_configure(**kwargs)
        structlog.configure(cache_logger_on_first_use=False)

    # create_app() binds the name at import; conftest imports it from the module at call time.
    monkeypatch.setattr(api_app, "configure_logging", configure_without_cache)
    monkeypatch.setattr(ads_logging, "configure_logging", configure_without_cache)
    # configure_logging installs its sys.excepthook; the hook and the one it replaced are put
    # back afterwards.
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(
        ads_logging._STATE, "previous_excepthook", ads_logging._STATE.previous_excepthook
    )
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root_level = root.level
    named: dict[str, tuple[int, list[logging.Handler], bool]] = {}
    for name in CONFIGURED_LOGGERS:
        logger = logging.getLogger(name)
        named[name] = (logger.level, list(logger.handlers), logger.propagate)
    structlog_was_configured = structlog.is_configured()
    structlog_config = structlog.get_config()
    state = ads_logging._STATE
    saved_redactor, saved_handler = state.redactor, state.handler
    saved_last_resort = logging.lastResort
    saved_output = (
        (saved_handler.stream, saved_handler.formatter) if saved_handler is not None else None
    )

    yield

    structlog.contextvars.clear_contextvars()
    logging.lastResort = saved_last_resort
    if saved_handler is not None and saved_output is not None:
        saved_handler.setStream(saved_output[0])
        saved_handler.setFormatter(saved_output[1])
    installed = state.handler
    if installed is not None and installed is not saved_handler:
        root.removeHandler(installed)
        installed.close()
    for handler in root_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(root_level)
    for name, (level, handlers, propagate) in named.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
        for handler in handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
    if structlog_was_configured:
        structlog.configure(**structlog_config)
    else:
        structlog.reset_defaults()
    state.redactor, state.handler = saved_redactor, saved_handler


@pytest.fixture(scope="module", autouse=True)
async def _analysis_process_stopped(anyio_backend: str) -> AsyncIterator[None]:
    """Stop the analysis process this module's ``POST /runs`` spawned, once the module ran."""
    del anyio_backend  # Requested so anyio runs this fixture; the backend is always asyncio.
    yield
    await runner.stop_analysis_process()


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def api(path: str) -> str:
    """Return ``path`` (relative to the API base, with or without a query) under ``/api/v1``."""
    return f"{API_PREFIX}{path}"


@asynccontextmanager
async def serve(
    app: FastAPI,
    base_url: str = "http://localhost",
    *,
    client_address: tuple[str, int] = ("127.0.0.1", 123),
) -> AsyncIterator[httpx.AsyncClient]:
    """Serve ``app`` in process to a fresh client, for tests that configure the app themselves.

    Args:
        app: The application, built by the test after any environment change it needs.
        base_url: The client's base URL; an ``https://`` one makes the cookie jar keep and send
            a ``Secure`` cookie.
        client_address: The ASGI peer address, which is the login rate-limit key.

    Yields:
        The client; no lifespan runs, exactly as for the conftest ``api_client``.
    """
    transport = httpx.ASGITransport(app=app, client=client_address)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        yield client


async def post_login(
    client: httpx.AsyncClient, identity: AdminIdentity, *, email: str, password: str
) -> httpx.Response:
    """Post one login attempt with the CSRF header, whatever its outcome."""
    return await client.post(
        api("/auth/login"),
        json={"email": email, "password": password},
        headers=dict(identity.csrf_headers),
    )


def assert_error(response: httpx.Response, status: int, code: str) -> schemas.ErrorResponse:
    """Assert ``response`` is the AAP 0.5.3 error envelope with ``status`` and ``code``.

    The body must be exactly ``{"error": {"code", "message"}}``: ``ErrorResponse`` ignores
    unknown keys, so the key sets are compared as well.

    Returns:
        The parsed envelope.
    """
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message"}, body
    parsed = schemas.ErrorResponse.model_validate(body)
    assert parsed.error.code == code, body
    assert parsed.error.message.strip(), body
    return parsed


def cookie_attributes(response: httpx.Response, name: str) -> dict[str, str]:
    """Return the attributes of the ``Set-Cookie`` header of cookie ``name``, keys lower case.

    A flag attribute such as ``HttpOnly`` maps to ``""``; the cookie's own value is under
    ``"value"``.
    """
    for header in response.headers.get_list("set-cookie"):
        pair, *attributes = (part.strip() for part in header.split(";"))
        cookie_name, _, value = pair.partition("=")
        if cookie_name.strip() != name:
            continue
        parsed = {"value": value}
        for attribute in attributes:
            key, _, attribute_value = attribute.partition("=")
            parsed[key.strip().lower()] = attribute_value.strip()
        return parsed
    pytest.fail(f"no Set-Cookie header for {name}: {response.headers.get_list('set-cookie')}")


def page_items(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every item of a ``RecommendationsPage`` body, section by section, group by group."""
    return [
        item
        for section in page["sections"]
        for group in section["groups"]
        for item in group["items"]
    ]


def read_config_file(name: str) -> dict[str, Any]:
    """Parse one of the repository's ``config/*.yaml`` files."""
    loaded = yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), name
    return loaded


# ---------------------------------------------------------------------------------------------
# The simulated seed, built once per module and restored for every test that needs it
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeedFacts:
    """The ids the route tests address in the seeded data; identical in every restored copy.

    Attributes:
        run_id: The succeeded default-window run.
        paused_campaign_id: The Google campaign the seed's user decision paused while the
            platform still shows it active: the subject of the open discrepancy.
        paused_decision_id: That user decision.
        other_campaign_id: Another active Google campaign that owns its budget, still at its
            baseline decision.
        discrepancy_id: The open ``status`` discrepancy of ``paused_campaign_id``.
        c0_item_id: The run's C0 item for that discrepancy.
        item_id: A non-C0 item of the run, which accepts feedback.
    """

    run_id: int
    paused_campaign_id: int
    paused_decision_id: int
    other_campaign_id: int
    discrepancy_id: int
    c0_item_id: int
    item_id: int


@dataclass(slots=True)
class SeedStore:
    """The module's copy of the seeded tables.

    Attributes:
        schema: The private schema holding one copy of each seeded table.
        tables: The copied tables of ``public``, parents before children.
        identity_tables: The copied tables with an ``id`` identity column.
        facts: The seed's ids; ``None`` until the first test that needs the seed built it.
    """

    schema: str
    tables: tuple[str, ...] = ()
    identity_tables: tuple[str, ...] = ()
    facts: SeedFacts | None = None


@dataclass(frozen=True, slots=True)
class Seeded:
    """What a route-schema test receives: the signed-in client on the seeded database.

    Attributes:
        client: The conftest ``authed_client``: signed in, CSRF header on every request.
        facts: The seed's ids.
    """

    client: httpx.AsyncClient
    facts: SeedFacts


# The C0 detection that follows an entity sync opens exactly this row for a campaign whose
# newest snapshot's status differs from its current decision: both sides hold only the
# compared field (``{"status": ...}``), and the row names the snapshot and the decision it
# compared. The entity-sync job and the ORM model that write it lie outside this module's
# dependencies, so the seed writes the same row in SQL; the domain functions that later read and
# resolve it (the C0 rule, GET /discrepancies, POST /resolve) are the production ones.
_OPEN_STATUS_DISCREPANCY: Final = text(
    """
    INSERT INTO discrepancy (
        source, dataset_id, subject_type, subject_id, kind, decided_value, platform_value,
        snapshot_id, decision_id, detected_at, state
    )
    SELECT c.source, c.dataset_id, 'campaign', c.id, 'status',
           jsonb_build_object('status', d.status), jsonb_build_object('status', s.status),
           s.id, d.id, :detected_at, 'open'
    FROM campaign AS c
    JOIN decision AS d
      ON d.id = :decision_id AND d.subject_type = 'campaign' AND d.subject_id = c.id
     AND d.dataset_id = c.dataset_id
    JOIN LATERAL (
        SELECT snapshot.id, snapshot.status
        FROM entity_snapshot AS snapshot
        WHERE snapshot.entity_type = 'campaign' AND snapshot.entity_id = c.id
          AND snapshot.dataset_id = c.dataset_id
        ORDER BY snapshot.captured_at DESC, snapshot.id DESC
        LIMIT 1
    ) AS s ON TRUE
    WHERE c.id = :campaign_id AND s.status IS DISTINCT FROM d.status
    RETURNING id
    """
)


def quoted(name: str) -> str:
    """Quote a catalogue identifier for SQL, doubling any embedded quote."""
    return '"' + name.replace('"', '""') + '"'


def store_seed(engine: Engine, store: SeedStore) -> None:
    """Copy every seeded table of ``public`` into ``store.schema``.

    Tables come from the database catalogue in foreign-key order (parents first), so a restore
    in the same order satisfies every key; the identifiers are the catalogue's own, never input.
    """
    inspector = inspect(engine)
    ordered = [
        name
        for name, _ in inspector.get_sorted_table_and_fkc_names(schema="public")
        if name is not None and name not in NOT_SEEDED_TABLES
    ]
    identity = [
        name
        for name in ordered
        if any(column["name"] == "id" for column in inspector.get_columns(name, schema="public"))
    ]
    schema = quoted(store.schema)
    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        for name in ordered:
            connection.execute(
                text(f"CREATE TABLE {schema}.{quoted(name)} AS TABLE public.{quoted(name)}")
            )
    store.tables = tuple(ordered)
    store.identity_tables = tuple(identity)


def restore_seed(engine: Engine, store: SeedStore) -> None:
    """Restore the seeded copy into the freshly truncated ``public`` tables.

    Rows keep their ids, so :class:`SeedFacts` holds in every copy; each identity sequence then
    continues after its highest restored id, so rows a test writes get fresh ids.
    """
    schema = quoted(store.schema)
    with engine.begin() as connection:
        for name in store.tables:
            table = quoted(name)
            connection.execute(
                text(f"INSERT INTO public.{table} SELECT * FROM {schema}.{table}")  # noqa: S608 - catalogue identifiers, never input
            )
        for name in store.identity_tables:
            table = quoted(name)
            connection.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence(:table, 'id'), max(id)) "  # noqa: S608 - catalogue identifiers, never input
                    f"FROM public.{table} HAVING count(*) > 0"
                ),
                {"table": f"public.{table}"},
            )


async def build_seed(client: httpx.AsyncClient, engine: Engine) -> SeedFacts:
    """Build the simulated world, the discrepancy and the default run; return their ids.

    1. ``bootstrap_sources(..., force_simulated=True)`` with ``DATA_MODE=simulated`` and a
       short backfill: every source builds, backfills and activates a simulated dataset, and
       the first entity sync of each writes the baseline decisions (AAP 0.11.2 first start).
       The environment change is undone, and the settings cache cleared, before anything else.
    2. ``POST /decisions`` pauses an active Google campaign that owns its budget: the user's
       decision, not yet applied in the platform.
    3. The ``status`` discrepancy the next entity sync's detection raises for it.
    4. One default-window run, which pins the C0 item and holds what depends on it.

    Args:
        client: The signed-in client of the test that builds the seed.
        engine: The synchronous engine on the test database.

    Returns:
        The seed's ids.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATA_MODE", "simulated")
        patch.setenv("BACKFILL_DAYS", SEED_BACKFILL_DAYS)
        get_settings.cache_clear()
        try:
            report = await mode.bootstrap_sources(get_settings(), force_simulated=True)
        finally:
            get_settings.cache_clear()
    get_settings.cache_clear()
    assert not report.failures, dict(report.failures)
    assert {source: resolved.mode for source, resolved in report.modes.items()} == dict.fromkeys(
        SOURCES, "simulated"
    )

    listed = await client.get(api("/decisions"), params={"subject_type": "campaign", "limit": 500})
    assert listed.status_code == 200, listed.text
    candidates = sorted(
        view["subject"]["id"]
        for view in listed.json()
        if view["subject"]["source"] == "google"
        and view["shape"] == "own_budget"
        and view["status"] == "active"
        and view["origin"] == "baseline"
    )
    assert len(candidates) >= 2, (
        f"the simulated world has too few own-budget campaigns: {listed.text}"
    )
    paused_campaign_id, other_campaign_id = candidates[0], candidates[1]

    decided = await client.post(
        api("/decisions"),
        json={
            "subject": {"type": "campaign", "id": paused_campaign_id},
            "patch": {"status": "paused"},
            "note": "Paused pending a landing-page fix",
        },
    )
    assert decided.status_code == 201, decided.text
    paused_decision_id = int(decided.json()["id"])

    with engine.begin() as connection:
        discrepancy_id = int(
            connection.execute(
                _OPEN_STATUS_DISCREPANCY,
                {
                    "campaign_id": paused_campaign_id,
                    "decision_id": paused_decision_id,
                    "detected_at": datetime.now(UTC),
                },
            ).scalar_one()
        )

    now = datetime.now(UTC)
    window = await runner.default_window(now=now)
    run = await runner.run_analysis(window, "default", "scheduled", reason="test_api seed", now=now)
    assert run.status == "succeeded"

    page = await client.get(api("/recommendations"))
    assert page.status_code == 200, page.text
    items = page_items(page.json())
    c0_items = [item for item in items if item["rule_id"] == "C0"]
    other_items = [item for item in items if item["rule_id"] != "C0"]
    assert len(c0_items) == 1, [item["rule_id"] for item in items]
    assert other_items, "the seeded default run produced no item other than C0"
    return SeedFacts(
        run_id=run.id,
        paused_campaign_id=paused_campaign_id,
        paused_decision_id=paused_decision_id,
        other_campaign_id=other_campaign_id,
        discrepancy_id=discrepancy_id,
        c0_item_id=int(c0_items[0]["id"]),
        item_id=int(other_items[0]["id"]),
    )


@pytest.fixture(scope="module")
def seed_store(sync_engine: Engine) -> Iterator[SeedStore]:
    """The module's seed copy, built lazily by :func:`seeded`; its schema is dropped at the end."""
    store = SeedStore(schema=f"test_api_seed_{uuid.uuid4().hex}")
    try:
        yield store
    finally:
        with sync_engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted(store.schema)} CASCADE"))


@pytest.fixture
async def seeded(
    authed_client: httpx.AsyncClient, sync_engine: Engine, seed_store: SeedStore
) -> Seeded:
    """The signed-in client on the seeded database (:func:`build_seed`).

    The first test that asks builds the seed and copies it; every later one restores the copy
    into the tables ``db`` has just emptied. ``authed_client`` signs in first, so the admin row
    it needs is in place and is never part of the copy.
    """
    facts = seed_store.facts
    if facts is None:
        facts = await build_seed(authed_client, sync_engine)
        store_seed(sync_engine, seed_store)
        seed_store.facts = facts
    else:
        restore_seed(sync_engine, seed_store)
    return Seeded(client=authed_client, facts=facts)


# ---------------------------------------------------------------------------------------------
# Authentication and session (AAP 0.5.3 "Authentication"; AAP 0.3.2 "Security architecture")
# ---------------------------------------------------------------------------------------------


async def test_login_me_logout_flow(
    api_client: httpx.AsyncClient, admin_user: AppUser, test_identity: AdminIdentity
) -> None:
    """AAP 0.5.3: login sets the session, ``/auth/me`` reads it, logout ends it.

    Login answers 200 ``Me`` and sets the cookie; ``GET /auth/me`` answers the same ``Me``;
    ``POST /auth/logout`` answers 200 ``Me`` (the route's documented response) and expires the
    cookie; afterwards ``GET /auth/me`` is 401 ``unauthenticated``.
    """
    del admin_user
    signed_in = await post_login(
        api_client, test_identity, email=test_identity.email, password=test_identity.password
    )
    assert signed_in.status_code == 200, signed_in.text
    assert schemas.Me.model_validate(signed_in.json()) == schemas.Me(email=test_identity.email)
    assert test_identity.session_cookie in api_client.cookies

    me = await api_client.get(api("/auth/me"))
    assert me.status_code == 200, me.text
    assert me.json() == {"email": test_identity.email}

    logged_out = await api_client.post(
        api("/auth/logout"), headers=dict(test_identity.csrf_headers)
    )
    assert logged_out.status_code == 200, logged_out.text
    assert schemas.Me.model_validate(logged_out.json()).email == test_identity.email
    expired = cookie_attributes(logged_out, security.SESSION_COOKIE)
    assert "1970" in expired.get("expires", ""), expired
    assert test_identity.session_cookie not in api_client.cookies

    assert_error(await api_client.get(api("/auth/me")), 401, "unauthenticated")


async def test_invalid_credentials_401(
    api_client: httpx.AsyncClient, admin_user: AppUser, test_identity: AdminIdentity
) -> None:
    """AAP 0.5.3: a wrong password and an unknown email get one indistinguishable 401.

    Both are 401 ``invalid_credentials`` with the same message, and neither sets a session.
    """
    del admin_user
    wrong_password = await post_login(
        api_client, test_identity, email=test_identity.email, password="not-the-admin-password"
    )
    unknown_email = await post_login(
        api_client, test_identity, email="nobody@example.org", password=test_identity.password
    )
    first = assert_error(wrong_password, 401, "invalid_credentials")
    second = assert_error(unknown_email, 401, "invalid_credentials")
    assert first.error.message == second.error.message
    assert wrong_password.headers.get_list("set-cookie") == []
    assert unknown_email.headers.get_list("set-cookie") == []
    assert test_identity.session_cookie not in api_client.cookies


async def test_session_cookie_flags_local_http(
    api_client: httpx.AsyncClient, admin_user: AppUser, login: LoginFunction
) -> None:
    """AAP 0.3.2: the session cookie is HttpOnly, SameSite=Lax, 12 h, and not Secure on local HTTP.

    With the local default ``APP_BASE_URL=http://localhost:8080`` the cookie must work over
    plain HTTP, so it carries no ``Secure`` attribute.
    """
    del admin_user
    assert get_settings().app_base_url == "http://localhost:8080"
    response = await login(api_client)
    attributes = cookie_attributes(response, security.SESSION_COOKIE)
    assert security.SESSION_COOKIE == "ads_engine_session"
    assert "httponly" in attributes
    assert attributes["samesite"].lower() == "lax"
    assert attributes["max-age"] == str(12 * 3600) == str(security.SESSION_MAX_AGE)
    assert attributes["path"] == "/"
    assert "secure" not in attributes


async def test_session_cookie_secure_for_https_base_url(
    set_env: Callable[..., None], admin_user: AppUser, login: LoginFunction
) -> None:
    """AAP 0.3.2: an ``https://`` ``APP_BASE_URL`` marks the session cookie ``Secure``.

    The application is built after the environment change, and the client speaks ``https``, so
    its cookie jar keeps the Secure cookie and sends it back.
    """
    del admin_user
    set_env(APP_BASE_URL="https://ads.example.com")
    app = create_app()
    async with serve(app, "https://ads.example.com") as client:
        response = await login(client)
        attributes = cookie_attributes(response, security.SESSION_COOKIE)
        assert "secure" in attributes
        assert "httponly" in attributes
        assert attributes["samesite"].lower() == "lax"
        assert attributes["max-age"] == str(security.SESSION_MAX_AGE)
        me = await client.get(api("/auth/me"))
        assert me.status_code == 200, me.text


async def test_session_expires_after_12_hours(
    admin_user: AppUser, login: LoginFunction, test_identity: AdminIdentity
) -> None:
    """AAP 0.3.2: a session lasts 12 h from sign-in, measured on the request clock.

    The clock is ``deps.get_now``, overridden, never slept on. The session signed in at
    :data:`SIGN_IN` still opens ``/auth/me`` one second before 12 h; one second after 12 h the
    same cookie is refused with 401 ``unauthenticated`` on every protected route, and the
    response expires it.
    """
    del admin_user
    clock = [SIGN_IN]
    app = create_app()
    app.dependency_overrides[deps.get_now] = lambda: clock[0]
    async with serve(app) as client:
        await login(client)
        token = client.cookies[test_identity.session_cookie]
        cookie = {"Cookie": f"{test_identity.session_cookie}={token}"}

        clock[0] = SIGN_IN + timedelta(hours=12) - timedelta(seconds=1)
        client.cookies.clear()
        still_valid = await client.get(api("/auth/me"), headers=cookie)
        assert still_valid.status_code == 200, still_valid.text

        clock[0] = SIGN_IN + timedelta(hours=12) + timedelta(seconds=1)
        for path in ("/auth/me", "/runs/latest", "/sources", "/config"):
            client.cookies.clear()
            expired = await client.get(api(path), headers=cookie)
            assert_error(expired, 401, "unauthenticated")
            cleared = cookie_attributes(expired, security.SESSION_COOKIE)
            assert "1970" in cleared.get("expires", ""), cleared
        client.cookies.clear()
        logout = await client.post(
            api("/auth/logout"), headers={**cookie, **test_identity.csrf_headers}
        )
        assert_error(logout, 401, "unauthenticated")


# ---------------------------------------------------------------------------------------------
# Rate limit, CSRF and errors (AAP 0.5.3 "Rate limits" and status codes)
# ---------------------------------------------------------------------------------------------


async def test_sixth_login_attempt_in_a_minute_is_429(
    admin_user: AppUser, test_identity: AdminIdentity
) -> None:
    """AAP 0.5.3: login is limited to 5 attempts per minute per IP; the 6th gets 429.

    Every attempt counts, a successful one included: four wrong passwords and one correct
    sign-in exhaust the limit, so the 6th attempt is 429 ``rate_limited`` with ``Retry-After``
    even with the right password, while another client IP is still served. Once the oldest
    attempt leaves the 60 s window (the limiter's injectable clock, never a sleep), a login
    succeeds again.
    """
    del admin_user
    assert (security.LOGIN_MAX_ATTEMPTS, security.LOGIN_WINDOW_SECONDS) == (5, 60)
    ticks = [0.0]
    app = create_app()
    app.state.login_limiter = security.LoginRateLimiter(clock=lambda: ticks[0])
    email, password = test_identity.email, test_identity.password
    async with serve(app) as client:
        for _ in range(security.LOGIN_MAX_ATTEMPTS - 1):
            wrong = await post_login(client, test_identity, email=email, password="wrong-password")
            assert_error(wrong, 401, "invalid_credentials")
            ticks[0] += 1.0
        fifth = await post_login(client, test_identity, email=email, password=password)
        assert fifth.status_code == 200, fifth.text
        ticks[0] += 1.0

        sixth = await post_login(client, test_identity, email=email, password=password)
        assert_error(sixth, 429, "rate_limited")
        # The oldest attempt (t=0) leaves the window at t=60; the 6th came at t=5.
        assert sixth.headers["Retry-After"] == str(security.LOGIN_WINDOW_SECONDS - 5)

        async with serve(app, client_address=("192.0.2.10", 50000)) as other_ip:
            elsewhere = await post_login(other_ip, test_identity, email=email, password=password)
            assert elsewhere.status_code == 200, elsewhere.text

        ticks[0] = float(security.LOGIN_WINDOW_SECONDS)
        client.cookies.clear()
        again = await post_login(client, test_identity, email=email, password=password)
        assert again.status_code == 200, again.text
        assert test_identity.session_cookie in client.cookies


@pytest.mark.parametrize(
    "csrf_value",
    [None, "XMLHttpRequest", "ADS-ENGINE", ""],
    ids=["missing", "other-value", "wrong-case", "empty"],
)
async def test_mutating_request_without_csrf_header_is_403(
    api_client: httpx.AsyncClient,
    admin_user: AppUser,
    login: LoginFunction,
    test_identity: AdminIdentity,
    csrf_value: str | None,
) -> None:
    """AAP 0.5.3: mutating requests require ``X-Requested-With: ads-engine``; GETs do not.

    A signed-in client sends every mutating route, ``POST /auth/login`` included, without the
    header or with another value: each is 403 ``csrf_header_missing`` and changes nothing (the
    sync request is not queued, logout does not end the session). ``GET`` routes answer 200
    without the header.
    """
    del admin_user
    await login(api_client)
    headers = {} if csrf_value is None else {security.CSRF_HEADER: csrf_value}

    refused_login = await api_client.post(
        api("/auth/login"),
        json={"email": test_identity.email, "password": test_identity.password},
        headers=headers,
    )
    assert_error(refused_login, 403, "csrf_header_missing")
    for request in CSRF_PROTECTED_REQUESTS:
        response = await api_client.request(
            request.method, api(request.path), json=request.body, headers=headers
        )
        assert_error(response, 403, "csrf_header_missing")

    me = await api_client.get(api("/auth/me"))
    assert me.status_code == 200, me.text
    sources = await api_client.get(api("/sources"))
    assert sources.status_code == 200, sources.text
    assert [entry["sync_pending"] for entry in sources.json()] == [False, False, False]


@pytest.mark.parametrize(
    "request_spec", PROTECTED_REQUESTS, ids=lambda spec: f"{spec.method} {spec.path}"
)
async def test_protected_routes_401_without_session(
    api_client: httpx.AsyncClient,
    test_identity: AdminIdentity,
    request_spec: ProtectedRequest,
) -> None:
    """AAP 0.5.3: every endpoint except login and healthz requires the session cookie.

    The request carries the CSRF header (and a valid body where the route takes one), so the
    401 ``unauthenticated`` is the session check's answer and not another guard's.
    """
    response = await api_client.request(
        request_spec.method,
        api(request_spec.path),
        json=request_spec.body,
        headers=dict(test_identity.csrf_headers),
    )
    assert_error(response, 401, "unauthenticated")
    assert test_identity.session_cookie not in api_client.cookies


async def test_error_shape_on_every_error_status(
    admin_user: AppUser,
    login: LoginFunction,
    test_identity: AdminIdentity,
) -> None:
    """AAP 0.5.3: 401, 403, 404, 409, 422 and 429 all render ``{"error": {"code", "message"}}``.

    Each body is exactly the envelope, with no other key at either level: 401 without a
    session, 403 without the CSRF header, 404 for an unknown run and an unknown path, 409 for a
    second pending sync request, 422 for an invalid body and 429 for the 6th login attempt.
    The login limiter runs on a fixed injected clock, so all six attempts count at one instant:
    the 429 never depends on how long the requests and password checks take, and its
    ``Retry-After`` is the whole window.
    """
    del admin_user
    app = create_app()
    app.state.login_limiter = security.LoginRateLimiter(clock=lambda: 0.0)
    csrf = dict(test_identity.csrf_headers)
    async with serve(app) as client:
        assert_error(await client.get(api("/auth/me")), 401, "unauthenticated")
        assert_error(await client.post(api("/runs"), json={}), 403, "csrf_header_missing")

        await login(client)  # attempt 1 of the window
        assert_error(await client.get(api("/runs/999999")), 404, "not_found")
        assert_error(await client.get(api("/no-such-route")), 404, "not_found")
        first_sync = await client.post(api("/sources/google/sync"), headers=csrf)
        assert first_sync.status_code == 202, first_sync.text
        assert_error(
            await client.post(api("/sources/google/sync"), headers=csrf), 409, "sync_pending"
        )
        invalid = await client.post(api("/runs"), json={"window_start": "soon"}, headers=csrf)
        assert_error(invalid, 422, "validation_error")

        for _ in range(security.LOGIN_MAX_ATTEMPTS - 1):  # attempts 2 to 5
            wrong = await post_login(
                client, test_identity, email=test_identity.email, password="wrong-password"
            )
            assert_error(wrong, 401, "invalid_credentials")
        limited = await post_login(
            client, test_identity, email=test_identity.email, password=test_identity.password
        )
        assert_error(limited, 429, "rate_limited")
        # Every attempt counted at t=0, so the oldest leaves the window a full window later.
        assert limited.headers["Retry-After"] == str(security.LOGIN_WINDOW_SECONDS)


async def test_healthz_without_auth(api_client: httpx.AsyncClient) -> None:
    """AAP 0.5.3: ``GET /healthz`` needs no session and reports the database reachable."""
    response = await api_client.get(api("/healthz"))
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}
    assert response.headers.get_list("set-cookie") == []


# ---------------------------------------------------------------------------------------------
# Bounded waits: /healthz and the request deadline (AAP 0.5.3 "GET /healthz", error envelope)
# ---------------------------------------------------------------------------------------------

LOG_FIELDS: Final = frozenset({"event", "level", "logger", "timestamp"})
"""The fields every product log line carries besides its own."""

SHORT_DEADLINE_S: Final = 0.5
"""The request deadline the deadline tests patch in, instead of the production 45 s."""

UNWIND_S: Final = 1.5
"""How long a slow-to-unwind application keeps cleaning up after its cancellation.

It stands in for psycopg, which on a server that stopped answering spends up to about 10 s
cancelling the query of a request the deadline cancelled (a cancel request, then a wait).
"""


@asynccontextmanager
async def silent_server() -> AsyncIterator[tuple[int, list[asyncio.StreamWriter]]]:
    """Serve TCP on a free loopback port that accepts every connection and never answers.

    To libpq this is a paused or partitioned PostgreSQL: the kernel completes the TCP
    handshake, but no reply to the startup message ever comes.

    Yields:
        The port, and the server side of every connection accepted so far; each is closed,
        with the server, on exit.
    """
    accepted: list[asyncio.StreamWriter] = []

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Keep the connection open and silent until the server shuts down."""
        del reader
        accepted.append(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], accepted
    finally:
        for writer in accepted:
            writer.close()
        server.close()
        await server.wait_closed()


@contextmanager
def refusing_port() -> Iterator[int]:
    """Hold a loopback port that is bound but not listening, so every connection is refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]


async def point_database_at(set_env: Callable[..., None], port: int, password: str) -> None:
    """Dispose this test's engines, then point ``DATABASE_URL`` at loopback ``port``."""
    await db_session.dispose_engine()
    set_env(DATABASE_URL=f"postgresql+psycopg://ads:{password}@127.0.0.1:{port}/ads_engine")


def events_named(captured_logs: CapturedLogs, event: str) -> list[dict[str, Any]]:
    """Return the captured log records of ``event``."""
    return [record for record in captured_logs() if record.get("event") == event]


async def settle_unwinds(unwinding: set[asyncio.Task[None]]) -> None:
    """Wait until every request the deadline let go has finished unwinding and been released.

    Such a request keeps unwinding after its 503; waiting for it keeps the assertions that
    follow deterministic and leaves no task pending when the test's event loop closes.

    Args:
        unwinding: The deadline middleware's set, ``app.state.request_unwinds`` for an
            application ``create_app`` built.
    """
    if unwinding:
        await asyncio.wait(set(unwinding), timeout=UNWIND_S + 5.0)
    # Yield once more, so the done-callbacks that release each request have all run.
    await asyncio.sleep(0)
    assert unwinding == set()


async def test_healthz_503_within_its_bound_when_the_database_never_answers(
    api_client: httpx.AsyncClient, set_env: Callable[..., None], captured_logs: CapturedLogs
) -> None:
    """AAP 0.5.3: a database that stopped answering makes ``/healthz`` a prompt 503.

    The ping runs on a fresh connection bounded by 3 s, so the answer is 503
    ``database_unavailable`` after about 3 s, never psycopg's 130 s connect wait. It is logged
    once as the WARNING ``health.database_unavailable`` with the error class and the bound only,
    never the exception text, the URL or its password.
    """
    password = "-".join(("pw", "healthz", "w7k"))
    async with silent_server() as (port, accepted):
        await point_database_at(set_env, port, password)
        captured_logs.configure(settings=get_settings())
        captured_logs.clear()
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = await api_client.get(api("/healthz"))
        elapsed = loop.time() - started
        assert len(accepted) == 1

    error = assert_error(response, 503, "database_unavailable")
    assert error.error.message == "Database unavailable"
    assert health._PING_TIMEOUT_S == 3.0
    assert health._PING_TIMEOUT_S - 0.1 <= elapsed < health._PING_TIMEOUT_S + 2.0
    [record] = events_named(captured_logs, "health.database_unavailable")
    assert set(record) == LOG_FIELDS | {"error_type", "timeout_s"}
    assert record["level"] == "warning"
    assert record["logger"] == "ads_engine.api.routes.health"
    assert record["error_type"] == "TimeoutError"
    assert record["timeout_s"] == health._PING_TIMEOUT_S
    assert events_named(captured_logs, "api.request_timeout") == []
    assert password not in captured_logs.text


async def test_healthz_503_at_once_when_the_database_refuses(
    api_client: httpx.AsyncClient, set_env: Callable[..., None], captured_logs: CapturedLogs
) -> None:
    """A refused connection is 503 ``database_unavailable`` without waiting for the bound."""
    password = "-".join(("pw", "refused", "h3q"))
    with refusing_port() as port:
        await point_database_at(set_env, port, password)
        captured_logs.configure(settings=get_settings())
        captured_logs.clear()
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = await api_client.get(api("/healthz"))
        elapsed = loop.time() - started

    assert_error(response, 503, "database_unavailable")
    assert elapsed < health._PING_TIMEOUT_S
    [record] = events_named(captured_logs, "health.database_unavailable")
    assert set(record) == LOG_FIELDS | {"error_type", "timeout_s"}
    assert record["error_type"] == "OperationalError"
    assert password not in captured_logs.text


async def test_database_backed_request_past_the_deadline_is_503_request_timeout(
    db: object,
    monkeypatch: pytest.MonkeyPatch,
    set_env: Callable[..., None],
    captured_logs: CapturedLogs,
    test_identity: AdminIdentity,
) -> None:
    """A request waiting on a database that never answers ends at the request deadline.

    ``POST /auth/login`` reads ``app_user``; with the database silent the read never completes,
    so the deadline cancels it. The answer is the 503 ``request_timeout`` envelope with no
    session cookie, and one ``api.request_timeout`` WARNING names the method, the path and the
    deadline, never the body.
    """
    del db  # Requested for the per-test engine lifecycle: the engine is disposed on teardown.
    monkeypatch.setattr(api_app, "_REQUEST_TIMEOUT_S", SHORT_DEADLINE_S)
    password = "-".join(("pw", "deadline", "j4m"))
    async with silent_server() as (port, accepted):
        await point_database_at(set_env, port, password)
        app = create_app()
        captured_logs.configure(settings=get_settings())
        captured_logs.clear()
        loop = asyncio.get_running_loop()
        async with serve(app) as client:
            started = loop.time()
            response = await post_login(
                client, test_identity, email=test_identity.email, password=test_identity.password
            )
            elapsed = loop.time() - started
            assert test_identity.session_cookie not in client.cookies
        # The cancelled login unwinds after its 503; it ends while the server is still silent.
        await settle_unwinds(app.state.request_unwinds)
        assert len(accepted) == 1

    error = assert_error(response, 503, "request_timeout")
    assert error.error.message == "The request did not complete in time"
    assert SHORT_DEADLINE_S - 0.05 <= elapsed < SHORT_DEADLINE_S + 3.0
    [record] = events_named(captured_logs, "api.request_timeout")
    assert set(record) == LOG_FIELDS | {"method", "path", "timeout_s", "response_started"}
    assert record["level"] == "warning"
    assert record["logger"] == "ads_engine.api.app"
    assert (record["method"], record["path"]) == ("POST", api("/auth/login"))
    assert record["timeout_s"] == SHORT_DEADLINE_S
    assert record["response_started"] is False
    for value in (password, test_identity.email, test_identity.password):
        assert value not in captured_logs.text


async def test_request_deadline_answers_only_a_request_still_running(
    monkeypatch: pytest.MonkeyPatch, captured_logs: CapturedLogs
) -> None:
    """Only a request still running at the deadline is cancelled and answered 503.

    Under a 0.5 s deadline a route that never finishes is cancelled, answered 503
    ``request_timeout`` and logged without its query string. A fast route answers 200 as usual,
    and a ``TimeoutError`` the route raised itself stays an ordinary 500 ``internal_error``.
    """
    monkeypatch.setattr(api_app, "_REQUEST_TIMEOUT_S", SHORT_DEADLINE_S)
    app = create_app()
    cancelled = asyncio.Event()

    async def stalled() -> dict[str, bool]:
        """Wait for an event nobody sets, noting the cancellation."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"finished": True}

    async def fast() -> dict[str, bool]:
        """Answer at once."""
        return {"finished": True}

    async def own_timeout() -> dict[str, bool]:
        """Fail with a TimeoutError of the route's own, well inside the deadline."""
        raise TimeoutError("the route's own timeout")

    for path, endpoint in (
        ("/test-stalled", stalled),
        ("/test-fast", fast),
        ("/test-own-timeout", own_timeout),
    ):
        app.add_api_route(api(path), endpoint, methods=["GET"])
    captured_logs.configure(settings=get_settings())
    captured_logs.clear()
    marker = "-".join(("query", "marker", "0451"))
    # Starlette re-raises an unhandled error after answering 500; the client must not.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        stalled_response = await client.get(api(f"/test-stalled?marker={marker}"))
        fast_response = await client.get(api("/test-fast"))
        own_response = await client.get(api("/test-own-timeout"))
    # The 503 is sent at the deadline; the stalled route's cancellation unwinds after it.
    await settle_unwinds(app.state.request_unwinds)

    assert_error(stalled_response, 503, "request_timeout")
    assert cancelled.is_set()
    assert fast_response.status_code == 200, fast_response.text
    assert fast_response.json() == {"finished": True}
    assert_error(own_response, 500, "internal_error")
    [record] = events_named(captured_logs, "api.request_timeout")
    assert (record["method"], record["path"]) == ("GET", api("/test-stalled"))
    assert record["response_started"] is False
    assert marker not in captured_logs.text
    unhandled = events_named(captured_logs, "api.unhandled_error")
    assert [record["error_type"] for record in unhandled] == ["TimeoutError"]


def http_scope(path: str) -> Scope:
    """Return a minimal ASGI ``http`` scope for ``GET path?secret=1``."""
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"secret=1",
        "headers": [],
    }


async def receive_nothing() -> Message:
    """An ASGI receive channel with an empty request body."""
    return {"type": "http.request", "body": b"", "more_body": False}


async def stall_forever(scope: Scope, receive: Receive, send: Send) -> None:
    """An ASGI application that never answers."""
    await asyncio.Event().wait()


async def test_request_deadline_never_starts_a_second_response(
    captured_logs: CapturedLogs,
) -> None:
    """A deadline passing after the response started is logged, and nothing more is sent.

    Driven at the ASGI level: a test client cannot read a response the application abandons,
    which a real server answers by closing the connection.
    """
    captured_logs.configure(settings=get_settings())
    captured_logs.clear()
    start: Message = {"type": "http.response.start", "status": 200, "headers": []}

    async def started_then_stalled(scope: Scope, receive: Receive, send: Send) -> None:
        """Start a response, then never finish it."""
        await send(start)
        await asyncio.Event().wait()

    sent: list[Message] = []

    async def collect(message: Message) -> None:
        """Record what the middleware sends."""
        sent.append(message)

    middleware = api_app._RequestDeadlineMiddleware(started_then_stalled, timeout=0.2)
    await middleware(http_scope("/api/v1/stream"), receive_nothing, collect)
    await settle_unwinds(middleware.unwinding)

    assert sent == [start]
    [record] = events_named(captured_logs, "api.request_timeout")
    assert (record["method"], record["path"]) == ("GET", "/api/v1/stream")
    assert record["timeout_s"] == 0.2
    assert record["response_started"] is True


async def test_request_deadline_leaves_other_scopes_and_outside_cancellation_alone() -> None:
    """The deadline applies to ``http`` scopes only, and never swallows a foreign cancellation.

    A lifespan scope outlives a 0.05 s deadline untouched. An ``http`` request cancelled from
    outside (a client disconnect, a server shutdown) well before its deadline ends cancelled,
    with nothing sent.
    """
    finished: list[str] = []

    async def slow_lifespan(scope: Scope, receive: Receive, send: Send) -> None:
        """Take longer than the deadline, then finish."""
        await asyncio.sleep(0.2)
        finished.append(scope["type"])

    sent: list[Message] = []

    async def collect(message: Message) -> None:
        """Record what the middleware sends."""
        sent.append(message)

    await api_app._RequestDeadlineMiddleware(slow_lifespan, timeout=0.05)(
        {"type": "lifespan"}, receive_nothing, collect
    )
    assert finished == ["lifespan"]

    middleware = api_app._RequestDeadlineMiddleware(stall_forever, timeout=30.0)
    task = asyncio.create_task(middleware(http_scope("/api/v1/x"), receive_nothing, collect))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert sent == []
    await settle_unwinds(middleware.unwinding)


async def test_request_deadline_answers_at_the_deadline_while_the_request_unwinds(
    captured_logs: CapturedLogs,
) -> None:
    """The 503 goes out at the deadline, not after the cancelled request's cleanup.

    With a database that stopped answering, psycopg spends up to about 10 s cancelling the query
    of a request the deadline cancelled. Here the application keeps cleaning up for 1.5 s after
    its cancellation under a 0.2 s deadline: the 503 ``request_timeout`` start and body are sent,
    and ``api.request_timeout`` is logged, at the deadline, while the request is still held in
    the middleware's ``unwinding`` set. The unwind completes afterwards, and the response the
    application tries to start during it is dropped, never forwarded to the finished answer.
    """
    captured_logs.configure(settings=get_settings())
    captured_logs.clear()
    loop = asyncio.get_running_loop()
    deadline_s = 0.2
    late: Message = {"type": "http.response.start", "status": 500, "headers": []}
    unwound_at: list[float] = []

    async def slow_to_unwind(scope: Scope, receive: Receive, send: Send) -> None:
        """Stall; once cancelled, clean up for a while, try to answer, then end cancelled."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(UNWIND_S)
            await send(late)
            unwound_at.append(loop.time())
            raise

    sent: list[tuple[float, Message]] = []

    async def collect(message: Message) -> None:
        """Record what the middleware sends, and when."""
        sent.append((loop.time(), message))

    unwinding: set[asyncio.Task[None]] = set()
    middleware = api_app._RequestDeadlineMiddleware(
        slow_to_unwind, timeout=deadline_s, unwinding=unwinding
    )
    started = loop.time()
    await middleware(http_scope("/api/v1/slow"), receive_nothing, collect)
    returned_after = loop.time() - started
    timeout_records = events_named(captured_logs, "api.request_timeout")
    still_unwinding = len(unwinding)
    unwound_before_return = list(unwound_at)
    await settle_unwinds(unwinding)

    assert deadline_s - 0.05 <= returned_after < deadline_s + 0.5
    assert unwound_before_return == []
    assert still_unwinding == 1
    [(start_at, start), (body_at, body)] = sent
    assert start["type"] == "http.response.start"
    assert start["status"] == 503
    assert body["type"] == "http.response.body"
    assert json.loads(body["body"]) == {
        "error": {"code": "request_timeout", "message": "The request did not complete in time"}
    }
    assert start_at - started < deadline_s + 0.5
    assert body_at - started < deadline_s + 0.5
    [unwound] = unwound_at
    assert unwound - started >= deadline_s + UNWIND_S - 0.05
    assert late not in [message for _, message in sent]
    [record] = timeout_records
    assert set(record) == LOG_FIELDS | {"method", "path", "timeout_s", "response_started"}
    assert record["level"] == "warning"
    assert (record["method"], record["path"]) == ("GET", "/api/v1/slow")
    assert record["timeout_s"] == deadline_s
    assert record["response_started"] is False
    assert events_named(captured_logs, "api.request_timeout") == [record]
    assert events_named(captured_logs, "api.request_unwind_failed") == []


@pytest.mark.parametrize("owner", [None, "analysis_run_failed"])
async def test_request_deadline_logs_a_failed_unwind_once_without_its_text(
    owner: str | None, captured_logs: CapturedLogs
) -> None:
    """A request that fails while unwinding past the deadline is logged once, never its text.

    The application raises a ``RuntimeError`` carrying a marker instead of ending cancelled. The
    503 is still sent at the deadline, and the failure is logged once as
    ``api.request_unwind_failed`` with the method, the path without its query string and the
    error class: at ERROR level, or as a WARNING naming ``logged_as`` when its owner already
    logged it. The marker appears in no log line, and asyncio never reports the task's
    exception as unretrieved.
    """
    captured_logs.configure(settings=get_settings())
    captured_logs.clear()
    marker = "-".join(("unwind", "marker", "8812"))
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def report(_: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """Record what asyncio reports, such as an exception never retrieved."""
        reported.append(context)

    async def fails_while_unwinding(scope: Scope, receive: Receive, send: Send) -> None:
        """Stall; once cancelled, fail with an error of its own."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            error = RuntimeError(marker)
            if owner is not None:
                ads_logging.mark_logged(error, owner)
            raise error from None

    sent: list[Message] = []

    async def collect(message: Message) -> None:
        """Record what the middleware sends."""
        sent.append(message)

    unwinding: set[asyncio.Task[None]] = set()
    middleware = api_app._RequestDeadlineMiddleware(
        fails_while_unwinding, timeout=0.2, unwinding=unwinding
    )
    loop.set_exception_handler(report)
    try:
        await middleware(http_scope("/api/v1/failing"), receive_nothing, collect)
        [request] = unwinding
        await settle_unwinds(unwinding)
        assert request.done()
        assert not request.cancelled()
        # An unretrieved exception is reported when its task is collected.
        del request
        gc.collect()
    finally:
        loop.set_exception_handler(previous_handler)

    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 503
    [record] = events_named(captured_logs, "api.request_unwind_failed")
    expected = {"method", "path", "error_type"} | ({"logged_as"} if owner is not None else set())
    assert set(record) == LOG_FIELDS | expected
    assert record["level"] == ("error" if owner is None else "warning")
    assert record["logger"] == "ads_engine.api.app"
    assert (record["method"], record["path"]) == ("GET", "/api/v1/failing")
    assert record["error_type"] == "RuntimeError"
    assert record.get("logged_as") == owner
    assert marker not in captured_logs.text
    assert "secret=1" not in captured_logs.text
    assert [context for context in reported if marker in str(context.get("exception"))] == []


async def test_request_deadline_outside_cancellation_waits_for_the_unwind(
    captured_logs: CapturedLogs,
) -> None:
    """An outside cancellation propagates once the request has unwound, with nothing sent.

    One cancellation from outside (a client disconnect, a server shutdown) is held until the
    application has finished unwinding, as it would be had the application run in the caller's
    task, so a graceful shutdown still covers the cleanup. A second cancellation ends the
    middleware at once, while the request stays held in ``unwinding`` until it ends; its
    outcome, a failure here, is then retrieved and logged once.
    """
    captured_logs.configure(settings=get_settings())
    captured_logs.clear()
    marker = "-".join(("outside", "marker", "3307"))
    release = asyncio.Event()
    unwound: list[str] = []

    async def unwinds_on_release(scope: Scope, receive: Receive, send: Send) -> None:
        """Stall; once cancelled, unwind only when released, failing on ``/twice``."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            unwound.append(scope["path"])
            if scope["path"] == "/api/v1/twice":
                raise RuntimeError(marker) from None
            raise

    sent: list[Message] = []

    async def collect(message: Message) -> None:
        """Record what the middleware sends."""
        sent.append(message)

    unwinding: set[asyncio.Task[None]] = set()
    middleware = api_app._RequestDeadlineMiddleware(
        unwinds_on_release, timeout=30.0, unwinding=unwinding
    )

    once = asyncio.create_task(middleware(http_scope("/api/v1/once"), receive_nothing, collect))
    await asyncio.sleep(0.05)
    once.cancel()
    await asyncio.sleep(0.05)
    assert not once.done()
    assert len(unwinding) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await once
    assert once.cancelled()
    assert unwound == ["/api/v1/once"]
    await settle_unwinds(unwinding)

    release.clear()
    twice = asyncio.create_task(middleware(http_scope("/api/v1/twice"), receive_nothing, collect))
    await asyncio.sleep(0.05)
    twice.cancel()
    await asyncio.sleep(0.05)
    twice.cancel()
    with pytest.raises(asyncio.CancelledError):
        await twice
    assert twice.cancelled()
    assert unwound == ["/api/v1/once"]
    assert len(unwinding) == 1
    release.set()
    await settle_unwinds(unwinding)

    assert unwound == ["/api/v1/once", "/api/v1/twice"]
    assert sent == []
    [record] = events_named(captured_logs, "api.request_unwind_failed")
    assert (record["method"], record["path"]) == ("GET", "/api/v1/twice")
    assert record["error_type"] == "RuntimeError"
    assert marker not in captured_logs.text
    assert events_named(captured_logs, "api.request_timeout") == []


def add_slow_to_unwind_route(
    app: FastAPI, path: str, unwind: Callable[[], Awaitable[object]]
) -> None:
    """Add ``GET path`` to ``app``: a route that stalls, and once cancelled awaits ``unwind()``."""

    async def slow_to_unwind() -> dict[str, bool]:
        """Stall; once cancelled, keep unwinding until ``unwind()`` completes."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await unwind()
            raise
        return {"finished": True}

    app.add_api_route(api(path), slow_to_unwind, methods=["GET"])


async def test_lifespan_drains_request_unwinds_before_disposing_the_pool(
    monkeypatch: pytest.MonkeyPatch, captured_logs: CapturedLogs
) -> None:
    """Shutdown waits for the requests answered at the deadline to finish their cleanup.

    uvicorn waits only for its own request tasks, while a request answered 503 at the deadline
    keeps unwinding after its task has ended. The lifespan therefore waits for the deadline's
    ``app.state.request_unwinds`` before ``dispose_engine``: a request still unwinding when
    shutdown begins has finished, and been released, by the time the pool is disposed, and no
    ``api.request_unwinds_pending`` warning is logged.
    """
    monkeypatch.setattr(api_app, "_REQUEST_TIMEOUT_S", SHORT_DEADLINE_S)
    app = create_app()
    app.state.thresholds = Thresholds.load(CONFIG_DIR)
    unwound = asyncio.Event()

    async def unwind() -> None:
        """Clean up for a while, then note the cleanup finished."""
        await asyncio.sleep(UNWIND_S)
        unwound.set()

    add_slow_to_unwind_route(app, "/test-slow-unwind", unwind)
    disposed: list[tuple[bool, int]] = []

    async def recording_dispose_engine() -> None:
        """Record whether the cleanup had finished, and how many requests were still held."""
        disposed.append((unwound.is_set(), len(app.state.request_unwinds)))

    stops: list[tuple[bool, int, int]] = []

    async def no_analysis_process() -> int:
        """Spawn nothing: the analysis process's own lifecycle is tested with the runs routes."""
        return 0

    async def recording_stop_analysis_process() -> None:
        """Record the drain's state, and how often the pool was disposed, at the stop."""
        stops.append((unwound.is_set(), len(app.state.request_unwinds), len(disposed)))

    monkeypatch.setattr(api_app, "dispose_engine", recording_dispose_engine)
    monkeypatch.setattr(runner, "start_analysis_process", no_analysis_process)
    monkeypatch.setattr(runner, "stop_analysis_process", recording_stop_analysis_process)
    captured_logs.configure(settings=get_settings())
    captured_logs.clear()
    async with api_app._lifespan(app):
        async with serve(app) as client:
            response = await client.get(api("/test-slow-unwind"))
        assert_error(response, 503, "request_timeout")
        held_at_shutdown = len(app.state.request_unwinds)
        unwound_at_shutdown = unwound.is_set()

    assert (held_at_shutdown, unwound_at_shutdown) == (1, False)
    # The analysis process stops once the drain has finished, and before the pool is disposed.
    assert stops == [(True, 0, 0)]
    assert disposed == [(True, 0)]
    assert len(events_named(captured_logs, "api.stopping")) == 1
    assert events_named(captured_logs, "api.request_unwinds_pending") == []


async def test_lifespan_disposes_the_pool_once_the_unwind_bound_passes(
    monkeypatch: pytest.MonkeyPatch, captured_logs: CapturedLogs
) -> None:
    """A cleanup still running after the drain's bound is logged, and the pool is disposed.

    Under a 0.2 s bound, a request whose cleanup never ends on its own leaves shutdown waiting
    only that long: one ``api.request_unwinds_pending`` WARNING names the one request still
    unwinding and the bound, then ``dispose_engine`` runs with it still held.
    """
    drain_s = 0.2
    monkeypatch.setattr(api_app, "_REQUEST_TIMEOUT_S", SHORT_DEADLINE_S)
    monkeypatch.setattr(api_app, "_REQUEST_UNWIND_DRAIN_S", drain_s)
    app = create_app()
    app.state.thresholds = Thresholds.load(CONFIG_DIR)
    release = asyncio.Event()
    add_slow_to_unwind_route(app, "/test-stuck-unwind", release.wait)
    disposed: list[int] = []

    async def recording_dispose_engine() -> None:
        """Record how many requests were still unwinding when the pool was disposed."""
        disposed.append(len(app.state.request_unwinds))

    async def no_analysis_process() -> int:
        """Spawn nothing, so the shutdown measured here is the drain's alone."""
        return 0

    async def no_stop() -> None:
        """Stop nothing: no analysis process was spawned."""

    monkeypatch.setattr(api_app, "dispose_engine", recording_dispose_engine)
    monkeypatch.setattr(runner, "start_analysis_process", no_analysis_process)
    monkeypatch.setattr(runner, "stop_analysis_process", no_stop)
    captured_logs.configure(settings=get_settings())
    captured_logs.clear()
    loop = asyncio.get_running_loop()
    try:
        async with api_app._lifespan(app):
            async with serve(app) as client:
                response = await client.get(api("/test-stuck-unwind"))
            assert_error(response, 503, "request_timeout")
            stopping_at = loop.time()
        shutdown_took = loop.time() - stopping_at
    finally:
        release.set()
        await settle_unwinds(app.state.request_unwinds)

    assert disposed == [1]
    assert drain_s - 0.05 <= shutdown_took < drain_s + 1.0
    [record] = events_named(captured_logs, "api.request_unwinds_pending")
    assert set(record) == LOG_FIELDS | {"pending", "timeout_s"}
    assert record["level"] == "warning"
    assert record["logger"] == "ads_engine.api.app"
    assert (record["pending"], record["timeout_s"]) == (1, drain_s)


async def test_openapi_document_401_without_session(
    api_client: httpx.AsyncClient, test_identity: AdminIdentity
) -> None:
    """AAP 0.5.3: ``/openapi.json`` is not one of the two endpoints that need no session.

    FastAPI's own schema route would answer anonymously, so ``create_app`` serves the document
    from a route behind the session check. An anonymous ``GET`` is 401 ``unauthenticated``; an
    anonymous ``HEAD`` is the same 401 with no body and the envelope's length, never the
    document's.
    """
    anonymous_get = await api_client.get(api("/openapi.json"))
    assert_error(anonymous_get, 401, "unauthenticated")

    anonymous_head = await api_client.head(api("/openapi.json"))
    assert anonymous_head.status_code == 401, anonymous_head.headers
    assert anonymous_head.headers["content-type"].startswith("application/json")
    assert anonymous_head.content == b""
    assert anonymous_head.headers["content-length"] == str(len(anonymous_get.content))
    assert test_identity.session_cookie not in api_client.cookies


async def test_openapi_document_served_to_signed_in_session(
    authed_client: httpx.AsyncClient,
) -> None:
    """AAP 0.3.1: a signed-in session reads the OpenAPI document FastAPI generates.

    ``GET`` answers 200 with exactly ``app.openapi()``, whose paths are the endpoint table and
    nothing else: the schema route leaves itself out. ``HEAD`` answers 200 with the document's
    length and no body.
    """
    response = await authed_client.get(api("/openapi.json"))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    document = response.json()
    assert document == create_app().openapi()
    published = {
        (method, path.removeprefix(API_PREFIX))
        for path, operations in document["paths"].items()
        for method in operations
    }
    assert published == AAP_ROUTES

    head = await authed_client.head(api("/openapi.json"))
    assert head.status_code == 200, head.headers
    assert head.content == b""
    assert head.headers["content-length"] == str(len(response.content))


# ---------------------------------------------------------------------------------------------
# HEAD requests (RFC 9110 §9.3.2: HEAD answers as GET does, without the content)
# ---------------------------------------------------------------------------------------------

HEAD_PARITY_PATHS: Final = (
    "/auth/me",
    "/runs/latest",
    "/runs/{run_id}",
    "/runs?kind=custom&limit=5",
    "/recommendations",
    "/recommendations?run_id={run_id}",
    "/decisions",
    "/discrepancies?state=open",
    "/sources",
    "/config",
    "/healthz",
    "/openapi.json",
)
"""Every ``GET`` path under ``/api/v1``, ``{run_id}`` standing for the seeded run."""

PROTECTED_GET_REQUESTS: Final = tuple(
    request for request in PROTECTED_REQUESTS if request.method == "GET"
)
"""The session-protected ``GET`` endpoints, each requested once."""


def headers_except_cookies(response: httpx.Response) -> list[tuple[str, str]]:
    """Every response header but ``Set-Cookie``, sorted, names lower case.

    A session cookie is re-signed on each response with the signing second, so its value is
    compared by name only (:func:`cookie_names`).
    """
    return sorted(
        (name.lower(), value)
        for name, value in response.headers.multi_items()
        if name.lower() != "set-cookie"
    )


def cookie_names(response: httpx.Response) -> list[str]:
    """The names of the cookies ``response`` sets, in header order."""
    return [header.partition("=")[0].strip() for header in response.headers.get_list("set-cookie")]


async def test_head_answers_as_get_on_every_get_route(seeded: Seeded, login: LoginFunction) -> None:
    """RFC 9110 §9.3.2: ``HEAD`` answers every ``GET`` route with ``GET``'s status and headers.

    On the seeded database, each ``GET`` path answers ``HEAD`` with the same status, the same
    headers (``Content-Type`` and a ``Content-Length`` equal to the ``GET`` body's length) and
    the same cookies, and with no body. The request clock is fixed, so the read-time overlay
    and freshness, and with them the body lengths, are identical between the two requests.
    """
    run_id = seeded.facts.run_id
    app = create_app()
    instant = datetime.now(UTC)
    app.dependency_overrides[deps.get_now] = lambda: instant
    async with serve(app) as client:
        await login(client)
        for template in HEAD_PARITY_PATHS:
            path = api(template.format(run_id=run_id))
            get = await client.get(path)
            head = await client.head(path)
            assert get.status_code == 200, (path, get.text)
            assert get.content, path
            assert head.status_code == get.status_code, path
            assert head.content == b"", path
            assert head.headers["content-type"] == get.headers["content-type"], path
            assert head.headers["content-length"] == str(len(get.content)), path
            assert headers_except_cookies(head) == headers_except_cookies(get), path
            assert cookie_names(head) == cookie_names(get), path


async def test_anonymous_head_on_healthz_answers_as_get(api_client: httpx.AsyncClient) -> None:
    """``HEAD /healthz`` needs no session and answers 200 with ``GET``'s headers and no body.

    A liveness probe that sends ``HEAD`` (``curl -I``, an uptime monitor, a load-balancer
    check) therefore sees a healthy API as healthy.
    """
    get = await api_client.get(api("/healthz"))
    head = await api_client.head(api("/healthz"))
    assert get.status_code == 200, get.text
    assert head.status_code == 200, head.headers
    assert head.content == b""
    assert head.headers["content-length"] == str(len(get.content))
    assert headers_except_cookies(head) == headers_except_cookies(get)
    assert head.headers.get_list("set-cookie") == []


@pytest.mark.parametrize(
    "request_spec", PROTECTED_GET_REQUESTS, ids=lambda spec: f"HEAD {spec.path}"
)
async def test_anonymous_head_on_a_protected_route_is_401_without_body(
    api_client: httpx.AsyncClient,
    test_identity: AdminIdentity,
    request_spec: ProtectedRequest,
) -> None:
    """AAP 0.5.3: ``HEAD`` meets the session check exactly as ``GET`` does.

    An anonymous ``HEAD`` on each protected ``GET`` route is 401 with the JSON content type
    and the envelope's length, and no body; no session is set.
    """
    get = await api_client.get(api(request_spec.path))
    assert_error(get, 401, "unauthenticated")
    head = await api_client.head(api(request_spec.path))
    assert head.status_code == 401, head.headers
    assert head.headers["content-type"].startswith("application/json")
    assert head.content == b""
    assert head.headers["content-length"] == str(len(get.content))
    assert test_identity.session_cookie not in api_client.cookies


async def test_405_allow_names_head_wherever_get_is_served(
    api_client: httpx.AsyncClient, test_identity: AdminIdentity
) -> None:
    """A 405's ``Allow`` names ``HEAD`` beside ``GET``; ``HEAD`` on a POST-only route is 405.

    ``POST`` on a ``GET``-only path is 405 ``method_not_allowed`` with ``Allow: GET, HEAD``.
    ``HEAD /auth/login`` is served as ``GET``, which the route does not take: 405 with
    ``Allow: POST``, ``GET``'s headers and no body.
    """
    csrf = dict(test_identity.csrf_headers)
    for path in ("/healthz", "/auth/me"):
        refused = await api_client.post(api(path), headers=csrf)
        assert_error(refused, 405, "method_not_allowed")
        assert refused.headers["allow"] == "GET, HEAD", path

    get_login = await api_client.get(api("/auth/login"))
    assert_error(get_login, 405, "method_not_allowed")
    assert get_login.headers["allow"] == "POST"
    head_login = await api_client.head(api("/auth/login"))
    assert head_login.status_code == 405, head_login.headers
    assert head_login.headers["allow"] == "POST"
    assert head_login.content == b""
    assert head_login.headers["content-length"] == str(len(get_login.content))


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Allow": "GET"}, {"Allow": "GET, HEAD"}),
        ({"Allow": "GET, HEAD"}, {"Allow": "GET, HEAD"}),
        ({"Allow": "POST"}, {"Allow": "POST"}),
        (
            {"allow": "POST, GET", "Retry-After": "5"},
            {"allow": "POST, GET, HEAD", "Retry-After": "5"},
        ),
        ({"Retry-After": "5"}, {"Retry-After": "5"}),
    ],
    ids=["get-only", "head-present", "no-get", "lower-case-name-and-other-header", "no-allow"],
)
def test_allow_with_head_extends_only_an_allow_naming_get(
    headers: dict[str, str], expected: dict[str, str]
) -> None:
    """``HEAD`` is added to an ``Allow`` naming ``GET`` without it; nothing else changes."""
    assert api_app._allow_with_head(headers) == expected


async def test_head_middleware_serves_get_and_drops_only_the_body() -> None:
    """Driven at the ASGI level, since ``httpx.ASGITransport`` itself drops a ``HEAD`` body.

    For ``HEAD`` the wrapped application sees ``GET`` on a copy of the scope, with the original
    receive channel; the server's scope still says ``HEAD``. The start message is forwarded
    unchanged, the two streamed body chunks are dropped, and exactly one empty final body
    message is sent.
    """
    start: Message = {
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", b"9"),
            (b"set-cookie", b"ads_engine_session=signed; path=/"),
        ],
    }
    seen: list[tuple[str, object, bool]] = []

    async def streaming_app(scope: Scope, receive: Receive, send: Send) -> None:
        """Record the method and channel it was given, then stream a body in two chunks."""
        seen.append((scope["type"], scope.get("method"), receive is receive_nothing))
        await send(start)
        await send({"type": "http.response.body", "body": b"chunk", "more_body": True})
        await send({"type": "http.response.body", "body": b"-two", "more_body": False})

    sent: list[Message] = []

    async def collect(message: Message) -> None:
        """Record what the middleware sends."""
        sent.append(message)

    scope = {**http_scope("/api/v1/stream"), "method": "HEAD"}
    await api_app._HeadAsGetMiddleware(streaming_app)(scope, receive_nothing, collect)

    assert seen == [("http", "GET", True)]
    assert scope["method"] == "HEAD"
    assert sent == [start, {"type": "http.response.body", "body": b"", "more_body": False}]
    assert sent[0] is start


async def test_head_middleware_passes_other_methods_and_scopes_through() -> None:
    """``GET``, ``POST``, lifespan and websocket scopes reach the application untouched.

    The application receives the very scope, receive and send it was called with, and what it
    sends, a body included, is forwarded as sent.
    """
    passed: list[tuple[Scope, Receive, Send]] = []
    body: Message = {"type": "http.response.body", "body": b"{}", "more_body": False}

    async def recording_app(scope: Scope, receive: Receive, send: Send) -> None:
        """Record the arguments it was called with; answer an http scope with a body."""
        passed.append((scope, receive, send))
        if scope["type"] == "http":
            await send(body)

    sent: list[Message] = []

    async def collect(message: Message) -> None:
        """Record what reaches the server."""
        sent.append(message)

    middleware = api_app._HeadAsGetMiddleware(recording_app)
    scopes: list[Scope] = [
        http_scope("/api/v1/healthz"),
        {**http_scope("/api/v1/runs"), "method": "POST"},
        {"type": "lifespan"},
        {"type": "websocket", "path": "/api/v1/ws", "headers": []},
    ]
    for scope in scopes:
        await middleware(scope, receive_nothing, collect)

    assert len(passed) == len(scopes)
    for scope, (seen_scope, seen_receive, seen_send) in zip(scopes, passed, strict=True):
        assert seen_scope is scope
        assert seen_receive is receive_nothing
        assert seen_send is collect
    assert [scope.get("method") for scope in scopes] == ["GET", "POST", None, None]
    assert sent == [body, body]


async def test_head_errors_rendered_outside_the_middleware_log_the_real_method(
    monkeypatch: pytest.MonkeyPatch, captured_logs: CapturedLogs
) -> None:
    """The request deadline and the catch-all 500 sit outside the HEAD handling.

    A ``HEAD`` still running at a 0.5 s deadline is 503 and one that crashes is 500, each with
    the JSON content type and no body, and ``api.request_timeout`` and ``api.unhandled_error``
    name the client's method, ``HEAD``.
    """
    monkeypatch.setattr(api_app, "_REQUEST_TIMEOUT_S", SHORT_DEADLINE_S)
    app = create_app()

    async def stalled() -> dict[str, bool]:
        """Wait for an event nobody sets."""
        await asyncio.Event().wait()
        return {"finished": True}

    async def crashed() -> dict[str, bool]:
        """Fail with an unexpected error."""
        raise RuntimeError("unexpected failure")

    app.add_api_route(api("/test-stalled"), stalled, methods=["GET"])
    app.add_api_route(api("/test-crashed"), crashed, methods=["GET"])
    captured_logs.configure(settings=get_settings())
    captured_logs.clear()
    # Starlette re-raises an unhandled error after answering 500; the client must not.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        timed_out = await client.head(api("/test-stalled"))
        failed = await client.head(api("/test-crashed"))
    await settle_unwinds(app.state.request_unwinds)

    for response, status in ((timed_out, 503), (failed, 500)):
        assert response.status_code == status, response.headers
        assert response.headers["content-type"].startswith("application/json")
        assert response.content == b""
    [timeout_record] = events_named(captured_logs, "api.request_timeout")
    assert (timeout_record["method"], timeout_record["path"]) == ("HEAD", api("/test-stalled"))
    [unhandled] = events_named(captured_logs, "api.unhandled_error")
    assert (unhandled["method"], unhandled["error_type"]) == ("HEAD", "RuntimeError")


# ---------------------------------------------------------------------------------------------
# No caching (CWE-525: every answer carries exactly one Cache-Control: no-store)
# ---------------------------------------------------------------------------------------------


def assert_no_store(response: httpx.Response) -> None:
    """Assert ``response`` carries exactly one ``Cache-Control`` header, ``no-store``.

    ``no-cache`` or a second ``Cache-Control`` fails: ``no-cache`` still lets a cache store the
    body, and a repeated header leaves caches to combine the values.
    """
    request = response.request
    assert response.headers.get_list("cache-control") == ["no-store"], (
        request.method,
        str(request.url),
        response.headers.multi_items(),
    )


async def test_auth_answers_are_no_store(admin_user: AppUser, test_identity: AdminIdentity) -> None:
    """CWE-525: sign-in, identity and sign-out answers are never kept by a cache.

    A failed login (401), a successful one (200 with the session ``Set-Cookie``),
    ``GET /auth/me``, logout (200 expiring the cookie) and the 6th attempt's 429 with its
    ``Retry-After`` each carry exactly one ``Cache-Control: no-store``, beside their unchanged
    status, body and cookies. The login limiter runs on a fixed injected clock, so the 429 never
    depends on how long the requests and password checks take.
    """
    del admin_user
    app = create_app()
    app.state.login_limiter = security.LoginRateLimiter(clock=lambda: 0.0)
    email, password = test_identity.email, test_identity.password
    async with serve(app) as client:
        failed = await post_login(client, test_identity, email=email, password="wrong-password")
        assert_error(failed, 401, "invalid_credentials")
        assert failed.headers.get_list("set-cookie") == []
        signed_in = await post_login(client, test_identity, email=email, password=password)
        assert signed_in.status_code == 200, signed_in.text
        assert schemas.Me.model_validate(signed_in.json()) == schemas.Me(email=email)
        assert security.SESSION_COOKIE in cookie_names(signed_in)
        me = await client.get(api("/auth/me"))
        assert me.status_code == 200, me.text
        assert me.json() == {"email": email}
        logged_out = await client.post(
            api("/auth/logout"), headers=dict(test_identity.csrf_headers)
        )
        assert logged_out.status_code == 200, logged_out.text
        expired = cookie_attributes(logged_out, security.SESSION_COOKIE)
        assert "1970" in expired.get("expires", ""), expired
        for _ in range(security.LOGIN_MAX_ATTEMPTS - 2):  # attempts 3 to 5
            wrong = await post_login(client, test_identity, email=email, password="wrong-password")
            assert_error(wrong, 401, "invalid_credentials")
        limited = await post_login(client, test_identity, email=email, password=password)
        assert_error(limited, 429, "rate_limited")
        assert limited.headers["Retry-After"] == str(security.LOGIN_WINDOW_SECONDS)

    for response in (failed, signed_in, me, logged_out, limited):
        assert_no_store(response)


async def test_signed_in_reads_are_no_store(seeded: Seeded) -> None:
    """CWE-525: evidence, decisions and the OpenAPI document are never kept by a cache.

    On the seeded database the signed-in ``GET /recommendations`` (recommendation and CRM
    evidence), ``GET /decisions`` and ``GET`` and ``HEAD /openapi.json`` each answer 200 with
    exactly one ``Cache-Control: no-store``; the ``HEAD`` answer still has no body.
    """
    client = seeded.client
    page = await client.get(api("/recommendations"))
    decisions = await client.get(api("/decisions"))
    document = await client.get(api("/openapi.json"))
    document_head = await client.head(api("/openapi.json"))
    for response in (page, decisions, document, document_head):
        assert response.status_code == 200, (str(response.request.url), response.text)
        assert_no_store(response)
    assert page_items(page.json())
    assert decisions.json()
    assert document_head.content == b""
    assert document_head.headers["content-length"] == str(len(document.content))


async def test_error_answers_and_healthz_are_no_store(
    api_client: httpx.AsyncClient,
    admin_user: AppUser,
    login: LoginFunction,
    test_identity: AdminIdentity,
) -> None:
    """CWE-525: every error status the handlers render, and ``/healthz``, is ``no-store``.

    401 without a session, 403 without the CSRF header, 404 for an unknown path, 405 with its
    ``Allow`` kept, 422 for an invalid body and the anonymous ``GET /healthz`` each carry exactly
    one ``Cache-Control: no-store``, and each error is still exactly the envelope.
    """
    del admin_user
    csrf = dict(test_identity.csrf_headers)
    anonymous = await api_client.get(api("/recommendations"))
    assert_error(anonymous, 401, "unauthenticated")
    healthz = await api_client.get(api("/healthz"))
    assert healthz.status_code == 200, healthz.text
    assert healthz.json() == {"status": "ok"}
    not_allowed = await api_client.post(api("/healthz"), headers=csrf)
    assert_error(not_allowed, 405, "method_not_allowed")
    assert not_allowed.headers["allow"] == "GET, HEAD"
    unknown = await api_client.get(api("/no-such-route"))
    assert_error(unknown, 404, "not_found")

    await login(api_client)
    no_csrf = await api_client.post(api("/runs"), json={})
    assert_error(no_csrf, 403, "csrf_header_missing")
    invalid = await api_client.post(api("/runs"), json={"window_start": "soon"}, headers=csrf)
    assert_error(invalid, 422, "validation_error")

    for response in (anonymous, healthz, not_allowed, unknown, no_csrf, invalid):
        assert_no_store(response)


async def test_deadline_503_and_catch_all_500_are_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """CWE-525: the answers rendered outside the routes' error handlers are ``no-store`` too.

    The request deadline renders its 503 ``request_timeout`` inside the cache policy, while
    Starlette's server-error middleware renders the catch-all 500 ``internal_error`` outside
    every user middleware. Under a 0.5 s deadline, ``GET`` and ``HEAD`` of a route that never
    finishes and of one that raises each carry exactly one ``Cache-Control: no-store``.
    """
    monkeypatch.setattr(api_app, "_REQUEST_TIMEOUT_S", SHORT_DEADLINE_S)
    app = create_app()

    async def stalled() -> dict[str, bool]:
        """Wait for an event nobody sets."""
        await asyncio.Event().wait()
        return {"finished": True}

    async def crashed() -> dict[str, bool]:
        """Fail with an unexpected error."""
        raise RuntimeError("unexpected failure")

    app.add_api_route(api("/test-stalled"), stalled, methods=["GET"])
    app.add_api_route(api("/test-crashed"), crashed, methods=["GET"])
    # Starlette re-raises an unhandled error after answering 500; the client must not.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        timed_out = await client.get(api("/test-stalled"))
        failed = await client.get(api("/test-crashed"))
        timed_out_head = await client.head(api("/test-stalled"))
        failed_head = await client.head(api("/test-crashed"))
    await settle_unwinds(app.state.request_unwinds)

    assert_error(timed_out, 503, "request_timeout")
    assert_error(failed, 500, "internal_error")
    for response, status in ((timed_out_head, 503), (failed_head, 500)):
        assert response.status_code == status, response.headers
        assert response.content == b""
    for response in (timed_out, failed, timed_out_head, failed_head):
        assert_no_store(response)


async def test_no_store_middleware_replaces_cache_control_and_leaves_other_scopes_alone() -> None:
    """Driven at the ASGI level: a response start gets exactly one ``Cache-Control: no-store``.

    Every ``Cache-Control`` the application sent, a repeated one included, is replaced by one
    ``no-store`` in the first one's place; every other header keeps its value and order, and the
    status and body are forwarded as sent. A start message without ``headers`` gains the one
    header. Lifespan and websocket scopes reach the application with the very scope, receive
    and send they were called with, and what a websocket sends is forwarded untouched.
    """
    body: Message = {"type": "http.response.body", "body": b"{}", "more_body": False}
    starts: dict[str, Message] = {
        "/api/v1/cached": {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"public, max-age=600"),
                (b"set-cookie", b"ads_engine_session=signed; path=/"),
                (b"cache-control", b"max-age=60"),
            ],
        },
        "/api/v1/bare": {"type": "http.response.start", "status": 204},
    }

    async def answering_app(scope: Scope, receive: Receive, send: Send) -> None:
        """Send the start message registered for the path, then the body."""
        await send(starts[scope["path"]])
        await send(body)

    sent: list[Message] = []

    async def collect(message: Message) -> None:
        """Record what reaches the server."""
        sent.append(message)

    middleware = api_app._NoStoreMiddleware(answering_app)
    for path in starts:
        await middleware(http_scope(path), receive_nothing, collect)

    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.start",
        "http.response.body",
    ]
    assert (sent[0]["status"], sent[2]["status"]) == (200, 204)
    assert sent[0]["headers"] == [
        (b"content-type", b"application/json"),
        (b"cache-control", b"no-store"),
        (b"set-cookie", b"ads_engine_session=signed; path=/"),
    ]
    assert sent[2]["headers"] == [(b"cache-control", b"no-store")]
    assert sent[1] is body
    assert sent[3] is body

    passed: list[tuple[Scope, Receive, Send]] = []
    accept: Message = {"type": "websocket.accept", "headers": [(b"cache-control", b"public")]}

    async def recording_app(scope: Scope, receive: Receive, send: Send) -> None:
        """Record the arguments it was called with; accept a websocket with its own header."""
        passed.append((scope, receive, send))
        if scope["type"] == "websocket":
            await send(accept)

    forwarded: list[Message] = []

    async def collect_other(message: Message) -> None:
        """Record what reaches the server from a non-http scope."""
        forwarded.append(message)

    other_scopes: list[Scope] = [
        {"type": "lifespan"},
        {"type": "websocket", "path": "/api/v1/ws", "headers": []},
    ]
    for scope in other_scopes:
        await api_app._NoStoreMiddleware(recording_app)(scope, receive_nothing, collect_other)

    assert len(passed) == len(other_scopes)
    for scope, (seen_scope, seen_receive, seen_send) in zip(other_scopes, passed, strict=True):
        assert seen_scope is scope
        assert seen_receive is receive_nothing
        assert seen_send is collect_other
    assert forwarded == [accept]
    assert forwarded[0]["headers"] == [(b"cache-control", b"public")]


# ---------------------------------------------------------------------------------------------
# Startup guard (AAP 0.3.2 "Plain HTTP is accepted only when ...")
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url", ["http://ads.example.com", "http://192.0.2.10:8080", "http://localhost.example.com"]
)
def test_create_app_refuses_non_localhost_http(set_env: Callable[..., None], base_url: str) -> None:
    """AAP 0.3.2: the API refuses to start on a plain-``http`` base URL off the local machine."""
    set_env(APP_BASE_URL=base_url)
    with pytest.raises(ConfigurationError, match="APP_BASE_URL must use https://"):
        create_app()


@pytest.mark.parametrize("base_url", ["http://localhost:8080", "http://127.0.0.1:8080"])
def test_create_app_accepts_localhost_and_loopback(
    set_env: Callable[..., None], base_url: str
) -> None:
    """AAP 0.3.2: plain HTTP is accepted on ``localhost`` and ``127.0.0.1``, with no Secure flag."""
    set_env(APP_BASE_URL=base_url)
    app = create_app()
    assert isinstance(app, FastAPI)
    settings = app.state.settings
    assert isinstance(settings, Settings)
    assert settings.app_base_url == base_url
    assert settings.secure_cookies is False


@pytest.mark.parametrize("secret", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_create_app_requires_session_secret(
    set_env: Callable[..., None], secret: str | None
) -> None:
    """AAP 0.3.2: without ``SESSION_SECRET`` no session can be signed, so the API will not start.

    An empty or whitespace-only value counts as unset (AAP 0.3.3).
    """
    set_env(SESSION_SECRET=secret)
    with pytest.raises(ConfigurationError, match="SESSION_SECRET"):
        create_app()


DATABASE_PASSWORD: Final = "pw-startup-db-0451"
"""The password of every ``DATABASE_URL`` the startup tests set; it never reaches a log."""

UNUSABLE_DATABASE_URLS: Final = (
    pytest.param(
        "not-a-url", "DATABASE_URL is not a valid SQLAlchemy database URL", id="malformed"
    ),
    pytest.param(
        f"postgresql://ads:{DATABASE_PASSWORD}@db.example.test:port/ads_engine",
        "DATABASE_URL is not a valid SQLAlchemy database URL",
        id="malformed-port",
    ),
    pytest.param(
        f"mysql://ads:{DATABASE_PASSWORD}@db.example.test/ads_engine",
        "DATABASE_URL must use the postgresql+psycopg driver (psycopg 3); got 'mysql'",
        id="mysql",
    ),
    pytest.param(
        f"postgresql+asyncpg://ads:{DATABASE_PASSWORD}@db.example.test/ads_engine",
        "DATABASE_URL must use the postgresql+psycopg driver (psycopg 3); got 'postgresql+asyncpg'",
        id="asyncpg",
    ),
)
"""``DATABASE_URL`` values the engine refuses, each with the refusal's exact message."""


@pytest.mark.parametrize(("database_url", "message"), UNUSABLE_DATABASE_URLS)
def test_create_app_refuses_a_database_url_the_engine_cannot_use(
    set_env: Callable[..., None], database_url: str, message: str
) -> None:
    """The API stops at startup on a ``DATABASE_URL`` the engine would refuse, as the worker does.

    The refusal is the engine's own rule (``normalise_database_url``), raised as a
    ``ConfigurationError`` naming ``DATABASE_URL`` and at most the driver, never the URL; the
    parse error is not chained, because the URL carries the password.
    """
    set_env(DATABASE_URL=database_url)
    with pytest.raises(ConfigurationError) as excinfo:
        create_app()

    error = excinfo.value
    assert str(error) == message
    assert error.fields == ("database_url",)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert DATABASE_PASSWORD not in str(error)


@pytest.mark.parametrize(
    "database_url",
    [
        f"postgresql://ads:{DATABASE_PASSWORD}@db.example.test:5432/ads_engine",
        f"postgres://ads:{DATABASE_PASSWORD}@db.example.test:5432/ads_engine",
        f"postgresql+psycopg://ads:{DATABASE_PASSWORD}@db.example.test:5432/ads_engine",
    ],
    ids=["postgresql", "postgres", "postgresql+psycopg"],
)
def test_create_app_accepts_every_postgres_scheme_the_engine_accepts(
    set_env: Callable[..., None], database_url: str
) -> None:
    """``postgresql://`` and ``postgres://`` are rewritten to psycopg 3, so the API starts on them.

    The host does not resolve: the startup check parses the URL and never connects.
    """
    set_env(DATABASE_URL=database_url)
    app = create_app()
    assert isinstance(app, FastAPI)
    settings = app.state.settings
    assert isinstance(settings, Settings)
    assert settings.database_url.get_secret_value() == database_url


async def test_unreachable_database_starts_the_api_and_healthz_answers_503(
    set_env: Callable[..., None],
) -> None:
    """A well-formed URL of a server that refuses connections is not a startup refusal.

    The API starts, and ``GET /healthz`` reports the database as 503 ``database_unavailable``
    (AAP 0.5.3) rather than 500, so the Compose healthcheck keeps the stack waiting on it.
    """
    set_env(DATABASE_URL=f"postgresql://ads:{DATABASE_PASSWORD}@127.0.0.1:1/ads_engine")
    await db_session.dispose_engine()
    try:
        app = create_app()
        async with serve(app) as client:
            response = await client.get(api("/healthz"))
    finally:
        await db_session.dispose_engine()
    error = assert_error(response, 503, "database_unavailable")
    assert error.error.message == "Database unavailable"


# ---------------------------------------------------------------------------------------------
# Startup failure logging (the refusals above as structured logs, AAP 0.11.1)
# ---------------------------------------------------------------------------------------------

STARTUP_FAILED_EVENT: Final = "api.startup_failed"
"""The ERROR event ``create_app()`` logs before a startup refusal propagates."""


def startup_failures(output: str) -> list[dict[str, Any]]:
    """Return the ``api.startup_failed`` events among the JSON log lines in ``output``.

    ``create_app()`` configures logging onto ``sys.stdout``, which ``capsys`` captures.

    Raises:
        AssertionError: When a line is not a JSON object.
    """
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        assert isinstance(record, dict), f"log line is not a JSON object: {line!r}"
        if record.get("event") == STARTUP_FAILED_EVENT:
            events.append(record)
    return events


def assert_one_startup_failure(
    output: str, *, error_type: str, fields: list[str], message: str
) -> None:
    """Assert ``output`` holds exactly one ``api.startup_failed`` ERROR event, as given.

    ``message`` is what the event tells the operator: a ``ConfigurationError``'s own text, or
    one ``NAME: message`` per failed setting for a ``ValidationError``.
    """
    failures = startup_failures(output)
    assert len(failures) == 1, failures
    failure = failures[0]
    assert failure["level"] == "error"
    assert failure["logger"] == "ads_engine.api.app"
    assert failure["error_type"] == error_type
    assert failure["fields"] == fields
    assert failure["message"] == message


def test_unsafe_base_url_logs_startup_failed(
    set_env: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """The base-URL refusal is logged once, naming its field, class and fix but neither value.

    ``ConfigurationError`` still propagates, so uvicorn refuses to start (AAP 0.3.2).
    """
    host = "ads-startup.example.com"
    set_env(APP_BASE_URL=f"http://{host}")
    secret = get_settings().session_secret
    assert secret is not None
    capsys.readouterr()

    with pytest.raises(ConfigurationError, match="APP_BASE_URL must use https://") as excinfo:
        create_app()

    output = capsys.readouterr().out
    assert_one_startup_failure(
        output,
        error_type="ConfigurationError",
        fields=["app_base_url"],
        message=str(excinfo.value),
    )
    assert host not in output
    assert secret.get_secret_value() not in output


@pytest.mark.parametrize("secret", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_missing_session_secret_logs_startup_failed(
    set_env: Callable[..., None], capsys: pytest.CaptureFixture[str], secret: str | None
) -> None:
    """A missing ``SESSION_SECRET`` is logged once, naming its field and class, then raised."""
    host = "ads-startup.example.com"
    set_env(SESSION_SECRET=secret, APP_BASE_URL=f"https://{host}")
    capsys.readouterr()

    with pytest.raises(ConfigurationError, match="SESSION_SECRET is not set"):
        create_app()

    output = capsys.readouterr().out
    assert_one_startup_failure(
        output,
        error_type="ConfigurationError",
        fields=["session_secret"],
        message="SESSION_SECRET is not set; run make init",
    )
    assert host not in output


def test_session_secret_recheck_logs_startup_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The factory's own ``SESSION_SECRET`` check logs the same event when it is the one to refuse.

    ``check_api_startup`` is neutralised, so the refusal comes from ``create_app()`` itself.
    """
    monkeypatch.setattr(Settings, "check_api_startup", lambda self: None)
    settings = Settings(session_secret=None)
    capsys.readouterr()

    with pytest.raises(ConfigurationError, match="SESSION_SECRET is not set"):
        create_app(settings)

    assert_one_startup_failure(
        capsys.readouterr().out,
        error_type="ConfigurationError",
        fields=["session_secret"],
        message="SESSION_SECRET is not set; run make init",
    )


INT_PARSING_MESSAGE: Final = "Input should be a valid integer, unable to parse string as an integer"
"""Pydantic's message for an integer setting whose text is not a number."""


def test_unparseable_environment_logs_startup_failed(
    set_env: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """An environment value that fails validation is logged once by field name, then raised.

    The message names each variable with pydantic's own message, in the order the settings
    declare them. Neither input reaches the log, although a validation message may quote its
    input.
    """
    seed, budget = "seed-not-a-number-0451", "budget-not-a-number-0451"
    set_env(MOCK_SEED=seed, GOOGLE_DAILY_CALL_BUDGET=budget)
    capsys.readouterr()

    with pytest.raises(ValidationError):
        create_app()

    output = capsys.readouterr().out
    assert_one_startup_failure(
        output,
        error_type="ValidationError",
        fields=["google_daily_call_budget", "mock_seed"],
        message=(
            f"MOCK_SEED: {INT_PARSING_MESSAGE}; GOOGLE_DAILY_CALL_BUDGET: {INT_PARSING_MESSAGE}"
        ),
    )
    assert seed not in output
    assert budget not in output


def test_startup_failed_logs_each_validator_message_whole_and_never_the_value(
    set_env: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """A settings validator's message is logged whole, since it never quotes the value.

    ``DISPLAY_TIMEZONE`` and ``LINKEDIN_AD_ACCOUNT_IDS`` reject their values with fixed text,
    so nothing is withheld from it and neither value appears. Pydantic's own template messages
    never quote the input either, so ``DATA_MODE`` keeps the allowed values it lists.
    """
    zone, account, mode = "Mars/Olympus-0451", "acct-not-numeric-0451", "bogus-mode-0451"
    set_env(DISPLAY_TIMEZONE=zone, LINKEDIN_AD_ACCOUNT_IDS=account, DATA_MODE=mode)
    capsys.readouterr()

    with pytest.raises(ValidationError):
        create_app()

    output = capsys.readouterr().out
    # One entry per failure, in the order the settings declare the fields.
    assert_one_startup_failure(
        output,
        error_type="ValidationError",
        fields=["data_mode", "display_timezone", "linkedin_ad_account_ids"],
        message=(
            "DISPLAY_TIMEZONE: Value error, DISPLAY_TIMEZONE must be an IANA time zone name "
            "such as UTC or America/New_York; DATA_MODE: Input should be 'auto' or 'simulated'; "
            "LINKEDIN_AD_ACCOUNT_IDS: Value error, LINKEDIN_AD_ACCOUNT_IDS entries must be "
            "numeric ad account ids or urn:li:sponsoredAccount:<digits>"
        ),
    )
    for value in (zone, account, mode):
        assert value not in output


def test_startup_failed_withholds_the_input_a_validator_quotes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A validator message that quotes its input keeps its text but not the quoted literal.

    No settings validator quotes its input, so the safeguard is proven on an injected
    ``ValidationError``: each single- or double-quoted literal of a ``value_error`` or an
    ``assertion_error`` message becomes ``<value withheld>``.
    """
    sentinel = "-".join(("api", "sentinel", uuid.uuid4().hex))
    quoted = ValidationError.from_exception_data(
        "Settings",
        [
            {
                "type": "value_error",
                "loc": ("linkedin_ad_account_ids",),
                "input": sentinel,
                "ctx": {"error": ValueError(f"entries must be numeric; got {sentinel!r}")},
            },
            {
                "type": "assertion_error",
                "loc": ("display_timezone",),
                "input": sentinel,
                "ctx": {"error": AssertionError(f'unknown zone "{sentinel}"')},
            },
        ],
    )

    def refuse_settings() -> Settings:
        raise quoted

    monkeypatch.setattr(api_app, "get_settings", refuse_settings)
    capsys.readouterr()

    with pytest.raises(ValidationError) as excinfo:
        create_app()

    assert excinfo.value is quoted
    output = capsys.readouterr().out
    assert_one_startup_failure(
        output,
        error_type="ValidationError",
        fields=["display_timezone", "linkedin_ad_account_ids"],
        message=(
            "LINKEDIN_AD_ACCOUNT_IDS: Value error, entries must be numeric; got "
            "<value withheld>; DISPLAY_TIMEZONE: Assertion failed, unknown zone <value withheld>"
        ),
    )
    assert sentinel not in output


@pytest.mark.parametrize(("database_url", "message"), UNUSABLE_DATABASE_URLS)
def test_unusable_database_url_logs_startup_failed(
    set_env: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
    database_url: str,
    message: str,
) -> None:
    """A ``DATABASE_URL`` the engine refuses is logged once by field name, never by value."""
    set_env(DATABASE_URL=database_url)
    capsys.readouterr()

    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        create_app()

    output = capsys.readouterr().out
    assert_one_startup_failure(
        output, error_type="ConfigurationError", fields=["database_url"], message=message
    )
    assert database_url not in output
    assert DATABASE_PASSWORD not in output


@pytest.mark.parametrize(
    ("variables", "error_type"),
    [
        ({"SESSION_SECRET": None}, ConfigurationError),
        ({"DATABASE_URL": "not-a-url"}, ConfigurationError),
        ({"MOCK_SEED": "seed-not-a-number-0451"}, ValidationError),
    ],
    ids=["session-secret", "database-url", "validation-error"],
)
def test_startup_refusal_is_marked_as_logged(
    set_env: Callable[..., None],
    variables: Mapping[str, str | None],
    error_type: type[Exception],
) -> None:
    """Each refusal leaves ``create_app()`` marked as logged by ``api.startup_failed``.

    The ``sys.excepthook`` that ``configure_logging`` installs therefore writes nothing more
    when the refusal ends the uvicorn process.
    """
    set_env(**variables)

    with pytest.raises(error_type) as excinfo:
        create_app()

    assert ads_logging.logged_as(excinfo.value) == STARTUP_FAILED_EVENT
    assert sys.excepthook is ads_logging._log_uncaught_exception


UVICORN_FACTORY_COMMAND: Final = (
    sys.executable,
    "-m",
    "uvicorn",
    "ads_engine.api.app:create_app",
    "--factory",
    "--host",
    "127.0.0.1",
    "--port",
    "0",
)
"""The ``api`` container's command, on an ephemeral port; a refusal exits before it binds."""


@pytest.mark.parametrize(
    ("variables", "error_type", "fields", "withheld"),
    [
        (
            {"APP_BASE_URL": "http://ads-startup.example.com"},
            "ConfigurationError",
            ["app_base_url"],
            "ads-startup.example.com",
        ),
        ({"SESSION_SECRET": None}, "ConfigurationError", ["session_secret"], None),
        (
            {"GOOGLE_DAILY_CALL_BUDGET": "budget-not-a-number-0451"},
            "ValidationError",
            ["google_daily_call_budget"],
            "budget-not-a-number-0451",
        ),
        ({"DATABASE_URL": "not-a-url"}, "ConfigurationError", ["database_url"], "not-a-url"),
        (
            {"DATABASE_URL": f"mysql://ads:{DATABASE_PASSWORD}@db.example.test/ads_engine"},
            "ConfigurationError",
            ["database_url"],
            DATABASE_PASSWORD,
        ),
    ],
    ids=["unsafe-base-url", "no-session-secret", "unparseable-budget", "malformed-db", "mysql-db"],
)
def test_uvicorn_startup_refusal_writes_only_json_lines(
    set_env: Callable[..., None],
    variables: Mapping[str, str | None],
    error_type: str,
    fields: list[str],
    withheld: str | None,
) -> None:
    """uvicorn exits 1 on a refusal, and its whole output is JSON with one ``api.startup_failed``.

    uvicorn catches only a factory's ``TypeError``, so the refusal ends the interpreter; the
    excepthook ``configure_logging`` installed writes no raw traceback after the logged event.
    The process runs in this test's isolated environment, never with host credentials.
    """
    set_env(**variables)
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and program, no user input
        UVICORN_FACTORY_COMMAND,
        cwd=BACKEND_DIR,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "Traceback" not in output
    for line in output.splitlines():
        if line.strip():
            assert isinstance(json.loads(line), dict), line
    (failure,) = startup_failures(output)
    assert failure["level"] == "error"
    assert failure["error_type"] == error_type
    assert failure["fields"] == fields
    assert failure["message"]
    if withheld is not None:
        assert withheld not in output


# ---------------------------------------------------------------------------------------------
# Every route returns its schema (AAP 0.5.3 endpoint table)
# ---------------------------------------------------------------------------------------------


async def test_run_routes_return_the_run_schema(seeded: Seeded) -> None:
    """AAP 0.5.3 ``GET /runs/latest``, ``GET /runs/{id}``, ``GET /runs``, ``POST /runs`` → ``Run``.

    The latest default run carries every ``Run`` field: the window, each source's native day
    boundary (Google, then LinkedIn on UTC), the configured ``default_days`` and
    ``trusted_min_days``, the data modes it read and its ``config_hash``. A custom run over a
    preset window ending on the default end answers 201 and heads ``GET /runs?kind=custom``.
    """
    client, facts = seeded.client, seeded.facts
    config = (await client.get(api("/config"))).json()

    latest = await client.get(api("/runs/latest"))
    assert latest.status_code == 200, latest.text
    assert set(latest.json()) == set(schemas.Run.model_fields)
    run = schemas.Run.model_validate(latest.json())
    assert (run.id, run.kind, run.status, run.trigger) == (
        facts.run_id,
        "default",
        "succeeded",
        "scheduled",
    )
    assert [day.source for day in run.source_days] == ["google", "linkedin"]
    assert all(
        day.boundary and (day.start, day.end) == (run.window_start, run.window_end)
        for day in run.source_days
    )
    assert run.source_days[1].boundary == "UTC"
    assert run.default_days == config["window"]["default_days"] == run.days
    assert run.trusted_min_days == config["window"]["trusted_min_days"]
    assert run.short_window is False
    assert run.data_modes == schemas.DataModes(
        google="simulated", linkedin="simulated", hubspot="simulated"
    )
    assert run.config_hash == config["config_hash"]
    assert run.finished_at is not None

    by_id = await client.get(api(f"/runs/{facts.run_id}"))
    assert by_id.status_code == 200, by_id.text
    assert by_id.json() == latest.json()

    days = min(config["window"]["presets"])
    created = await client.post(
        api("/runs"),
        json={
            "window_start": (run.window_end - timedelta(days=days - 1)).isoformat(),
            "window_end": run.window_end.isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    custom = schemas.Run.model_validate(created.json())
    assert (custom.kind, custom.trigger, custom.status, custom.days) == (
        "custom",
        "manual",
        "succeeded",
        days,
    )

    listed = await client.get(api("/runs"), params={"kind": "custom", "limit": 5})
    assert listed.status_code == 200, listed.text
    runs = TypeAdapter(list[schemas.Run]).validate_python(listed.json())
    assert [entry.id for entry in runs] == [custom.id]
    assert (await client.get(api("/runs/latest"))).json()["id"] == facts.run_id


async def test_recommendations_page_returns_five_sections_with_overlay(seeded: Seeded) -> None:
    """AAP 0.5.3 ``GET /recommendations`` → ``RecommendationsPage``.

    Exactly the five sections, keyed ``keywords, campaigns, budget, creative, white_papers`` in
    that order; every item carries its read-time ``current`` overlay; the open discrepancy is
    counted in the page overlay and its C0 item is pinned in the Campaigns discrepancy group.
    """
    client, facts = seeded.client, seeded.facts
    response = await client.get(api("/recommendations"))
    assert response.status_code == 200, response.text
    body = response.json()
    page = schemas.RecommendationsPage.model_validate(body)
    assert [section["key"] for section in body["sections"]] == list(SECTION_KEYS)
    assert page.run.id == facts.run_id
    assert page.overlay.open_discrepancies == 1
    assert page.overlay.source_modes == schemas.SourceModes(
        google="simulated", linkedin="simulated", hubspot="simulated"
    )

    items = page_items(body)
    assert len(items) >= 2
    for item in items:
        assert set(item) == set(schemas.Item.model_fields), item["id"]
        current = schemas.ItemOverlay.model_validate(item["current"])
        assert current.historical is False, item["id"]

    campaigns = next(section for section in page.sections if section.key == "campaigns")
    discrepancies = next(group for group in campaigns.groups if group.key == DISCREPANCY_GROUP)
    assert [(item.id, item.rule_id, item.held) for item in discrepancies.items] == [
        (facts.c0_item_id, "C0", False)
    ]

    by_run = await client.get(api("/recommendations"), params={"run_id": facts.run_id})
    assert by_run.status_code == 200, by_run.text
    assert [item["id"] for item in page_items(by_run.json())] == [item["id"] for item in items]


async def test_feedback_route_returns_feedback_and_refuses_c0(seeded: Seeded) -> None:
    """AAP 0.5.3 ``POST /recommendations/{id}/feedback`` → 201 ``Feedback``; C0 is exempt.

    A snooze on a non-C0 item is recorded for ``feedback.snooze_days`` and hides the item at
    once; feedback on the C0 item is 422 ``feedback_not_allowed``; an unknown item is 404.
    """
    client, facts = seeded.client, seeded.facts
    snooze_days = read_config_file("thresholds.yaml")["feedback"]["snooze_days"]

    response = await client.post(
        api(f"/recommendations/{facts.item_id}/feedback"), json={"kind": "snooze"}
    )
    assert response.status_code == 201, response.text
    assert set(response.json()) == set(schemas.Feedback.model_fields)
    feedback = schemas.Feedback.model_validate(response.json())
    assert (feedback.recommendation_id, feedback.kind) == (facts.item_id, "snooze")
    assert feedback.until - feedback.created_at == timedelta(days=snooze_days)
    assert feedback.created_by == (await client.get(api("/auth/me"))).json()["email"]

    page = (await client.get(api("/recommendations"))).json()
    hidden = next(item for item in page_items(page) if item["id"] == facts.item_id)
    assert hidden["current"]["hidden_by"] == "snooze"

    c0 = await client.post(
        api(f"/recommendations/{facts.c0_item_id}/feedback"), json={"kind": "dismiss"}
    )
    assert_error(c0, 422, "feedback_not_allowed")
    unknown = await client.post(
        api(f"/recommendations/{UNKNOWN_ID}/feedback"), json={"kind": "done"}
    )
    assert_error(unknown, 404, "not_found")


_ITEM_ROW_LOCK_WAITERS: Final = text(
    """
    SELECT count(*) FROM pg_stat_activity
    WHERE datname = current_database() AND pid <> pg_backend_pid()
      AND wait_event_type = 'Lock' AND query LIKE '%FROM recommendation%FOR NO KEY UPDATE%'
    """
)
"""The backends waiting for an item's row lock in ``post_feedback`` (``_hold_item_row``)."""

_ACTIVITY: Final = text(
    """
    SELECT state, wait_event_type, wait_event, left(query, 160) FROM pg_stat_activity
    WHERE datname = current_database() AND pid <> pg_backend_pid()
    """
)
"""The other backends on the test database, reported when the lock wait never forms."""

LOCK_WAIT_LIMIT_S: Final = 15.0
"""How long the concurrency test waits for both requests to queue on the item's row lock."""


def read_activity(engine: Engine, statement: Any) -> list[Any]:
    """Run a ``pg_stat_activity`` query in a transaction of its own and return its rows.

    PostgreSQL reads the activity view once per transaction and serves that snapshot until
    the transaction ends, so a poll that reuses one transaction never sees a change.
    """
    with engine.connect() as connection:
        return list(connection.execute(statement).all())


def feedback_counts(engine: Engine, rec_id: int) -> tuple[int, int]:
    """Return the item's ``recommendation_feedback`` rows and the ``feedback`` rerun requests."""
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT count(*) FROM recommendation_feedback WHERE recommendation_id = :id"),
            {"id": rec_id},
        ).scalar_one()
        requests = connection.execute(
            text("SELECT count(*) FROM analysis_request WHERE reason = 'feedback'")
        ).scalar_one()
    return int(rows), int(requests)


async def post_item_feedback(
    client: httpx.AsyncClient, rec_id: int, body: Mapping[str, object]
) -> schemas.Feedback:
    """Post feedback on one item, assert 201 with exactly the ``Feedback`` keys, return it."""
    response = await client.post(api(f"/recommendations/{rec_id}/feedback"), json=dict(body))
    assert response.status_code == 201, response.text
    assert set(response.json()) == set(schemas.Feedback.model_fields)
    return schemas.Feedback.model_validate(response.json())


async def test_feedback_route_answers_a_repeat_with_the_feedback_it_repeats(
    seeded: Seeded, sync_engine: Engine
) -> None:
    """A repeated identical feedback is answered with the first one and writes nothing.

    The second half of a double click returns 201 with the same ``Feedback``, id and
    ``until`` included: one ``recommendation_feedback`` row and one ``feedback`` rerun
    request (AAP 0.3.2) stand for both.
    """
    client, facts = seeded.client, seeded.facts
    rows_before, requests_before = feedback_counts(sync_engine, facts.item_id)
    assert rows_before == 0

    first = await post_item_feedback(client, facts.item_id, {"kind": "snooze"})
    repeat = await post_item_feedback(client, facts.item_id, {"kind": "snooze"})

    assert repeat == first
    assert feedback_counts(sync_engine, facts.item_id) == (1, requests_before + 1)
    page = (await client.get(api("/recommendations"))).json()
    hidden = next(item for item in page_items(page) if item["id"] == facts.item_id)
    assert hidden["current"]["hidden_by"] == "snooze"


async def test_feedback_route_records_a_different_kind_or_note_as_new_feedback(
    seeded: Seeded, sync_engine: Engine
) -> None:
    """Only a repeat of the item's latest feedback in force is replayed; anything else is new.

    A dismiss after a snooze is a second row and hides the item as ``dismiss``; repeating it
    is replayed. A snooze after that dismiss is new again, because the dismiss is now the
    latest; a snooze with a note differs from the one without, so it is new too.
    """
    client, facts = seeded.client, seeded.facts
    _, requests_before = feedback_counts(sync_engine, facts.item_id)

    snooze = await post_item_feedback(client, facts.item_id, {"kind": "snooze"})
    dismiss = await post_item_feedback(client, facts.item_id, {"kind": "dismiss"})
    dismiss_again = await post_item_feedback(client, facts.item_id, {"kind": "dismiss"})
    snooze_again = await post_item_feedback(client, facts.item_id, {"kind": "snooze"})
    noted = await post_item_feedback(
        client, facts.item_id, {"kind": "snooze", "note": "Revisit after the launch"}
    )

    assert dismiss.id != snooze.id
    assert dismiss_again == dismiss
    assert len({snooze.id, dismiss.id, snooze_again.id, noted.id}) == 4
    assert (snooze_again.kind, noted.kind, noted.note) == (
        "snooze",
        "snooze",
        "Revisit after the launch",
    )
    assert feedback_counts(sync_engine, facts.item_id) == (4, requests_before + 4)


async def test_feedback_route_records_one_row_for_concurrent_identical_requests(
    seeded: Seeded, sync_engine: Engine
) -> None:
    """Two identical feedback requests in flight together write one row: the second waits.

    An outside transaction holds the item's row until both requests queue on its lock, which
    proves they overlap; released, one records the feedback and the other, under READ
    COMMITTED, finds it and answers with it. Both answer 201 with the same ``Feedback``.
    """
    client, facts = seeded.client, seeded.facts
    _, requests_before = feedback_counts(sync_engine, facts.item_id)

    with sync_engine.connect() as blocker:
        blocker.execute(
            text("SELECT id FROM recommendation WHERE id = :id FOR NO KEY UPDATE"),
            {"id": facts.item_id},
        )
        requests = [
            asyncio.ensure_future(post_item_feedback(client, facts.item_id, {"kind": "done"}))
            for _ in range(2)
        ]
        try:
            deadline = asyncio.get_running_loop().time() + LOCK_WAIT_LIMIT_S
            while read_activity(sync_engine, _ITEM_ROW_LOCK_WAITERS)[0][0] < len(requests):
                assert not any(request.done() for request in requests), (
                    "a feedback request finished without waiting for the item's row lock"
                )
                assert asyncio.get_running_loop().time() < deadline, (
                    "the feedback requests never queued on the item's row lock: "
                    f"{read_activity(sync_engine, _ACTIVITY)}"
                )
                await asyncio.sleep(0.05)
        except BaseException:
            blocker.rollback()
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            raise
        blocker.rollback()
        first, second = await asyncio.gather(*requests)

    assert first == second
    assert first.kind == "done"
    assert feedback_counts(sync_engine, facts.item_id) == (1, requests_before + 1)


async def test_feedback_route_refuses_c0_and_historical_items_before_any_repeat(
    seeded: Seeded, sync_engine: Engine
) -> None:
    """The 422 for C0 and the 409 for a historical item come first, repeats included.

    Feedback on the C0 item is 422 ``feedback_not_allowed`` every time and stores nothing.
    Once a dataset the item read is retired, a repeat of the feedback it already carries is
    409 ``subject_retired``, never a replay, and nothing more is stored.
    """
    client, facts = seeded.client, seeded.facts
    for _ in range(2):
        c0 = await client.post(
            api(f"/recommendations/{facts.c0_item_id}/feedback"), json={"kind": "dismiss"}
        )
        assert_error(c0, 422, "feedback_not_allowed")
    assert feedback_counts(sync_engine, facts.c0_item_id)[0] == 0

    await post_item_feedback(client, facts.item_id, {"kind": "snooze"})
    counts = feedback_counts(sync_engine, facts.item_id)
    page = (await client.get(api("/recommendations"))).json()
    item = next(item for item in page_items(page) if item["id"] == facts.item_id)
    provenance_ids = sorted({entry["dataset_id"] for entry in item["evidence"]["provenance"]})
    assert provenance_ids
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE source_dataset SET state = 'retired', retired_at = now() "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": provenance_ids},
        )

    for kind in ("snooze", "dismiss"):
        refused = await client.post(
            api(f"/recommendations/{facts.item_id}/feedback"), json={"kind": kind}
        )
        assert_error(refused, 409, "subject_retired")
    assert feedback_counts(sync_engine, facts.item_id) == counts


async def test_decision_routes_return_the_decision_schema(seeded: Seeded) -> None:
    """AAP 0.5.3 ``GET /decisions`` → ``Decision[]``; ``POST /decisions`` → 201 ``Decision``.

    Every logged row carries its decision shape and complete expected state; the seed's user
    decision is listed. A status patch on an own-budget campaign answers 201 with the shape,
    the patched status, the budget it kept, the patch it carried and the signed-in author.
    """
    client, facts = seeded.client, seeded.facts
    decisions = TypeAdapter(list[schemas.Decision])

    listed = await client.get(api("/decisions"))
    assert listed.status_code == 200, listed.text
    log = decisions.validate_python(listed.json())
    assert log
    assert {row.shape for row in log} <= {"own_budget", "shared_member", "budget"}
    assert {row.origin for row in log} == {"baseline", "user"}
    paused = next(row for row in log if row.id == facts.paused_decision_id)
    assert (paused.subject.id, paused.shape, paused.status, paused.origin) == (
        facts.paused_campaign_id,
        "own_budget",
        "paused",
        "user",
    )

    history = await client.get(
        api("/decisions"),
        params={"subject_type": "campaign", "subject_id": facts.other_campaign_id},
    )
    assert history.status_code == 200, history.text
    [baseline] = decisions.validate_python(history.json())
    assert (baseline.origin, baseline.status) == ("baseline", "active")

    created = await client.post(
        api("/decisions"),
        json={
            "subject": {"type": "campaign", "id": facts.other_campaign_id},
            "patch": {"status": "paused"},
        },
    )
    assert created.status_code == 201, created.text
    assert set(created.json()) == set(schemas.Decision.model_fields)
    decision = schemas.Decision.model_validate(created.json())
    assert (decision.subject.type, decision.subject.id, decision.shape) == (
        "campaign",
        facts.other_campaign_id,
        "own_budget",
    )
    assert (decision.status, decision.origin, decision.tombstone) == ("paused", "user", False)
    assert (decision.daily_budget, decision.total_budget) == (
        baseline.daily_budget,
        baseline.total_budget,
    )
    assert dict(decision.patch) == {"status": "paused"}
    assert decision.decided_by == (await client.get(api("/auth/me"))).json()["email"]


async def test_discrepancy_routes_list_and_resolve(seeded: Seeded) -> None:
    """AAP 0.5.3 ``GET /discrepancies?state=open`` and ``POST /discrepancies/{id}/resolve``.

    The open ``status`` discrepancy is listed with its subject, shape, both sides and exactly
    the resolutions its (kind, subject type) pair allows (AAP 0.5.4.4). Resolving it with
    ``platform_correct`` answers 200 ``Discrepancy``, closed at once, and adopts the platform
    status as the campaign's new decision.
    """
    client, facts = seeded.client, seeded.facts
    discrepancies = TypeAdapter(list[schemas.Discrepancy])

    listed = await client.get(api("/discrepancies"), params={"state": "open"})
    assert listed.status_code == 200, listed.text
    [row] = discrepancies.validate_python(listed.json())
    assert set(listed.json()[0]) == set(schemas.Discrepancy.model_fields)
    assert (row.id, row.kind, row.state, row.shape) == (
        facts.discrepancy_id,
        "status",
        "open",
        "own_budget",
    )
    assert (row.subject.type, row.subject.id, row.subject.source) == (
        "campaign",
        facts.paused_campaign_id,
        "google",
    )
    assert row.decided_value is not None
    assert row.decided_value.get("status") == "paused"
    assert row.platform_value is not None
    assert row.platform_value.get("status") == "active"
    assert row.resolutions == ["platform_correct", "decision_correct"]

    resolved = await client.post(
        api(f"/discrepancies/{facts.discrepancy_id}/resolve"),
        json={"resolution": "platform_correct", "note": "Running as intended"},
    )
    assert resolved.status_code == 200, resolved.text
    closed = schemas.Discrepancy.model_validate(resolved.json())
    assert (closed.id, closed.state, closed.resolution) == (
        facts.discrepancy_id,
        "resolved",
        "platform_correct",
    )
    assert closed.resolved_at is not None

    remaining = await client.get(api("/discrepancies"), params={"state": "open"})
    assert remaining.status_code == 200, remaining.text
    assert remaining.json() == []
    latest = await client.get(
        api("/decisions"),
        params={"subject_type": "campaign", "subject_id": facts.paused_campaign_id, "limit": 1},
    )
    [adopted] = TypeAdapter(list[schemas.Decision]).validate_python(latest.json())
    assert (adopted.origin, adopted.status) == ("resolution", "active")


_ACTIVE_GOOGLE_ZONE: Final = text(
    """
    SELECT a.timezone
    FROM ad_account AS a
    JOIN source_dataset AS d ON d.id = a.dataset_id
    WHERE d.source = 'google' AND d.role = 'primary' AND d.state = 'active'
      AND a.source = 'google' AND a.timezone IS NOT NULL
    ORDER BY a.id
    LIMIT 1
    """
)
"""The stored time zone of the active Google dataset's account, which ends Google's days."""


async def test_sources_route_returns_every_source_in_order(
    seeded: Seeded, sync_engine: Engine
) -> None:
    """AAP 0.5.3 ``GET /sources`` → ``Source[]``, ordered google, linkedin, hubspot.

    Each entry carries the source's mode, health status, reason, native day boundary, active
    dataset and the freshness of each of its named datasets; Google adds its webhook calls in
    the last 24 h against the product's cap of 1,000, and HubSpot the companion CRM dataset of
    the simulated LinkedIn dataset. Google's day boundary is the stored time zone of the active
    Google dataset's account; LinkedIn's and HubSpot's are UTC (AAP 0.5.4.1).
    """
    with sync_engine.connect() as connection:
        google_zone = connection.execute(_ACTIVE_GOOGLE_ZONE).scalar_one()
    assert isinstance(google_zone, str)
    assert google_zone.strip()

    response = await seeded.client.get(api("/sources"))
    assert response.status_code == 200, response.text
    entries = TypeAdapter(list[schemas.Source]).validate_python(response.json())
    assert [entry.source for entry in entries] == list(SOURCES) == ["google", "linkedin", "hubspot"]
    for entry in entries:
        assert set(response.json()[SOURCES.index(entry.source)]) == set(schemas.Source.model_fields)
        assert entry.mode == "simulated", entry.source
        assert entry.status == "ok", entry.source
        assert entry.reason, entry.source
        assert entry.active_dataset is not None, entry.source
        assert (
            entry.active_dataset.mode,
            entry.active_dataset.role,
            entry.active_dataset.state,
        ) == ("simulated", "primary", "active")
        assert entry.datasets, entry.source
        for freshness in entry.datasets:
            assert freshness.dataset in DATASETS, freshness.dataset
            assert freshness.dataset.startswith(f"{entry.source}."), freshness.dataset
        assert entry.sync_pending is False

    google, linkedin, hubspot = entries
    assert google.day_boundary == google_zone.strip()
    assert (linkedin.day_boundary, hubspot.day_boundary) == ("UTC", "UTC")
    assert isinstance(google.calls_24h, int)
    assert google.calls_24h >= 0
    assert google.call_cap == GOOGLE_CALL_CAP
    assert (linkedin.calls_24h, linkedin.call_cap) == (None, None)
    assert (hubspot.calls_24h, hubspot.call_cap) == (None, None)
    assert hubspot.companion_dataset is not None
    assert (hubspot.companion_dataset.role, hubspot.companion_dataset.mode) == (
        "companion",
        "simulated",
    )
    assert all(row.dataset.startswith("hubspot.") for row in hubspot.companion_datasets)


async def test_sources_route_google_day_boundary_follows_the_stored_account_zone(
    seeded: Seeded, sync_engine: Engine
) -> None:
    """Google's ``day_boundary`` is chosen as ``runner.window_inputs`` chooses its zone.

    The account rows of the active Google dataset are read lowest id first and the first zone
    that is not blank is reported stripped. A blank zone is no zone: the boundary is then
    ``None``, never UTC, while LinkedIn's and HubSpot's stay UTC.
    """

    async def boundaries(zone: str) -> dict[str, str | None]:
        with sync_engine.begin() as connection:
            updated = connection.execute(
                text(
                    "UPDATE ad_account SET timezone = :zone WHERE source = 'google' "
                    "AND dataset_id = (SELECT id FROM source_dataset WHERE source = 'google' "
                    "AND role = 'primary' AND state = 'active')"
                ),
                {"zone": zone},
            ).rowcount
        assert updated >= 1
        response = await seeded.client.get(api("/sources"))
        assert response.status_code == 200, response.text
        return {entry["source"]: entry["day_boundary"] for entry in response.json()}

    assert await boundaries("  Europe/Berlin ") == {
        "google": "Europe/Berlin",
        "linkedin": "UTC",
        "hubspot": "UTC",
    }
    assert await boundaries("   ") == {"google": None, "linkedin": "UTC", "hubspot": "UTC"}


async def test_sources_route_google_day_boundary_unknown_without_a_dataset(
    authed_client: httpx.AsyncClient,
) -> None:
    """Before Google has an active dataset its day boundary is ``None``, never a guessed zone.

    On an empty database (no dataset of any source yet) LinkedIn's and HubSpot's boundaries
    are still UTC: they do not depend on stored data.
    """
    response = await authed_client.get(api("/sources"))
    assert response.status_code == 200, response.text
    assert {entry["source"]: entry["day_boundary"] for entry in response.json()} == {
        "google": None,
        "linkedin": "UTC",
        "hubspot": "UTC",
    }


async def test_sources_route_reports_no_mode_for_a_live_placeholder(
    authed_client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A ``live`` connection row with no active dataset reports ``mode`` ``null``, never live.

    Such a row never verified live access: here a Google source whose first live query hit a
    defect, so it holds status ``error`` with the defect as its reason and has no dataset.
    ``GET /sources`` reports that status and reason with no mode, as the overlay's
    ``source_modes`` does. Once a live dataset activates (the row's mode and active dataset set
    together), the same row reports ``live``.
    """
    reason = "QueryError in Q-G1: UNRECOGNIZED_FIELD"
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO source_connection (source, mode, status, reason, last_attempt_at) "
                "VALUES ('google', 'live', 'error', :reason, now())"
            ),
            {"reason": reason},
        )

    response = await authed_client.get(api("/sources"))
    assert response.status_code == 200, response.text
    google = schemas.Source.model_validate(response.json()[SOURCES.index("google")])
    assert (google.mode, google.status, google.reason) == (None, "error", reason)
    assert (google.active_dataset, google.datasets) == (None, [])

    with sync_engine.begin() as connection:
        dataset_id = connection.execute(
            text(
                "INSERT INTO source_dataset (source, mode, state, role, activated_at) "
                "VALUES ('google', 'live', 'active', 'primary', now()) RETURNING id"
            )
        ).scalar_one()
        connection.execute(
            text(
                "UPDATE source_connection SET status = 'ok', reason = NULL, "
                "active_dataset_id = :dataset_id WHERE source = 'google'"
            ),
            {"dataset_id": dataset_id},
        )

    activated = await authed_client.get(api("/sources"))
    assert activated.status_code == 200, activated.text
    google = schemas.Source.model_validate(activated.json()[SOURCES.index("google")])
    assert (google.mode, google.status) == ("live", "ok")
    assert google.active_dataset is not None
    assert (google.active_dataset.id, google.active_dataset.mode) == (dataset_id, "live")


async def test_sync_request_202_then_409(authed_client: httpx.AsyncClient) -> None:
    """AAP 0.5.3 ``POST /sources/{source}/sync``: 202, then 409 while one is pending.

    The request is queued for the worker (``picked_at`` null); a second one for the same source
    is 409 ``sync_pending``; another source has its own slot; an unknown source is 422.
    """
    queued = await authed_client.post(api("/sources/google/sync"))
    assert queued.status_code == 202, queued.text
    request = schemas.SyncRequestView.model_validate(queued.json())
    assert (request.source, request.picked_at) == ("google", None)

    assert_error(await authed_client.post(api("/sources/google/sync")), 409, "sync_pending")
    other = await authed_client.post(api("/sources/linkedin/sync"))
    assert other.status_code == 202, other.text
    assert_error(await authed_client.post(api("/sources/nope/sync")), 422, "validation_error")

    sources = (await authed_client.get(api("/sources"))).json()
    assert {entry["source"]: entry["sync_pending"] for entry in sources} == {
        "google": True,
        "linkedin": True,
        "hubspot": False,
    }


async def test_config_route_returns_the_config_view(seeded: Seeded) -> None:
    """AAP 0.5.3 ``GET /config`` → ``ConfigView``, read from ``config/*.yaml`` and the portal.

    The window settings (``default_days``, ``trusted_min_days``, ``presets``), the HubSpot MQL
    and Qualified Lead bindings, the white paper registry and the thresholds equal the
    repository files; the bindings verified against the simulated portals are present, and
    ``config_hash`` is the one the seeded run stored.
    """
    client, facts = seeded.client, seeded.facts
    response = await client.get(api("/config"))
    assert response.status_code == 200, response.text
    assert set(response.json()) == set(schemas.ConfigView.model_fields)
    view = schemas.ConfigView.model_validate(response.json())

    thresholds = read_config_file("thresholds.yaml")
    window = thresholds["window"]
    assert view.window.default_days == window["default_days"]
    assert view.window.trusted_min_days == window["trusted_min_days"]
    assert view.window.presets == sorted(window["presets"])
    assert view.window.default_days in view.window.presets
    assert view.window.max_days == window["max_days"]
    assert view.thresholds["feedback"] == thresholds["feedback"]

    mapping = read_config_file("hubspot_mapping.yaml")
    assert (view.hubspot_mapping.mql.mode, view.hubspot_mapping.mql.stage_labels) == (
        mapping["mql"]["mode"],
        [mapping["mql"]["stage_label"]],
    )
    assert view.hubspot_mapping.ql.stage_labels == mapping["ql"]["stage_labels"]
    assert view.hubspot_mapping.booking_sources == mapping["crm"]["booking_sources"]

    assets = read_config_file("content_assets.yaml")["assets"]
    assert [(asset.slug, asset.title, asset.lead_forms) for asset in view.content_assets] == [
        (asset["slug"], asset["title"], asset["lead_forms"]) for asset in assets
    ]

    assert view.crm_bindings is not None
    assert view.companion_crm_bindings is not None
    run = (await client.get(api(f"/runs/{facts.run_id}"))).json()
    assert view.config_hash == run["config_hash"]
    assert len(view.config_hash) == 64


def test_openapi_lists_every_route() -> None:
    """AAP 0.5.3: the OpenAPI document publishes exactly the endpoint table under ``/api/v1``.

    Every path operation is one of the table's endpoints and every endpoint is published; the
    401 matrix of :func:`test_protected_routes_401_without_session` covers every one of them
    except the two that need no session.
    """
    app = create_app()
    paths = app.openapi()["paths"]
    published = {
        (method, path.removeprefix(API_PREFIX))
        for path, operations in paths.items()
        for method in operations
    }
    assert all(path.startswith(f"{API_PREFIX}/") for path in paths), sorted(paths)
    assert published == AAP_ROUTES
    matrix = {(request.method.lower(), request.template) for request in PROTECTED_REQUESTS}
    assert matrix == AAP_ROUTES - UNAUTHENTICATED_ROUTES
    assert len(PROTECTED_REQUESTS) == len(matrix)


# ---------------------------------------------------------------------------------------------
# Boundaries and redaction (AAP 0.3.2; AAP 0.11.1 logging points)
# ---------------------------------------------------------------------------------------------


async def test_failed_login_logs_redacted(
    api_client: httpx.AsyncClient,
    admin_user: AppUser,
    test_identity: AdminIdentity,
    captured_logs: CapturedLogs,
) -> None:
    """AAP 0.11.1: authentication failures are logged, without emails, passwords or secrets.

    An unknown email and a wrong password each log ``auth.login_failed`` with its reason; no
    line holds either email, either password or the ``SESSION_SECRET``.
    """
    del admin_user
    settings = get_settings()
    secret = settings.session_secret
    assert secret is not None
    # create_app() (the api_client fixture) reconfigured logging to stdout; capture from here.
    captured_logs.configure(settings=settings)
    captured_logs.clear()

    unknown_email, wrong_password = "nobody@example.org", "not-the-admin-password-0451"
    unknown = await post_login(
        api_client, test_identity, email=unknown_email, password=test_identity.password
    )
    assert_error(unknown, 401, "invalid_credentials")
    wrong = await post_login(
        api_client, test_identity, email=test_identity.email, password=wrong_password
    )
    assert_error(wrong, 401, "invalid_credentials")

    failures = [record for record in captured_logs() if record.get("event") == "auth.login_failed"]
    assert sorted(record.get("reason") for record in failures) == ["bad_password", "unknown_user"]
    logged = captured_logs.text
    for value in (
        unknown_email,
        test_identity.email,
        wrong_password,
        test_identity.password,
        secret.get_secret_value(),
    ):
        assert value not in logged


_BOUNDARY_PROGRAM: Final = textwrap.dedent(
    f"""
    import json
    import sys

    from ads_engine.api.app import create_app

    app = create_app()
    forbidden = ("ads_engine.connectors", "ads_engine.sync", "ads_engine.simulation")
    loaded = sorted(
        name
        for name in sys.modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    )
    routes = sorted(name for name in sys.modules if name.startswith("ads_engine.api.routes."))
    print({SUBPROCESS_MARKER!r} + json.dumps({{"loaded": loaded, "routes": routes}}))
    """
)
"""Builds the application in a fresh interpreter and reports which ``ads_engine`` modules it
loaded; the result line is prefixed with :data:`SUBPROCESS_MARKER`."""


def test_api_never_loads_connectors_or_simulation() -> None:
    """AAP 0.3.2: the API never calls LinkedIn, n8n or HubSpot: it does not even import them.

    A fresh interpreter builds the whole application (every route module loaded) with the
    test's database URL and session secret; ``ads_engine.connectors``, ``ads_engine.sync`` and
    ``ads_engine.simulation`` are absent from ``sys.modules`` afterwards.
    """
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and program, no user input
        [sys.executable, "-c", _BOUNDARY_PROGRAM],
        cwd=BACKEND_DIR,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.startswith(SUBPROCESS_MARKER)]
    assert len(lines) == 1, completed.stdout
    result = json.loads(lines[0].removeprefix(SUBPROCESS_MARKER))
    assert result["loaded"] == []
    assert set(result["routes"]) >= {
        f"ads_engine.api.routes.{name}"
        for name in (
            "auth",
            "runs",
            "recommendations",
            "decisions",
            "discrepancies",
            "sources",
            "config",
            "health",
        )
    }


# ---------------------------------------------------------------------------------------------
# Configuration errors (AAP 0.5.3 error envelope; CWE-209: no internals in a response)
# ---------------------------------------------------------------------------------------------

CONFIG_ERROR_HINT: Final = "Check the API logs for details."
"""The fixed sentence that ends every ``config_error`` message."""

YAML_LEAK_MARKER: Final = "leaky_marker_9f3"
"""A key on the broken line of an invalid ``thresholds.yaml``; PyYAML quotes that line."""

INVALID_THRESHOLDS_YAML: Final = f"window:\n  default_days: 28\n{YAML_LEAK_MARKER}: value: other\n"
"""A ``thresholds.yaml`` PyYAML rejects on its third line ("mapping values are not allowed")."""

CONFIG_ERROR_ROUTE: Final = "/test/config-error"
"""A route the focused message tests add to the real application; it raises the case's error."""


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    """``tmp_path`` with symlinks resolved, as ``Path.cwd()`` reports it after a chdir there."""
    return tmp_path.resolve()


def make_working_directory(root: Path) -> Path:
    """Create and return ``root/empty/nested``, which has no ``config/`` beside or above it."""
    directory = root / "empty" / "nested"
    directory.mkdir(parents=True)
    return directory


def write_config_dir(root: Path, thresholds_text: str | None) -> Path:
    """Write ``root/config``: the repository's other config files, and ``thresholds_text``.

    Args:
        root: The directory to create ``config/`` in.
        thresholds_text: The content of ``thresholds.yaml``; ``None`` leaves the file out.

    Returns:
        The written config directory.
    """
    directory = root / "config"
    directory.mkdir()
    for name in CONFIG_FILES:
        if name != "thresholds.yaml":
            source = (CONFIG_DIR / name).read_text(encoding="utf-8")
            (directory / name).write_text(source, encoding="utf-8")
    if thresholds_text is not None:
        (directory / "thresholds.yaml").write_text(thresholds_text, encoding="utf-8")
    return directory


async def get_config_error(
    client: httpx.AsyncClient, captured_logs: CapturedLogs, path: str = "/config"
) -> tuple[str, dict[str, Any]]:
    """Request ``GET path`` expecting 500 ``config_error``; return its message and its log line.

    Logging is pointed at ``captured_logs`` first, because building the application
    reconfigured it to stdout.

    Returns:
        The response's message and the one ``api.config_error`` record the request logged,
        after checking that record names the request's path and method.
    """
    captured_logs.configure(settings=get_settings())
    captured_logs.clear()
    response = await client.get(api(path))
    message = assert_error(response, 500, "config_error").error.message
    records = [record for record in captured_logs() if record.get("event") == "api.config_error"]
    assert len(records) == 1, records
    record = records[0]
    assert (record["path"], record["method"]) == (api(path), "GET"), record
    return message, record


@pytest.mark.parametrize(
    ("thresholds_text", "logged_detail"),
    [
        pytest.param(INVALID_THRESHOLDS_YAML, YAML_LEAK_MARKER, id="invalid-yaml"),
        pytest.param(None, "file not found", id="missing-file"),
    ],
)
async def test_config_error_from_a_config_file_names_only_the_file(
    authed_client: httpx.AsyncClient,
    captured_logs: CapturedLogs,
    monkeypatch: pytest.MonkeyPatch,
    config_root: Path,
    thresholds_text: str | None,
    logged_detail: str,
) -> None:
    """CWE-209: a broken ``thresholds.yaml`` is 500 ``config_error`` naming only the file.

    The working directory holds a ``config/`` whose ``thresholds.yaml`` is invalid YAML or
    missing; nothing is stored on ``app.state.thresholds``, so ``GET /config`` loads the files
    on first use. The message names ``thresholds.yaml`` and points at the logs: neither the
    directory's disk path nor PyYAML's diagnostic and quoted source line reach the client. The
    ``api.config_error`` line carries the full detail.
    """
    directory = write_config_dir(config_root, thresholds_text)
    monkeypatch.chdir(config_root)

    message, record = await get_config_error(authed_client, captured_logs)

    assert message == f"Configuration error in thresholds.yaml. {CONFIG_ERROR_HINT}"
    internals = (str(config_root), YAML_LEAK_MARKER, "invalid YAML", "line 3", "file not found")
    for internal in internals:
        assert internal not in message
    assert (record["file"], record["key"]) == ("thresholds.yaml", None)
    assert logged_detail in record["error"]
    if thresholds_text is None:
        assert str(directory / "thresholds.yaml") in record["error"]
    else:
        assert "invalid YAML" in record["error"]


@pytest.mark.parametrize(
    "explicit_directory",
    [False, True],
    ids=["no-candidate-directory", "missing-explicit-directory"],
)
async def test_config_error_for_a_missing_config_directory_names_no_path(
    authed_client: httpx.AsyncClient,
    captured_logs: CapturedLogs,
    monkeypatch: pytest.MonkeyPatch,
    config_root: Path,
    explicit_directory: bool,
) -> None:
    """CWE-209: a missing config directory is 500 ``config_error`` naming no file and no path.

    Two loader failures: no ``config/`` beside or above the working directory (the error's
    file is ``config`` and its detail names both candidate paths), and a load from an explicit
    directory that does not exist (the error's file is that directory's disk path). Either
    way the message is the generic one, and the path is only in the ``api.config_error`` line.
    """
    if explicit_directory:
        missing = config_root / "missing-config"
        monkeypatch.setattr(Thresholds, "load", functools.partial(Thresholds.load, missing))
        expected_file = str(missing)
    else:
        monkeypatch.chdir(make_working_directory(config_root))
        expected_file = "config"

    message, record = await get_config_error(authed_client, captured_logs)

    assert message == f"Configuration error. {CONFIG_ERROR_HINT}"
    assert str(config_root) not in message
    assert record["file"] == expected_file
    assert str(config_root) in record["error"]


async def test_config_error_for_a_corrupt_stored_binding_names_document_and_key(
    seeded: Seeded, sync_engine: Engine, captured_logs: CapturedLogs
) -> None:
    """CWE-209: a corrupt ``source_dataset.crm_bindings`` names the document and key only.

    The active HubSpot dataset's stored ``mql`` binding loses its required keys. ``GET
    /config`` is 500 ``config_error`` naming ``source_dataset.crm_bindings`` and ``mql``; the
    decoder's detail, which lists the missing keys, is only in the ``api.config_error`` line.
    """
    with sync_engine.begin() as connection:
        corrupted = connection.execute(
            text(
                "UPDATE source_dataset "
                "SET crm_bindings = jsonb_set(crm_bindings, '{mql}', '{\"available\": true}') "
                "WHERE source = 'hubspot' AND role = 'primary' AND state = 'active' "
                "AND crm_bindings IS NOT NULL"
            )
        ).rowcount
    assert corrupted == 1

    message, record = await get_config_error(seeded.client, captured_logs)

    assert message == (
        f"Configuration error in source_dataset.crm_bindings at mql. {CONFIG_ERROR_HINT}"
    )
    assert (record["file"], record["key"]) == ("source_dataset.crm_bindings", "mql")
    assert record["error"].startswith("source_dataset.crm_bindings: mql: ")
    detail = record["error"].removeprefix("source_dataset.crm_bindings: mql: ")
    assert detail
    assert detail not in message


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ConfigError("hubspot_mapping.yaml", "ql.stage_labels[0]", "'Lead' is listed twice"),
            "Configuration error in hubspot_mapping.yaml at ql.stage_labels[0].",
            id="logical-file-and-key",
        ),
        pytest.param(
            ConfigError(
                "content_assets.yaml", "assets[1].lead_forms[0]", "listed under two assets"
            ),
            "Configuration error in content_assets.yaml at assets[1].lead_forms[0].",
            id="indexed-key",
        ),
        pytest.param(
            ConfigError("/srv/ads/config", None, "config directory not found"),
            "Configuration error.",
            id="disk-path-file",
        ),
        pytest.param(
            ConfigError("../config/thresholds.yaml", "window.presets", "unreadable /srv/ads"),
            "Configuration error at window.presets.",
            id="path-shaped-file",
        ),
        pytest.param(
            ConfigError(
                "config", None, "cannot compute config_hash: Permission denied: '/srv/ads'"
            ),
            "Configuration error.",
            id="generic-file",
        ),
        pytest.param(
            ConfigError("thresholds.yaml", "k1./srv/ads/data", "unknown key below /srv"),
            "Configuration error in thresholds.yaml.",
            id="key-with-a-path",
        ),
        pytest.param(
            ConfigError("thresholds.yaml", "window.presets: [14, 28", "unexpected end at /srv"),
            "Configuration error in thresholds.yaml.",
            id="key-with-yaml-text",
        ),
        pytest.param(
            ConfigError("thresholds.yaml", "window.presets\n", "trailing newline below /srv"),
            "Configuration error in thresholds.yaml.",
            id="key-with-a-newline",
        ),
        pytest.param(
            ConfigError("thresholds.yaml", "k" * 121, "overlong key below /srv"),
            "Configuration error in thresholds.yaml.",
            id="overlong-key",
        ),
        pytest.param(
            ConfigError("thresholds.yaml", "k1.r\u00e9gle", "non-ASCII key below /srv"),
            "Configuration error in thresholds.yaml.",
            id="non-ascii-key",
        ),
    ],
)
async def test_config_error_message_names_only_logical_parts(
    captured_logs: CapturedLogs, error: ConfigError, expected: str
) -> None:
    """CWE-209: the ``config_error`` message is built from logical parts, never the detail.

    The real application, with one extra route raising the case's :class:`ConfigError`,
    answers 500 ``config_error``. The file is named only when it is one of the three config
    files or ``source_dataset.crm_bindings``; the key only when it is a short run of ASCII
    identifiers and ``[n]`` indexes; the detail never. The log line keeps the error's full text.
    """
    app = create_app()

    async def raise_config_error() -> None:
        """Fail the request with the case's error, as a loader or decoder would."""
        raise error

    app.add_api_route(api(CONFIG_ERROR_ROUTE), raise_config_error, methods=["GET"])
    async with serve(app) as client:
        message, record = await get_config_error(client, captured_logs, CONFIG_ERROR_ROUTE)

    assert message == f"{expected} {CONFIG_ERROR_HINT}"
    assert error.detail not in message
    assert "/srv" not in message
    assert record["error"] == str(error)
    assert (record["file"], record["key"]) == (error.file, error.key)
