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
  custom run never stands in for it, and reads it in one backward probe of
  ``ix_analysis_run_kind_status_finished`` however long the run history; ``get_run`` and
  ``list_runs`` read any status;
* ``run_analysis`` reads the context in one read-only ``REPEATABLE READ`` snapshot, writes
  every output once with the dataset ids and modes it read, stamps the requests it served,
  and is deterministic; on the simulated default world all five sections populate;
* the typed entity keys keep only ids of stored rows, and a run whose outputs carry more
  distinct ids than PostgreSQL's 65,535 bind parameters still persists in one transaction;
* the lock, the single-running index, crash recovery, a failing rule, a contradicting
  override, a lost lock and a rule writing a runner-owned evidence key each leave the
  database in the state the contract names;
* an isolated run (``POST /runs``) loads, computes and persists in the API's analysis
  process: it stores what the analysis thread stores, contenders are refused at once while
  it runs, a cancellation, a lost row and a killed process each leave the run failed and
  the lock free, an analysis process dies with its killed API process and leaves no session
  waiting behind it, a run stored before its cancellation took hold stays succeeded, a process
  that died idle or never started is replaced, a changed environment spawns a fresh
  process, and the process's task entry, initializer and lifecycle behave as documented.

Everything runs against the PostgreSQL 18 test database at ``TEST_DATABASE_URL`` (falling
back to ``DATABASE_URL``), in a private schema holding every table, which the application's
engine is pointed at; an unreachable database fails the tests, never skips them.
"""

import asyncio
import ctypes
import dataclasses
import errno
import itertools
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Executable, Update, create_engine, event, func, select, text
from sqlalchemy.engine import URL, Connection, Result, make_url
from sqlalchemy.exc import ArgumentError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on the metadata
from ads_engine.analysis import keywords, metrics, precedence, runner
from ads_engine.analysis.context import (
    C0,
    ENTITY_BUDGET,
    ENTITY_CAMPAIGN,
    ENTITY_CONTENT_ASSET,
    ENTITY_CREATIVE,
    ENTITY_CREATIVE_PAIR,
    ENTITY_KEYWORD,
    ENTITY_SEARCH_TERM,
    ENTITY_SEGMENT,
    ENTITY_TERM_CLUSTER,
    K1,
    K3,
    KEYWORDS,
    SECTION_ORDER,
    WHITE_PAPERS,
    AnalysisContext,
    DiscrepancyInfo,
    EntityRef,
    Withheld,
    creative_pair_external_id,
    keyword_external_id,
    search_term_external_id,
    segment_external_id,
)
from ads_engine.analysis.lock import RunInProgress, analysis_lock
from ads_engine.analysis.thresholds import ConfigError, Thresholds, resolve_config_dir
from ads_engine.analysis.window import InvalidWindow
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, AdGroup, Campaign, Keyword
from ads_engine.db.models.analysis import (
    AnalysisRequest,
    AnalysisRun,
    Recommendation,
    RunFinding,
    RunSuppression,
    RunWithheld,
)
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.session import dispose_engine, get_engine, reset_engine, session_scope
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

_BIND_LIMIT = 65_535
"""PostgreSQL's most bind parameters in one statement (the wire protocol's int16 count)."""


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


@pytest.fixture(scope="module", autouse=True)
async def _analysis_process_stopped(anyio_backend: str) -> AsyncIterator[None]:
    """Stop the analysis process the module's isolated runs spawned, once the module ran."""
    del anyio_backend  # Requested so anyio runs this fixture; the backend is always asyncio.
    yield
    await runner.stop_analysis_process()


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
            default_days=28,
            trusted_min_days=14,
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


_LATEST_INDEX = "ix_analysis_run_kind_status_finished"
"""The index ``latest_default_run`` reads: (``kind``, ``status``, ``finished_at``)."""

_RUN_HISTORY = 5_000
"""Older custom and default runs, succeeded and failed, finishing a minute apart."""


async def _seed_run_history() -> tuple[int, int]:
    """Insert a long run history, then the newer rows ``latest_default_run`` must tell apart.

    :data:`_RUN_HISTORY` runs finish a minute apart before :data:`NOW`: every tenth is
    ``custom``, every seventh ``failed``, the rest succeeded ``default`` runs. Newer than all
    of them come a succeeded ``custom`` run, a ``failed`` and a ``running`` default run, a
    succeeded default row without ``finished_at``, two succeeded default runs finishing at
    the same instant, and last, with the highest id, a succeeded default run that finished
    before those two. The table is then analysed, as autovacuum would after such a load.

    Returns:
        The ids of the two runs tied on the newest finish, lower id first.
    """
    async with session_scope() as session:
        await session.execute(
            text(
                "INSERT INTO analysis_run (kind, window_start, window_end, "
                "source_day_boundaries, trigger, data_modes, dataset_ids, config_hash, "
                "default_days, trusted_min_days, status, started_at, finished_at) "
                "SELECT CASE WHEN i % 10 = 0 THEN 'custom' ELSE 'default' END, "
                ":window_start, :window_end, '[]'::jsonb, "
                "CASE WHEN i % 10 = 0 THEN 'manual' ELSE 'scheduled' END, "
                "'{}'::jsonb, '{}'::jsonb, :config_hash, 28, 14, "
                "CASE WHEN i % 7 = 0 THEN 'failed' ELSE 'succeeded' END, "
                ":base + make_interval(mins => i), :base + make_interval(mins => i + 1) "
                "FROM generate_series(1, :total) AS i"
            ),
            {
                "window_start": END - timedelta(days=27),
                "window_end": END,
                "config_hash": "0" * 64,
                "base": NOW - timedelta(minutes=_RUN_HISTORY + 60),
                "total": _RUN_HISTORY,
            },
        )
    newest = NOW + timedelta(hours=1)
    await _insert_run("custom", "succeeded", NOW + timedelta(hours=2))
    await _insert_run("default", "failed", NOW + timedelta(hours=2))
    await _insert_run("default", "running", None)
    await _insert_run("default", "succeeded", None)
    tied = (
        await _insert_run("default", "succeeded", newest),
        await _insert_run("default", "succeeded", newest),
    )
    await _insert_run("default", "succeeded", NOW)
    async with session_scope() as session:
        await session.execute(text("ANALYZE analysis_run"))
    return tied


async def _latest_default_run_sent() -> tuple[int | None, str, dict[str, Any]]:
    """Call ``latest_default_run`` and return the run's id and the one statement it sent.

    A ``before_cursor_execute`` listener records the ``analysis_run`` statement exactly as the
    driver receives it, so the plan test explains what the function issues, not a re-creation
    of it.

    Returns:
        The run's id (``None`` for no run), the statement and its parameters.
    """
    engine = get_engine().sync_engine
    sent: list[tuple[str, dict[str, Any]]] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "FROM analysis_run" in statement:
            assert isinstance(parameters, Mapping), parameters
            sent.append((statement, dict(parameters)))

    event.listen(engine, "before_cursor_execute", record)
    try:
        async with session_scope() as session:
            run = await runner.latest_default_run(session)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(sent) == 1, sent
    statement, parameters = sent[0]
    return (None if run is None else run.id), statement, parameters


def _numbered(statement: str, parameters: Mapping[str, Any]) -> str:
    """Return ``statement`` with each ``%(name)s`` placeholder rewritten as ``$1``, ``$2``, …"""
    numbers: dict[str, int] = {}

    def number(match: re.Match[str]) -> str:
        return f"${numbers.setdefault(match.group(1), len(numbers) + 1)}"

    numbered = re.sub(r"%\((\w+)\)s", number, statement)
    assert set(numbers) == set(parameters), (numbers, parameters)
    return numbered


def _plan_nodes(plan: Any) -> list[dict[str, Any]]:
    """Return every node of a ``FORMAT JSON`` plan tree, parents before their children."""
    pending: list[dict[str, Any]] = [plan[0]["Plan"]]
    nodes: list[dict[str, Any]] = []
    while pending:
        node = pending.pop(0)
        nodes.append(node)
        pending.extend(node.get("Plans", []))
    return nodes


@pytest.mark.anyio
async def test_latest_default_run_takes_the_newest_finish_then_the_highest_id(
    app_db: str,
) -> None:
    """Among succeeded default runs the newest ``finished_at`` wins, ties going to the higher id.

    Neither a newer custom, failed or running run, nor a succeeded row without
    ``finished_at``, nor a later-inserted run that finished earlier displaces the winner.
    """
    tied = await _seed_run_history()

    latest, _, _ = await _latest_default_run_sent()

    assert latest == max(tied)


