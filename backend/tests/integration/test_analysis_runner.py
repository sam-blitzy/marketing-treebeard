"""Integration tests for ``ads_engine.analysis.runner``: window inputs, run selection, runs.

AAP 0.5.1 (``run_analysis(window, kind, trigger) -> AnalysisRun``, persisting runs,
recommendations, findings, withheld patterns and suppression counts), 0.5.3 ("Where outputs
are frozen", "Run selection and the analysis lock"), 0.3.2 (analysis requests served by a
run), 0.1.1 (reproducibility; a custom 180-day run computes in under 30 s) and 0.11.1 (the
run summary log line). The API, CLI and worker behaviour built on the runner is covered by
``test_runs_and_decisions.py`` and ``test_end_to_end_simulated.py``; this module covers the
runner itself:

* ``window_inputs``, ``default_window`` and ``custom_window`` read the Google account's time
  zone and the active day sources, and ``InvalidWindow`` propagates;
* ``latest_default_run`` returns only the latest succeeded default run, so a manual 90-day
  custom run never stands in for it; ``get_run`` and ``list_runs`` read any status;
* ``run_analysis`` reads the context in one read-only ``REPEATABLE READ`` snapshot, writes
  every output once with the dataset ids and modes it read, stamps the requests it served,
  and is deterministic; on the simulated default world all five sections populate;
* the lock, the single-running index, crash recovery, a failing rule, a contradicting
  override, a lost lock and a rule writing a runner-owned evidence key each leave the
  database in the state the contract names.

Everything runs against the PostgreSQL 18 test database at ``TEST_DATABASE_URL`` (falling
back to ``DATABASE_URL``), in a private schema holding every table, which the application's
engine is pointed at; an unreachable database fails the tests, never skips them.
"""

import asyncio
import dataclasses
import itertools
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on the metadata
from ads_engine.analysis import keywords, metrics, precedence, runner
from ads_engine.analysis.context import (
    ENTITY_BUDGET,
    ENTITY_CAMPAIGN,
    ENTITY_CONTENT_ASSET,
    ENTITY_CREATIVE,
    ENTITY_CREATIVE_PAIR,
    ENTITY_KEYWORD,
    ENTITY_SEARCH_TERM,
    ENTITY_SEGMENT,
    ENTITY_TERM_CLUSTER,
    SECTION_ORDER,
    AnalysisContext,
    EntityRef,
    creative_pair_external_id,
    keyword_external_id,
    search_term_external_id,
    segment_external_id,
)
from ads_engine.analysis.lock import RunInProgress, analysis_lock
from ads_engine.analysis.thresholds import ConfigError, Thresholds, resolve_config_dir
from ads_engine.analysis.window import InvalidWindow
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Campaign
from ads_engine.db.models.analysis import (
    AnalysisRequest,
    AnalysisRun,
    Recommendation,
    RunFinding,
    RunSuppression,
    RunWithheld,
)
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.session import dispose_engine, reset_engine, session_scope
from ads_engine.logging import logged_as
from ads_engine.settings import Settings, get_settings
from ads_engine.simulation.scenarios import FIXED_NOW, SCENARIOS, ContextSpec

pytestmark = pytest.mark.integration

# Read once at import, before any fixture removes DATABASE_URL from the environment.
_CONFIGURED_TEST_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The run instant of the seeded-database tests; yesterday is 2026-10-06 in New York and UTC."""

END = date(2026, 10, 6)
GOOGLE_TZ = "America/New_York"
_KEY_COLUMNS = ("campaign_id", "budget_id", "ad_group_id", "keyword_id", "creative_id")

_CALLER_CONTEXT: ContextVar[str] = ContextVar("analysis_runner_test_caller", default="unset")
"""A context variable the caller sets, to show the analysis thread runs in its context."""

_WAIT_S = 30.0
"""The longest any test waits on a thread event before it fails instead of hanging."""


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    if not _CONFIGURED_TEST_URL:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database",
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
def anyio_backend() -> str:
    """Run every async test on asyncio, the loop psycopg's async driver uses here."""
    return "asyncio"


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding every table of the metadata; drop it afterwards."""
    url = _database_url()
    schema = f"analysis_runner_test_{uuid.uuid4().hex}"
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

    Every settings variable inherited from the host shell is removed first, and the engine
    is disposed on this test's own loop.

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


@dataclasses.dataclass
class _World:
    """The ids of the seeded rows the assertions name."""

    google: int = 0
    linkedin: int = 0
    companion: int = 0
    hubspot: int = 0
    campaign: int = 0


async def _seed() -> _World:
    """Seed active datasets (Google live, LinkedIn simulated with companion, HubSpot live).

    Google holds one account in New York time and one campaign, so ``window_inputs`` reads a
    real zone and the typed entity keys can point at a stored campaign.
    """
    world = _World()
    async with session_scope() as session:

        async def dataset(source: str, mode: str, parent: int | None = None) -> int:
            row = SourceDataset(
                source=source,
                mode=mode,
                state="active",
                role="primary" if parent is None else "companion",
                parent_dataset_id=parent,
                reason="credentials not configured" if mode == "simulated" else None,
                activated_at=NOW - timedelta(days=30),
                retired_at=None,
                crm_bindings=None,
            )
            session.add(row)
            await session.flush()
            return row.id

        world.google = await dataset("google", "live")
        world.linkedin = await dataset("linkedin", "simulated")
        world.companion = await dataset("hubspot", "simulated", parent=world.linkedin)
        world.hubspot = await dataset("hubspot", "live")
        account = AdAccount(
            source="google",
            dataset_id=world.google,
            external_id="1234567890",
            name="Main",
            currency="USD",
            timezone=GOOGLE_TZ,
        )
        session.add(account)
        await session.flush()
        campaign = Campaign(
            source="google",
            dataset_id=world.google,
            account_id=account.id,
            external_id="555",
            name="Brand",
            status="active",
            raw_status="ENABLED",
            objective_or_channel="SEARCH",
            campaign_group_id=None,
            budget_id=None,
            daily_budget=Decimal("20"),
            total_budget=None,
            budget_period="DAILY",
        )
        session.add(campaign)
        await session.flush()
        world.campaign = campaign.id
    return world


async def _insert_run(kind: str, status: str, finished: datetime | None) -> int:
    """Insert a bare ``analysis_run`` row and return its id."""
    async with session_scope() as session:
        run = AnalysisRun(
            kind=kind,
            window_start=END - timedelta(days=27),
            window_end=END,
            source_day_boundaries=[],
            trigger="manual",
            data_modes={},
            dataset_ids={},
            config_hash="0" * 64,
            status=status,
            started_at=NOW,
            finished_at=finished,
        )
        session.add(run)
    return run.id


async def _requests(count: int, *, run_id: int | None = None) -> list[int]:
    """Insert ``count`` analysis requests (served by ``run_id`` when given)."""
    ids: list[int] = []
    async with session_scope() as session:
        for index in range(count):
            row = AnalysisRequest(
                reason=f"sync-{index}",
                picked_at=None if run_id is None else NOW,
                run_id=run_id,
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
    return ids


async def _rows(model: Any, run_id: int) -> list[Any]:
    """Every row of a run-output table for ``run_id``, in insertion order."""
    async with session_scope() as session:
        result = await session.execute(
            select(model).where(model.run_id == run_id).order_by(model.id)
        )
        return list(result.scalars())


async def _count(model: Any) -> int:
    """The number of rows in ``model``'s table."""
    async with session_scope() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _runs() -> list[AnalysisRun]:
    """Every run, by id."""
    async with session_scope() as session:
        return list((await session.execute(select(AnalysisRun).order_by(AnalysisRun.id))).scalars())


