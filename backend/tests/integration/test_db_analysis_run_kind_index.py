"""The index behind ``GET /runs``, against real PostgreSQL 18.

``GET /runs?kind=&limit=`` (AAP 0.5.3) reads :func:`ads_engine.analysis.runner.list_runs`:
``kind`` equality, ordered by ``id`` descending, at most ``limit`` rows (20 by default, 100 at
most). ``custom`` runs are sparse among the worker's ``default`` runs, so without a matching
index the planner either sorts every run of the kind or walks the primary key backwards
through arbitrarily many ``default`` runs. ``ix_analysis_run_kind_id`` on (``kind``,
``id DESC``) serves the read as one index range in the returned order, with no sort.

The tests check, on the server:

* the catalog definitions of every ``analysis_run`` index, ``ix_analysis_run_kind_id``
  included, are identical whether the model metadata or the hand-written migration
  ``0001_initial_schema`` builds the table;
* ``list_runs`` returns the newest runs of the kind, in every status, in ``id`` descending
  order, and never more than ``limit``; its statement carries the checked kind inline and
  binds only the limit;
* the planner reads the exact statement ``list_runs`` sends through the index with no sort
  node, visiting only ``limit`` rows, both with the bound values (custom plan) and as a
  prepared statement under ``plan_cache_mode = force_generic_plan``. A generic plan of
  ``kind = $1`` would assume half the runs match and walk the primary key backwards through
  every newer ``default`` run, which is why the kind is inline.

The module creates two private schemas in the database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``): one built from the model metadata (``analysis_run``, which
has no foreign key) and one built by the migration's ``upgrade()``, which the module seeds with
:data:`TOTAL_RUNS` runs, one in :data:`CUSTOM_EVERY` of them ``custom``, then analyses. Every
connection's ``search_path`` is pinned to one of them, and both are dropped with ``CASCADE`` at
teardown, so ``public`` and every other schema stay untouched. The tests only read the seeded
rows. An unreachable database fails the tests; it never skips them.
"""

import importlib.util
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, Table, create_engine, event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from ads_engine.analysis import runner
from ads_engine.api.routes.runs import DEFAULT_LIMIT, MAX_LIMIT
from ads_engine.db.base import Base
from ads_engine.db.models.analysis import AnalysisRun

INDEX = "ix_analysis_run_kind_id"
VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION = VERSIONS / "0001_initial_schema.py"

ANALYSIS_RUN: Table = Base.metadata.tables[AnalysisRun.__tablename__]

# Run ``i`` has id ``i``; every CUSTOM_EVERY-th run is ``custom``, so 40 custom runs lie among
# 19,960 default runs, and the newest custom run is the newest run of all.
TOTAL_RUNS = 20_000
CUSTOM_EVERY = 500
# Every FAILED_EVERY-th run failed; the newest run is still running.
FAILED_EVERY = 1_500

CUSTOM_IDS: tuple[int, ...] = tuple(range(TOTAL_RUNS, 0, -CUSTOM_EVERY))
DEFAULT_IDS: tuple[int, ...] = tuple(i for i in range(TOTAL_RUNS, 0, -1) if i % CUSTOM_EVERY)


@dataclass(frozen=True, slots=True)
class Databases:
    """The two private schemas of the module and the engines pinned to them.

    Attributes:
        url: The test database URL on the psycopg 3 driver.
        models: An engine whose connections use only the schema the model metadata built.
        models_schema: That schema's name.
        migration: An engine whose connections use only the schema the migration built, which
            holds the seeded runs.
        migration_schema: That schema's name.
    """

    url: URL
    models: Engine
    models_schema: str
    migration: Engine
    migration_schema: str


@dataclass(frozen=True, slots=True)
class Listed:
    """One call of :func:`~ads_engine.analysis.runner.list_runs` and the SQL it sent.

    Attributes:
        runs: ``(id, kind, status)`` of each returned run, in the returned order.
        sql: The statement exactly as the driver received it.
        params: The statement's bound parameters, by name.
    """

    runs: list[tuple[int, str, str]]
    sql: str
    params: dict[str, Any]


