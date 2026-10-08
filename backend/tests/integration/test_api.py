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
* **Startup guard** (AAP 0.3.2): plain ``http://`` is accepted only on ``localhost`` and
  ``127.0.0.1``, and the API refuses to start without ``SESSION_SECRET``.
* **Every route returns its schema** (AAP 0.5.3 endpoint table), on simulated data seeded the
  way the worker seeds a first start.
* **Boundaries and redaction** (AAP 0.3.2, 0.11.1): a failed login is logged without the email,
  the password or the session secret, and the API process never imports the connectors, the sync
  jobs or the simulator.

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

import json
import logging
import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
import pytest
import structlog
import yaml
from fastapi import FastAPI
from pydantic import TypeAdapter
from sqlalchemy import Engine, inspect, text

from ads_engine import logging as ads_logging
from ads_engine.analysis import runner
from ads_engine.api import app as api_app
from ads_engine.api import deps, schemas, security
from ads_engine.api.app import API_PREFIX, create_app
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
    off, and the handlers, levels, structlog configuration, redactor and last-resort handler
    are put back afterwards.
    """
    real_configure = ads_logging.configure_logging

    def configure_without_cache(**kwargs: Any) -> None:
        """Configure logging as the product does, but with structlog's logger caching off."""
        real_configure(**kwargs)
        structlog.configure(cache_logger_on_first_use=False)

    # create_app() binds the name at import; conftest imports it from the module at call time.
    monkeypatch.setattr(api_app, "configure_logging", configure_without_cache)
    monkeypatch.setattr(ads_logging, "configure_logging", configure_without_cache)
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
    api_client: httpx.AsyncClient,
    admin_user: AppUser,
    login: LoginFunction,
    test_identity: AdminIdentity,
) -> None:
    """AAP 0.5.3: 401, 403, 404, 409, 422 and 429 all render ``{"error": {"code", "message"}}``.

    Each body is exactly the envelope, with no other key at either level: 401 without a
    session, 403 without the CSRF header, 404 for an unknown run and an unknown path, 409 for a
    second pending sync request, 422 for an invalid body and 429 for the 6th login attempt.
    """
    del admin_user
    csrf = dict(test_identity.csrf_headers)
    assert_error(await api_client.get(api("/auth/me")), 401, "unauthenticated")
    assert_error(await api_client.post(api("/runs"), json={}), 403, "csrf_header_missing")

    await login(api_client)  # attempt 1 of the minute
    assert_error(await api_client.get(api("/runs/999999")), 404, "not_found")
    assert_error(await api_client.get(api("/no-such-route")), 404, "not_found")
    first_sync = await api_client.post(api("/sources/google/sync"), headers=csrf)
    assert first_sync.status_code == 202, first_sync.text
    assert_error(
        await api_client.post(api("/sources/google/sync"), headers=csrf), 409, "sync_pending"
    )
    invalid = await api_client.post(api("/runs"), json={"window_start": "soon"}, headers=csrf)
    assert_error(invalid, 422, "validation_error")

    for _ in range(security.LOGIN_MAX_ATTEMPTS - 1):  # attempts 2 to 5
        wrong = await post_login(
            api_client, test_identity, email=test_identity.email, password="wrong-password"
        )
        assert_error(wrong, 401, "invalid_credentials")
    limited = await post_login(
        api_client, test_identity, email=test_identity.email, password=test_identity.password
    )
    assert_error(limited, 429, "rate_limited")
    assert int(limited.headers["Retry-After"]) >= 1


async def test_healthz_without_auth(api_client: httpx.AsyncClient) -> None:
    """AAP 0.5.3: ``GET /healthz`` needs no session and reports the database reachable."""
    response = await api_client.get(api("/healthz"))
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}
    assert response.headers.get_list("set-cookie") == []


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


async def test_sources_route_returns_every_source_in_order(seeded: Seeded) -> None:
    """AAP 0.5.3 ``GET /sources`` → ``Source[]``, ordered google, linkedin, hubspot.

    Each entry carries the source's mode, health status, reason, active dataset and the
    freshness of each of its named datasets; Google adds its webhook calls in the last 24 h
    against the product's cap of 1,000, and HubSpot the companion CRM dataset of the simulated
    LinkedIn dataset.
    """
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
