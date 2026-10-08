"""Claiming due ``lead_match`` rows: which rows, in which order, through which index.

``ads_engine.sync.lead_match._claim`` locks the next batch of one CRM dataset's due rows
(AAP 0.3.3, "Retry schedule" and "Restart safety"): ``pending`` rows, and ``no_match`` and
``error`` rows whose retry is due, ordered by ``next_attempt_at`` with ``NULL`` first and ties
broken by ``lead_id``, at most :data:`~ads_engine.sync.lead_match.BATCH_SIZE` rows, with
``FOR UPDATE OF lead_match SKIP LOCKED``. The partial index ``ix_lead_match_claim`` on
(``crm_dataset_id``, ``next_attempt_at NULLS FIRST``, ``lead_id``) holds exactly the claimable
rows in that order, so a batch reads its rows in order and stops at its limit.

The tests check, on the server:

* the claim returns exactly the due rows, in that order, at most one batch of them; never a
  ``matched`` or ``no_email`` row, a row whose retries stopped, a row not yet due, a row of
  another CRM dataset or a lead of a retired LinkedIn dataset; the optional LinkedIn dataset
  filter narrows it;
* the model metadata and the hand-written migration ``0001_initial_schema`` create the
  identical ``ix_lead_match_claim``, the migration repeats its predicate verbatim, and
  ``ix_lead_match_due`` is still there;
* the statement the claim sends reads ``lead_match`` through ``ix_lead_match_claim`` with no
  sort, both with its values bound (custom plan) and as a prepared statement under
  ``plan_cache_mode = force_generic_plan``.

The module creates two private schemas in the database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``): one built from the model metadata (``lead_match`` and the
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
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, Table, create_engine, event, insert, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from ads_engine.db.base import Base
from ads_engine.db.models import accounts, crm, ops
from ads_engine.sync.lead_match import BATCH_SIZE, _claim

pytestmark = pytest.mark.integration

CLAIM_INDEX = "ix_lead_match_claim"
DUE_INDEX = "ix_lead_match_due"
CLAIM_INDEXDEF = (
    "CREATE INDEX ix_lead_match_claim ON lead_match USING btree "
    "(crm_dataset_id, next_attempt_at NULLS FIRST, lead_id) "
    "WHERE ((status = 'pending'::text) OR "
    "((status = ANY (ARRAY['no_match'::text, 'error'::text])) "
    "AND (next_attempt_at IS NOT NULL)))"
)
DUE_INDEXDEF = (
    "CREATE INDEX ix_lead_match_due ON lead_match USING btree "
    "(crm_dataset_id, status, next_attempt_at)"
)
MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0001_initial_schema.py"
)

LEAD_MATCH: Table = Base.metadata.tables[crm.LeadMatch.__tablename__]

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The claim's instant: rows due at or before it are claimable."""

SUBMITTED_AT = NOW - timedelta(days=3)


def _at(hours: float) -> datetime:
    """Return the instant ``hours`` hours after :data:`NOW` (before it when negative)."""
    return NOW + timedelta(hours=hours)


@dataclass(frozen=True, slots=True)
class Database:
    """The two private schemas of the module and how to reach them.

    Attributes:
        url: The test database URL on the psycopg 3 driver.
        models: An engine whose connections use only the schema the model metadata built.
        models_schema: That schema's name.
        migration: An engine whose connections use only the schema the migration built.
        migration_schema: That schema's name.
    """

    url: URL
    models: Engine
    models_schema: str
    migration: Engine
    migration_schema: str


@dataclass(frozen=True, slots=True)
class World:
    """Ids of the datasets and campaigns every claim test starts from.

    Attributes:
        linkedin: The active live LinkedIn dataset.
        linkedin_building: A live LinkedIn dataset still being built.
        linkedin_retired: A retired live LinkedIn dataset.
        crm: The active live HubSpot primary dataset whose rows are claimed.
        other_crm: A building live HubSpot primary dataset staging its own rows.
        campaigns: The one campaign of each LinkedIn dataset, keyed by dataset id.
        contact: A contact of ``crm``, named by its ``matched`` rows.
    """

    linkedin: int
    linkedin_building: int
    linkedin_retired: int
    crm: int
    other_crm: int
    campaigns: dict[int, int]
    contact: int


