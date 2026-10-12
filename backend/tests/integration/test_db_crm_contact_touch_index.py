"""The partial index behind Google-touch contact discovery, against real PostgreSQL 18.

``ads_engine.analysis.metrics._load_crm`` discovers a HubSpot dataset's Google-touch contacts
(AAP 0.3.3, "Google-to-CRM link") with
:func:`~ads_engine.analysis.metrics._google_touch_contacts`: ``dataset_id`` equality,
``google_campaign_ref IS NOT NULL`` and a half-open ``google_touch_at`` range, ordered by
touch, then HubSpot id in code-point order. ``ix_crm_contact_dataset_google_touch`` on
(``dataset_id``, ``google_touch_at``) ``WHERE google_campaign_ref IS NOT NULL`` serves that
read as one index range, so the contacts that carry no Google ref are never visited.

The tests check, on the server:

* the catalog definition of the index, including its ``WHERE`` clause, is identical whether
  the model metadata or the hand-written migration ``0001_initial_schema`` builds the table;
* the statement returns exactly the contacts of its dataset that carry a ref and were touched
  inside the range, in the documented order;
* the planner matches the partial predicate and reads the statement through the index, both
  with the bound values (custom plan) and as a prepared statement under
  ``plan_cache_mode = force_generic_plan``.

The module creates two private schemas in the database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``): one built from the model metadata (``crm_contact`` and the
tables its foreign keys reach) and one built by the migration's ``upgrade()``. Every
connection's ``search_path`` is pinned to one of them, and both are dropped with ``CASCADE``
at teardown, so ``public`` and every other schema stay untouched. Each test runs in one
transaction that is rolled back afterwards. An unreachable database fails the tests; it never
skips them.
"""

import importlib.util
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, Table, create_engine, insert, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from ads_engine.analysis import metrics
from ads_engine.db.base import Base
from ads_engine.db.models import crm, ops

INDEX = "ix_crm_contact_dataset_google_touch"
PREDICATE = "google_campaign_ref IS NOT NULL"
VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION = VERSIONS / "0001_initial_schema.py"

CRM_CONTACT: Table = Base.metadata.tables[crm.CrmContact.__tablename__]

LOWER = datetime(2026, 9, 9, tzinfo=UTC)
UPPER = datetime(2026, 10, 7, tzinfo=UTC)
NOON = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Databases:
    """The two private schemas of the module and the engines pinned to them.

    Attributes:
        models: An engine whose connections use only the schema the model metadata built.
        models_schema: That schema's name.
        migration: An engine whose connections use only the schema the migration built.
        migration_schema: That schema's name.
    """

    models: Engine
    models_schema: str
    migration: Engine
    migration_schema: str


# ---------------------------------------------------------------------------------------
# Database: two private schemas per module, one rolled-back transaction per test
# ---------------------------------------------------------------------------------------


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the Google-touch index tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _engine(url: URL, schema: str) -> Engine:
    """Return an engine whose every connection uses only ``schema``, in UTC."""
    return create_engine(
        url,
        poolclass=NullPool,
        connect_args={"options": f"-c search_path={schema} -c TimeZone=UTC"},
    )


def _with_foreign_key_targets(*seeds: Table) -> list[Table]:
    """Return ``seeds`` together with every table their foreign keys reach, transitively."""
    found: dict[str, Table] = {}
    pending = list(seeds)
    while pending:
        table = pending.pop()
        if table.name in found:
            continue
        found[table.name] = table
        pending.extend(foreign_key.column.table for foreign_key in table.foreign_keys)
    return list(found.values())


def _migration_module() -> ModuleType:
    """Load ``0001_initial_schema`` from its file, as Alembic's script directory does."""
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


@pytest.fixture(scope="module")
def databases() -> Iterator[Databases]:
    """Build the model schema and the migration schema; drop both afterwards.

    Yields:
        The engines pinned to each schema, and the schema names.
    """
    url = _database_url()
    suffix = uuid.uuid4().hex
    models_schema = f"crm_touch_models_test_{suffix}"
    migration_schema = f"crm_touch_migration_test_{suffix}"
    models = _engine(url, models_schema)
    migration = _engine(url, migration_schema)
    try:
        with models.begin() as connection:
            connection.execute(CreateSchema(models_schema))
            Base.metadata.create_all(connection, tables=_with_foreign_key_targets(CRM_CONTACT))
        with migration.begin() as connection:
            connection.execute(CreateSchema(migration_schema))
            with Operations.context(MigrationContext.configure(connection)):
                _migration_module().upgrade()
    except SQLAlchemyError as exc:
        _drop(models, models_schema)
        _drop(migration, migration_schema)
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield Databases(models, models_schema, migration, migration_schema)
    finally:
        _drop(models, models_schema)
        _drop(migration, migration_schema)


