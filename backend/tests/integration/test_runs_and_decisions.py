"""Run selection, changed defaults, the shared analysis lock, decision patches and frozen outputs.

AAP 0.11.2, row ``integration/test_runs_and_decisions.py``. One named test (or group of tests)
per guarantee of that row:

* **Run selection** (AAP 0.5.3 "Run kinds", "Selection"): a manual 90-day run never becomes
  ``/runs/latest``, and ``/recommendations`` without ``run_id`` returns the default run; custom
  windows outside their bounds are 422.
* **Changed defaults** (AAP 0.5.4.1): ``window.default_days: 21`` makes the default run 21 days
  and the configuration marks that preset; a default missing from ``window.presets`` fails
  config load.
* **Analysis lock** (AAP 0.5.3 "One lock for every entry point", "Lock unavailable", "Crash
  recovery"): ``POST /runs`` answers 409 ``run_in_progress`` while another session holds the
  lock, the CLI exits 3, the worker leaves its ``analysis_request`` pending, and a ``running``
  row left by a crashed process is marked ``failed``.
* **Decision patches**, **Budget-subject patches** and **Shared-member patches** (AAP 0.5.2
  "Decisions: patch model" and the shapes table): every 422 rule, through ``POST /decisions``.
* **Retired subjects** (AAP 0.3.2): ``POST /decisions`` and ``POST /discrepancies/{id}/resolve``
  on a subject of a retired dataset answer 409 ``subject_retired``.
* **Stored outputs** (AAP 0.5.3 "Where outputs are frozen"): findings, withheld patterns and
  suppression counts are returned from storage without re-running any rule.

How the data is set up. The product's own first-start path seeds the database: a fixed-clock
``SourceModeController`` and ``sync.mode.bootstrap_sources(..., force_simulated=True)`` with
``BACKFILL_DAYS=120`` (so a 90-day custom window has data), then one default
``runner.run_analysis`` at :data:`NOW` that serves the bootstrap's analysis requests, exactly as
the worker would. That bootstrap takes over a minute, so it runs **once per module**
(:func:`seed_snapshot`) and its committed state is copied into a private schema; before every
test, after ``conftest.db`` has truncated every table, :func:`seeded` copies that state back.
Every test therefore starts from the identical seeded database while the bootstrap's cost is
paid once.

The API runs in process on ``create_app()``. Its clock (``deps.get_now``) is fixed at
:data:`NOW` *before* the admin logs in: a session is refused when it was issued after the
request's clock, so ``conftest.authed_client`` (signed in on the real clock, which is later than
:data:`NOW`) cannot serve a fixed-clock application. :func:`client` therefore signs in through
``conftest.login`` on its own application, with the conftest admin and CSRF header. The
application's logging configuration goes through ``conftest.captured_logs`` and is undone
after each test (:func:`app`), so no later test module inherits it.

Real PostgreSQL 18 (``TEST_DATABASE_URL``); no network: every source is simulated through the
simulator's ``MockTransport``, and the seeding runs under the same real-transport guard the
conftest applies to every test.
"""

import asyncio
import re
import subprocess
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

import anyio
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, Table, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import ColumnElement

from ads_engine.analysis import runner
from ads_engine.analysis.lock import analysis_lock
from ads_engine.analysis.thresholds import ConfigError, Thresholds
from ads_engine.api import app as api_app
from ads_engine.api import deps, schemas
from ads_engine.api.app import create_app
from ads_engine.db import session as db_session
from ads_engine.db.models import (
    DATASETS,
    AdAccount,
    AnalysisRequest,
    AnalysisRun,
    AppUser,
    Budget,
    Campaign,
    DatasetFreshness,
    Decision,
    Discrepancy,
    EntitySnapshot,
    Recommendation,
    RunFinding,
    RunSuppression,
    RunWithheld,
    SourceDataset,
)

# The settings cache must be cleared around the module's one-time seeding, which runs outside
# conftest's per-test environment isolation; the seeding helper's contract names get_settings().
from ads_engine.settings import Settings, get_settings
from ads_engine.sync.datasets import DatasetManager
from ads_engine.sync.mode import SourceModeController, bootstrap_sources
from ads_engine.sync.scheduler import Scheduler

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

NOW: Final = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The fixed clock of the seeding, the default runs and the API (``deps.get_now``).