# ---------------------------------------------------------------------------------------
# Database: two private schemas per module, one rolled-back transaction per test
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
            "for the lead-match claim tests"
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
def database() -> Iterator[Database]:
    """Build the model schema and the migration schema; drop both afterwards.

    Yields:
        The database URL, the engines pinned to each schema, and the schema names.
    """
    url = _database_url()
    # Both names stay within PostgreSQL's 63-character identifier limit.
    suffix = uuid.uuid4().hex
    models_schema = f"lm_claim_models_test_{suffix}"
    migration_schema = f"lm_claim_migration_test_{suffix}"
    models = create_engine(url, poolclass=NullPool, connect_args=_connect_args(models_schema))
    migration = create_engine(url, poolclass=NullPool, connect_args=_connect_args(migration_schema))
    try:
        with models.begin() as connection:
            connection.execute(CreateSchema(models_schema))
            Base.metadata.create_all(connection, tables=_with_foreign_key_targets(LEAD_MATCH))
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
        yield Database(url, models, models_schema, migration, migration_schema)
    finally:
        _drop(models, models_schema)
        _drop(migration, migration_schema)


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """Yield a session on the model schema inside a transaction rolled back afterwards."""
    engine = create_async_engine(
        database.url, poolclass=NullPool, connect_args=_connect_args(database.models_schema)
    )
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            transaction = await session.begin()
            try:
                yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------------------


async def _dataset(session: AsyncSession, source: str, state: str) -> int:
    """Insert a live primary ``source_dataset`` row and return its id."""
    statement = (
        insert(ops.SourceDataset)
        .values(source=source, mode="live", state=state, role="primary")
        .returning(ops.SourceDataset.id)
    )
    return int((await session.execute(statement)).scalar_one())


async def _campaign(session: AsyncSession, dataset_id: int) -> int:
    """Insert an ad account of a LinkedIn dataset with one campaign; return the campaign id."""
    account_id = (
        await session.execute(
            insert(accounts.AdAccount)
            .values(
                source="linkedin",
                dataset_id=dataset_id,
                external_id=f"5{dataset_id}",
                name="Account",
                currency="USD",
            )
            .returning(accounts.AdAccount.id)
        )
    ).scalar_one()
    campaign_id = (
        await session.execute(
            insert(accounts.Campaign)
            .values(
                source="linkedin",
                dataset_id=dataset_id,
                account_id=account_id,
                external_id=f"6{dataset_id}",
                name="Campaign",
                status="active",
                raw_status="ACTIVE",
            )
            .returning(accounts.Campaign.id)
        )
    ).scalar_one()
    return int(campaign_id)


async def _leads(session: AsyncSession, world: World, dataset_id: int, count: int) -> list[int]:
    """Insert ``count`` leads of ``dataset_id``'s campaign and return their ids, ascending."""
    rows = [
        {
            "source": "linkedin",
            "dataset_id": dataset_id,
            "external_id": uuid.uuid4().hex,
            "submitted_at": SUBMITTED_AT,
            "campaign_id": world.campaigns[dataset_id],
            "lead_form_urn": "urn:li:leadGenForm:1",
            "lead_form_version": 1,
        }
        for _ in range(count)
    ]
    result = await session.execute(
        insert(crm.Lead).returning(crm.Lead.id, sort_by_parameter_order=True), rows
    )
    return sorted(int(lead_id) for lead_id in result.scalars())


async def _match(
    session: AsyncSession,
    lead_id: int,
    crm_dataset_id: int,
    status: str,
    next_attempt_at: datetime | None,
    **values: Any,
) -> None:
    """Insert one ``lead_match`` row for (``lead_id``, ``crm_dataset_id``)."""
    await session.execute(
        insert(crm.LeadMatch).values(
            lead_id=lead_id,
            crm_dataset_id=crm_dataset_id,
            status=status,
            next_attempt_at=next_attempt_at,
            **values,
        )
    )