def _patch_context(monkeypatch: pytest.MonkeyPatch, ctx: AnalysisContext) -> None:
    """Make ``metrics.load_context`` return ``ctx`` (the pure stand-in for a full sync)."""

    async def loaded(*_args: Any, **_kwargs: Any) -> AnalysisContext:
        return ctx

    monkeypatch.setattr(metrics, "load_context", loaded)


class _LogRecorder:
    """Stand-in for the runner's module logger that records every event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str, event: str, fields: dict[str, Any]) -> None:
        self.events.append((level, event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self._record("info", event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._record("warning", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._record("error", event, fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._record("exception", event, fields)

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for _, name, fields in self.events if name == event]


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """Record the runner's log events."""
    recorder = _LogRecorder()
    monkeypatch.setattr(runner, "_log", recorder)
    return recorder


@pytest.fixture(scope="module")
def default_ctx() -> AnalysisContext:
    """The simulated default world's context at the fixed instant (all five sections)."""
    return SCENARIOS["default"].build_context(now=FIXED_NOW)


# ---------------------------------------------------------------------------
# Window inputs
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_window_inputs_without_active_datasets(app_db: str) -> None:
    async with session_scope() as session:
        config, info = await runner.window_inputs(session)
    assert info.google_timezone is None
    assert info.day_sources == ("google", "linkedin")
    now = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
    window = await runner.default_window(now=now)
    assert window.end == date(2026, 10, 5)
    assert window.days == config.default_days


@pytest.mark.anyio
async def test_windows_read_the_google_time_zone_and_active_day_sources(app_db: str) -> None:
    await _seed()
    async with session_scope() as session:
        _, info = await runner.window_inputs(session)
    assert info.google_timezone == GOOGLE_TZ
    assert info.day_sources == ("google", "linkedin")
    # At 02:00 UTC it is still the previous day in New York: the earlier yesterday wins.
    early = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
    window = await runner.default_window(now=early)
    assert window.end == date(2026, 10, 5)
    assert window.boundary("google") == GOOGLE_TZ
    assert window.boundary("linkedin") == "UTC"
    custom = await runner.custom_window(date(2026, 9, 1), date(2026, 9, 30), now=early)
    assert (custom.start, custom.end, custom.days) == (date(2026, 9, 1), date(2026, 9, 30), 30)
    with pytest.raises(InvalidWindow) as raised:
        await runner.custom_window(date(2026, 9, 1), date(2026, 10, 6), now=early)
    assert raised.value.code == "end_after_default_end"
    with pytest.raises(ValueError, match="naive"):
        await runner.default_window(now=datetime(2026, 10, 7))  # noqa: DTZ001 - naive on purpose


@pytest.mark.anyio
async def test_window_inputs_with_only_linkedin_active(app_db: str) -> None:
    async with session_scope() as session:
        session.add(
            SourceDataset(
                source="linkedin",
                mode="simulated",
                state="active",
                role="primary",
                parent_dataset_id=None,
                reason="credentials not configured",
                activated_at=NOW,
                retired_at=None,
                crm_bindings=None,
            )
        )
    async with session_scope() as session:
        _, info = await runner.window_inputs(session)
    assert info.day_sources == ("linkedin",)
    assert info.google_timezone is None