@pytest.fixture
def connection(databases: Databases) -> Iterator[Connection]:
    """Yield a connection to the model schema inside a transaction rolled back afterwards."""
    with databases.models.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _dataset(connection: Connection, state: str) -> int:
    """Insert a live primary HubSpot ``source_dataset`` row in ``state`` and return its id."""
    statement = (
        insert(ops.SourceDataset)
        .values(source="hubspot", mode="live", state=state, role="primary")
        .returning(ops.SourceDataset.id)
    )
    return int(connection.execute(statement).scalar_one())


def _contact(
    connection: Connection,
    dataset_id: int,
    hs_id: str,
    *,
    ref: str | None,
    touch: datetime | None,
) -> None:
    """Insert one HubSpot contact of ``dataset_id`` with its Google ref and touch instant."""
    connection.execute(
        insert(crm.CrmContact).values(
            source="hubspot",
            dataset_id=dataset_id,
            hs_id=hs_id,
            google_campaign_ref=ref,
            google_touch_at=touch,
        )
    )


def _indexdef(connection: Connection, schema: str) -> str:
    """Return ``pg_indexes.indexdef`` of :data:`INDEX` in ``schema``, schema prefix removed."""
    definition: str = connection.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = :schema AND tablename = 'crm_contact' AND indexname = :name"
        ),
        {"schema": schema, "name": INDEX},
    ).scalar_one()
    return definition.replace(f" ON {schema}.", " ON ")


def _seeded_statement(connection: Connection) -> tuple[str, dict[str, Any]]:
    """Seed two datasets and compile the application's statement as the driver receives it.

    Each dataset gets 2,000 contacts, one in ten with a Google ref and a touch one day apart
    from ``LOWER`` onwards, and the tables are analysed, so the plan is costed on real
    statistics. The statement reads the first dataset over the module range.
    """
    datasets = (_dataset(connection, "active"), _dataset(connection, "retired"))
    for dataset_id in datasets:
        connection.execute(
            text(
                "INSERT INTO crm_contact (source, dataset_id, hs_id, google_campaign_ref, "
                "google_touch_at) "
                "SELECT 'hubspot', :dataset_id, i::text, "
                "CASE WHEN i % 10 = 0 THEN '111' END, "
                "CASE WHEN i % 10 = 0 THEN :lower + make_interval(days => i / 10) END "
                "FROM generate_series(1, 2000) AS i"
            ),
            {"dataset_id": dataset_id, "lower": LOWER},
        )
    connection.exec_driver_sql("ANALYZE source_dataset")
    connection.exec_driver_sql("ANALYZE crm_contact")
    compiled = metrics._google_touch_contacts(datasets[0], LOWER, UPPER).compile(
        dialect=connection.dialect
    )
    return str(compiled), dict(compiled.params)


def _plan_nodes(plan: Any) -> list[tuple[str, str | None, str | None]]:
    """Return every (node type, relation, index) of a ``FORMAT JSON`` plan tree."""
    nodes: list[tuple[str, str | None, str | None]] = []
    pending = [plan]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            if "Node Type" in node:
                nodes.append((node["Node Type"], node.get("Relation Name"), node.get("Index Name")))
            pending.extend(value for value in node.values() if isinstance(value, list | dict))
    return nodes


def _assert_reads_through_the_index(plan: Any) -> None:
    """Assert ``plan`` reads ``crm_contact`` only through :data:`INDEX`, never sequentially.

    Every scan node counts, including a bitmap index scan on any other index (which names no
    relation), so a plan combining :data:`INDEX` with another index fails too.
    """
    nodes = _plan_nodes(plan)
    reads = [node for node in nodes if node[1] == "crm_contact" or node[2] is not None]
    assert ("Seq Scan", "crm_contact", None) not in nodes, nodes
    assert reads, nodes
    assert {index for _, _, index in reads if index is not None} == {INDEX}, nodes
    assert {kind for kind, _, _ in reads} <= {
        "Index Scan",
        "Bitmap Index Scan",
        "Bitmap Heap Scan",
    }, nodes


# ---------------------------------------------------------------------------------------
# The catalog definition
# ---------------------------------------------------------------------------------------


@pytest.mark.db
def test_the_models_and_the_migration_create_the_identical_partial_index(
    connection: Connection, databases: Databases
) -> None:
    """Both schemas hold the dataset and touch columns, partial on a present Google ref."""
    expected = (
        f"CREATE INDEX {INDEX} ON crm_contact USING btree (dataset_id, google_touch_at) "
        f"WHERE ({PREDICATE})"
    )

    with databases.migration.connect() as migration:
        from_migration = _indexdef(migration, databases.migration_schema)

    assert _indexdef(connection, databases.models_schema) == expected
    assert from_migration == expected


# ---------------------------------------------------------------------------------------
# The statement's rows and order
# ---------------------------------------------------------------------------------------