@pytest.fixture
async def world(session: AsyncSession) -> World:
    """Build three LinkedIn datasets with a campaign each, two CRM datasets and a contact."""
    linkedin = await _dataset(session, "linkedin", "active")
    linkedin_building = await _dataset(session, "linkedin", "building")
    linkedin_retired = await _dataset(session, "linkedin", "retired")
    crm_dataset = await _dataset(session, "hubspot", "active")
    other_crm = await _dataset(session, "hubspot", "building")
    campaigns = {
        dataset_id: await _campaign(session, dataset_id)
        for dataset_id in (linkedin, linkedin_building, linkedin_retired)
    }
    contact = (
        await session.execute(
            insert(crm.CrmContact)
            .values(source="hubspot", dataset_id=crm_dataset, hs_id="101")
            .returning(crm.CrmContact.id)
        )
    ).scalar_one()
    return World(
        linkedin=linkedin,
        linkedin_building=linkedin_building,
        linkedin_retired=linkedin_retired,
        crm=crm_dataset,
        other_crm=other_crm,
        campaigns=campaigns,
        contact=int(contact),
    )


def _claimed(claims: list[Any]) -> list[tuple[int, str]]:
    """Return each claim's (``lead_id``, ``status``), in claim order."""
    return [(claim.match.lead_id, claim.match.status) for claim in claims]


# ---------------------------------------------------------------------------------------
# Which rows, in which order
# ---------------------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.anyio
async def test_the_claim_returns_the_due_rows_by_next_attempt_null_first_then_lead_id(
    session: AsyncSession, world: World
) -> None:
    """Pending and due retries interleave on ``next_attempt_at``; terminal rows never come.

    Ties are broken by ``lead_id``, not by status or insertion order: the two never-scheduled
    pending rows and the two retries due at the same instant are inserted in reverse
    ``lead_id`` order, and the error and no-match rows of the tie alternate.
    """
    lead = await _leads(session, world, world.linkedin, 16)
    # Due, in the claim's order once sorted: (next_attempt_at NULLS FIRST, lead_id).
    await _match(session, lead[5], world.crm, "pending", None)
    await _match(session, lead[1], world.crm, "pending", None)
    await _match(session, lead[0], world.crm, "pending", _at(-2))
    await _match(session, lead[7], world.crm, "no_match", _at(-3), attempts=2)
    await _match(session, lead[9], world.crm, "error", _at(-1), attempts=1)
    await _match(session, lead[2], world.crm, "no_match", _at(-1), attempts=1)
    await _match(session, lead[3], world.crm, "error", NOW, attempts=4)
    # Terminal: matched and no_email, even with a stale next_attempt_at in the past.
    await _match(
        session,
        lead[4],
        world.crm,
        "matched",
        None,
        contact_id=world.contact,
        contact_read_status="ok",
    )
    await _match(
        session,
        lead[6],
        world.crm,
        "matched",
        _at(-4),
        contact_id=world.contact,
        contact_read_status="ok",
    )
    await _match(session, lead[8], world.crm, "no_email", None, reason="form map mismatch")
    await _match(session, lead[10], world.crm, "no_email", _at(-4), reason="unknown version")
    # Retries that stopped: no next attempt is scheduled.
    await _match(session, lead[11], world.crm, "no_match", None, attempts=9)
    await _match(session, lead[12], world.crm, "error", None, attempts=1)
    # Not yet due, whatever the status.
    await _match(session, lead[13], world.crm, "no_match", _at(1), attempts=1)
    await _match(session, lead[14], world.crm, "error", NOW + timedelta(microseconds=1))
    await _match(session, lead[15], world.crm, "pending", _at(1))

    claims = await _claim(session, world.crm, NOW, None)

    assert _claimed(claims) == [
        (lead[1], "pending"),
        (lead[5], "pending"),
        (lead[7], "no_match"),
        (lead[0], "pending"),
        (lead[2], "no_match"),
        (lead[9], "error"),
        (lead[3], "error"),
    ]
    assert {claim.lead_dataset_id for claim in claims} == {world.linkedin}
    assert {claim.lead_mode for claim in claims} == {"live"}
    assert {claim.account_id for claim in claims} == {f"5{world.linkedin}"}
    assert {claim.submitted_at for claim in claims} == {SUBMITTED_AT}