# ---------------------------------------------------------------------------
# Run selection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_latest_default_run_ignores_custom_failed_and_running_runs(app_db: str) -> None:
    older = await _insert_run("default", "succeeded", NOW)
    custom = await _insert_run("custom", "succeeded", NOW + timedelta(hours=1))
    failed = await _insert_run("default", "failed", NOW + timedelta(hours=2))
    running = await _insert_run("default", "running", None)
    async with session_scope() as session:
        latest = await runner.latest_default_run(session)
        assert latest is not None
        assert latest.id == older
        found = await runner.get_run(session, running)
        assert found is not None
        assert found.status == "running"
        assert await runner.get_run(session, 999_999) is None
        assert [run.id for run in await runner.list_runs(session, kind="custom", limit=5)] == [
            custom
        ]
        defaults = await runner.list_runs(session, kind="default", limit=2)
        assert [run.id for run in defaults] == [running, failed]
        with pytest.raises(ValueError, match="kind"):
            await runner.list_runs(session, kind="manual", limit=5)
        with pytest.raises(ValueError, match="limit"):
            await runner.list_runs(session, kind="custom", limit=0)
        with pytest.raises(TypeError):
            await runner.list_runs(session, kind="custom", limit=True)
        with pytest.raises(TypeError):
            await runner.get_run(session, "1")  # type: ignore[arg-type]
    newer = await _insert_run("default", "succeeded", NOW + timedelta(hours=3))
    async with session_scope() as session:
        latest = await runner.latest_default_run(session)
        assert latest is not None
        assert latest.id == newer


# ---------------------------------------------------------------------------
# run_analysis on the seeded database (the real load_context)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_reads_one_snapshot_and_records_what_it_read(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    world = await _seed()
    seen: dict[str, str] = {}
    original = metrics.load_context

    async def spying(session: Any, *args: Any, **kwargs: Any) -> AnalysisContext:
        seen["isolation"] = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()
        seen["read_only"] = (await session.execute(text("SHOW transaction_read_only"))).scalar_one()
        return await original(session, *args, **kwargs)

    monkeypatch.setattr(metrics, "load_context", spying)
    pending = await _requests(2)
    window = await runner.default_window(now=NOW)
    run = await runner.run_analysis(
        window, "default", "event", reason="sync", request_ids=[*pending, pending[0]], now=NOW
    )

    assert seen == {"isolation": "repeatable read", "read_only": "on"}
    async with session_scope() as session:
        restored = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()
    assert restored == "read committed"
    assert (run.status, run.kind, run.trigger) == ("succeeded", "default", "event")
    assert run.started_at == NOW
    assert run.finished_at is not None
    assert (run.window_start, run.window_end) == (window.start, window.end)
    assert run.source_day_boundaries == window.source_days_json()
    assert run.dataset_ids == {
        "google": world.google,
        "linkedin": world.linkedin,
        "hubspot": world.hubspot,
        "hubspot_linkedin_effective": world.companion,
    }
    assert run.data_modes == {"google": "live", "linkedin": "simulated", "hubspot": "live"}
    async with session_scope() as session:
        stored = await session.get(AnalysisRun, run.id)
        assert stored is not None
        assert stored.status == "succeeded"
        requests = list(
            (await session.execute(select(AnalysisRequest).order_by(AnalysisRequest.id))).scalars()
        )
    assert [(row.run_id, row.picked_at is not None) for row in requests] == [(run.id, True)] * 2
    summary = log.named("analysis_run_summary")
    assert len(summary) == 1
    assert summary[0]["run_id"] == run.id
    assert summary[0]["config_hash"] == run.config_hash
    assert set(summary[0]["items"]) == set(SECTION_ORDER)
    assert summary[0]["requests"] == 2
    # The run's total duration shares the sync jobs' field name; the stages keep their own.
    assert isinstance(summary[0]["duration_ms"], int)
    assert "elapsed_ms" not in summary[0]
    assert all(isinstance(summary[0][key], int) for key in ("load_ms", "compute_ms", "persist_ms"))


@pytest.mark.anyio
async def test_requests_already_served_are_left_alone(app_db: str) -> None:
    await _seed()
    earlier = await _insert_run("default", "succeeded", NOW)
    served = await _requests(1, run_id=earlier)
    pending = await _requests(1)
    run = await runner.run_analysis(
        await runner.default_window(now=NOW),
        "default",
        "event",
        request_ids=[*served, *pending],
        now=NOW,
    )
    async with session_scope() as session:
        rows = {
            row.id: row.run_id for row in (await session.execute(select(AnalysisRequest))).scalars()
        }
    assert rows == {served[0]: earlier, pending[0]: run.id}


@pytest.mark.anyio
async def test_a_custom_run_never_becomes_the_latest_default_run(app_db: str) -> None:
    await _seed()
    default = await runner.run_analysis(
        await runner.default_window(now=NOW), "default", "scheduled", now=NOW
    )
    window = await runner.custom_window(END - timedelta(days=89), END, now=NOW)
    custom = await runner.run_analysis(window, "custom", "manual", now=NOW)
    assert (custom.kind, custom.status) == ("custom", "succeeded")
    assert (custom.window_end - custom.window_start).days + 1 == 90
    # A custom run over exactly the default window is still a custom run.
    same = await runner.run_analysis(
        await runner.custom_window(default.window_start, default.window_end, now=NOW),
        "custom",
        "manual",
        now=NOW,
    )
    async with session_scope() as session:
        latest = await runner.latest_default_run(session)
        customs = await runner.list_runs(session, kind="custom", limit=10)
    assert latest is not None
    assert latest.id == default.id
    assert [run.id for run in customs] == [same.id, custom.id]