@pytest.mark.db
def test_the_statement_returns_the_in_range_contacts_with_a_ref_in_touch_then_id_order(
    connection: Connection,
) -> None:
    """Only the dataset's ref-carrying contacts touched in ``[lower, upper)``, in order.

    Ids sharing one touch differ in letter case and length, so only code-point order puts
    ``10`` before ``9``, and ``B2`` before ``a1``, at the same instant.
    """
    active = _dataset(connection, "active")
    retired = _dataset(connection, "retired")
    for hs_id, touch in (("a1", NOON), ("10", NOON), ("B2", NOON), ("9", NOON)):
        _contact(connection, active, hs_id, ref="111", touch=touch)
    _contact(connection, active, "first", ref="222", touch=LOWER)
    _contact(connection, active, "last", ref="222", touch=UPPER - timedelta(microseconds=1))
    _contact(connection, active, "at-upper", ref="222", touch=UPPER)
    _contact(connection, active, "before", ref="222", touch=LOWER - timedelta(microseconds=1))
    _contact(connection, active, "no-ref", ref=None, touch=NOON)
    _contact(connection, active, "no-touch", ref="333", touch=None)
    _contact(connection, retired, "other-dataset", ref="111", touch=NOON)

    rows = connection.execute(metrics._google_touch_contacts(active, LOWER, UPPER)).all()

    assert [(row.google_touch_at, row.hs_id, row.google_campaign_ref) for row in rows] == [
        (LOWER, "first", "222"),
        (NOON, "10", "111"),
        (NOON, "9", "111"),
        (NOON, "B2", "111"),
        (NOON, "a1", "111"),
        (UPPER - timedelta(microseconds=1), "last", "222"),
    ]
    assert tuple(rows[0]) == ("first", None, None, None, "222", LOWER)


@pytest.mark.db
def test_the_statement_of_a_dataset_without_google_contacts_returns_nothing(
    connection: Connection,
) -> None:
    """A dataset whose contacts carry no ref, or none touched in range, discovers none."""
    active = _dataset(connection, "active")
    _contact(connection, active, "no-ref", ref=None, touch=NOON)
    _contact(connection, active, "old", ref="111", touch=LOWER - timedelta(days=400))

    assert connection.execute(metrics._google_touch_contacts(active, LOWER, UPPER)).all() == []


# ---------------------------------------------------------------------------------------
# The plan: the partial predicate is proved for custom and generic plans
# ---------------------------------------------------------------------------------------


@pytest.mark.db
def test_the_planner_reads_the_bound_statement_through_the_partial_index(
    connection: Connection,
) -> None:
    """With the values bound as the application binds them, the plan uses the index.

    Sequential scans are disabled for the transaction so the small table cannot win on cost;
    the index can then serve the statement only if the planner proves its predicate.
    """
    sql, params = _seeded_statement(connection)
    connection.exec_driver_sql("SET LOCAL enable_seqscan = off")

    plan: list[dict[str, Any]] = connection.exec_driver_sql(
        "EXPLAIN (FORMAT JSON) " + sql, params
    ).scalar_one()

    _assert_reads_through_the_index(plan)


@pytest.mark.db
def test_the_planner_reads_the_generic_prepared_statement_through_the_partial_index(
    connection: Connection,
) -> None:
    """A prepared statement's generic plan, with no value known, still uses the index.

    psycopg prepares a statement server-side once it repeats, and PostgreSQL may then run a
    generic plan; the predicate holds there because ``IS NOT NULL`` carries no parameter.
    """
    sql, params = _seeded_statement(connection)
    names = re.findall(r"%\((\w+)\)s", sql)
    assert sorted(names) == sorted(params)
    types: list[str] = []
    values: list[str] = []
    for position, name in enumerate(names, start=1):
        sql = sql.replace(f"%({name})s", f"${position}", 1)
        value = params[name]
        if isinstance(value, datetime):
            types.append("timestamptz")
            values.append(f"'{value.isoformat()}'")
        else:
            assert isinstance(value, int), (name, value)
            types.append("bigint")
            values.append(str(value))
    connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
    connection.exec_driver_sql("SET LOCAL plan_cache_mode = force_generic_plan")
    statement = f"crm_touch_{uuid.uuid4().hex}"
    connection.exec_driver_sql(f"PREPARE {statement} ({', '.join(types)}) AS {sql}")
    try:
        # EXECUTE takes no bind parameters; a generic plan ignores the values' contents.
        plan: list[dict[str, Any]] = connection.exec_driver_sql(
            f"EXPLAIN (FORMAT JSON) EXECUTE {statement} ({', '.join(values)})"
        ).scalar_one()
    finally:
        connection.exec_driver_sql(f"DEALLOCATE {statement}")

    _assert_reads_through_the_index(plan)