It lies before the real clock of any run of this suite, so the CLI (which reads the real clock)
always accepts a window ending on :data:`DEFAULT_END`.
"""

DEFAULT_END: Final = date(2026, 10, 6)
"""The default window end at :data:`NOW`: yesterday both in New York (the simulated Google
account's zone) and in UTC."""

TICK_AT: Final = NOW + timedelta(minutes=10)
"""The scheduler's clock: inside every sync cadence the backfill at :data:`NOW` satisfied."""

LATER: Final = NOW + timedelta(minutes=1)
"""The instant of a second default run, after a feedback write at :data:`NOW`."""

BACKFILL_DAYS: Final = 120
"""``BACKFILL_DAYS`` of the seeding: at least 120, so a 90-day custom run has data."""

API: Final = "/api/v1"

GOOGLE_DATASETS: Final[tuple[str, ...]] = tuple(
    name for name in DATASETS if name.startswith("google.")
)
"""The freshness rows a Google dataset needs before it can activate."""

# Every table of the application schema, parents before children, so a copy in this order
# satisfies every foreign key. ``SourceDataset.metadata`` is the declarative ``Base.metadata``.
_TABLES: Final[tuple[Table, ...]] = tuple(SourceDataset.metadata.sorted_tables)

_NETWORK_REFUSED: Final = "real network I/O attempted while seeding; the sources must be simulated"


def _fixed_now() -> datetime:
    """The API clock of every test: :data:`NOW`."""
    return NOW


# ---------------------------------------------------------------------------------------------
# The seeded world, built once per module and restored before every test
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Seed:
    """The seeded database's run and decision subjects, chosen by their attributes.

    Attributes:
        default_run_id: The seeded default run (28 days ending :data:`DEFAULT_END`).
        google_dataset_id: Google's active primary dataset.
        linkedin_campaign_id: An active LinkedIn ``own_budget`` campaign in USD with a daily
            budget and no lifetime budget.
        linkedin_daily_budget: Its decided (baseline) daily budget.
        lifetime_campaign_id: A LinkedIn campaign with only a lifetime (``total_budget``) budget.
        google_campaign_id: An active Google ``own_budget`` campaign on a ``DAILY`` budget that
            is not explicitly shared.
        shared_budget_id: A Google explicitly shared ``DAILY`` budget.
        shared_budget_external_id: Its Google id, which the shared-member 422 names.
        member_campaign_id: An active campaign drawing from that shared budget.
    """

    default_run_id: int
    google_dataset_id: int
    linkedin_campaign_id: int
    linkedin_daily_budget: Decimal
    lifetime_campaign_id: int
    google_campaign_id: int
    shared_budget_id: int
    shared_budget_external_id: str
    member_campaign_id: int


@dataclass(frozen=True, slots=True)
class SeedSnapshot:
    """The seeded state saved in a private schema, and the statements that restore it.

    Attributes:
        seed: The seeded run and subjects.
        restore: SQL that copies every saved table back into ``public`` (parents first) and
            moves each identity past its highest restored id.
    """

    seed: Seed
    restore: tuple[str, ...]


def _refuse_sync(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
    """Replacement of ``httpx.HTTPTransport.handle_request`` while seeding: always refuses."""
    raise RuntimeError(_NETWORK_REFUSED)


async def _refuse_async(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
    """Replacement of ``httpx.AsyncHTTPTransport.handle_async_request`` while seeding."""
    raise RuntimeError(_NETWORK_REFUSED)


def _quote(engine: Engine, name: str) -> str:
    """Quote one SQL identifier for PostgreSQL."""
    return engine.dialect.identifier_preparer.quote(name)


def _truncate_public(engine: Engine) -> None:
    """Empty every application table, as ``conftest.db`` does before each test."""
    tables = ", ".join(_quote(engine, table.name) for table in _TABLES)
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '15s'"))
        connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


async def _first(session: AsyncSession, model: Any, *where: ColumnElement[bool]) -> Any:
    """Return the lowest-id row of ``model`` matching ``where``, or ``None``."""
    statement = select(model).where(*where).order_by(model.id).limit(1)
    return await session.scalar(statement)


async def _choose_subjects(session: AsyncSession, run_id: int) -> Seed:
    """Pick the decision subjects the tests need from the seeded active datasets.

    The default simulated world holds every kind of subject this module needs, each with its
    baseline decision, so none is inserted by hand; a world without one fails the seeding,
    naming what is missing, rather than letting a test skip or pass vacuously.

    Raises:
        RuntimeError: When a subject or its baseline decision is missing.
    """
    active = {
        row.source: row.id
        for row in await session.scalars(
            select(SourceDataset).where(
                SourceDataset.state == "active", SourceDataset.role == "primary"
            )
        )
    }
    google, linkedin = active.get("google"), active.get("linkedin")
    if google is None or linkedin is None:
        raise RuntimeError(f"the seeding left no active Google or LinkedIn dataset: {active}")
    linkedin_campaign = await session.scalar(
        select(Campaign)
        .join(AdAccount, AdAccount.id == Campaign.account_id)
        .where(
            Campaign.dataset_id == linkedin,
            Campaign.status == "active",
            Campaign.daily_budget.is_not(None),
            Campaign.total_budget.is_(None),
            AdAccount.currency == "USD",
        )
        .order_by(Campaign.id)
        .limit(1)
    )
    lifetime = await _first(
        session,
        Campaign,
        Campaign.dataset_id == linkedin,
        Campaign.daily_budget.is_(None),
        Campaign.total_budget.is_not(None),
    )
    google_campaign = await session.scalar(
        select(Campaign)
        .join(Budget, Budget.id == Campaign.budget_id)
        .where(
            Campaign.dataset_id == google,
            Campaign.status == "active",
            Budget.explicitly_shared.is_(False),
            Budget.period == "DAILY",
        )
        .order_by(Campaign.id)
        .limit(1)
    )
    shared = await _first(
        session,
        Budget,
        Budget.dataset_id == google,
        Budget.explicitly_shared.is_(True),
        Budget.period == "DAILY",
        Budget.amount.is_not(None),
    )
    member = (
        None
        if shared is None
        else await _first(
            session,
            Campaign,
            Campaign.dataset_id == google,
            Campaign.budget_id == shared.id,
            Campaign.status == "active",
        )
    )
    subjects: dict[str, tuple[str, Any]] = {
        "LinkedIn USD own-budget campaign with a daily budget": ("campaign", linkedin_campaign),
        "LinkedIn lifetime-only campaign": ("campaign", lifetime),
        "Google own-budget DAILY campaign": ("campaign", google_campaign),
        "Google explicitly shared DAILY budget": ("budget", shared),
        "Google shared-budget member campaign": ("campaign", member),
    }
    missing = [label for label, (_, row) in subjects.items() if row is None]
    for label, (subject_type, row) in subjects.items():
        if row is None:
            continue
        baseline = await _first(
            session,
            Decision,
            Decision.subject_type == subject_type,
            Decision.subject_id == row.id,
            Decision.origin == "baseline",
        )
        if baseline is None:
            missing.append(f"baseline decision of the {label}")
    if missing:
        raise RuntimeError(f"the default simulated world lacks: {', '.join(missing)}")
    # Present by the check above; the daily budget is non-null by the query's own filter.
    assert linkedin_campaign is not None
    assert linkedin_campaign.daily_budget is not None
    assert google_campaign is not None
    assert member is not None
    return Seed(
        default_run_id=run_id,
        google_dataset_id=google,
        linkedin_campaign_id=linkedin_campaign.id,
        linkedin_daily_budget=linkedin_campaign.daily_budget,
        lifetime_campaign_id=lifetime.id,
        google_campaign_id=google_campaign.id,
        shared_budget_id=shared.id,
        shared_budget_external_id=shared.external_id,
        member_campaign_id=member.id,
    )


async def _seed_world() -> Seed:
    """Bootstrap every source simulated at :data:`NOW` and run the first default analysis.

    The default run serves the analysis requests the bootstrap queued, as the worker's first
    run does, so every test starts with no pending request.

    Runs on its own event loop (see :func:`seed_snapshot`), so the application engine is reset
    before and disposed after, and never crosses into a test's loop.
    """
    db_session.reset_engine()
    try:
        settings = get_settings()
        controller = SourceModeController(settings, clock=_fixed_now)
        if settings.backfill_days != BACKFILL_DAYS:
            raise RuntimeError(f"BACKFILL_DAYS is {settings.backfill_days}, not {BACKFILL_DAYS}")
        report = await bootstrap_sources(settings, force_simulated=True, controller=controller)
        if report.failures:
            raise RuntimeError(f"the simulated bootstrap failed: {dict(report.failures)}")
        async with db_session.session_scope() as session:
            pending = list(
                await session.scalars(
                    select(AnalysisRequest.id)
                    .where(AnalysisRequest.picked_at.is_(None))
                    .order_by(AnalysisRequest.id)
                )
            )
        window = await runner.default_window(now=NOW)
        run = await runner.run_analysis(
            window, "default", "scheduled", reason="seed", request_ids=pending, now=NOW
        )
        async with db_session.session_scope() as session:
            return await _choose_subjects(session, run.id)
    finally:
        await db_session.dispose_engine()
        db_session.reset_engine()


def _restore_statements(engine: Engine, schema: str) -> tuple[str, ...]:
    """Build the SQL that copies the saved tables back and moves every identity past them.

    Identifiers come from the model metadata and a generated schema name and are quoted, so
    the composed statements carry no external input.
    """
    saved = _quote(engine, schema)
    statements: list[str] = []
    for table in _TABLES:
        name = _quote(engine, table.name)
        statements.append(
            f"INSERT INTO public.{name} OVERRIDING SYSTEM VALUE "  # noqa: S608 - quoted metadata names
            f"SELECT * FROM {saved}.{name}"
        )
    for table in _TABLES:
        column = table.c.get("id")
        if column is None or column.identity is None:
            continue
        name = _quote(engine, table.name)
        statements.append(
            f"SELECT setval(pg_get_serial_sequence('public.{table.name}', 'id'), "  # noqa: S608
            f"COALESCE(MAX(id), 0) + 1, false) FROM public.{name}"
        )
    return tuple(statements)


@pytest.fixture(scope="module")
def seed_snapshot(sync_engine: Engine) -> Iterator[SeedSnapshot]:
    """Seed the simulated world once for the module and save it in a private schema.

    The bootstrap runs with ``asyncio.run`` on a worker thread, so its event loop never meets
    the anyio runner of the tests, with ``BACKFILL_DAYS`` set, ``DATABASE_URL`` pointing at the
    test database and every real ``httpx`` transport refusing, the guard ``conftest`` applies
    to each test. Every table is then copied, data only, into a schema of its own, dropped when
    the module ends.

    Yields:
        The saved seed and the statements :func:`seeded` restores it with.
    """
    schema = f"seed_runs_decisions_{uuid.uuid4().hex[:12]}"
    _truncate_public(sync_engine)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", sync_engine.url.render_as_string(hide_password=False))
        patch.setenv("BACKFILL_DAYS", str(BACKFILL_DAYS))
        patch.setattr(httpx.HTTPTransport, "handle_request", _refuse_sync)
        patch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse_async)
        get_settings.cache_clear()
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="seed") as pool:
                seed = pool.submit(asyncio.run, _seed_world()).result()
        finally:
            get_settings.cache_clear()
    saved = _quote(sync_engine, schema)
    with sync_engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {saved}"))
        for table in _TABLES:
            name = _quote(sync_engine, table.name)
            connection.execute(text(f"CREATE TABLE {saved}.{name} AS TABLE public.{name}"))
    try:
        yield SeedSnapshot(seed=seed, restore=_restore_statements(sync_engine, schema))
    finally:
        with sync_engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {saved} CASCADE"))


@pytest.fixture
async def seeded(db: async_sessionmaker[AsyncSession], seed_snapshot: SeedSnapshot) -> Seed:
    """Restore the seeded database into the tables ``conftest.db`` has just emptied."""
    async with db() as session, session.begin():
        for statement in seed_snapshot.restore:
            await session.execute(text(statement))
    return seed_snapshot.seed


class _Identity(Protocol):
    """The part of ``conftest.test_identity`` this module reads."""

    @property
    def csrf_headers(self) -> Mapping[str, str]:
        """The ``X-Requested-With: ads-engine`` header of mutating requests."""
        ...


class _ThresholdsFactory(Protocol):
    """``conftest.make_thresholds``: overridden copies of ``config/`` under ``tmp_path``."""

    def __call__(self, overrides: Mapping[str, Any] | None = None) -> Thresholds:
        """Write an overridden copy of ``config/`` and load it."""
        ...

    def write(self, overrides: Mapping[str, Any] | None = None) -> Path:
        """Write an overridden copy of ``config/`` and return its directory, unloaded."""
        ...


class _CapturedLogs(Protocol):
    """The part of ``conftest.captured_logs`` this module uses."""

    def configure(self, *, settings: Settings | None = None, level: str = "INFO") -> None:
        """(Re)configure logging onto the capture buffer, with structlog's logger caching off."""
        ...


@pytest.fixture
def app(
    seeded: Seed, captured_logs: _CapturedLogs, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FastAPI]:
    """The real application on the seeded database, its request clock fixed at :data:`NOW`.

    ``create_app()`` configures logging as in production, but through ``captured_logs``: onto
    its buffer instead of the test's captured stdout, which pytest closes, with structlog's
    logger caching off from that moment, and back to its pre-test state on teardown. Left in
    place, that configuration would keep a root handler on the closed stream, and module loggers
    first used under its cache would bypass ``structlog.testing.capture_logs`` in later modules.
    Overrides (the clock, and a test's thresholds) are cleared at teardown.
    """
    del seeded  # Requested so the database is seeded before the application serves it.
    monkeypatch.setattr(api_app, "configure_logging", captured_logs.configure)
    application = create_app()
    application.dependency_overrides[deps.get_now] = _fixed_now
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
async def client(
    app: FastAPI,
    admin_user: AppUser,
    login: Callable[..., Awaitable[httpx.Response]],
    test_identity: _Identity,
) -> AsyncIterator[httpx.AsyncClient]:
    """A client of :func:`app` signed in as the conftest admin, with the CSRF header set.

    It signs in after the clock override, so its session is issued at :data:`NOW`.
    """
    del admin_user  # Requested so the admin row exists before signing in.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        await login(http)
        http.headers.update(test_identity.csrf_headers)
        yield http


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _error(response: httpx.Response, status: int, code: str) -> str:
    """Assert an error of exactly ``{"error": {"code", "message"}}``; return its message."""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message"}, body
    error = schemas.ErrorResponse.model_validate(body).error
    assert error.code == code, body
    assert error.message.strip(), body
    return error.message


async def _count(
    db: async_sessionmaker[AsyncSession], model: Any, *where: ColumnElement[bool]
) -> int:
    """Count the rows of ``model`` matching ``where``."""
    async with db() as session:
        counted = await session.scalar(select(func.count()).select_from(model).where(*where))
    return int(counted or 0)


async def _decisions_of(
    db: async_sessionmaker[AsyncSession], subject_type: str, subject_id: int
) -> int:
    """Count the decision rows of one subject."""
    return await _count(
        db, Decision, Decision.subject_type == subject_type, Decision.subject_id == subject_id
    )


async def _decide(
    client: httpx.AsyncClient, subject_type: str, subject_id: int, patch: Mapping[str, str]
) -> httpx.Response:
    """``POST /decisions`` with a patch on one subject."""
    return await client.post(
        f"{API}/decisions",
        json={"subject": {"type": subject_type, "id": subject_id}, "patch": dict(patch)},
    )


def _decision(response: httpx.Response) -> schemas.Decision:
    """Assert a 201 decision and return it as the ``Decision`` schema."""
    assert response.status_code == 201, response.text
    return schemas.Decision.model_validate(response.json())


def _window(days: int, end: date = DEFAULT_END) -> dict[str, str]:
    """The ``POST /runs`` body of a ``days``-day window ending on ``end``."""
    return {
        "window_start": (end - timedelta(days=days - 1)).isoformat(),
        "window_end": end.isoformat(),
    }


async def _post_run(client: httpx.AsyncClient, days: int) -> schemas.Run:
    """``POST /runs`` for a ``days``-day window ending :data:`DEFAULT_END`; assert 201."""
    response = await client.post(f"{API}/runs", json=_window(days))
    assert response.status_code == 201, response.text
    return schemas.Run.model_validate(response.json())


async def _get_run(client: httpx.AsyncClient, path: str) -> schemas.Run:
    """``GET`` one run view; assert 200."""
    response = await client.get(f"{API}{path}")
    assert response.status_code == 200, response.text
    return schemas.Run.model_validate(response.json())


async def _retire_google(db: async_sessionmaker[AsyncSession], seed: Seed) -> int:
    """Retire Google's seeded dataset the way the product does: activate a replacement.

    ``DatasetManager`` begins a building Google dataset, its three freshness rows are
    recorded complete (the readiness a Google build needs), and ``activate`` retires the
    seeded dataset in the same transaction.

    Returns:
        The replacement dataset's id.
    """
    manager = DatasetManager(clock=_fixed_now)
    replacement = await manager.begin(
        "google", "simulated", reason="replacement for the retired-subject tests"
    )
    async with db() as session, session.begin():
        session.add_all(
            [
                DatasetFreshness(
                    source="google",
                    dataset_id=replacement,
                    dataset=name,
                    last_attempt_at=NOW,
                    last_success_at=NOW,
                    covered_from=DEFAULT_END - timedelta(days=BACKFILL_DAYS - 1),
                    covered_through=DEFAULT_END,
                    completeness="complete",
                    reason=None,
                )
                for name in GOOGLE_DATASETS
            ]
        )
    activation = await manager.activate(replacement)
    assert seed.google_dataset_id in activation.retired, activation
    async with db() as session:
        retired = await session.get(SourceDataset, seed.google_dataset_id)
        assert retired is not None
        assert retired.state == "retired"
    return replacement


def _refuse_recompute(*args: object, **kwargs: object) -> Any:
    """Stand-in for every analysis entry point: reading a page must never call one."""
    raise AssertionError("GET /recommendations re-ran analysis instead of reading stored outputs")


# ---------------------------------------------------------------------------------------------
# Run selection (AAP 0.5.3 "Run kinds", "Selection")
# ---------------------------------------------------------------------------------------------


async def test_manual_90_day_run_never_becomes_latest(
    seeded: Seed, client: httpx.AsyncClient, thresholds: Thresholds
) -> None:
    """Run selection: a manual 90-day run never becomes ``/runs/latest``.

    ``POST /runs`` records a ``custom`` run even though it finished after the default run,
    and ``/runs/latest`` keeps returning the latest succeeded default run.
    """
    default = await _get_run(client, f"/runs/{seeded.default_run_id}")
    custom = await _post_run(client, 90)
    assert custom.kind == "custom"
    assert custom.trigger == "manual"
    assert custom.status == "succeeded"
    assert custom.days == 90
    assert (custom.window_start, custom.window_end) == (
        DEFAULT_END - timedelta(days=89),
        DEFAULT_END,
    )
    # The custom run is the newest run: selection by recency alone would have picked it.
    assert custom.finished_at is not None
    assert default.finished_at is not None
    assert custom.finished_at > default.finished_at

    latest = await _get_run(client, "/runs/latest")
    assert latest.id == seeded.default_run_id
    assert latest.kind == "default"
    assert latest.days == thresholds.default_days

    listed = await client.get(f"{API}/runs", params={"kind": "custom"})
    assert listed.status_code == 200, listed.text
    assert [schemas.Run.model_validate(run).id for run in listed.json()] == [custom.id]
    defaults = await client.get(f"{API}/runs", params={"kind": "default"})
    assert defaults.status_code == 200, defaults.text
    assert [schemas.Run.model_validate(run).id for run in defaults.json()] == [
        seeded.default_run_id
    ]


async def test_recommendations_without_run_id_returns_default(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Run selection: ``/recommendations`` without ``run_id`` returns the default run.

    A newer custom run is reachable only by its id (``/actions?run=<id>``).
    """
    custom = await _post_run(client, 56)
    async with db() as session:
        latest = await runner.latest_default_run(session)
    assert latest is not None
    assert latest.id == seeded.default_run_id

    response = await client.get(f"{API}/recommendations")
    assert response.status_code == 200, response.text
    page = schemas.RecommendationsPage.model_validate(response.json())
    assert page.run.id == seeded.default_run_id
    assert page.run.kind == "default"

    response = await client.get(f"{API}/recommendations", params={"run_id": custom.id})
    assert response.status_code == 200, response.text
    page = schemas.RecommendationsPage.model_validate(response.json())
    assert page.run.id == custom.id
    assert page.run.kind == "custom"
    assert page.run.days == 56


async def test_custom_window_limits(
    seeded: Seed,
    client: httpx.AsyncClient,
    db: async_sessionmaker[AsyncSession],
    thresholds: Thresholds,
) -> None:
    """Run selection: a custom window outside its bounds is a 422, and no run is written.

    The end may be no later than the default window end, and the span runs from 1 day to
    ``window.max_days`` (AAP 0.5.3 ``POST /runs``).
    """
    del seeded  # Requested for the seeded database.
    cases: Sequence[tuple[str, dict[str, str]]] = (
        ("end_after_default_end", _window(28, DEFAULT_END + timedelta(days=1))),
        (
            "end_before_start",
            {
                "window_start": DEFAULT_END.isoformat(),
                "window_end": (DEFAULT_END - timedelta(days=1)).isoformat(),
            },
        ),
        ("too_long", _window(thresholds.max_days + 1)),
    )
    runs_before = await _count(db, AnalysisRun)
    for code, body in cases:
        response = await client.post(f"{API}/runs", json=body)
        _error(response, 422, code)
    assert await _count(db, AnalysisRun) == runs_before
    # The longest allowed span is accepted, so the 422 above is the bound and nothing else.
    longest = await _post_run(client, thresholds.max_days)
    assert longest.days == thresholds.max_days


# ---------------------------------------------------------------------------------------------
# Changed defaults (AAP 0.5.4.1 window keys; AAP 0.5.5 window picker)
# ---------------------------------------------------------------------------------------------


async def test_default_days_21_changes_default_run(
    seeded: Seed,
    app: FastAPI,
    client: httpx.AsyncClient,
    make_thresholds: _ThresholdsFactory,
) -> None:
    """Changed defaults: ``window.default_days: 21`` makes the default run 21 days.

    The worker path computes the default window from the loaded configuration;
    ``/runs/latest`` reports the configured default, and ``/config`` carries it with the
    presets, so the window picker marks the 21-day preset as the default.
    """
    del seeded  # Requested for the seeded database.
    changed = make_thresholds({"window.default_days": 21, "window.presets": [14, 21, 28, 56, 90]})
    window = await runner.default_window(now=NOW, thresholds=changed)
    run = await runner.run_analysis(window, "default", "event", thresholds=changed, now=NOW)
    assert run.kind == "default"
    assert run.status == "succeeded"
    assert (run.window_start, run.window_end) == (DEFAULT_END - timedelta(days=20), DEFAULT_END)
    assert run.config_hash == changed.config_hash

    app.dependency_overrides[deps.get_thresholds] = lambda: changed
    latest = await _get_run(client, "/runs/latest")
    assert latest.id == run.id
    assert latest.kind == "default"
    assert latest.days == 21
    assert latest.default_days == 21

    response = await client.get(f"{API}/config")
    assert response.status_code == 200, response.text
    config = schemas.ConfigView.model_validate(response.json())
    assert config.window.default_days == 21
    assert config.window.presets == [14, 21, 28, 56, 90]
    assert config.window.default_days in config.window.presets
    assert config.thresholds["window"]["default_days"] == 21
    assert config.config_hash == changed.config_hash


def test_default_missing_from_presets_fails_config_load(
    make_thresholds: _ThresholdsFactory,
) -> None:
    """Changed defaults: a default missing from ``window.presets`` fails config load.

    Both through a loaded override and through a copied ``thresholds.yaml`` edited by hand,
    whose unedited copy loads.
    """
    with pytest.raises(ConfigError) as overridden:
        make_thresholds({"window.default_days": 21})
    assert overridden.value.key == "window.presets"

    directory = make_thresholds.write()
    assert Thresholds.load(directory).default_days == 28
    path = directory / "thresholds.yaml"
    edited, replaced = re.subn(
        r"(?m)^  default_days: 28$", "  default_days: 21", path.read_text(encoding="utf-8")
    )
    assert replaced == 1
    path.write_text(edited, encoding="utf-8")
    with pytest.raises(ConfigError) as loaded:
        Thresholds.load(directory)
    assert loaded.value.key == "window.presets"
    assert "window.default_days" in loaded.value.detail


# ---------------------------------------------------------------------------------------------
# Analysis lock (AAP 0.5.3 "One lock for every entry point", "Lock unavailable", "Crash
# recovery")
# ---------------------------------------------------------------------------------------------


async def test_post_runs_409_while_lock_held(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Analysis lock: ``POST /runs`` answers 409 ``run_in_progress`` while the lock is held.

    The lock is held on its own dedicated connection, as the worker holds it during a run;
    no ``analysis_run`` row is written, and once it is released the same request succeeds.
    """
    del seeded  # Requested for the seeded database.
    runs_before = await _count(db, AnalysisRun)
    async with analysis_lock():
        response = await client.post(f"{API}/runs", json=_window(28))
        _error(response, 409, "run_in_progress")
        assert await _count(db, AnalysisRun) == runs_before
    released = await _post_run(client, 28)
    assert released.kind == "custom"
    assert await _count(db, AnalysisRun) == runs_before + 1


async def test_cli_analyze_exits_3_while_lock_held(
    seeded: Seed,
    db: async_sessionmaker[AsyncSession],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Analysis lock: ``ads-engine analyze`` exits with code 3 while the lock is held.

    The CLI runs as a separate process on the same database; after the lock is released the
    same command exits 0 and writes one ``custom`` run of exactly that window.
    """
    del seeded  # Requested for the seeded database.
    start = DEFAULT_END - timedelta(days=27)
    argv = ("analyze", "--start", start.isoformat(), "--end", DEFAULT_END.isoformat())

    def analyze() -> subprocess.CompletedProcess[str]:
        """Run the CLI command off the event loop."""
        return run_cli(*argv)

    custom_before = await _count(db, AnalysisRun, AnalysisRun.kind == "custom")
    async with analysis_lock():
        held = await anyio.to_thread.run_sync(analyze)
    assert held.returncode == 3, held.stderr
    assert await _count(db, AnalysisRun, AnalysisRun.kind == "custom") == custom_before

    released = await anyio.to_thread.run_sync(analyze)
    assert released.returncode == 0, released.stderr
    async with db() as session:
        runs = list(
            await session.scalars(
                select(AnalysisRun).where(AnalysisRun.kind == "custom").order_by(AnalysisRun.id)
            )
        )
    assert len(runs) == custom_before + 1
    written = runs[-1]
    assert (written.window_start, written.window_end) == (start, DEFAULT_END)
    assert written.trigger == "manual"
    assert written.status == "succeeded"


async def test_worker_leaves_analysis_request_pending_while_locked(
    seeded: Seed, db: async_sessionmaker[AsyncSession]
) -> None:
    """Analysis lock: the worker leaves its ``analysis_request`` pending while locked.

    A scheduler tick behind another holder of the lock defers the coalesced request
    (``picked_at`` and ``run_id`` stay NULL, no run is written); the next tick after the
    release serves it with exactly one default run.
    """
    del seeded  # Requested for the seeded database.
    async with db() as session, session.begin():
        request = AnalysisRequest(reason="test", requested_at=TICK_AT - timedelta(minutes=5))
        session.add(request)
        await session.flush()
        request_id = request.id
    runs_before = await _count(db, AnalysisRun)
    controller = SourceModeController(get_settings(), clock=lambda: TICK_AT)
    scheduler = Scheduler(controller=controller, clock=lambda: TICK_AT)

    async with analysis_lock():
        deferred = await scheduler.tick(TICK_AT)
    assert deferred.analysis_deferred
    assert deferred.analysis_run_id is None
    async with db() as session:
        pending = await session.get(AnalysisRequest, request_id)
        assert pending is not None
        assert pending.picked_at is None
        assert pending.run_id is None
    assert await _count(db, AnalysisRun) == runs_before

    served = await scheduler.tick(TICK_AT)
    assert not served.analysis_deferred
    assert served.analysis_run_id is not None
    async with db() as session:
        picked = await session.get(AnalysisRequest, request_id)
        assert picked is not None
        assert picked.picked_at is not None
        assert picked.run_id == served.analysis_run_id
        run = await session.get(AnalysisRun, served.analysis_run_id)
        assert run is not None
        assert (run.kind, run.trigger, run.status) == ("default", "event", "succeeded")
    assert await _count(db, AnalysisRun) == runs_before + 1


async def test_crashed_running_row_marked_failed(
    seeded: Seed, db: async_sessionmaker[AsyncSession]
) -> None:
    """Analysis lock: a ``running`` row left by a crashed process is marked ``failed``.

    The row has no live lock holder, so the next run's lock acquisition fails it and the run
    succeeds; the partial unique index admits at most one ``running`` row meanwhile.
    """
    async with db() as session:
        default = await session.get(AnalysisRun, seeded.default_run_id)
        assert default is not None

    def running_row() -> AnalysisRun:
        """A ``running`` row over the seeded default window, as a crashed run leaves it."""
        assert default is not None
        return AnalysisRun(
            kind="default",
            trigger="scheduled",
            status="running",
            window_start=default.window_start,
            window_end=default.window_end,
            source_day_boundaries=default.source_day_boundaries,
            config_hash=default.config_hash,
            started_at=NOW - timedelta(hours=1),
            finished_at=None,
            data_modes={},
            dataset_ids={},
        )

    async with db() as session, session.begin():
        crashed = running_row()
        session.add(crashed)
        await session.flush()
        crashed_id = crashed.id
    with pytest.raises(IntegrityError, match="uq_analysis_run_single_running"):
        async with db() as session, session.begin():
            session.add(running_row())

    window = await runner.default_window(now=NOW)
    run = await runner.run_analysis(window, "default", "scheduled", now=NOW)
    assert run.status == "succeeded"
    async with db() as session:
        failed = await session.get(AnalysisRun, crashed_id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.finished_at is not None
    assert await _count(db, AnalysisRun, AnalysisRun.status == "running") == 0


# ---------------------------------------------------------------------------------------------
# Decision patches through POST /decisions (AAP 0.5.2 "Decisions: patch model")
# ---------------------------------------------------------------------------------------------


async def test_budget_only_patch_keeps_decided_status(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Decision patches: a budget-only patch keeps the decided status.

    The baseline of the own-budget campaign is ``active``; after a status-only ``paused``
    decision, a ``daily_budget`` patch is materialised as the complete expected state
    ``paused`` plus the new budget, and is the subject's newest row.
    """
    subject = seeded.linkedin_campaign_id
    paused = _decision(await _decide(client, "campaign", subject, {"status": "paused"}))
    assert paused.shape == "own_budget"
    assert paused.status == "paused"
    assert paused.daily_budget == seeded.linkedin_daily_budget

    raised = _decision(await _decide(client, "campaign", subject, {"daily_budget": "275.50"}))
    assert (raised.subject.type, raised.subject.id) == ("campaign", subject)
    assert raised.shape == "own_budget"
    assert raised.status == "paused"
    assert raised.daily_budget == Decimal("275.50")
    assert raised.total_budget is None
    assert raised.budget_id is None
    assert raised.tombstone is False
    assert raised.origin == "user"
    assert raised.patch == {"daily_budget": Decimal("275.50")}

    async with db() as session:
        newest = await session.scalar(
            select(Decision)
            .where(Decision.subject_type == "campaign", Decision.subject_id == subject)
            .order_by(Decision.decided_at.desc(), Decision.id.desc())
            .limit(1)
        )
    assert newest is not None
    assert newest.id == raised.id
    assert (newest.status, newest.daily_budget, newest.total_budget) == (
        "paused",
        Decimal("275.50"),
        None,
    )


async def test_empty_patch_422(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Decision patches: an empty patch is a 422, and nothing is written."""
    subject = seeded.linkedin_campaign_id
    before = await _decisions_of(db, "campaign", subject)
    _error(await _decide(client, "campaign", subject, {}), 422, "empty_patch")
    assert await _decisions_of(db, "campaign", subject) == before


async def test_no_change_patch_422(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Decision patches: a patch equal to the current expected state is a 422."""
    subject = seeded.linkedin_campaign_id
    before = await _decisions_of(db, "campaign", subject)
    unchanged_budget = format(seeded.linkedin_daily_budget, "f")
    for patch in ({"status": "active"}, {"status": "active", "daily_budget": unchanged_budget}):
        _error(await _decide(client, "campaign", subject, patch), 422, "no_change")
    assert await _decisions_of(db, "campaign", subject) == before


async def test_daily_budget_on_lifetime_campaign_422(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Decision patches: a daily budget on a lifetime-only campaign is a 422."""
    subject = seeded.lifetime_campaign_id
    before = await _decisions_of(db, "campaign", subject)
    message = _error(
        await _decide(client, "campaign", subject, {"daily_budget": "100.00"}),
        422,
        "field_not_allowed",
    )
    assert "daily budget" in message
    assert await _decisions_of(db, "campaign", subject) == before


async def test_amount_finer_than_minor_unit_422(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Decision patches: an amount finer than the currency's minor unit is a 422.

    A USD LinkedIn amount is validated to cents; a Google amount must be a whole number of
    micros, so a seventh decimal place is refused while six are accepted.
    """
    linkedin = seeded.linkedin_campaign_id
    google = seeded.google_campaign_id
    linkedin_before = await _decisions_of(db, "campaign", linkedin)
    google_before = await _decisions_of(db, "campaign", google)

    _error(
        await _decide(client, "campaign", linkedin, {"daily_budget": "10.005"}),
        422,
        "sub_minor_unit",
    )
    # Seven decimal places exceed numeric(18,6) and are refused by the request schema itself.
    _error(
        await _decide(client, "campaign", google, {"daily_budget": "1.0000005"}),
        422,
        "validation_error",
    )
    assert await _decisions_of(db, "campaign", linkedin) == linkedin_before
    assert await _decisions_of(db, "campaign", google) == google_before

    # A whole number of micros is Google's native precision, finer than cents but accepted.
    micros = _decision(await _decide(client, "campaign", google, {"daily_budget": "210.000001"}))
    assert micros.daily_budget == Decimal("210.000001")


async def test_status_only_patch_keeps_budget(seeded: Seed, client: httpx.AsyncClient) -> None:
    """Decision patches: a status-only patch keeps the last decided budget."""
    subject = seeded.linkedin_campaign_id
    budget = _decision(await _decide(client, "campaign", subject, {"daily_budget": "260.00"}))
    assert budget.status == "active"
    paused = _decision(await _decide(client, "campaign", subject, {"status": "paused"}))
    assert paused.shape == "own_budget"
    assert paused.status == "paused"
    assert paused.daily_budget == Decimal("260.00")
    assert paused.total_budget is None
    assert paused.patch == {"status": "paused"}


# ---------------------------------------------------------------------------------------------
# Budget-subject and shared-member patches (AAP 0.5.2 shapes table)
# ---------------------------------------------------------------------------------------------


async def test_budget_subject_rejects_status(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Budget-subject patches: ``status`` on a shared budget is a 422; it has no status."""
    subject = seeded.shared_budget_id
    before = await _decisions_of(db, "budget", subject)
    _error(await _decide(client, "budget", subject, {"status": "paused"}), 422, "field_not_allowed")
    assert await _decisions_of(db, "budget", subject) == before


async def test_budget_subject_rejects_both_amounts(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Budget-subject patches: ``daily_budget`` and ``total_budget`` together are a 422."""
    subject = seeded.shared_budget_id
    before = await _decisions_of(db, "budget", subject)
    _error(
        await _decide(
            client, "budget", subject, {"daily_budget": "300.00", "total_budget": "9000.00"}
        ),
        422,
        "field_not_allowed",
    )
    assert await _decisions_of(db, "budget", subject) == before


async def test_budget_subject_rejects_total_on_daily(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Budget-subject patches: ``total_budget`` on a ``DAILY`` shared budget is a 422."""
    subject = seeded.shared_budget_id
    before = await _decisions_of(db, "budget", subject)
    message = _error(
        await _decide(client, "budget", subject, {"total_budget": "9000.00"}),
        422,
        "field_not_allowed",
    )
    assert "DAILY" in message
    assert await _decisions_of(db, "budget", subject) == before


async def test_budget_subject_accepts_daily_on_daily(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Budget-subject patches: ``daily_budget`` on a ``DAILY`` budget is materialised.

    The decision has the ``budget`` shape, no status, the new daily amount and no total; it is
    written on the budget subject alone, never on its member campaigns.
    """
    subject = seeded.shared_budget_id
    decisions_before = await _count(db, Decision)
    member_before = await _decisions_of(db, "campaign", seeded.member_campaign_id)
    decided = _decision(await _decide(client, "budget", subject, {"daily_budget": "300.00"}))
    assert (decided.subject.type, decided.subject.id) == ("budget", subject)
    assert decided.shape == "budget"
    assert decided.status is None
    assert decided.daily_budget == Decimal("300.00")
    assert decided.total_budget is None
    assert decided.budget_id is None
    assert await _count(db, Decision) == decisions_before + 1
    assert await _decisions_of(db, "campaign", seeded.member_campaign_id) == member_before


async def test_shared_member_rejects_any_amount_naming_budget(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Shared-member patches: any amount on a shared member is a 422 naming the budget.

    The amount belongs to the shared budget, so the message names the budget that owns it.
    """
    subject = seeded.member_campaign_id
    before = await _decisions_of(db, "campaign", subject)
    for patch in ({"daily_budget": "100.00"}, {"total_budget": "9000.00"}):
        message = _error(
            await _decide(client, "campaign", subject, patch), 422, "shared_budget_amount"
        )
        assert seeded.shared_budget_external_id in message
    assert await _decisions_of(db, "campaign", subject) == before


async def test_shared_member_status_patch_keeps_budget_id(
    seeded: Seed, client: httpx.AsyncClient
) -> None:
    """Shared-member patches: a status patch keeps the member's shared ``budget_id``."""
    decided = _decision(
        await _decide(client, "campaign", seeded.member_campaign_id, {"status": "paused"})
    )
    assert decided.shape == "shared_member"
    assert decided.status == "paused"
    assert decided.budget_id == seeded.shared_budget_id
    assert decided.daily_budget is None
    assert decided.total_budget is None


# ---------------------------------------------------------------------------------------------
# Retired subjects (AAP 0.3.2; AAP 0.5.3 409 subject_retired)
# ---------------------------------------------------------------------------------------------


async def test_decision_on_retired_subject_409(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Retired subjects: ``POST /decisions`` on a campaign of a retired dataset is a 409."""
    subject = seeded.google_campaign_id
    # The subject accepts this patch while its dataset is active.
    accepted = _decision(await _decide(client, "campaign", subject, {"status": "paused"}))
    assert accepted.status == "paused"

    await _retire_google(db, seeded)
    before = await _decisions_of(db, "campaign", subject)
    _error(await _decide(client, "campaign", subject, {"status": "active"}), 409, "subject_retired")
    assert await _decisions_of(db, "campaign", subject) == before


async def test_resolve_on_retired_subject_409(
    seeded: Seed, client: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> None:
    """Retired subjects: resolving a discrepancy of a retired dataset is a 409.

    The ``status`` discrepancy compares the campaign's newest snapshot with its baseline; after
    the dataset retires, ``platform_correct`` is refused, the row stays open and no decision is
    written.
    """
    subject = seeded.google_campaign_id
    async with db() as session, session.begin():
        snapshot_id = await session.scalar(
            select(EntitySnapshot.id)
            .where(
                EntitySnapshot.dataset_id == seeded.google_dataset_id,
                EntitySnapshot.entity_type == "campaign",
                EntitySnapshot.entity_id == subject,
            )
            .order_by(EntitySnapshot.captured_at.desc(), EntitySnapshot.id.desc())
            .limit(1)
        )
        decision_id = await session.scalar(
            select(Decision.id)
            .where(Decision.subject_type == "campaign", Decision.subject_id == subject)
            .order_by(Decision.decided_at.desc(), Decision.id.desc())
            .limit(1)
        )
        assert snapshot_id is not None
        assert decision_id is not None
        discrepancy = Discrepancy(
            source="google",
            dataset_id=seeded.google_dataset_id,
            subject_type="campaign",
            subject_id=subject,
            kind="status",
            decided_value={"status": "active"},
            platform_value={"status": "paused"},
            snapshot_id=snapshot_id,
            decision_id=decision_id,
            detected_at=NOW,
            state="open",
            resolved_at=None,
            resolution=None,
        )
        session.add(discrepancy)
        await session.flush()
        discrepancy_id = discrepancy.id

    await _retire_google(db, seeded)
    before = await _decisions_of(db, "campaign", subject)
    response = await client.post(
        f"{API}/discrepancies/{discrepancy_id}/resolve", json={"resolution": "platform_correct"}
    )
    _error(response, 409, "subject_retired")
    async with db() as session:
        unchanged = await session.get(Discrepancy, discrepancy_id)
        assert unchanged is not None
        assert (unchanged.state, unchanged.resolution) == ("open", None)
    assert await _decisions_of(db, "campaign", subject) == before


# ---------------------------------------------------------------------------------------------
# Stored outputs (AAP 0.5.3 "Where outputs are frozen")
# ---------------------------------------------------------------------------------------------


def _items(page: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    """Every item of a raw ``RecommendationsPage`` body, by id."""
    return {
        item["id"]: item
        for section in page["sections"]
        for group in section["groups"]
        for item in group["items"]
    }


async def test_findings_withheld_suppressions_read_from_storage(
    seeded: Seed,
    client: httpx.AsyncClient,
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored outputs: findings, withheld lines and suppression counts come from storage.

    A dismissal makes the next default run store a suppression count. Then every analysis
    entry point (the runner, context loading, precedence and each rule module) is replaced by
    a function that fails the test, and ``GET /recommendations`` still returns, per section,
    exactly the stored ``run_finding``, ``run_withheld`` and ``run_suppression`` rows, and
    every item's stored evidence.
    """
    first = await client.get(f"{API}/recommendations")
    assert first.status_code == 200, first.text
    dismissible = next(item for item in _items(first.json()).values() if item["rule_id"] != "C0")
    feedback = await client.post(
        f"{API}/recommendations/{dismissible['id']}/feedback", json={"kind": "dismiss"}
    )
    assert feedback.status_code == 201, feedback.text
    window = await runner.default_window(now=LATER)
    run = await runner.run_analysis(window, "default", "event", now=LATER)
    assert run.id != seeded.default_run_id

    async with db() as session:
        findings = list(
            await session.scalars(
                select(RunFinding).where(RunFinding.run_id == run.id).order_by(RunFinding.id)
            )
        )
        withheld = list(
            await session.scalars(
                select(RunWithheld).where(RunWithheld.run_id == run.id).order_by(RunWithheld.id)
            )
        )
        suppressions = list(
            await session.scalars(
                select(RunSuppression)
                .where(RunSuppression.run_id == run.id)
                .order_by(RunSuppression.id)
            )
        )
        recommendations = {
            row.id: row
            for row in await session.scalars(
                select(Recommendation).where(Recommendation.run_id == run.id)
            )
        }
    assert findings
    assert withheld
    assert suppressions
    assert recommendations

    targets = (
        "ads_engine.analysis.runner.run_analysis",
        "ads_engine.analysis.metrics.load_context",
        "ads_engine.analysis.precedence.apply_precedence",
        *(f"ads_engine.analysis.{module}.evaluate" for module in runner.EVALUATION_ORDER),
    )
    for target in targets:
        monkeypatch.setattr(target, _refuse_recompute)

    response = await client.get(f"{API}/recommendations")
    assert response.status_code == 200, response.text
    body = response.json()
    page = schemas.RecommendationsPage.model_validate(body)
    assert page.run.id == run.id
    for section in page.sections:
        stored_findings = [row for row in findings if row.section == section.key]
        assert [(f.code, f.message) for f in section.findings] == [
            (row.code, row.message) for row in stored_findings
        ]
        assert [f.model_dump(mode="json") for f in section.findings] == [
            schemas.Finding.from_row(row).model_dump(mode="json") for row in stored_findings
        ]
        stored_withheld = [row for row in withheld if row.section == section.key]
        assert [
            (w.rule_id, w.reason, w.sample.observed, w.sample.required, w.sample.unit)
            for w in section.withheld
        ] == [
            (
                row.rule_id,
                row.reason,
                None if row.observed is None else format(row.observed, "f"),
                None if row.required is None else format(row.required, "f"),
                row.unit,
            )
            for row in stored_withheld
        ]
        assert [w.model_dump(mode="json") for w in section.withheld] == [
            schemas.Withheld.from_row(row).model_dump(mode="json") for row in stored_withheld
        ]
        expected: dict[str, int] = {}
        for row in suppressions:
            if row.section == section.key:
                expected[row.reason] = expected.get(row.reason, 0) + row.count
        assert section.suppressed == expected

    items = _items(body)
    assert set(items) == set(recommendations)
    for item_id, item in items.items():
        stored = recommendations[item_id].evidence
        assert item["evidence"] == schemas.Evidence.model_validate(stored).model_dump(mode="json")
        assert item["evidence"]["thresholds"] == stored["thresholds"]
        assert item["evidence"]["extra"] == stored.get("extra")