@pytest.mark.db
@pytest.mark.anyio
async def test_the_claim_keeps_to_its_crm_dataset_and_skips_retired_linkedin_leads(
    session: AsyncSession, world: World
) -> None:
    """Rows staged for another CRM dataset and leads of a retired LinkedIn dataset stay out."""
    active = await _leads(session, world, world.linkedin, 3)
    retired = await _leads(session, world, world.linkedin_retired, 2)
    await _match(session, active[0], world.crm, "pending", None)
    await _match(session, active[1], world.other_crm, "pending", None)
    await _match(session, active[2], world.other_crm, "no_match", _at(-1), attempts=1)
    await _match(session, active[0], world.other_crm, "error", _at(-2), attempts=1)
    await _match(session, retired[0], world.crm, "pending", None)
    await _match(session, retired[1], world.crm, "no_match", _at(-1), attempts=1)

    assert _claimed(await _claim(session, world.crm, NOW, None)) == [(active[0], "pending")]
    assert _claimed(await _claim(session, world.other_crm, NOW, None)) == [
        (active[1], "pending"),
        (active[0], "error"),
        (active[2], "no_match"),
    ]


@pytest.mark.db
@pytest.mark.anyio
async def test_the_linkedin_dataset_filter_narrows_the_claim(
    session: AsyncSession, world: World
) -> None:
    """With a LinkedIn dataset named, only its leads are claimed; without one, every live one."""
    active = await _leads(session, world, world.linkedin, 1)
    building = await _leads(session, world, world.linkedin_building, 1)
    await _match(session, active[0], world.crm, "pending", None)
    await _match(session, building[0], world.crm, "no_match", _at(-1), attempts=1)

    assert _claimed(await _claim(session, world.crm, NOW, None)) == [
        (active[0], "pending"),
        (building[0], "no_match"),
    ]
    assert _claimed(await _claim(session, world.crm, NOW, world.linkedin_building)) == [
        (building[0], "no_match")
    ]
    assert _claimed(await _claim(session, world.crm, NOW, world.linkedin)) == [
        (active[0], "pending")
    ]


@pytest.mark.db
@pytest.mark.anyio
async def test_the_claim_takes_at_most_one_batch_from_the_front_of_the_order(
    session: AsyncSession, world: World
) -> None:
    """``BATCH_SIZE`` rows come back, the first ones in order; the rest wait for a later batch."""
    lead = await _leads(session, world, world.linkedin, BATCH_SIZE + 6)
    for lead_id in lead[1:]:
        await _match(session, lead_id, world.crm, "pending", None)
    await _match(session, lead[0], world.crm, "no_match", _at(-24), attempts=3)

    claims = await _claim(session, world.crm, NOW, None)

    assert len(claims) == BATCH_SIZE
    assert _claimed(claims) == [(lead_id, "pending") for lead_id in lead[1 : BATCH_SIZE + 1]]


@pytest.mark.db
@pytest.mark.anyio
async def test_the_claim_returns_nothing_when_no_row_is_due(
    session: AsyncSession, world: World
) -> None:
    """Only terminal, stopped and future rows left: the claim is empty and the run ends."""
    lead = await _leads(session, world, world.linkedin, 4)
    await _match(session, lead[0], world.crm, "no_email", None, reason="form map mismatch")
    await _match(session, lead[1], world.crm, "no_match", None, attempts=9)
    await _match(session, lead[2], world.crm, "error", _at(6), attempts=2)
    await _match(session, lead[3], world.crm, "pending", _at(1))

    assert await _claim(session, world.crm, NOW, None) == []