# ---------------------------------------------------------------------------------------
# Database: two private schemas per module, the seeded runs committed once
# ---------------------------------------------------------------------------------------


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the analysis_run kind index tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _options(schema: str) -> dict[str, str]:
    """Return connect arguments that pin a connection to ``schema``, in UTC."""
    return {"options": f"-c search_path={schema} -c TimeZone=UTC"}


def _engine(url: URL, schema: str) -> Engine:
    """Return an engine whose every connection uses only ``schema``, in UTC."""
    return create_engine(url, poolclass=NullPool, connect_args=_options(schema))


def _migration_module() -> ModuleType:
    """Load ``0001_initial_schema`` by path; ``migrations/versions`` is not a package."""
    spec = importlib.util.spec_from_file_location("migration_0001_initial_schema", MIGRATION)
    assert spec is not None, MIGRATION
    assert spec.loader is not None, MIGRATION
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop(engine: Engine, schema: str) -> None:
    """Drop ``schema`` with everything in it, if it exists, and dispose of ``engine``."""
    try:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
    finally:
        engine.dispose()


def _discard(engine: Engine, schema: str) -> str:
    """Drop ``schema`` after a failed setup; return why that failed too, or ``""``.

    The setup failure is what the test reports, so a drop that fails as well (an unreachable
    server refuses it the same way) is appended to that report instead of replacing it.
    """
    try:
        _drop(engine, schema)
    except SQLAlchemyError as exc:
        return f"; schema {schema} not dropped: {exc}"
    return ""


def _seed(connection: Connection) -> None:
    """Insert :data:`TOTAL_RUNS` runs with ids ``1..TOTAL_RUNS`` and analyse the table.

    Run ``i`` is ``custom`` (``manual``) when ``i`` is a multiple of :data:`CUSTOM_EVERY`,
    otherwise ``default`` (``scheduled``). It failed when ``i`` is a multiple of
    :data:`FAILED_EVERY`, and the newest run is still ``running`` with no ``finished_at``.
    """
    connection.execute(
        text(
            "INSERT INTO analysis_run (id, kind, window_start, window_end, "
            "source_day_boundaries, trigger, data_modes, dataset_ids, config_hash, "
            "default_days, trusted_min_days, status, started_at, finished_at) "
            "SELECT i, "
            "CASE WHEN i % :custom_every = 0 THEN 'custom' ELSE 'default' END, "
            "DATE '2026-07-01', DATE '2026-09-28', '[]'::jsonb, "
            "CASE WHEN i % :custom_every = 0 THEN 'manual' ELSE 'scheduled' END, "
            "'{}'::jsonb, '{}'::jsonb, 'run-' || i, 28, 14, "
            "CASE WHEN i = :total THEN 'running' "
            "WHEN i % :failed_every = 0 THEN 'failed' ELSE 'succeeded' END, "
            "TIMESTAMPTZ '2026-01-01 00:00:00+00' + make_interval(mins => i), "
            "CASE WHEN i = :total THEN NULL "
            "ELSE TIMESTAMPTZ '2026-01-01 00:00:00+00' + make_interval(mins => i + 1) END "
            "FROM generate_series(1, :total) AS i"
        ),
        {"custom_every": CUSTOM_EVERY, "failed_every": FAILED_EVERY, "total": TOTAL_RUNS},
    )
    connection.exec_driver_sql("ANALYZE analysis_run")


@pytest.fixture(scope="module")
def databases() -> Iterator[Databases]:
    """Build the model schema and the seeded migration schema; drop both afterwards.

    Yields:
        The database URL, the engines pinned to each schema, and the schema names.
    """
    url = _database_url()
    suffix = uuid.uuid4().hex
    models_schema = f"run_kind_models_test_{suffix}"
    migration_schema = f"run_kind_migration_test_{suffix}"
    models = _engine(url, models_schema)
    migration = _engine(url, migration_schema)
    try:
        with models.begin() as connection:
            connection.execute(CreateSchema(models_schema))
            Base.metadata.create_all(connection, tables=[ANALYSIS_RUN])
        with migration.begin() as connection:
            connection.execute(CreateSchema(migration_schema))
            with Operations.context(MigrationContext.configure(connection)):
                _migration_module().upgrade()
        with migration.begin() as connection:
            _seed(connection)
    except SQLAlchemyError as exc:
        cleanup = _discard(models, models_schema) + _discard(migration, migration_schema)
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}{cleanup}"
        )
    try:
        yield Databases(url, models, models_schema, migration, migration_schema)
    finally:
        _drop(models, models_schema)
        _drop(migration, migration_schema)