@pytest.mark.parametrize("plan_kind", ["custom", "generic"])
@pytest.mark.anyio
async def test_latest_default_run_reads_one_backward_probe_of_its_index(
    app_db: str, plan_kind: str
) -> None:
    """The statement ``latest_default_run`` sends is one backward probe of its index.

    The plan is a ``Limit`` over an ``Incremental Sort`` presorted on ``finished_at`` over a
    backward ``Index Scan`` of :data:`_LATEST_INDEX`, every predicate an index condition: no
    sequential or bitmap scan and no full sort of the run history. That holds for the custom
    plan of the bound values and for the generic plan a server-side prepared statement may
    switch to (``EXPLAIN (GENERIC_PLAN)``). Nothing is disabled: on the analysed statistics
    the planner chooses the index on cost. The executed custom plan reads only the two runs
    tied on the newest finish and the next one, which closes their group, out of the
    :data:`_RUN_HISTORY` older runs.
    """
    await _seed_run_history()
    _, statement, parameters = await _latest_default_run_sent()

    async with session_scope() as session:
        connection = await session.connection()
        if plan_kind == "custom":
            result = await connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + statement, parameters
            )
        else:
            result = await connection.exec_driver_sql(
                "EXPLAIN (GENERIC_PLAN, FORMAT JSON) " + _numbered(statement, parameters)
            )
        plan: list[dict[str, Any]] = result.scalar_one()

    nodes = _plan_nodes(plan)
    assert [node["Node Type"] for node in nodes] == ["Limit", "Incremental Sort", "Index Scan"], (
        nodes
    )
    presorted, scan = nodes[1], nodes[2]
    assert presorted["Presorted Key"] == ["finished_at"], presorted
    assert (scan["Relation Name"], scan["Index Name"], scan["Scan Direction"]) == (
        "analysis_run",
        _LATEST_INDEX,
        "Backward",
    ), scan
    assert "finished_at IS NOT NULL" in scan["Index Cond"], scan
    assert "Filter" not in scan, scan
    if plan_kind == "custom":
        assert scan["Actual Loops"] == 1, scan
        assert scan["Actual Rows"] <= 3, scan


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
        restored: str = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()
    assert restored == "read committed"
    assert (run.status, run.kind, run.trigger) == ("succeeded", "default", "event")
    assert run.started_at == NOW
    assert run.finished_at is not None
    assert (run.window_start, run.window_end) == (window.start, window.end)
    assert run.source_day_boundaries == window.source_days_json()
    assert (run.default_days, run.trusted_min_days) == (
        window.default_days,
        window.trusted_min_days,
    )
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
    assert (run.default_days, run.trusted_min_days) == (21, config.trusted_min_days)


@pytest.mark.anyio
async def test_a_run_stores_the_window_settings_it_was_computed_with(app_db: str) -> None:
    """AAP 0.5.3 "Where outputs are frozen": the row keeps the window's ``window.*`` values.

    A 20-day custom run under ``window.trusted_min_days: 21`` stores 21 and the configured
    ``default_days``: the minimum its short window was judged against. A later run of the same
    window under the shipped configuration stores 14 and leaves the earlier row as it was.
    """
    await _seed()
    config = Thresholds.load(resolve_config_dir(), overrides={"window.trusted_min_days": 21})
    shipped = Thresholds.load(resolve_config_dir())
    assert (config.trusted_min_days, shipped.trusted_min_days) == (21, 14)
    window = await runner.custom_window(END - timedelta(days=19), END, now=NOW, thresholds=config)
    assert (window.days, window.short_window) == (20, True)
    judged = await runner.run_analysis(window, "custom", "manual", thresholds=config, now=NOW)
    later = await runner.run_analysis(
        await runner.custom_window(END - timedelta(days=19), END, now=NOW, thresholds=shipped),
        "custom",
        "manual",
        thresholds=shipped,
        now=NOW,
    )
    async with session_scope() as session:
        stored = {
            run.id: (run.default_days, run.trusted_min_days)
            for run in (await session.execute(select(AnalysisRun))).scalars()
        }
    assert stored == {
        judged.id: (config.default_days, 21),
        later.id: (shipped.default_days, 14),
    }
    assert (judged.default_days, judged.trusted_min_days) == (config.default_days, 21)


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
    with pytest.raises(ValueError, match="display_timezone must be a loadable IANA time zone"):
        await runner.run_analysis(short, "custom", "manual", now=NOW, display_timezone="Mars/Base")
    assert await _count(AnalysisRun) == 0