# ---------------------------------------------------------------------------------------
# The catalog: identical claim index from models and migration; the due index stays
# ---------------------------------------------------------------------------------------


def _indexdef(connection: Connection, schema: str, name: str) -> str:
    """Return ``pg_indexes.indexdef`` of ``lead_match``'s index ``name``, schema prefix removed."""
    definition: str = connection.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = :schema AND tablename = 'lead_match' AND indexname = :name"
        ),
        {"schema": schema, "name": name},
    ).scalar_one()
    return definition.replace(f" ON {schema}.", " ON ")


@pytest.mark.db
def test_the_models_and_the_migration_create_the_identical_claim_index(
    database: Database,
) -> None:
    """Both schemas hold ``ix_lead_match_claim`` exactly, and keep ``ix_lead_match_due``."""
    with database.models.connect() as models, database.migration.connect() as migration:
        for connection, schema in (
            (models, database.models_schema),
            (migration, database.migration_schema),
        ):
            assert _indexdef(connection, schema, CLAIM_INDEX) == CLAIM_INDEXDEF, schema
            assert _indexdef(connection, schema, DUE_INDEX) == DUE_INDEXDEF, schema


def test_the_migration_repeats_the_claimable_predicate_verbatim() -> None:
    """The frozen revision's literal is the model's text, character for character."""
    assert _migration_module().LEAD_MATCH_CLAIMABLE_SQL == crm.LEAD_MATCH_CLAIMABLE_SQL


# ---------------------------------------------------------------------------------------
# The plan: the claim reads ix_lead_match_claim in order, custom and generic plans alike
# ---------------------------------------------------------------------------------------