@pytest.fixture
def connection(databases: Databases) -> Iterator[Connection]:
    """Yield a connection to the seeded schema inside a transaction rolled back afterwards."""
    with databases.migration.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


async def _list_runs(databases: Databases, kind: str, limit: int) -> Listed:
    """Call ``list_runs`` on the seeded schema through an application-style async session.

    A ``before_cursor_execute`` listener records the one ``analysis_run`` statement the call
    sends, exactly as the driver receives it, so the plan tests explain that statement and not
    a re-creation of it.
    """
    engine = create_async_engine(
        databases.url, poolclass=NullPool, connect_args=_options(databases.migration_schema)
    )
    sent: list[tuple[str, dict[str, Any]]] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        """Keep each ``analysis_run`` statement and its parameters."""
        del conn, cursor, context, many  # The listener's signature is fixed by SQLAlchemy.
        if "FROM analysis_run" in statement:
            sent.append((statement, dict(parameters)))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with async_sessionmaker(engine)() as session:
            runs = [
                (run.id, run.kind, run.status)
                for run in await runner.list_runs(session, kind=kind, limit=limit)
            ]
    finally:
        await engine.dispose()
    assert len(sent) == 1, sent
    sql, params = sent[0]
    return Listed(runs, sql, params)


# ---------------------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------------------


def _plan_nodes(plan: Any) -> list[dict[str, Any]]:
    """Return every node of a ``FORMAT JSON`` plan tree, parents before their children."""
    root: dict[str, Any] = plan[0]["Plan"]
    nodes: list[dict[str, Any]] = []
    pending = [root]
    while pending:
        node = pending.pop(0)
        nodes.append(node)
        pending.extend(node.get("Plans", []))
    return nodes


def _assert_one_ordered_index_range(plan: Any, limit: int) -> None:
    """Assert ``plan`` is a limit over one index scan of :data:`INDEX`, visiting ``limit`` rows.

    No sort node of any kind, no other scan, and ``kind`` is an index condition rather than a
    filter applied to rows the scan already read.
    """
    nodes = _plan_nodes(plan)
    assert [node["Node Type"] for node in nodes] == ["Limit", "Index Scan"], nodes
    scan = nodes[1]
    assert scan["Relation Name"] == "analysis_run", scan
    assert scan["Index Name"] == INDEX, scan
    assert "kind" in scan["Index Cond"], scan
    assert "Filter" not in scan, scan
    assert scan["Actual Loops"] == 1, scan
    assert scan["Actual Rows"] == limit, scan


def _indexdefs(connection: Connection, schema: str) -> dict[str, str]:
    """Return ``pg_indexes.indexdef`` of each ``analysis_run`` index in ``schema`` by name."""
    rows = connection.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = :schema AND tablename = 'analysis_run'"
        ),
        {"schema": schema},
    ).all()
    return {
        str(name): str(definition).replace(f" ON {schema}.", " ON ") for name, definition in rows
    }


# ---------------------------------------------------------------------------------------
# The catalog definition
# ---------------------------------------------------------------------------------------


@pytest.mark.db
def test_the_models_and_the_migration_create_the_identical_analysis_run_indexes(
    connection: Connection, databases: Databases
) -> None:
    """Both schemas hold the same four indexes, (kind, id DESC) among them."""
    with databases.models.connect() as models:
        from_models = _indexdefs(models, databases.models_schema)

    from_migration = _indexdefs(connection, databases.migration_schema)

    assert from_migration == from_models
    assert set(from_migration) == {
        "pk_analysis_run",
        INDEX,
        "ix_analysis_run_kind_status_finished",
        "uq_analysis_run_single_running",
    }
    assert from_migration[INDEX] == (
        f"CREATE INDEX {INDEX} ON analysis_run USING btree (kind, id DESC)"
    )