@pytest.mark.anyio
async def test_changed_default_days_give_a_run_of_that_length(app_db: str) -> None:
    await _seed()
    config = Thresholds.load(
        resolve_config_dir(),
        overrides={"window.default_days": 21, "window.presets": [14, 21, 28, 56, 90]},
    )
    window = await runner.default_window(now=NOW, thresholds=config)
    run = await runner.run_analysis(window, "default", "scheduled", thresholds=config, now=NOW)
    assert (run.window_end - run.window_start).days + 1 == 21
    assert run.config_hash == config.config_hash


@pytest.mark.anyio
async def test_invalid_arguments_write_no_run(app_db: str) -> None:
    await _seed()
    short = await runner.custom_window(END - timedelta(days=20), END, now=NOW)
    with pytest.raises(ValueError, match="default run needs the configured default window"):
        await runner.run_analysis(short, "default", "scheduled", now=NOW)
    with pytest.raises(ValueError, match="kind"):
        await runner.run_analysis(short, "nightly", "scheduled", now=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="trigger"):
        await runner.run_analysis(short, "custom", "cron", now=NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="AnalysisWindow"):
        await runner.run_analysis("28d", "custom", "manual", now=NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="request_ids"):
        await runner.run_analysis(short, "custom", "manual", request_ids=[True], now=NOW)
    with pytest.raises(ValueError, match="naive"):
        await runner.run_analysis(short, "custom", "manual", now=datetime(2026, 10, 7))  # noqa: DTZ001
    assert await _count(AnalysisRun) == 0


# ---------------------------------------------------------------------------
# run_analysis on the simulated default world: every section, frozen and deterministic
# ---------------------------------------------------------------------------


def _shape(rows: list[Any]) -> list[tuple[Any, ...]]:
    """What a stored recommendation shows, without its ids."""
    return [
        (
            row.section,
            row.group,
            row.rank,
            row.rule_id,
            row.entity_type,
            row.entity_id,
            row.action,
            row.reason,
            row.evidence,
            row.flags,
            row.data_status,
            row.held,
            row.held_reason,
            row.required_datasets,
            row.decision_subject,
        )
        for row in rows
    ]


@pytest.mark.anyio
async def test_default_world_persists_all_five_sections_deterministically(
    app_db: str, monkeypatch: pytest.MonkeyPatch, default_ctx: AnalysisContext
) -> None:
    _patch_context(monkeypatch, default_ctx)
    first = await runner.run_analysis(
        default_ctx.window,
        "default",
        "scheduled",
        thresholds=default_ctx.thresholds,
        now=default_ctx.now,
    )
    items = await _rows(Recommendation, first.id)
    by_section = {
        section: [row for row in items if row.section == section] for section in SECTION_ORDER
    }
    assert all(by_section[section] for section in SECTION_ORDER), {
        section: len(rows) for section, rows in by_section.items()
    }
    for section, rows in by_section.items():
        assert sorted(row.rank for row in rows) == list(range(1, len(rows) + 1)), section
    window_json = default_ctx.window.to_json()
    for row in items:
        assert row.data_status == "simulated"
        assert "simulated" in row.flags
        evidence = row.evidence
        assert evidence["window"] == window_json
        assert evidence["window"]["source_days"]
        assert {entry["mode"] for entry in evidence["provenance"]} == {"simulated"}
        extra = evidence["extra"]
        assert "depends_on" in extra
        assert set(extra["flag_messages"]) == set(row.flags)
        assert all(message for message in extra["flag_messages"].values())
        entity = extra["entity"]
        assert entity["type"] == row.entity_type
        assert row.entity_id.startswith(f"{entity['source']}:{entity['type']}:")
        assert entity["name"]
        # The pure context's ids name no stored row, so every typed key stays NULL.
        assert all(getattr(row, column) is None for column in _KEY_COLUMNS)
    assert first.data_modes == {
        "google": "simulated",
        "linkedin": "simulated",
        "hubspot": "simulated",
    }
    linkedin_crm = default_ctx.linkedin_crm
    assert linkedin_crm is not None
    assert first.dataset_ids["hubspot_linkedin_effective"] == linkedin_crm.dataset.dataset_id
    findings = await _rows(RunFinding, first.id)
    withheld = await _rows(RunWithheld, first.id)
    assert findings
    assert withheld
    assert all(isinstance(row.observed, Decimal | None) for row in withheld)
    suppressions = await _rows(RunSuppression, first.id)
    assert all(row.count > 0 for row in suppressions)

    second = await runner.run_analysis(
        default_ctx.window,
        "default",
        "scheduled",
        thresholds=default_ctx.thresholds,
        now=default_ctx.now,
    )
    assert _shape(await _rows(Recommendation, second.id)) == _shape(items)
    assert [(row.section, row.code, row.message, row.entity, row.evidence) for row in findings] == [
        (row.section, row.code, row.message, row.entity, row.evidence)
        for row in await _rows(RunFinding, second.id)
    ]
    assert [
        (row.section, row.rule_id, row.entity, row.reason, row.observed, row.required, row.unit)
        for row in withheld
    ] == [
        (row.section, row.rule_id, row.entity, row.reason, row.observed, row.required, row.unit)
        for row in await _rows(RunWithheld, second.id)
    ]
    assert [(row.section, row.reason, row.count) for row in suppressions] == [
        (row.section, row.reason, row.count) for row in await _rows(RunSuppression, second.id)
    ]