async def _claim_statement(
    session: AsyncSession, world: World, lead_dataset_id: int | None
) -> tuple[str, dict[str, Any]]:
    """Run the claim once and return the SQL and parameters it handed the driver."""
    captured: list[tuple[str, dict[str, Any]]] = []

    def capture(
        _connection: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "FOR UPDATE" in statement:
            captured.append((statement, dict(parameters)))

    sync_connection = (await session.connection()).sync_connection
    assert sync_connection is not None
    event.listen(sync_connection, "before_cursor_execute", capture)
    try:
        await _claim(session, world.crm, NOW, lead_dataset_id)
    finally:
        event.remove(sync_connection, "before_cursor_execute", capture)
    assert len(captured) == 1, captured
    return captured[0]


async def _restrict_planner(connection: AsyncConnection) -> None:
    """Disable every plan shape but an ordered index scan for this transaction.

    The tables are tiny, so a sequential or bitmap scan followed by a sort would otherwise
    win on cost. With those disabled, the planner can avoid a sort only by reading
    ``lead_match`` through an index whose order is the claim's and whose predicate it has
    proved the claim implies.
    """
    for setting in (
        "enable_seqscan",
        "enable_bitmapscan",
        "enable_sort",
        "enable_incremental_sort",
    ):
        await connection.exec_driver_sql(f"SET LOCAL {setting} = off")


def _nodes(plan: Any) -> list[dict[str, Any]]:
    """Return every node of a ``FORMAT JSON`` plan tree, parents before children."""
    nodes: list[dict[str, Any]] = []
    pending = [plan[0]["Plan"]]
    while pending:
        node = pending.pop(0)
        nodes.append(node)
        pending.extend(node.get("Plans", []))
    return nodes


def _assert_reads_the_claim_index_in_order(plan: Any) -> dict[str, Any]:
    """Assert the plan locks and limits an ordered ``ix_lead_match_claim`` scan, unsorted.

    Returns:
        The plan node that reads ``lead_match``.
    """
    nodes = _nodes(plan)
    kinds = [node["Node Type"] for node in nodes]
    assert kinds[:2] == ["Limit", "LockRows"], kinds
    assert not {"Sort", "Incremental Sort"} & set(kinds), kinds
    reads = [node for node in nodes if node.get("Relation Name") == "lead_match"]
    assert [(node["Node Type"], node.get("Index Name")) for node in reads] == [
        ("Index Scan", CLAIM_INDEX)
    ], reads
    assert reads[0]["Scan Direction"] == "Forward", reads
    assert not any(node.get("Disabled") for node in nodes), nodes
    return reads[0]


def _literal(value: Any) -> str:
    """Render one bound value as a SQL literal for ``EXECUTE``, which takes no parameters."""
    if isinstance(value, datetime):
        assert value.tzinfo is not None, value
        return f"'{value.isoformat()}'"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    assert isinstance(value, str), value
    return "'" + value.replace("'", "''") + "'"


def _sql_type(value: Any) -> str:
    """Return the PostgreSQL type a bound value is prepared with."""
    if isinstance(value, datetime):
        return "timestamptz"
    if isinstance(value, int) and not isinstance(value, bool):
        return "bigint"
    assert isinstance(value, str), value
    return "text"


@pytest.mark.db
@pytest.mark.anyio
@pytest.mark.parametrize("narrowed", [False, True], ids=["all-leads", "one-linkedin-dataset"])
async def test_the_bound_claim_reads_the_claim_index_in_order(
    session: AsyncSession, world: World, narrowed: bool
) -> None:
    """With its values bound as the driver binds them, the claim stops at its limit unsorted."""
    lead_dataset_id = world.linkedin if narrowed else None
    sql, params = await _claim_statement(session, world, lead_dataset_id)
    assert sql.endswith("FOR UPDATE OF lead_match SKIP LOCKED"), sql
    assert "ORDER BY lead_match.next_attempt_at ASC NULLS FIRST, lead_match.lead_id" in sql
    connection = await session.connection()
    await _restrict_planner(connection)

    plan = (await connection.exec_driver_sql("EXPLAIN (FORMAT JSON) " + sql, params)).scalar_one()

    _assert_reads_the_claim_index_in_order(plan)


@pytest.mark.db
@pytest.mark.anyio
@pytest.mark.parametrize("narrowed", [False, True], ids=["all-leads", "one-linkedin-dataset"])
async def test_the_generic_prepared_claim_reads_the_claim_index_in_order(
    session: AsyncSession, world: World, narrowed: bool
) -> None:
    """A prepared statement's generic plan, with no value known, still uses the claim index.

    psycopg prepares a statement server-side once it repeats, and PostgreSQL may then run a
    generic plan. The partial predicate is proved there because the claim sends it as literal
    SQL; status tests bound as parameters could not be matched against it.
    """
    lead_dataset_id = world.linkedin if narrowed else None
    sql, params = await _claim_statement(session, world, lead_dataset_id)
    names = re.findall(r"%\((\w+)\)s", sql)
    assert sorted(names) == sorted(params), (names, params)
    for position, name in enumerate(names, start=1):
        sql = sql.replace(f"%({name})s", f"${position}", 1)
    types = ", ".join(_sql_type(params[name]) for name in names)
    values = ", ".join(_literal(params[name]) for name in names)
    connection = await session.connection()
    await _restrict_planner(connection)
    await connection.exec_driver_sql("SET LOCAL plan_cache_mode = force_generic_plan")
    statement = f"lead_match_claim_{uuid.uuid4().hex}"
    await connection.exec_driver_sql(f"PREPARE {statement} ({types}) AS {sql}")
    try:
        plan = (
            await connection.exec_driver_sql(
                f"EXPLAIN (FORMAT JSON) EXECUTE {statement} ({values})"
            )
        ).scalar_one()
    finally:
        await connection.exec_driver_sql(f"DEALLOCATE {statement}")

    read = _assert_reads_the_claim_index_in_order(plan)
    # A generic plan keeps the parameter symbol where a custom plan would show the value.
    crm_position = names.index(next(name for name in names if name.startswith("crm_dataset_id")))
    assert f"${crm_position + 1}" in read["Index Cond"], read