# ---------------------------------------------------------------------------------------
# The rows and their order
# ---------------------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "limit", "expected_ids"),
    [
        ("custom", DEFAULT_LIMIT, CUSTOM_IDS[:DEFAULT_LIMIT]),
        ("custom", MAX_LIMIT, CUSTOM_IDS),
        ("custom", 1, CUSTOM_IDS[:1]),
        ("default", MAX_LIMIT, DEFAULT_IDS[:MAX_LIMIT]),
    ],
    ids=["custom-default-limit", "custom-fewer-than-max-limit", "custom-one", "default-max"],
)
async def test_list_runs_returns_the_newest_runs_of_the_kind_in_id_descending_order(
    databases: Databases, kind: str, limit: int, expected_ids: tuple[int, ...]
) -> None:
    """Only runs of ``kind``, newest id first, every status included, at most ``limit``."""
    listed = await _list_runs(databases, kind, limit)

    assert [run_id for run_id, _, _ in listed.runs] == list(expected_ids)
    assert {run_kind for _, run_kind, _ in listed.runs} == {kind}


@pytest.mark.db
@pytest.mark.anyio
async def test_list_runs_returns_custom_runs_in_every_status(databases: Databases) -> None:
    """The running newest run and failed runs are listed alongside succeeded ones."""
    listed = await _list_runs(databases, "custom", DEFAULT_LIMIT)

    statuses = {run_id: status for run_id, _, status in listed.runs}
    assert statuses[TOTAL_RUNS] == "running"
    assert statuses[19_500] == "failed"
    assert statuses[19_000] == "succeeded"


@pytest.mark.db
@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["custom", "default"])
async def test_list_runs_inlines_the_checked_kind_and_binds_only_the_limit(
    databases: Databases, kind: str
) -> None:
    """The kind is in the statement text, so every plan knows it; the limit stays bound."""
    listed = await _list_runs(databases, kind, DEFAULT_LIMIT)

    assert f"WHERE analysis_run.kind = '{kind}' ORDER BY analysis_run.id DESC" in " ".join(
        listed.sql.split()
    )
    assert list(listed.params.values()) == [DEFAULT_LIMIT]


# ---------------------------------------------------------------------------------------
# The plan: one ordered index range, for custom and generic plans
# ---------------------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.anyio
async def test_the_planner_reads_the_bound_statement_as_one_ordered_index_range(
    databases: Databases, connection: Connection
) -> None:
    """With the values bound as ``list_runs`` binds them, the plan is a limited index scan.

    Nothing is disabled: on the analysed statistics the planner chooses the index on cost.
    """
    listed = await _list_runs(databases, "custom", DEFAULT_LIMIT)

    plan: list[dict[str, Any]] = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + listed.sql, listed.params
    ).scalar_one()

    _assert_one_ordered_index_range(plan, DEFAULT_LIMIT)


@pytest.mark.db
@pytest.mark.anyio
async def test_the_planner_reads_the_generic_prepared_statement_as_one_ordered_index_range(
    databases: Databases, connection: Connection
) -> None:
    """A prepared statement's generic plan, built with the limit unknown, is the same.

    psycopg prepares a statement server-side once it repeats, and PostgreSQL may then run a
    generic plan. The kind is part of the statement text, so that plan is still costed on
    the knowledge that ``custom`` runs are sparse.
    """
    listed = await _list_runs(databases, "custom", DEFAULT_LIMIT)
    (name,) = listed.params
    limit = listed.params[name]
    assert isinstance(limit, int), listed.params
    sql = listed.sql.replace(f"%({name})s", "$1", 1)
    assert "%(" not in sql, sql
    connection.exec_driver_sql("SET LOCAL plan_cache_mode = force_generic_plan")
    statement = f"run_kind_{uuid.uuid4().hex}"
    connection.exec_driver_sql(f"PREPARE {statement} (bigint) AS {sql}")
    try:
        # EXECUTE takes no bind parameters; the generic plan is built without the value.
        plan: list[dict[str, Any]] = connection.exec_driver_sql(
            f"EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) EXECUTE {statement} ({limit})"
        ).scalar_one()
    finally:
        connection.exec_driver_sql(f"DEALLOCATE {statement}")

    _assert_one_ordered_index_range(plan, DEFAULT_LIMIT)