@pytest.mark.anyio
async def test_feedback_suppression_counts_are_stored_per_section(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    ctx = SCENARIOS["feedback.dismiss.case"].build_context()
    _patch_context(monkeypatch, ctx)
    run = await runner.run_analysis(
        ctx.window, ctx.run_kind, "event", thresholds=ctx.thresholds, now=ctx.now
    )
    stored = [(row.section, row.reason, row.count) for row in await _rows(RunSuppression, run.id)]
    assert stored == [("keywords", "feedback_dismiss", 1)]
    (summary,) = log.named("analysis_run_summary")
    assert summary["suppressed"] == {"feedback_dismiss": 1}


@pytest.mark.anyio
async def test_a_180_day_custom_run_computes_well_within_30_seconds(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    end = date(2026, 10, 1)
    scenario = dataclasses.replace(
        SCENARIOS["default"],
        context=ContextSpec(custom=(end - timedelta(days=179), end), kind="custom"),
    )
    ctx = scenario.build_context(now=FIXED_NOW)
    assert ctx.window.days == 180
    _patch_context(monkeypatch, ctx)
    started = time.perf_counter()
    run = await runner.run_analysis(
        ctx.window, "custom", "manual", thresholds=ctx.thresholds, now=ctx.now
    )
    elapsed = time.perf_counter() - started
    assert run.status == "succeeded"
    assert elapsed < 30
    (summary,) = log.named("analysis_run_summary")
    assert summary["days"] == 180
    assert summary["items_total"] == len(await _rows(Recommendation, run.id))


# ---------------------------------------------------------------------------
# Typed entity keys
# ---------------------------------------------------------------------------


def test_typed_keys_follow_each_entity_type(default_ctx: AnalysisContext) -> None:
    ctx = default_ctx
    index = runner._KeyIndex.of(ctx)
    keyword = next(iter(ctx.keywords.values()))
    ad_group = ctx.ad_groups[keyword.ad_group_id]
    creatives = sorted(ctx.creatives.values(), key=lambda item: item.id)
    pair = [item for item in creatives if item.campaign_id == creatives[0].campaign_id][:2]
    linkedin = ctx.campaigns[creatives[0].campaign_id]
    budget = next(iter(ctx.budgets.values()))

    def keys(ref: EntityRef, extra: dict[str, Any] | None = None) -> runner._TypedKeys:
        return runner._typed_keys(ctx, index, ref, extra)

    assert keys(EntityRef("google", ENTITY_KEYWORD, keyword.id, "x", "kw")) == runner._TypedKeys(
        campaign_id=keyword.campaign_id, ad_group_id=keyword.ad_group_id, keyword_id=keyword.id
    )
    term = search_term_external_id(ad_group.external_id, keyword.criterion_id, "acme")
    assert keys(EntityRef("google", ENTITY_SEARCH_TERM, None, term, "acme")) == runner._TypedKeys(
        campaign_id=ad_group.campaign_id, ad_group_id=ad_group.id
    )
    by_external = keyword_external_id(ad_group.external_id, keyword.criterion_id)
    assert keys(EntityRef("google", ENTITY_KEYWORD, None, by_external, "kw")).ad_group_id == (
        ad_group.id
    )
    assert keys(EntityRef("google", ENTITY_BUDGET, budget.id, budget.external_id, "b")) == (
        runner._TypedKeys(budget_id=budget.id)
    )
    assert keys(
        EntityRef("linkedin", ENTITY_CAMPAIGN, None, linkedin.external_id, "c")
    ) == runner._TypedKeys(campaign_id=linkedin.id)
    assert keys(
        EntityRef("linkedin", ENTITY_CREATIVE, None, creatives[0].urn, "cr")
    ) == runner._TypedKeys(campaign_id=creatives[0].campaign_id, creative_id=creatives[0].id)
    segment = segment_external_id(linkedin.external_id, "seniority", "Director")
    assert keys(EntityRef("linkedin", ENTITY_SEGMENT, None, segment, "s")) == runner._TypedKeys(
        campaign_id=linkedin.id
    )
    if len(pair) == 2:
        pair_id = creative_pair_external_id(pair[0].urn, pair[1].urn)
        assert keys(
            EntityRef("linkedin", ENTITY_CREATIVE_PAIR, None, pair_id, "p")
        ) == runner._TypedKeys(campaign_id=pair[0].campaign_id)
    cluster = EntityRef("google", ENTITY_TERM_CLUSTER, None, "acme pricing", "cl")
    assert keys(cluster, {"campaign_ids": [keyword.campaign_id]}) == runner._TypedKeys(
        campaign_id=keyword.campaign_id
    )
    assert keys(cluster, {"campaign_ids": [1, 2]}) == runner._TypedKeys()
    assert keys(cluster) == runner._TypedKeys()
    asset = EntityRef("linkedin", ENTITY_CONTENT_ASSET, None, "guide|USD", "Guide")
    assert keys(asset) == runner._TypedKeys()
    assert runner._typed_keys(ctx, index, None) == runner._TypedKeys()


@pytest.mark.anyio
async def test_typed_keys_keep_only_ids_of_stored_rows(app_db: str) -> None:
    world = await _seed()
    present = runner._TypedKeys(campaign_id=world.campaign)
    missing = runner._TypedKeys(campaign_id=world.campaign + 1000, budget_id=77)
    async with session_scope() as session:
        known = await runner._known_ids(session, [present, missing])
    assert present.row(known)["campaign_id"] == world.campaign
    assert missing.row(known) == dict.fromkeys(_KEY_COLUMNS)


# ---------------------------------------------------------------------------
# The lock, the second guard, crash recovery and failures
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_held_lock_raises_run_in_progress_and_leaves_requests_pending(
    app_db: str,
) -> None:
    await _seed()
    window = await runner.default_window(now=NOW)
    pending = await _requests(1)
    async with analysis_lock():
        with pytest.raises(RunInProgress):
            await runner.run_analysis(window, "default", "event", request_ids=pending, now=NOW)
    assert await _count(AnalysisRun) == 0
    async with session_scope() as session:
        request = (await session.execute(select(AnalysisRequest))).scalar_one()
    assert (request.picked_at, request.run_id) == (None, None)


@pytest.mark.anyio
async def test_the_single_running_index_is_reported_as_run_in_progress(
    app_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed()
    await _insert_run("custom", "running", None)

    @asynccontextmanager
    async def unguarded() -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(runner, "analysis_lock", unguarded)
    with pytest.raises(RunInProgress):
        await runner.run_analysis(await runner.default_window(now=NOW), "default", "event", now=NOW)
    assert await _count(AnalysisRun) == 1


@pytest.mark.anyio
async def test_a_crashed_running_row_is_failed_by_the_next_run(app_db: str) -> None:
    await _seed()
    orphan = await _insert_run("default", "running", None)
    run = await runner.run_analysis(
        await runner.default_window(now=NOW), "default", "event", now=NOW
    )
    statuses = {row.id: row.status for row in await _runs()}
    assert statuses == {orphan: "failed", run.id: "succeeded"}


@pytest.mark.anyio
async def test_concurrent_runs_one_succeeds_the_other_is_refused(app_db: str) -> None:
    await _seed()
    window = await runner.default_window(now=NOW)
    results = await asyncio.gather(
        runner.run_analysis(window, "default", "event", now=NOW),
        runner.run_analysis(window, "default", "event", now=NOW),
        return_exceptions=True,
    )
    assert sorted(type(result).__name__ for result in results) == ["AnalysisRun", "RunInProgress"]
    assert [row.status for row in await _runs()] == ["succeeded"]


@pytest.mark.anyio
async def test_a_failing_rule_fails_the_run_and_stores_nothing(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    await _seed()
    pending = await _requests(2)

    def exploding(_ctx: AnalysisContext) -> Any:
        raise RuntimeError("rule exploded")

    rules = tuple(
        (name, exploding if name == "keywords" else evaluate) for name, evaluate in runner._RULES
    )
    monkeypatch.setattr(runner, "_RULES", rules)
    with pytest.raises(RuntimeError, match="rule exploded") as raised:
        await runner.run_analysis(
            await runner.default_window(now=NOW), "default", "event", request_ids=pending, now=NOW
        )
    assert any("'keywords'" in note for note in raised.value.__notes__)
    (run,) = await _runs()
    assert run.status == "failed"
    assert run.finished_at is not None
    assert await _count(Recommendation) == 0
    async with session_scope() as session:
        served = [row.run_id for row in (await session.execute(select(AnalysisRequest))).scalars()]
    assert served == [run.id, run.id]
    (failed,) = log.named("analysis_run_failed")
    assert (failed["run_id"], failed["stage"], failed["error"]) == (run.id, "rules", "RuntimeError")
    assert failed["exc_info"] is raised.value
    assert isinstance(failed["duration_ms"], int)
    assert "elapsed_ms" not in failed
    # The run's failure is logged once with its traceback; callers read the marker.
    assert logged_as(raised.value) == "analysis_run_failed"
    async with analysis_lock():
        pass  # the lock was released


@pytest.mark.anyio
async def test_a_contradicting_override_fails_the_run(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    await _seed()
    seen: list[dict[str, str | None]] = []

    def contradicting(self: Thresholds, creative_forms: Any) -> None:
        seen.append(dict(creative_forms))
        raise ConfigError("content_assets.yaml", "assets[0].creative_overrides[0]", "contradicts")

    monkeypatch.setattr(Thresholds, "check_overrides", contradicting)
    with pytest.raises(ConfigError) as raised:
        await runner.run_analysis(await runner.default_window(now=NOW), "default", "event", now=NOW)
    assert seen == [{}]
    (run,) = await _runs()
    assert run.status == "failed"
    (failed,) = log.named("analysis_run_failed")
    assert failed["stage"] == "check_overrides"
    assert failed["config_file"] == "content_assets.yaml"
    assert logged_as(raised.value) == "analysis_run_failed"


@pytest.mark.anyio
async def test_a_run_that_lost_its_lock_stores_nothing(
    app_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed()
    original = precedence.apply_precedence

    def taken_over(results: Any, ctx: AnalysisContext) -> Any:
        out = original(results, ctx)
        # Another process took the lock after this one lost it and failed the row.
        engine = create_engine(make_url(app_db), poolclass=NullPool)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE analysis_run SET status = 'failed', finished_at = now()")
                )
        finally:
            engine.dispose()
        return out

    monkeypatch.setattr(precedence, "apply_precedence", taken_over)
    with pytest.raises(runner.RunNoLongerRunning) as raised:
        await runner.run_analysis(await runner.default_window(now=NOW), "default", "event", now=NOW)
    assert raised.value.status == "failed"
    (run,) = await _runs()
    assert run.status == "failed"
    assert await _count(Recommendation) == 0


@pytest.mark.anyio
async def test_a_rule_writing_a_runner_owned_key_fails_the_run(
    app_db: str, monkeypatch: pytest.MonkeyPatch, default_ctx: AnalysisContext
) -> None:
    _patch_context(monkeypatch, default_ctx)
    original = keywords.evaluate

    def clashing(ctx: AnalysisContext) -> Any:
        result = original(ctx)
        assert result.candidates
        candidates = tuple(
            dataclasses.replace(item, evidence=item.evidence.with_extra(entity={"name": "x"}))
            for item in result.candidates
        )
        return dataclasses.replace(result, candidates=candidates)

    rules = tuple(
        (name, clashing if name == "keywords" else evaluate) for name, evaluate in runner._RULES
    )
    monkeypatch.setattr(runner, "_RULES", rules)
    with pytest.raises(ValueError, match="runner-owned"):
        await runner.run_analysis(
            default_ctx.window,
            "default",
            "scheduled",
            thresholds=default_ctx.thresholds,
            now=default_ctx.now,
        )
    (run,) = await _runs()
    assert run.status == "failed"
    assert await _count(Recommendation) == 0


# ---------------------------------------------------------------------------
# Off the event loop, and cancellation that never outlives the lock
# ---------------------------------------------------------------------------

_Evaluate = Callable[[AnalysisContext], Any]


def _wrapped_rules(
    wrap: Callable[[str, _Evaluate], _Evaluate],
) -> tuple[tuple[str, _Evaluate], ...]:
    """``runner._RULES`` with every rule module's evaluator passed through ``wrap``."""
    return tuple((name, wrap(name, evaluate)) for name, evaluate in runner._RULES)


@pytest.mark.anyio
async def test_rules_run_off_the_event_loop_which_stays_responsive(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    await _seed()
    busy_s = 0.5
    seen: dict[str, Any] = {}

    def wrap(name: str, evaluate: _Evaluate) -> _Evaluate:
        if name != "keywords":
            return evaluate

        def busy(ctx: AnalysisContext) -> Any:
            seen["thread"] = threading.current_thread()
            seen["caller"] = _CALLER_CONTEXT.get()
            seen["start"] = time.perf_counter()
            deadline = seen["start"] + busy_s
            while time.perf_counter() < deadline:
                pass  # CPU-bound and holding the GIL, as the statistical rules are
            seen["end"] = time.perf_counter()
            return evaluate(ctx)

        return busy

    monkeypatch.setattr(runner, "_RULES", _wrapped_rules(wrap))
    window = await runner.default_window(now=NOW)
    ticks: list[float] = []
    done = asyncio.Event()

    async def ticking() -> None:
        while not done.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.01)

    token = _CALLER_CONTEXT.set("run_analysis caller")
    ticker = asyncio.create_task(ticking())
    try:
        run = await runner.run_analysis(window, "default", "event", now=NOW)
    finally:
        done.set()
        await ticker
        _CALLER_CONTEXT.reset(token)

    assert run.status == "succeeded"
    assert seen["thread"] is not threading.current_thread()
    assert seen["thread"].name.startswith("ads-engine-analysis")
    assert seen["caller"] == "run_analysis caller"
    # A blocked loop would leave no tick inside the busy half second, one 0.5 s gap.
    during = [tick for tick in ticks if seen["start"] <= tick <= seen["end"]]
    assert len(during) >= 5, len(during)
    points = [seen["start"], *during, seen["end"]]
    assert max(later - earlier for earlier, later in itertools.pairwise(points)) < 0.2
    (summary,) = log.named("analysis_run_summary")
    assert summary["compute_ms"] >= round(busy_s * 1000)


@pytest.mark.anyio
async def test_a_cancelled_run_holds_the_lock_until_its_computation_stops(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    await _seed()
    pending = await _requests(2)
    entered = threading.Event()
    release = threading.Event()
    ran: list[str] = []

    def wrap(name: str, evaluate: _Evaluate) -> _Evaluate:
        def evaluating(ctx: AnalysisContext) -> Any:
            ran.append(name)
            if name == "keywords":
                entered.set()
                assert release.wait(_WAIT_S), "the test never released the rule"
            return evaluate(ctx)

        return evaluating

    monkeypatch.setattr(runner, "_RULES", _wrapped_rules(wrap))
    window = await runner.default_window(now=NOW)
    task = asyncio.create_task(
        runner.run_analysis(window, "default", "event", request_ids=pending, now=NOW)
    )
    assert await asyncio.to_thread(entered.wait, _WAIT_S)
    task.cancel()
    await asyncio.sleep(0.2)
    assert not task.done()
    # The computation still runs, so the lock is still held: a second run is refused.
    with pytest.raises(RunInProgress):
        await runner.run_analysis(window, "default", "event", now=NOW)
    task.cancel()  # a repeated cancellation is held as well
    await asyncio.sleep(0.05)
    assert not task.done()
    assert [row.status for row in await _runs()] == ["running"]

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    # The computation stopped at the next checkpoint: no later rule module ran.
    assert ran == ["discrepancy", "keywords"]
    (run,) = await _runs()
    assert run.status == "failed"
    assert run.finished_at is not None
    assert await _count(Recommendation) == 0
    async with session_scope() as session:
        rows = await session.execute(select(AnalysisRequest).order_by(AnalysisRequest.id))
        served = [row.run_id for row in rows.scalars()]
    assert served == [run.id, run.id]
    (failed,) = log.named("analysis_run_failed")
    assert (failed["run_id"], failed["stage"], failed["error"]) == (
        run.id,
        "rules",
        "CancelledError",
    )
    async with analysis_lock():
        pass  # the lock was released


async def _served_by() -> list[int | None]:
    """The ``run_id`` of every analysis request, by id."""
    async with session_scope() as session:
        rows = await session.execute(select(AnalysisRequest).order_by(AnalysisRequest.id))
        return [row.run_id for row in rows.scalars()]


@pytest.mark.anyio
async def test_a_cancellation_as_the_running_row_commits_fails_that_row(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    await _seed()
    pending = await _requests(2)
    window = await runner.default_window(now=NOW)
    target: dict[str, asyncio.Task[AnalysisRun]] = {}
    original_close = AsyncSession.close

    async def closing(self: AsyncSession) -> None:
        # The insert's session closes after its commit, still holding the run row: the run
        # is cancelled there, as a caller giving up at that instant would cancel it.
        task = target.get("run")
        if task is not None and any(
            isinstance(item, AnalysisRun) for item in self.identity_map.values()
        ):
            del target["run"]
            task.cancel()
        await original_close(self)

    monkeypatch.setattr(AsyncSession, "close", closing)
    task = asyncio.create_task(
        runner.run_analysis(window, "default", "event", request_ids=pending, now=NOW)
    )
    target["run"] = task
    with pytest.raises(asyncio.CancelledError):
        await task

    assert target == {}  # the cancellation arrived while the committed insert closed
    assert task.cancelled()
    (run,) = await _runs()
    assert run.status == "failed"
    assert run.finished_at is not None
    assert await _served_by() == [run.id, run.id]
    assert log.named("analysis_run_started") == []
    (failed,) = log.named("analysis_run_failed")
    assert (failed["run_id"], failed["stage"], failed["error"]) == (
        run.id,
        "insert",
        "CancelledError",
    )
    async with analysis_lock():
        pass  # the lock was released


@pytest.mark.parametrize("first", ["rule_error", "cancelled"])
@pytest.mark.anyio
async def test_repeated_cancellation_while_the_failure_is_recorded_still_fails_the_row(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder, first: str
) -> None:
    await _seed()
    pending = await _requests(2)
    entered = threading.Event()
    release = threading.Event()

    def wrap(name: str, evaluate: _Evaluate) -> _Evaluate:
        if name != "keywords":
            return evaluate

        def failing(ctx: AnalysisContext) -> Any:
            if first == "rule_error":
                raise RuntimeError("rule exploded")
            entered.set()
            assert release.wait(_WAIT_S), "the test never released the rule"
            return evaluate(ctx)

        return failing

    monkeypatch.setattr(runner, "_RULES", _wrapped_rules(wrap))
    target: dict[str, asyncio.Task[AnalysisRun]] = {}
    cancels: list[int] = []
    original_serve = runner._serve_requests

    async def serving(
        session: AsyncSession, run_id: int, request_ids: Any, picked_at: datetime
    ) -> list[int]:
        # Only the failure record stamps requests here. Its update of the run row has run in
        # this transaction: cancel the run again and again, as a level-triggered cancel
        # scope would, before the requests are stamped and the transaction commits.
        task = target["run"]
        for _ in range(3):
            task.cancel()
            cancels.append(run_id)
            await asyncio.sleep(0)
        return await original_serve(session, run_id, request_ids, picked_at)

    monkeypatch.setattr(runner, "_serve_requests", serving)
    window = await runner.default_window(now=NOW)
    task = asyncio.create_task(
        runner.run_analysis(window, "default", "event", request_ids=pending, now=NOW)
    )
    target["run"] = task
    if first == "cancelled":
        assert await asyncio.to_thread(entered.wait, _WAIT_S)
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    (run,) = await _runs()
    assert cancels == [run.id] * 3
    assert run.status == "failed"
    assert run.finished_at is not None
    assert await _served_by() == [run.id, run.id]
    assert log.named("analysis_run_mark_failed_error") == []
    (failed,) = log.named("analysis_run_failed")
    assert (failed["stage"], failed["error"]) == (
        "rules",
        "RuntimeError" if first == "rule_error" else "CancelledError",
    )
    async with analysis_lock():
        pass  # the lock was released


@pytest.mark.parametrize("failure", ["database_error", "timeout"])
@pytest.mark.anyio
async def test_a_failure_to_record_the_failure_keeps_the_run_error(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder, failure: str
) -> None:
    await _seed()
    pending = await _requests(1)

    def exploding(_ctx: AnalysisContext) -> Any:
        raise RuntimeError("rule exploded")

    async def unavailable(*_args: Any, **_kwargs: Any) -> list[int]:
        if failure == "timeout":
            await asyncio.sleep(_WAIT_S)
        raise OperationalError("UPDATE analysis_request", {}, ConnectionError("server closed"))

    monkeypatch.setattr(
        runner,
        "_RULES",
        _wrapped_rules(lambda name, evaluate: exploding if name == "keywords" else evaluate),
    )
    monkeypatch.setattr(runner, "_serve_requests", unavailable)
    monkeypatch.setattr(runner, "_FAILURE_RECORD_TIMEOUT_S", 0.2)
    started = time.perf_counter()
    with pytest.raises(RuntimeError, match="rule exploded"):
        await runner.run_analysis(
            await runner.default_window(now=NOW), "default", "event", request_ids=pending, now=NOW
        )

    assert time.perf_counter() - started < _WAIT_S / 2
    (error,) = log.named("analysis_run_mark_failed_error")
    assert error["error"] == ("OperationalError" if failure == "database_error" else "TimeoutError")
    (failed,) = log.named("analysis_run_failed")
    assert (failed["stage"], failed["error"]) == ("rules", "RuntimeError")
    # The failure record rolled back whole: the row stays running and the request pending
    # until the next process to take the lock marks the row failed (crash recovery).
    (run,) = await _runs()
    assert run.status == "running"
    assert await _served_by() == [None]
    async with analysis_lock():
        pass
    assert [row.status for row in await _runs()] == ["failed"]