@pytest.mark.anyio
async def test_the_run_context_carries_the_display_timezone(
    app_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zone of the instants in frozen flag messages: the caller's, else the settings'.

    AAP 0.5.5: instants are formatted in ``DISPLAY_TIMEZONE``; ``run_analysis`` reads
    ``get_settings().display_timezone`` when its caller passes no zone.
    """
    await _seed()
    seen: list[str] = []
    original = metrics.load_context

    async def spying(*args: Any, **kwargs: Any) -> AnalysisContext:
        ctx = await original(*args, **kwargs)
        seen.append(ctx.display_timezone)
        return ctx

    monkeypatch.setattr(metrics, "load_context", spying)
    window = await runner.custom_window(END - timedelta(days=20), END, now=NOW)
    await runner.run_analysis(
        window, "custom", "manual", now=NOW, display_timezone="America/New_York"
    )
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Europe/Berlin")
    get_settings.cache_clear()
    await runner.run_analysis(window, "custom", "manual", now=NOW)
    assert seen == ["America/New_York", "Europe/Berlin"]
    assert await _count(AnalysisRun) == 2


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


_DECIMAL_TEXT = re.compile(r"-?\d+(?:\.\d+)?")
"""How stored evidence renders a ``Decimal``: a fixed-point string (``json_value``)."""


def _assert_card_leads_with_a_number(row: Recommendation) -> None:
    """A stored card leads with a number among its own metrics (AAP 0.1.2).

    "Every card leads with one headline number": ``extra.headline_metric`` names exactly one
    of the row's stored metrics, whose value is an integer, a fixed-point decimal string or
    ``null`` for an unavailable number, never text such as a status. White-paper rows are
    table rows with fixed columns and name no headline.
    """
    evidence = row.evidence
    headline = evidence["extra"].get("headline_metric")
    if row.section == WHITE_PAPERS:
        assert headline is None, row.rule_id
        return
    (metric,) = [item for item in evidence["metrics"] if item["key"] == headline]
    value = metric["value"]
    if value is None:
        assert metric.get("unavailable") is True, (row.rule_id, headline)
        return
    number = isinstance(value, int) and not isinstance(value, bool)
    decimal = isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value) is not None
    assert number or decimal, (row.rule_id, headline, value)


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
        assert row.entity_id == f"{entity['source']}:{entity['type']}:{entity['id']}"
        assert entity["name"]
        # Every card leads with a number among its own stored metrics (K1: the wasted
        # spend); white-paper rows are table rows and name none.
        _assert_card_leads_with_a_number(row)
        if row.rule_id == K1:
            assert extra["headline_metric"] == "wasted_spend"
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
    # Findings and withheld lines name their entity by the external id items use, so the
    # stored entity rebuilds the item key and a withheld pattern can be tied to its entity.
    output_keys: set[str] = set()
    for output in (*findings, *withheld):
        if output.entity is not None:
            assert isinstance(output.entity["id"], str), output.entity
            assert output.entity["id"], output.entity
            output_keys.add(
                f"{output.entity['source']}:{output.entity['type']}:{output.entity['id']}"
            )
    assert output_keys & {row.entity_id for row in items}
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


def _most_depended_on_campaign(ctx: AnalysisContext, source: str) -> int:
    """The ``source`` campaign the most of the context's items depend on (ties: lowest id)."""
    out = precedence.apply_precedence([evaluate(ctx) for _, evaluate in runner._RULES], ctx)
    counts: dict[int, int] = {}
    for item in out.all_items():
        for subject in item.candidate.depends_on:
            if subject.type == "campaign" and ctx.campaigns[subject.id].source == source:
                counts[subject.id] = counts.get(subject.id, 0) + 1
    return min(counts, key=lambda campaign_id: (-counts[campaign_id], campaign_id))


def _status_discrepancy(
    ctx: AnalysisContext, campaign_id: int, number: int, age: timedelta
) -> DiscrepancyInfo:
    """An open ``status`` discrepancy on a campaign: decided active, platform paused."""
    campaign = ctx.campaigns[campaign_id]
    budget_id = campaign.budget_id
    shared = budget_id is not None and ctx.budgets[budget_id].explicitly_shared
    return DiscrepancyInfo(
        id=number,
        subject_type="campaign",
        subject_id=campaign_id,
        subject_name=campaign.name,
        source=campaign.source,
        shape="shared_member" if shared else "own_budget",
        kind="status",
        decided_value={"status": "active"},
        platform_value={"status": "paused"},
        detected_at=ctx.now - age,
        state="open",
    )


@pytest.mark.anyio
async def test_stored_c0_cards_lead_with_the_recommendations_they_hold(
    app_db: str, monkeypatch: pytest.MonkeyPatch, default_ctx: AnalysisContext
) -> None:
    """AAP 0.1.2 and P1: a stored C0 card leads with the number of stored items it holds.

    The default world gets an open ``status`` discrepancy on the Google and on the LinkedIn
    campaign the most items depend on. Every stored card still leads with a number among its
    own metrics, and each C0 card's headline is ``held_recommendations``, appended after its
    ``platform`` and ``decided`` metrics, whose value is the number of stored non-C0 items
    whose ``blocking_discrepancies`` names its discrepancy.
    """
    google = _status_discrepancy(
        default_ctx, _most_depended_on_campaign(default_ctx, "google"), 1, timedelta(days=1)
    )
    linkedin = _status_discrepancy(
        default_ctx, _most_depended_on_campaign(default_ctx, "linkedin"), 2, timedelta(days=3)
    )
    ctx = dataclasses.replace(default_ctx, discrepancies=(google, linkedin), cache={})
    _patch_context(monkeypatch, ctx)
    run = await runner.run_analysis(
        ctx.window, "default", "scheduled", thresholds=ctx.thresholds, now=ctx.now
    )
    rows = await _rows(Recommendation, run.id)
    for row in rows:
        _assert_card_leads_with_a_number(row)
    held: dict[int, int] = {google.id: 0, linkedin.id: 0}
    for row in rows:
        if row.rule_id != C0:
            for discrepancy_id in row.evidence["extra"].get("blocking_discrepancies", ()):
                held[discrepancy_id] += 1
    cards = [row for row in rows if row.rule_id == C0]
    assert [card.evidence["extra"]["discrepancy_id"] for card in cards] == [linkedin.id, google.id]
    for card in cards:
        discrepancy_id = card.evidence["extra"]["discrepancy_id"]
        assert card.evidence["extra"]["headline_metric"] == "held_recommendations"
        assert [metric["key"] for metric in card.evidence["metrics"]] == [
            "platform",
            "decided",
            "held_recommendations",
        ]
        assert card.evidence["metrics"][-1] == {
            "key": "held_recommendations",
            "label": "Recommendations held until resolved",
            "value": held[discrepancy_id],
            "unit": "recommendations",
        }
        # Both campaigns were chosen because items depend on them, so each holds some.
        assert held[discrepancy_id] > 0, discrepancy_id
        # The action and reason still state the platform and decided values.
        assert card.action == "Resolve: platform shows Paused, last decided Active"


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


async def _seed_keyword(world: _World) -> tuple[int, int]:
    """Store one ad group and one keyword under the seeded Google campaign.

    Returns:
        ``(ad_group_id, keyword_id)``.
    """
    async with session_scope() as session:
        ad_group = AdGroup(
            source="google",
            dataset_id=world.google,
            campaign_id=world.campaign,
            external_id="777",
            name="Brand terms",
            status="ENABLED",
        )
        session.add(ad_group)
        await session.flush()
        keyword = Keyword(
            source="google",
            dataset_id=world.google,
            ad_group_id=ad_group.id,
            criterion_id="9001",
            text="acme analytics",
            match_type="EXACT",
            status="ENABLED",
            quality_score=7,
        )
        session.add(keyword)
        await session.flush()
        return ad_group.id, keyword.id


def _bind_count(parameters: object) -> int:
    """How many parameters one cursor execution sends (the largest set of an executemany)."""
    if isinstance(parameters, Mapping):
        return len(parameters)
    if isinstance(parameters, list | tuple):
        sets = [item for item in parameters if isinstance(item, Mapping | list | tuple)]
        if sets and len(sets) == len(parameters):
            return max(len(item) for item in sets)
        return len(parameters)
    return 0


@contextmanager
def _statement_binds() -> Iterator[list[tuple[str, int]]]:
    """Record ``(statement head, parameter count)`` of every statement the application sends.

    The head is the statement's first 120 characters with its whitespace collapsed, enough
    to name the verb and the table.
    """
    engine = get_engine().sync_engine
    seen: list[tuple[str, int]] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        seen.append((" ".join(statement[:240].split())[:120], _bind_count(parameters)))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _key_lookups(seen: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """The ``(table, parameter count)`` of each recorded typed-key existence ``SELECT``."""
    targets = {"campaign", "budget", "ad_group", "keyword", "creative"}
    lookups: list[tuple[str, int]] = []
    for head, count in seen:
        words = head.split(" ")
        if words[0] == "SELECT" and "FROM" in words:
            table = words[words.index("FROM") + 1]
            if table in targets and words[1] == f"{table}.id":
                lookups.append((table, count))
    return lookups


@pytest.mark.anyio
async def test_known_ids_beyond_the_bind_parameter_limit_travel_as_one_array(
    app_db: str, log: _LogRecorder
) -> None:
    world = await _seed()
    ad_group, stored = await _seed_keyword(world)
    keyword_ids = range(stored, stored + 70_000)
    assert len(keyword_ids) > _BIND_LIMIT
    keys = [
        *(runner._TypedKeys(keyword_id=keyword_id) for keyword_id in keyword_ids),
        runner._TypedKeys(campaign_id=world.campaign, ad_group_id=ad_group, keyword_id=stored),
        runner._TypedKeys(campaign_id=world.campaign + 1000, budget_id=77),
    ]
    with _statement_binds() as seen:
        async with session_scope() as session:
            known = await runner._known_ids(session, keys)

    assert known == {
        "campaign_id": frozenset({world.campaign}),
        "budget_id": frozenset(),
        "ad_group_id": frozenset({ad_group}),
        "keyword_id": frozenset({stored}),
        "creative_id": frozenset(),
    }
    # Each column with ids is checked by one statement with one (array) parameter.
    assert _key_lookups(seen) == [("campaign", 1), ("budget", 1), ("ad_group", 1), ("keyword", 1)]
    assert keys[-2].row(known) == {
        "campaign_id": world.campaign,
        "budget_id": None,
        "ad_group_id": ad_group,
        "keyword_id": stored,
        "creative_id": None,
    }
    assert keys[0].row(known)["keyword_id"] == stored
    assert all(item.row(known) == dict.fromkeys(_KEY_COLUMNS) for item in keys[1:-2])
    assert keys[-1].row(known) == dict.fromkeys(_KEY_COLUMNS)
    unmatched = {
        fields["column"]: fields["count"]
        for fields in log.named("analysis_run_entity_keys_unmatched")
    }
    assert unmatched == {"campaign_id": 1, "budget_id": 1, "keyword_id": len(keyword_ids) - 1}


@pytest.mark.anyio
async def test_a_run_with_more_typed_ids_than_bind_parameters_persists_in_one_transaction(
    app_db: str, log: _LogRecorder
) -> None:
    world = await _seed()
    ad_group, stored = await _seed_keyword(world)
    run_id = await _insert_run("default", "running", None)
    keyword_ids = range(stored, stored + _BIND_LIMIT + 65)
    total = len(keyword_ids)
    assert total > _BIND_LIMIT
    withheld = tuple(
        (
            runner._withheld_row(
                run_id,
                KEYWORDS,
                Withheld(
                    rule_id=K3,
                    section=KEYWORDS,
                    entity=EntityRef(
                        "google",
                        ENTITY_KEYWORD,
                        keyword_id,
                        keyword_external_id("777", keyword_id),
                        f"keyword {keyword_id}",
                    ),
                    reason="quality score on 3 of 5 required days in the recent 14 days",
                    observed=3,
                    required=5,
                    unit="days",
                ),
            ),
            runner._TypedKeys(
                campaign_id=world.campaign, ad_group_id=ad_group, keyword_id=keyword_id
            ),
        )
        for keyword_id in keyword_ids
    )
    prepared = runner._PreparedRun(
        recommendations=(),
        findings=(),
        withheld=withheld,
        suppression=(),
        data_modes={"google": "live", "linkedin": "simulated", "hubspot": "live"},
        dataset_ids={
            "google": world.google,
            "linkedin": world.linkedin,
            "hubspot": world.hubspot,
            "hubspot_linkedin_effective": world.companion,
        },
        summary={"withheld_total": total},
    )

    with _statement_binds() as seen:
        run = await runner._persist(run_id, prepared)

    assert (run.id, run.status) == (run_id, "succeeded")
    assert run.finished_at is not None
    assert _key_lookups(seen) == [("campaign", 1), ("ad_group", 1), ("keyword", 1)]
    assert max(count for _, count in seen) <= _BIND_LIMIT
    (unmatched,) = log.named("analysis_run_entity_keys_unmatched")
    assert unmatched == {"column": "keyword_id", "count": total - 1}
    async with session_scope() as session:
        stored_rows = (
            await session.execute(
                select(
                    func.count(),
                    func.count(RunWithheld.keyword_id),
                    func.min(RunWithheld.keyword_id),
                    func.count(RunWithheld.campaign_id),
                    func.count(RunWithheld.ad_group_id),
                    func.count(func.distinct(RunWithheld.entity["id"])),
                ).where(RunWithheld.run_id == run_id)
            )
        ).one()
        # Rows written by one transaction carry its id as xmin, as does the run row's
        # succeeded version: every output committed together with the state change.
        writers = await session.execute(
            text(
                "SELECT xmin::text FROM run_withheld WHERE run_id = :run_id "
                "UNION SELECT xmin::text FROM analysis_run WHERE id = :run_id"
            ),
            {"run_id": run_id},
        )
        transactions: Sequence[str] = writers.scalars().all()
    # Every row is stored; only the stored keyword keeps its key, every absent one is NULL,
    # while the stored campaign and ad group stay set and the evidence keeps each keyword.
    assert tuple(stored_rows) == (total, 1, stored, total, total, total)
    assert len(transactions) == 1


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


def _marks_the_run_failed(statement: object) -> bool:
    """Whether ``statement`` is the failure record's ``UPDATE analysis_run`` to ``failed``.

    The runner issues that update on its session; the request claim (``analysis_request``)
    and the lock's crash recovery (on the lock connection) never match.
    """
    return (
        isinstance(statement, Update)
        and statement.entity_description["type"] is AnalysisRun
        and statement.compile().params.get("status") == "failed"
    )


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
        # is cancelled there, before it claims its requests, as a caller giving up at that
        # instant would cancel it.
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
    # The run stopped before it claimed any request, so both stay pending and the next run
    # serves them: no refresh is lost.
    assert await _served_by() == [None, None]
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
    original_execute = AsyncSession.execute

    async def executing(
        self: AsyncSession, statement: Executable, *args: Any, **kwargs: Any
    ) -> Result[Any]:
        result = await original_execute(self, statement, *args, **kwargs)
        if not _marks_the_run_failed(statement):
            return result
        # The failure record's update of the run row has run in this transaction: cancel
        # the run again and again, as a level-triggered cancel scope would, before the
        # transaction commits.
        frozen = result.freeze()
        run_id: int = frozen().scalar_one()  # the row the update marked failed
        task = target["run"]
        for _ in range(3):
            task.cancel()
            cancels.append(run_id)
            await asyncio.sleep(0)
        return frozen()

    monkeypatch.setattr(AsyncSession, "execute", executing)
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
    # Claimed when the run started, the requests stay stamped with the failed run.
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

    original_execute = AsyncSession.execute

    async def unavailable(
        self: AsyncSession, statement: Executable, *args: Any, **kwargs: Any
    ) -> Result[Any]:
        # Only the failure record's update of the run row fails: it hangs past the record's
        # timeout, or the server closes the connection under it.
        if not _marks_the_run_failed(statement):
            return await original_execute(self, statement, *args, **kwargs)
        if failure == "timeout":
            await asyncio.sleep(_WAIT_S)
        raise OperationalError("UPDATE analysis_run", {}, ConnectionError("server closed"))

    monkeypatch.setattr(
        runner,
        "_RULES",
        _wrapped_rules(lambda name, evaluate: exploding if name == "keywords" else evaluate),
    )
    monkeypatch.setattr(AsyncSession, "execute", unavailable)
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
    # The failure record rolled back whole: the row stays running until the next process to
    # take the lock marks it failed (crash recovery). The request was claimed when the run
    # started, so it stays stamped with the run.
    (run,) = await _runs()
    assert run.status == "running"
    assert await _served_by() == [run.id]
    async with analysis_lock():
        pass
    assert [row.status for row in await _runs()] == ["failed"]


# ---------------------------------------------------------------------------
# The analysis process: isolated runs (POST /runs) outside the caller's GIL
# ---------------------------------------------------------------------------


def _refusing_rules() -> tuple[tuple[str, _Evaluate], ...]:
    """``runner._RULES`` with every evaluator refusing to run in this, the calling, process."""

    def wrap(name: str, evaluate: _Evaluate) -> _Evaluate:
        def refused(ctx: AnalysisContext) -> Any:
            raise AssertionError(f"rule module {name!r} ran in the calling process")

        return refused

    return _wrapped_rules(wrap)


def _stored_shape(rows: list[Any]) -> list[dict[str, Any]]:
    """Every column of stored run-output rows except their own id and their run's."""
    return [
        {
            column.key: getattr(row, column.key)
            for column in row.__table__.columns
            if column.key not in {"id", "run_id"}
        }
        for row in rows
    ]


class _HeldTable:
    """``fact_search_term_daily`` locked ``ACCESS EXCLUSIVE`` by a connection of its own.

    Only the context load reads that table, so an isolated run blocks on it inside the
    analysis process, after the calling process took the analysis lock and committed the
    ``running`` row, until :meth:`release`.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._released = False

    def waiters(self) -> int:
        """How many sessions of this database wait for the table's lock."""
        count = self._connection.execute(
            text(
                "SELECT count(*) FROM pg_locks"
                " WHERE database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                " AND relation = 'fact_search_term_daily'::regclass AND NOT granted"
            )
        ).scalar_one()
        return int(count)

    def wait_for_waiter(self) -> bool:
        """Wait, at most :data:`_WAIT_S`, until a session waits for the table's lock."""
        deadline = time.monotonic() + _WAIT_S
        while time.monotonic() < deadline:
            if self.waiters():
                return True
            time.sleep(0.05)
        return False

    def release(self) -> None:
        """End the transaction holding the lock; later calls do nothing."""
        if not self._released:
            self._released = True
            self._connection.rollback()


@contextmanager
def _search_terms_locked(app_db: str) -> Iterator[_HeldTable]:
    """Hold ``fact_search_term_daily`` locked for the block, released at its end at the latest."""
    engine = create_engine(make_url(app_db), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.execute(text("LOCK TABLE fact_search_term_daily IN ACCESS EXCLUSIVE MODE"))
            held = _HeldTable(connection)
            try:
                yield held
            finally:
                held.release()
    finally:
        engine.dispose()


def _fail_runs_elsewhere(app_db: str) -> None:
    """Mark every run ``failed`` from another session, as a process that took the lock would."""
    engine = create_engine(make_url(app_db), poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE analysis_run SET status = 'failed', finished_at = now()")
            )
    finally:
        engine.dispose()


def _exited(pid: int) -> bool:
    """Wait, at most :data:`_WAIT_S`, until process ``pid`` has exited and been reaped."""
    deadline = time.monotonic() + _WAIT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


_BACKEND_DIR = Path(__file__).resolve().parents[2]
"""``backend/``, the working directory of the test's own API process, whose config it reads."""

_PID_MARKER = "analysis-process-pid="
"""The prefix of the line on which :data:`_API_PARENT_PROGRAM` names its analysis process."""

_API_PARENT_PROGRAM = """
import asyncio
import sys
from datetime import date, datetime

from ads_engine.analysis import runner


async def main() -> None:
    now = datetime.fromisoformat(sys.argv[1])
    pid = await runner.start_analysis_process()
    print(f"analysis-process-pid={pid}", flush=True)
    window = await runner.custom_window(
        date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3]), now=now
    )
    await runner.run_analysis(window, "custom", "manual", now=now, isolated=True)


asyncio.run(main())
"""
"""An API process in miniature: it spawns its analysis process, names its pid on stdout, then
runs a custom run there (argv: the run instant, the window's first and last day)."""

_ORPHAN_EXIT_S = 2.0
"""The longest an analysis process may outlive its killed API process."""


async def _drain(stream: asyncio.StreamReader, lines: list[str], named: asyncio.Event) -> None:
    """Collect ``stream``'s lines until it closes; set ``named`` once the pid line arrived.

    Draining keeps the pipe from filling while the API process and its analysis process log.
    """
    async for raw in stream:
        line = raw.decode(errors="replace").rstrip("\n")
        lines.append(line)
        if line.startswith(_PID_MARKER):
            named.set()


def _named_pid(lines: list[str]) -> int:
    """The pid on the first :data:`_PID_MARKER` line of ``lines``."""
    marked = next(line for line in lines if line.startswith(_PID_MARKER))
    return int(marked.removeprefix(_PID_MARKER))


def _stat(pid: int) -> tuple[str, int, str] | None:
    """Process ``pid``'s state, parent pid and start time from ``/proc``; ``None`` once reaped.

    An orphan is re-parented to whichever process reaps orphans here, so a dead one may stay
    a zombie (state ``Z``) for a while: :func:`_alive` reads the state rather than probing
    with a signal, and the start time tells a reused pid from the original process.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError, ProcessLookupError:
        return None
    # The command name, in parentheses, may itself hold spaces and parentheses.
    fields = stat[stat.rindex(")") + 2 :].split()
    return fields[0], int(fields[1]), fields[19]


def _alive(pid: int, started: str) -> bool:
    """Whether process ``pid``, started at ``started`` (``/proc`` ticks), is running."""
    stat = _stat(pid)
    return stat is not None and stat[2] == started and stat[0] != "Z"


def _dead_within(pid: int, started: str, seconds: float) -> bool:
    """Wait, at most ``seconds``, until process ``pid`` (started at ``started``) has died."""
    deadline = time.monotonic() + seconds
    while _alive(pid, started):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _no_waiters(held: _HeldTable) -> bool:
    """Wait, at most :data:`_WAIT_S`, until no session waits for ``held``'s table lock."""
    deadline = time.monotonic() + _WAIT_S
    while held.waiters():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


async def _settled(task: asyncio.Task[Any]) -> None:
    """Cancel ``task`` if it is still running and wait, at most :data:`_WAIT_S`, for it."""
    if not task.done():
        task.cancel()
        await asyncio.wait((task,), timeout=_WAIT_S)


def _signals() -> Any:
    """Fresh shared values, as the analysis process receives them, for in-process tests."""
    context = multiprocessing.get_context("spawn")
    return runner._ProcessSignals(
        stop=context.Value("q", 0, lock=False), stage=context.Value("i", 0, lock=False)
    )


async def _job(run_id: int, *, token: int = 1) -> Any:
    """A job for the ``running`` row ``run_id``: the 28 days ending :data:`END`."""
    config = Thresholds.load(resolve_config_dir())
    window = await runner.custom_window(END - timedelta(days=27), END, now=NOW, thresholds=config)
    return runner._AnalysisJob(
        token=token,
        run_id=run_id,
        window=window,
        kind="custom",
        config=config,
        instant=NOW,
        display_timezone="UTC",
        log_context={"run_id": run_id, "caller": "in-process test"},
    )


class _UnrebuildableError(Exception):
    """An error whose constructor needs two arguments: it pickles but cannot be rebuilt."""

    def __init__(self, code: int, detail: str) -> None:
        super().__init__(f"{code}: {detail}")


class _UnpicklableError(Exception):
    """An error carrying a lambda, which cannot be pickled at all."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.callback = lambda: detail


@pytest.mark.anyio
async def test_an_isolated_run_computes_elsewhere_and_stores_what_the_thread_stores(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    await _seed()
    pending = await _requests(1)
    window = await runner.custom_window(END - timedelta(days=27), END, now=NOW)
    threaded = await runner.run_analysis(window, "custom", "manual", now=NOW)
    # Rules refused in this process: the isolated run computes in the analysis process.
    monkeypatch.setattr(runner, "_RULES", _refusing_rules())
    isolated = await runner.run_analysis(
        window, "custom", "manual", reason="api", request_ids=pending, now=NOW, isolated=True
    )

    assert (isolated.status, isolated.kind, isolated.trigger) == ("succeeded", "custom", "manual")
    assert isolated.id != threaded.id
    assert isolated.finished_at is not None
    assert (
        isolated.window_start,
        isolated.window_end,
        isolated.config_hash,
        isolated.data_modes,
        isolated.dataset_ids,
        isolated.source_day_boundaries,
    ) == (
        threaded.window_start,
        threaded.window_end,
        threaded.config_hash,
        threaded.data_modes,
        threaded.dataset_ids,
        threaded.source_day_boundaries,
    )
    stored_rows = 0
    for model in (Recommendation, RunFinding, RunWithheld, RunSuppression):
        rows = await _rows(model, isolated.id)
        stored_rows += len(rows)
        assert _stored_shape(rows) == _stored_shape(await _rows(model, threaded.id)), model
    assert stored_rows > 0
    assert [row.status for row in await _runs()] == ["succeeded", "succeeded"]
    assert await _served_by() == [isolated.id]
    # The calling process logs both lines, with the same fields as on the analysis thread.
    assert [entry["run_id"] for entry in log.named("analysis_run_started")] == [
        threaded.id,
        isolated.id,
    ]
    thread_summary, process_summary = log.named("analysis_run_summary")
    assert set(process_summary) == set(thread_summary)
    assert (process_summary["run_id"], process_summary["requests"]) == (isolated.id, 1)
    assert process_summary["reason"] == "api"
    for key in (
        "items",
        "items_total",
        "held",
        "withheld",
        "withheld_total",
        "findings_total",
        "suppressed",
        "allocations",
        "data_modes",
        "config_hash",
        "days",
    ):
        assert process_summary[key] == thread_summary[key], key
    timings = [process_summary[key] for key in ("load_ms", "compute_ms", "persist_ms")]
    assert all(isinstance(value, int) and value >= 0 for value in timings)
    assert process_summary["duration_ms"] >= process_summary["compute_ms"]
    assert log.named("analysis_run_failed") == []


@pytest.mark.anyio
async def test_while_an_isolated_run_loads_contenders_are_refused_and_a_cancel_stops_it(
    app_db: str, log: _LogRecorder
) -> None:
    await _seed()
    pending = await _requests(1)
    window = await runner.custom_window(END - timedelta(days=27), END, now=NOW)
    ticks: list[float] = []
    done = asyncio.Event()

    async def ticking() -> None:
        while not done.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.01)

    with _search_terms_locked(app_db) as held:
        task = asyncio.create_task(
            runner.run_analysis(
                window, "custom", "manual", request_ids=pending, now=NOW, isolated=True
            )
        )
        ticker = asyncio.create_task(ticking())
        try:
            assert await asyncio.to_thread(held.wait_for_waiter), "the load never reached it"
            # The run is blocked in the analysis process, holding the lock: contenders are
            # refused at once, on either path, and this process's loop keeps ticking.
            for isolated in (True, False):
                started = time.perf_counter()
                with pytest.raises(RunInProgress):
                    await runner.run_analysis(
                        window, "custom", "manual", now=NOW, isolated=isolated
                    )
                assert time.perf_counter() - started < 1.0
            await asyncio.sleep(0.3)
            assert not task.done()
            assert [row.status for row in await _runs()] == ["running"]
            task.cancel()
            await asyncio.wait((task,), timeout=_WAIT_S)
            assert task.done()
            assert task.cancelled()
        finally:
            done.set()
            await ticker
            await _settled(task)

    assert len(ticks) >= 20
    assert max(later - earlier for earlier, later in itertools.pairwise(ticks)) < 0.2
    (run,) = await _runs()
    assert run.status == "failed"
    assert run.finished_at is not None
    assert await _served_by() == [run.id]
    assert await _count(Recommendation) == 0
    (failed,) = log.named("analysis_run_failed")
    assert (failed["run_id"], failed["stage"], failed["error"]) == (
        run.id,
        "load_context",
        "CancelledError",
    )
    assert log.named("analysis_run_summary") == []
    async with analysis_lock():
        pass  # the lock was released


@pytest.mark.anyio
async def test_a_run_failed_elsewhere_crosses_back_as_run_no_longer_running(
    app_db: str, log: _LogRecorder
) -> None:
    await _seed()
    window = await runner.custom_window(END - timedelta(days=27), END, now=NOW)
    with _search_terms_locked(app_db) as held:
        task = asyncio.create_task(
            runner.run_analysis(window, "custom", "manual", now=NOW, isolated=True)
        )
        try:
            assert await asyncio.to_thread(held.wait_for_waiter), "the load never reached it"
            (running,) = await _runs()
            # Another process took the lock and failed the row; then the load goes on.
            await asyncio.to_thread(_fail_runs_elsewhere, app_db)
            held.release()
            with pytest.raises(runner.RunNoLongerRunning) as raised:
                await asyncio.wait_for(task, _WAIT_S)
        finally:
            await _settled(task)

    assert type(raised.value) is runner.RunNoLongerRunning
    assert (raised.value.run_id, raised.value.status) == (running.id, "failed")
    cause = raised.value.__cause__
    assert isinstance(cause, runner._RemoteTracebackError)
    assert "RunNoLongerRunning" in cause.text
    assert "_persist" in cause.text
    assert logged_as(raised.value) == "analysis_run_failed"
    (run,) = await _runs()
    assert run.status == "failed"
    assert await _count(Recommendation) == 0
    (failed,) = log.named("analysis_run_failed")
    assert (failed["run_id"], failed["stage"], failed["error"]) == (
        run.id,
        "persist",
        "RunNoLongerRunning",
    )
    async with analysis_lock():
        pass  # the lock was released


@pytest.mark.anyio
async def test_a_killed_analysis_process_fails_its_run_and_the_next_run_respawns_it(
    app_db: str, log: _LogRecorder
) -> None:
    await _seed()
    window = await runner.custom_window(END - timedelta(days=27), END, now=NOW)
    pid = await runner.start_analysis_process()
    with _search_terms_locked(app_db) as held:
        task = asyncio.create_task(
            runner.run_analysis(window, "custom", "manual", now=NOW, isolated=True)
        )
        try:
            assert await asyncio.to_thread(held.wait_for_waiter), "the load never reached it"
            os.kill(pid, signal.SIGKILL)
            with pytest.raises(BrokenProcessPool):
                await asyncio.wait_for(task, _WAIT_S)
        finally:
            await _settled(task)

    (run,) = await _runs()
    assert run.status == "failed"
    assert run.finished_at is not None
    (failed,) = log.named("analysis_run_failed")
    assert (failed["run_id"], failed["stage"], failed["error"]) == (
        run.id,
        "load_context",
        "BrokenProcessPool",
    )
    async with analysis_lock():
        pass  # the lock was released
    assert await asyncio.to_thread(_exited, pid)
    retried = await runner.run_analysis(window, "custom", "manual", now=NOW, isolated=True)
    assert retried.status == "succeeded"
    assert await runner.start_analysis_process() != pid


@pytest.mark.anyio
async def test_an_analysis_process_dies_with_its_killed_api_process(
    app_db: str, tmp_path: Path
) -> None:
    await _seed()
    first, last = END - timedelta(days=27), END
    # The API process runs under this test's environment: app_db's DATABASE_URL, and none of
    # the host's settings. Whatever temporary files its multiprocessing leaves stay here.
    environment = dict(os.environ)
    environment["TMPDIR"] = str(tmp_path)
    lines: list[str] = []
    named = asyncio.Event()
    child: tuple[int, str] | None = None
    with _search_terms_locked(app_db) as held:
        api = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _API_PARENT_PROGRAM,
            NOW.isoformat(),
            first.isoformat(),
            last.isoformat(),
            cwd=_BACKEND_DIR,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            limit=2**20,  # a log line of any realistic length still reads as one line
        )
        assert api.stdout is not None
        reader = asyncio.create_task(_drain(api.stdout, lines, named))
        try:
            try:
                await asyncio.wait_for(named.wait(), _WAIT_S)
            except TimeoutError:
                pytest.fail(f"the API process never named its analysis process: {lines}")
            pid = _named_pid(lines)
            stat = _stat(pid)
            assert stat is not None, lines
            assert stat[1] == api.pid  # the analysis process is the API process's own child
            child = (pid, stat[2])
            assert await asyncio.to_thread(held.wait_for_waiter), "the load never reached it"
            with pytest.raises(RunInProgress):
                async with analysis_lock():
                    pass  # the API process holds the run's lock while its child loads
            api.kill()
            await asyncio.wait_for(api.wait(), _WAIT_S)
            # The analysis process dies with the API process, and PostgreSQL then ends the
            # session it left waiting for the table: no part of the run outlives the lock.
            assert await asyncio.to_thread(_dead_within, *child, _ORPHAN_EXIT_S), lines
            assert await asyncio.to_thread(_no_waiters, held), "the orphan's load still waits"
            async with analysis_lock():
                pass  # the lock died with the API process; taking it fails the orphan's run
        finally:
            if api.returncode is None:
                with suppress(ProcessLookupError):
                    api.kill()
                await asyncio.wait_for(api.wait(), _WAIT_S)
            if child is not None and _alive(*child):
                os.kill(child[0], signal.SIGKILL)  # only the process the API process named
            await asyncio.wait((reader,), timeout=_WAIT_S)
            await _settled(reader)

    (orphaned,) = await _runs()
    assert orphaned.status == "failed"
    assert orphaned.finished_at is not None
    window = await runner.custom_window(first, last, now=NOW)
    successor = await runner.run_analysis(window, "custom", "manual", now=NOW)
    assert successor.status == "succeeded"
    assert [(run.id, run.status) for run in await _runs()] == [
        (orphaned.id, "failed"),
        (successor.id, "succeeded"),
    ]


@pytest.mark.anyio
async def test_a_changed_environment_spawns_a_fresh_analysis_process(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    await _seed()
    first = await runner.start_analysis_process()
    # An earlier test's process, spawned under that test's environment, may have been
    # replaced just now; an unchanged environment replaces nothing.
    replaced = len(log.named("analysis_process_replaced"))
    assert await runner.start_analysis_process() == first
    assert len(log.named("analysis_process_replaced")) == replaced
    monkeypatch.setenv("ADS_ENGINE_ANALYSIS_PROCESS_TEST", uuid.uuid4().hex)
    second = await runner.start_analysis_process()
    assert second != first
    assert log.named("analysis_process_replaced")[replaced:] == [{"reason": "environment_changed"}]
    assert await asyncio.to_thread(_exited, first)
    window = await runner.custom_window(END - timedelta(days=27), END, now=NOW)
    run = await runner.run_analysis(window, "custom", "manual", now=NOW, isolated=True)
    assert run.status == "succeeded"
    assert await runner.start_analysis_process() == second


@pytest.mark.anyio
async def test_the_task_entry_loads_computes_and_stores_the_run(
    app_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed()
    run_id = await _insert_run("custom", "running", None)
    signals = _signals()
    monkeypatch.setattr(runner, "_process_signals", signals)
    job = await _job(run_id)
    # The entry runs on an event loop of its own and disposes the engine it opened there.
    await dispose_engine()
    outcome = await asyncio.to_thread(runner._execute_in_process, job)

    assert isinstance(outcome, runner._ProcessSuccess), outcome
    assert (outcome.stored.id, outcome.stored.status) == (run_id, "succeeded")
    assert signals.stage.value == runner._STAGES.index("persist")
    timings = (outcome.load_ms, outcome.compute_ms, outcome.persist_ms)
    assert all(isinstance(value, int) and value >= 0 for value in timings)
    assert outcome.summary["items_total"] == len(await _rows(Recommendation, run_id))
    # The outcome crosses back as the executor hands it back, the detached run included.
    crossed = ForkingPickler.loads(ForkingPickler.dumps(outcome))
    assert isinstance(crossed, runner._ProcessSuccess)
    assert (crossed.stored.id, crossed.stored.status, crossed.stored.data_modes) == (
        run_id,
        "succeeded",
        outcome.stored.data_modes,
    )
    assert crossed.summary == outcome.summary
    (stored,) = await _runs()
    assert stored.status == "succeeded"


@pytest.mark.anyio
async def test_the_task_entry_of_a_stopped_job_stops_at_its_first_checkpoint(
    app_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed()
    run_id = await _insert_run("custom", "running", None)
    signals = _signals()
    monkeypatch.setattr(runner, "_process_signals", signals)
    job = await _job(run_id, token=7)
    signals.stop.value = 7
    await dispose_engine()
    outcome = await asyncio.to_thread(runner._execute_in_process, job)

    assert isinstance(outcome, runner._ProcessFailure), outcome
    assert (outcome.stage, outcome.stopped) == ("load_context", True)
    assert type(outcome.error) is runner._ComputationStoppedError
    assert "_ComputationStoppedError" in outcome.remote_traceback
    (run,) = await _runs()
    assert run.status == "running"
    assert await _count(Recommendation) == 0


@pytest.mark.parametrize(
    "error",
    [_UnrebuildableError(7, "seven"), _UnpicklableError("carries a lambda")],
    ids=["cannot_be_rebuilt", "cannot_be_pickled"],
)
@pytest.mark.anyio
async def test_an_error_that_cannot_cross_back_becomes_analysis_process_error(
    app_db: str, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    await _seed()
    run_id = await _insert_run("custom", "running", None)
    signals = _signals()
    monkeypatch.setattr(runner, "_process_signals", signals)
    job = await _job(run_id)

    async def failing(*_args: Any, **_kwargs: Any) -> AnalysisContext:
        raise error

    monkeypatch.setattr(runner, "_load", failing)
    await dispose_engine()
    outcome = await asyncio.to_thread(runner._execute_in_process, job)

    assert isinstance(outcome, runner._ProcessFailure), outcome
    assert (outcome.stage, outcome.stopped) == ("load_context", False)
    assert type(outcome.error) is runner.AnalysisProcessError
    assert outcome.error.error_type == f"{type(error).__module__}.{type(error).__qualname__}"
    assert str(error) in outcome.error.message
    assert type(error).__name__ in outcome.remote_traceback
    crossed = ForkingPickler.loads(ForkingPickler.dumps(outcome))
    assert type(crossed.error) is runner.AnalysisProcessError
    assert (crossed.error.message, crossed.error.error_type) == (
        outcome.error.message,
        outcome.error.error_type,
    )


def test_errors_that_cross_back_keep_their_fields() -> None:
    lost = runner.RunNoLongerRunning(5, "failed")
    lost.add_note("raised while persisting")
    assert runner._portable_error(lost) is lost
    crossed = ForkingPickler.loads(ForkingPickler.dumps(lost))
    assert type(crossed) is runner.RunNoLongerRunning
    assert (crossed.run_id, crossed.status, str(crossed)) == (5, "failed", str(lost))
    assert crossed.__notes__ == ["raised while persisting"]
    deleted = ForkingPickler.loads(ForkingPickler.dumps(runner.RunNoLongerRunning(6, None)))
    assert (deleted.run_id, deleted.status) == (6, None)
    stand_in = runner.AnalysisProcessError("could not hand it back", "pkg.SomeError")
    restored = ForkingPickler.loads(ForkingPickler.dumps(stand_in))
    assert type(restored) is runner.AnalysisProcessError
    assert (restored.message, restored.error_type, str(restored)) == (
        "could not hand it back",
        "pkg.SomeError",
        "could not hand it back",
    )
    for portable in (
        ConfigError("content_assets.yaml", "assets[0]", "contradicts"),
        RunInProgress(),
        asyncio.CancelledError(),
        ValueError("plain"),
    ):
        assert runner._portable_error(portable) is portable


def test_the_task_entry_refuses_a_process_it_was_not_prepared_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_process_signals", None)
    with pytest.raises(RuntimeError, match="_init_analysis_process"):
        runner._execute_in_process(SimpleNamespace())


@pytest.mark.parametrize("refused", [False, True], ids=["niced", "nice_refused"])
def test_the_initializer_prepares_the_process_it_runs_in(
    monkeypatch: pytest.MonkeyPatch, log: _LogRecorder, refused: bool
) -> None:
    calls: list[tuple[Any, ...]] = []

    def nice(increment: int) -> int:
        calls.append(("nice", increment))
        if refused:
            raise PermissionError("lowering the priority is not permitted")
        return increment

    # Only the runner's own references are replaced: this process keeps its signal handlers,
    # logging and priority, and is bound to no parent's death and runs no watchdog.
    monkeypatch.setattr(runner, "os", SimpleNamespace(nice=nice))
    monkeypatch.setattr(
        runner,
        "signal",
        SimpleNamespace(
            SIGINT=signal.SIGINT,
            SIG_IGN=signal.SIG_IGN,
            signal=lambda signum, handler: calls.append(("signal", signum, handler)),
        ),
    )
    monkeypatch.setattr(runner, "configure_logging", lambda: calls.append(("configure_logging",)))
    monkeypatch.setattr(runner, "_bind_to_parent", lambda pid: calls.append(("bind", pid)))
    monkeypatch.setattr(runner, "_process_signals", None)
    monkeypatch.setattr(runner, "_parent_pid", None)
    signals = _signals()
    runner._init_analysis_process(signals.stop, signals.stage, 4242)

    assert runner._process_signals == signals
    assert runner._parent_pid == 4242
    assert calls == [
        ("signal", signal.SIGINT, signal.SIG_IGN),
        ("configure_logging",),
        ("bind", 4242),
        ("nice", 10),
    ]
    expected = [{"niceness": 10, "error": "PermissionError"}] if refused else []
    assert log.named("analysis_process_nice_failed") == expected


class _Libc:
    """A stand-in C library whose ``prctl`` records its arguments and returns ``result``."""

    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[int, int]] = []

    def prctl(self, option: ctypes.c_int, value: ctypes.c_ulong) -> int:
        self.calls.append((option.value, value.value))
        return self.result


@pytest.mark.parametrize(
    ("result", "loadable", "accepted", "logged"),
    [
        (0, True, True, []),
        (-1, True, False, [{"errno": errno.EPERM}]),
        (0, False, False, [{"error": "OSError"}]),
    ],
    ids=["accepted", "refused", "no_libc"],
)
def test_the_parent_death_signal_asks_the_kernel_and_logs_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
    log: _LogRecorder,
    result: int,
    loadable: bool,
    accepted: bool,
    logged: list[dict[str, int | str]],
) -> None:
    libc = _Libc(result)
    loads: list[tuple[str | None, bool]] = []

    def load(name: str | None, *, use_errno: bool) -> _Libc:
        loads.append((name, use_errno))
        if not loadable:
            raise OSError("no C library")
        return libc

    # The runner's own ctypes reference only: this process's death signal stays unset.
    monkeypatch.setattr(runner, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        runner,
        "ctypes",
        SimpleNamespace(
            CDLL=load, c_int=ctypes.c_int, c_ulong=ctypes.c_ulong, get_errno=lambda: errno.EPERM
        ),
    )
    assert runner._set_parent_death_signal() is accepted
    assert loads == [(None, True)]
    assert libc.calls == ([(1, signal.SIGKILL.value)] if loadable else [])
    assert log.named("analysis_process_death_signal_failed") == logged


def test_the_parent_death_signal_is_linux_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def load(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the C library was loaded off Linux")

    monkeypatch.setattr(runner, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(runner, "ctypes", SimpleNamespace(CDLL=load))
    assert runner._set_parent_death_signal() is False


_DEATH_SIGNAL_PROGRAM = """
import ctypes

from ads_engine.analysis import runner

accepted = runner._set_parent_death_signal()
death_signal = ctypes.c_int(0)
ctypes.CDLL(None, use_errno=True).prctl(ctypes.c_int(2), ctypes.byref(death_signal))
print(accepted, death_signal.value)
"""
"""Sets the parent-death signal in a throwaway interpreter and prints whether the kernel
accepted it and what ``prctl(PR_GET_PDEATHSIG)`` reads back."""


@pytest.mark.skipif(sys.platform != "linux", reason="the parent-death signal is Linux's")
def test_the_kernel_holds_the_parent_death_signal_the_runner_sets() -> None:
    # A throwaway interpreter, so the test process itself never dies with its parent.
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and program, no user input
        [sys.executable, "-c", _DEATH_SIGNAL_PROGRAM],
        cwd=_BACKEND_DIR,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["True", str(signal.SIGKILL.value)]


def test_the_binding_exits_at_once_when_the_parent_is_already_gone(
    monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    calls: list[Any] = []

    def death_signal() -> bool:
        calls.append("death_signal")
        return True

    def thread(**kwargs: Any) -> None:
        calls.append(("thread", kwargs))

    monkeypatch.setattr(runner, "_set_parent_death_signal", death_signal)
    monkeypatch.setattr(
        runner,
        "os",
        SimpleNamespace(getppid=lambda: 1, _exit=lambda code: calls.append(("exit", code))),
    )
    monkeypatch.setattr(runner, "threading", SimpleNamespace(Thread=thread))
    runner._bind_to_parent(4242)

    # Re-parented before the signal was set: the process exits at once, and no watchdog starts.
    assert calls == ["death_signal", ("exit", 70)]
    assert log.named("analysis_process_orphaned") == [{"parent_pid": 4242}]


def test_the_binding_starts_a_daemon_watchdog_while_the_parent_lives(
    monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    started: list[dict[str, Any]] = []

    class _Thread:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def start(self) -> None:
            started.append(self.kwargs)

    def exited(code: int) -> None:
        raise AssertionError(f"exited {code} while the parent lives")

    monkeypatch.setattr(runner, "_set_parent_death_signal", lambda: True)
    monkeypatch.setattr(runner, "os", SimpleNamespace(getppid=lambda: 4242, _exit=exited))
    monkeypatch.setattr(runner, "threading", SimpleNamespace(Thread=_Thread))
    runner._bind_to_parent(4242)

    assert started == [
        {
            "target": runner._watch_parent,
            "args": (4242,),
            "name": "ads-engine-analysis-parent-watch",
            "daemon": True,
        }
    ]
    assert log.named("analysis_process_orphaned") == []


def test_the_watchdog_ends_the_process_once_its_parent_is_gone(
    monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    parents = iter([4242, 4242])
    seen: list[int] = []
    exits: list[int] = []

    def getppid() -> int:
        # Re-parented to init after two polls.
        seen.append(next(parents, 1))
        return seen[-1]

    monkeypatch.setattr(runner, "os", SimpleNamespace(getppid=getppid, _exit=exits.append))
    watchdog = threading.Thread(target=runner._watch_parent, args=(4242,), daemon=True)
    started = time.monotonic()
    watchdog.start()
    watchdog.join(_WAIT_S)

    assert not watchdog.is_alive()
    assert seen == [4242, 4242, 1]
    assert time.monotonic() - started >= 2 * runner._PARENT_POLL_S
    assert exits == [70]
    assert log.named("analysis_process_orphaned") == [{"parent_pid": 4242}]


@pytest.mark.anyio
async def test_the_analysis_process_starts_niced_ignoring_sigint_and_stops(
    log: _LogRecorder,
) -> None:
    await runner.stop_analysis_process()
    stopped = len(log.named("analysis_process_stopped"))
    pid = await runner.start_analysis_process()
    try:
        assert pid > 0
        assert pid != os.getpid()
        assert runner._analysis_process_pid() == os.getpid()  # the warm-up names its process
        assert await runner.start_analysis_process() == pid
        started = log.named("analysis_process_started")
        assert [entry["pid"] for entry in started] == [pid, pid]
        assert all(isinstance(entry["startup_ms"], int) for entry in started)
        process = runner._process
        assert process is not None
        niceness = await asyncio.wrap_future(process.executor.submit(os.nice, 0))
        assert niceness == min(os.nice(0) + 10, 19)
        handler = await asyncio.wrap_future(
            process.executor.submit(signal.getsignal, signal.SIGINT)
        )
        assert handler == signal.SIG_IGN
        # The pid it is bound to is its actual parent's, this process's.
        parent = await asyncio.wrap_future(process.executor.submit(os.getppid))
        assert parent == os.getpid()
    finally:
        await runner.stop_analysis_process()
    assert runner._process is None
    assert await asyncio.to_thread(_exited, pid)
    assert len(log.named("analysis_process_stopped")) == stopped + 1
    await runner.stop_analysis_process()  # nothing left to stop
    assert len(log.named("analysis_process_stopped")) == stopped + 1


@pytest.mark.anyio
async def test_a_job_the_stopping_process_never_ran_fails_with_analysis_process_error(
    app_db: str,
) -> None:
    await _seed()
    run_id = await _insert_run("custom", "running", None)
    job = await _job(run_id)
    await runner.start_analysis_process()
    process = runner._process
    assert process is not None
    # Sleeps occupy the worker and fill the executor's call queue, so the job stays pending:
    # exactly what a shutdown cancels.
    sleeps = [process.executor.submit(time.sleep, 0.3) for _ in range(5)]
    progress = runner._RunProgress(stage="load_context", run_id=run_id)
    task = asyncio.create_task(runner._in_analysis_process(job, progress))
    try:
        await asyncio.sleep(0)  # the task hands the job off before its first wait
        await runner.stop_analysis_process()
        with pytest.raises(runner.AnalysisProcessError, match="shut down before it ran"):
            await asyncio.wait_for(task, _WAIT_S)
    finally:
        await _settled(task)
    assert any(sleep.cancelled() for sleep in sleeps)
    assert progress.stage == "load_context"
    (run,) = await _runs()
    assert run.status == "running"


@pytest.mark.anyio
async def test_a_run_stored_before_its_cancellation_took_hold_stays_succeeded(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    await _seed()
    pending = await _requests(1)
    window = await runner.custom_window(END - timedelta(days=27), END, now=NOW)
    await runner.start_analysis_process()
    submit = runner._submit
    handed: list[Future[Any]] = []
    handed_off = asyncio.Event()
    delivered: Future[Any] = Future()

    def late(hand_off: Callable[[Any], Future[Any]]) -> tuple[Any, Future[Any]]:
        # The analysis process runs the job for real; only its outcome reaches the calling
        # process late, as when the cancellation arrives right after the commit. Called on
        # the event loop's thread, by the run's own wait.
        process, future = submit(hand_off)
        handed.append(future)
        handed_off.set()
        return process, delivered

    monkeypatch.setattr(runner, "_submit", late)
    task = asyncio.create_task(
        runner.run_analysis(window, "custom", "manual", request_ids=pending, now=NOW, isolated=True)
    )
    try:
        await asyncio.wait_for(handed_off.wait(), _WAIT_S)
        outcome = await asyncio.wait_for(asyncio.wrap_future(handed[0]), _WAIT_S)
        assert isinstance(outcome, runner._ProcessSuccess), outcome
        task.cancel()
        await asyncio.sleep(0.1)
        assert not task.done()  # the cancellation is held until the outcome arrives
        process = runner._process
        assert process is not None
        assert process.signals.stop.value > 0  # and it stopped the job, too late
        delivered.set_result(outcome)
        await asyncio.wait((task,), timeout=_WAIT_S)
        assert task.cancelled()
    finally:
        if not delivered.done():
            delivered.cancel()
        await _settled(task)

    (run,) = await _runs()
    assert (run.id, run.status) == (outcome.stored.id, "succeeded")
    assert await _served_by() == [run.id]
    assert await _count(Recommendation) == outcome.summary["items_total"]
    (summary,) = log.named("analysis_run_summary")
    assert (summary["run_id"], summary["requests"]) == (run.id, 1)
    assert log.named("analysis_run_failed") == []
    async with analysis_lock():
        pass  # the lock was released


@pytest.mark.anyio
async def test_a_cancellation_that_meets_a_killed_analysis_process_fails_the_run(
    app_db: str, log: _LogRecorder
) -> None:
    await _seed()
    window = await runner.custom_window(END - timedelta(days=27), END, now=NOW)
    pid = await runner.start_analysis_process()
    caught: list[asyncio.CancelledError] = []

    async def run() -> None:
        try:
            await runner.run_analysis(window, "custom", "manual", now=NOW, isolated=True)
        except asyncio.CancelledError as cancelled:
            caught.append(cancelled)
            raise

    with _search_terms_locked(app_db) as held:
        task = asyncio.create_task(run())
        try:
            assert await asyncio.to_thread(held.wait_for_waiter), "the load never reached it"
            # Killed first, so the analysis process never sees the stop the cancellation sets.
            os.kill(pid, signal.SIGKILL)
            task.cancel()
            await asyncio.wait((task,), timeout=_WAIT_S)
            assert task.cancelled()
        finally:
            await _settled(task)

    (cancelled,) = caught
    assert isinstance(cancelled.__cause__, BrokenProcessPool)
    (run,) = await _runs()
    assert run.status == "failed"
    assert await _count(Recommendation) == 0
    (failed,) = log.named("analysis_run_failed")
    assert (failed["run_id"], failed["stage"], failed["error"]) == (
        run.id,
        "load_context",
        "CancelledError",
    )
    async with analysis_lock():
        pass  # the lock was released
    assert await asyncio.to_thread(_exited, pid)


@pytest.mark.anyio
async def test_an_analysis_process_that_died_idle_is_replaced_at_the_next_hand_off(
    app_db: str, log: _LogRecorder
) -> None:
    await _seed()
    window = await runner.custom_window(END - timedelta(days=27), END, now=NOW)
    pid = await runner.start_analysis_process()
    replaced = len(log.named("analysis_process_replaced"))
    os.kill(pid, signal.SIGKILL)
    # The executor marks itself broken before it reaps its worker.
    assert await asyncio.to_thread(_exited, pid)

    run = await runner.run_analysis(window, "custom", "manual", now=NOW, isolated=True)

    assert run.status == "succeeded"
    assert log.named("analysis_process_replaced")[replaced:] == [{"reason": "broken"}]
    assert log.named("analysis_run_failed") == []
    assert await runner.start_analysis_process() != pid


@pytest.mark.anyio
async def test_an_analysis_process_that_cannot_start_is_discarded(
    monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    await runner.stop_analysis_process()
    with monkeypatch.context() as patch:
        # A worker whose initialisation fails exits at once: this initializer takes none of
        # the values it is called with.
        patch.setattr(runner, "_init_analysis_process", runner._analysis_process_pid)
        with pytest.raises(BrokenProcessPool):
            await asyncio.wait_for(runner.start_analysis_process(), _WAIT_S)
    assert runner._process is None
    assert log.named("analysis_process_started") == []
    pid = await runner.start_analysis_process()
    assert pid != os.getpid()
    assert [entry["pid"] for entry in log.named("analysis_process_started")] == [pid]


@pytest.mark.anyio
async def test_a_stopped_job_cancels_its_database_step_in_flight() -> None:
    signals = _signals()
    progress = runner._RunProgress(stage="persist", run_id=1, stop=runner._JobStop(signals, 3))
    # Another job's stop leaves this job's step running to its end.
    signals.stop.value = 2
    assert await runner._until_stopped(asyncio.sleep(0.12, result="stored"), progress) == "stored"
    rolled_back = asyncio.Event()

    async def step() -> None:
        try:
            await asyncio.sleep(_WAIT_S)
        except asyncio.CancelledError:
            rolled_back.set()
            raise

    asyncio.get_running_loop().call_later(0.1, progress.stop.set)
    started = time.perf_counter()
    with pytest.raises(runner._ComputationStoppedError):
        await runner._until_stopped(step(), progress)
    assert rolled_back.is_set()
    assert time.perf_counter() - started < 1.0
    assert signals.stop.value == 3


@pytest.mark.anyio
async def test_a_failed_disposal_after_a_job_is_logged_and_never_raised(
    monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    async def failing() -> None:
        raise OSError("connection reset by peer")

    monkeypatch.setattr(runner, "dispose_engine", failing)
    await runner._dispose_process_engine()
    assert log.named("analysis_process_dispose_failed") == [{"error": "OSError"}]


async def _client_check_interval() -> str:
    """``client_connection_check_interval`` as the application's sessions see it."""
    async with session_scope() as session:
        shown = await session.execute(text("SHOW client_connection_check_interval"))
        return str(shown.scalar_one())


@pytest.mark.anyio
async def test_a_job_engine_has_postgresql_check_that_its_process_is_connected(
    app_db: str, log: _LogRecorder
) -> None:
    await dispose_engine()
    runner._check_clients_of(get_engine())
    # Committed on connect, so it holds in every session on the connection, not only the first.
    assert await _client_check_interval() == "1s"
    assert await _client_check_interval() == "1s"
    assert log.named("analysis_process_client_check_unavailable") == []


@pytest.mark.anyio
async def test_a_refused_client_check_is_logged_and_the_connection_still_serves(
    app_db: str, monkeypatch: pytest.MonkeyPatch, log: _LogRecorder
) -> None:
    # PostgreSQL refuses a negative interval, as a server without the check refuses any.
    monkeypatch.setattr(runner, "_CLIENT_CHECK_SQL", "SET client_connection_check_interval = -1")
    await dispose_engine()
    runner._check_clients_of(get_engine())
    assert await _client_check_interval() == "0"
    assert log.named("analysis_process_client_check_unavailable") == [
        {"error": "InvalidParameterValue"}
    ]
