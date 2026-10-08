"""Tests that entity, snapshot and decision rows reference only parents of their own dataset.

AAP 0.3.3 lets only ``lead_match`` link two datasets, and AAP 0.5.2 makes every foreign key
between rows of one dataset ``ON DELETE CASCADE``. The twelve parent references between the
entity tables (``campaign_group`` and ``campaign`` to ``ad_account``, ``campaign`` to
``campaign_group`` and ``budget``, ``ad_group`` and ``creative`` to ``campaign``,
``keyword`` to ``ad_group``, ``lead_form_version`` to ``lead_form``), the ``budget_id`` keys
of ``entity_snapshot`` and ``decision``, and ``discrepancy``'s keys to ``entity_snapshot``
and ``decision`` are therefore composite foreign keys (``<column>``, ``dataset_id``) to the
parent's unique (``dataset_id``, ``id``). These tests show, on real PostgreSQL, that the
catalog holds exactly those constraints, that a row cannot reference another dataset's
parent (also through ``db/repositories.upsert_entities``), that a ``NULL`` in a nullable key
is accepted, and that purging a retired dataset never deletes an active dataset's rows.

The tests run against the PostgreSQL 18 test database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``). The module creates a private schema, builds every table
of the model metadata in it, points every connection's ``search_path`` at it alone, and
drops it with ``CASCADE`` at teardown, so the ``public`` schema and every other schema stay
untouched. Each test runs inside one transaction that is rolled back afterwards. An
unreachable database fails the tests; it never skips them.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from psycopg import errors as pg_errors
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import (
    AdAccount,
    AdGroup,
    Budget,
    Campaign,
    CampaignGroup,
    Creative,
    Keyword,
    LeadForm,
    LeadFormVersion,
)
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.repositories import upsert_entities

ACTIVE = "active"
"""Label of the seeded dataset each source currently reads."""

RETIRED = "retired"
"""Label of the seeded dataset each source has retired and will purge."""

AT = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The observation, decision and detection instant of every seeded row."""

EDGES: tuple[tuple[str, str, str, str], ...] = (
    ("campaign_group", "account_id", "ad_account", "linkedin"),
    ("campaign", "account_id", "ad_account", "google"),
    ("campaign", "campaign_group_id", "campaign_group", "linkedin"),
    ("campaign", "budget_id", "budget", "google"),
    ("ad_group", "campaign_id", "campaign", "google"),
    ("keyword", "ad_group_id", "ad_group", "google"),
    ("creative", "campaign_id", "campaign", "linkedin"),
    ("lead_form_version", "lead_form_id", "lead_form", "linkedin"),
    ("entity_snapshot", "budget_id", "budget", "google"),
    ("decision", "budget_id", "budget", "google"),
    ("discrepancy", "snapshot_id", "entity_snapshot", "google"),
    ("discrepancy", "decision_id", "decision", "google"),
)
"""Every dataset-qualified intra-dataset edge: child table, referencing column, parent table,
and the source whose seeded datasets exercise it."""

EDGE_IDS: list[str] = [f"{table}.{column}" for table, column, _, _ in EDGES]

NULLABLE_EDGES: tuple[tuple[str, str, str, str], ...] = tuple(
    edge
    for edge in EDGES
    if (edge[0], edge[1])
    in {
        ("campaign", "campaign_group_id"),
        ("campaign", "budget_id"),
        ("entity_snapshot", "budget_id"),
        ("decision", "budget_id"),
        ("discrepancy", "snapshot_id"),
        ("discrepancy", "decision_id"),
    }
)
"""The edges whose referencing column is nullable: ``MATCH SIMPLE`` leaves a ``NULL``
unchecked."""

PARENT_COLUMNS: dict[str, str] = {
    "ad_account": "account_id",
    "campaign_group": "campaign_group_id",
    "budget": "budget_id",
    "campaign": "campaign_id",
    "ad_group": "ad_group_id",
    "lead_form": "lead_form_id",
    "entity_snapshot": "snapshot_id",
    "decision": "decision_id",
}
"""Each parent table and the key, in a seeded dataset's refs, of the parent row's id."""

NEW_PARENT_UNIQUES: tuple[str, ...] = (
    "ad_account",
    "campaign_group",
    "budget",
    "lead_form",
    "entity_snapshot",
    "decision",
)
"""The parents whose (``dataset_id``, ``id``) unique this change adds."""

ANCHORED_PARENTS: tuple[str, ...] = (*NEW_PARENT_UNIQUES, "campaign", "ad_group")
"""Every parent table of :data:`EDGES`."""

SEEDED_TABLES: dict[str, tuple[str, ...]] = {
    "google": (
        "ad_account",
        "budget",
        "campaign",
        "ad_group",
        "keyword",
        "entity_snapshot",
        "decision",
        "discrepancy",
    ),
    "linkedin": (
        "ad_account",
        "campaign_group",
        "campaign",
        "creative",
        "lead_form",
        "lead_form_version",
    ),
}
"""The tables :func:`_seed_dataset` fills for each source, in insertion order."""

_FOREIGN_KEYS_SQL = sa.text(
    """
    SELECT con.conname,
           rel.relname,
           ARRAY(
               SELECT att.attname::text
               FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
               JOIN pg_attribute AS att
                 ON att.attrelid = con.conrelid AND att.attnum = k.attnum
               ORDER BY k.ord
           ),
           ref.relname,
           ARRAY(
               SELECT att.attname::text
               FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
               JOIN pg_attribute AS att
                 ON att.attrelid = con.confrelid AND att.attnum = k.attnum
               ORDER BY k.ord
           ),
           con.confdeltype::text,
           con.confmatchtype::text
    FROM pg_constraint AS con
    JOIN pg_class AS rel ON rel.oid = con.conrelid
    JOIN pg_namespace AS nsp ON nsp.oid = rel.relnamespace
    JOIN pg_class AS ref ON ref.oid = con.confrelid
    WHERE con.contype = 'f' AND nsp.nspname = :schema
    """
)
"""Every foreign key in the private schema: name, table, columns, referred table, referred
columns, ``confdeltype`` (``c`` is ``ON DELETE CASCADE``, ``n`` is ``SET NULL``) and
``confmatchtype`` (``s`` is ``MATCH SIMPLE``)."""

_UNIQUES_SQL = sa.text(
    """
    SELECT con.conname,
           rel.relname,
           ARRAY(
               SELECT att.attname::text
               FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
               JOIN pg_attribute AS att
                 ON att.attrelid = con.conrelid AND att.attnum = k.attnum
               ORDER BY k.ord
           )
    FROM pg_constraint AS con
    JOIN pg_class AS rel ON rel.oid = con.conrelid
    JOIN pg_namespace AS nsp ON nsp.oid = rel.relnamespace
    WHERE con.contype = 'u' AND nsp.nspname = :schema
    """
)
"""Every unique constraint in the private schema: name, table and columns in key order."""

type ForeignKeyRow = tuple[tuple[str, ...], str, tuple[str, ...], str, str]


@dataclass(frozen=True)
class SeededDataset:
    """One seeded ``source_dataset`` row and the ids of its rows.

    Attributes:
        id: The ``source_dataset.id``.
        source: The dataset's source, which every seeded row of the dataset carries.
        refs: The id of each seeded parent row, keyed by the column children reference it
            by (:data:`PARENT_COLUMNS`), plus ``subject_id`` for the decision subject.
        rows: The ids of every seeded row, keyed by table.
    """

    id: int
    source: str
    refs: Mapping[str, int]
    rows: Mapping[str, frozenset[int]]


type World = dict[tuple[str, str], SeededDataset]


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the entity lineage tests"
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
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[Engine, URL, str]]:
    """Create a private schema holding every table of the metadata; drop it afterwards.

    Yields:
        An engine whose connections see only the private schema, the database URL and the
        schema's name.
    """
    url = _database_url()
    schema = f"entity_lineage_test_{uuid.uuid4().hex}"
    engine = sa.create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection)
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield engine, url, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
def connection(database: tuple[Engine, URL, str]) -> Iterator[Connection]:
    """Yield a connection inside a transaction that is rolled back after the test."""
    engine, _, _ = database
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
async def session(database: tuple[Engine, URL, str]) -> AsyncIterator[AsyncSession]:
    """Yield an async session whose work is rolled back, on an engine of this test's loop."""
    _, url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            try:
                yield session
            finally:
                await session.rollback()
    finally:
        await engine.dispose()


def _table(name: str) -> sa.Table:
    """Return the ``Base.metadata`` table named ``name``."""
    return Base.metadata.tables[name]


def _insert(connection: Connection, table_name: str, values: Mapping[str, object]) -> int:
    """Insert one row into ``table_name`` and return its generated ``id``."""
    table = _table(table_name)
    return int(connection.execute(sa.insert(table).values(values).returning(table.c.id)).one()[0])


def _child_row(
    table_name: str,
    dataset: SeededDataset,
    refs: Mapping[str, int | None],
    offset: int = 0,
) -> dict[str, object]:
    """Build a valid ``table_name`` row in ``dataset`` whose parent keys are taken from ``refs``.

    ``offset`` moves the row's natural key (its external id, version or discrepancy kind),
    so a second row of the same table never collides with the first on a unique constraint
    before its foreign keys are checked. Every value satisfies the table's ``CHECK``
    constraints, including ``ck_decision_shape_columns``: a decision with a ``budget_id`` is
    a ``shared_member`` row, one without is ``own_budget``.
    """
    row: dict[str, object] = {"source": dataset.source, "dataset_id": dataset.id}
    if table_name == "ad_account":
        row.update(external_id=f"{dataset.source}-account-{offset}", name="Account", currency="USD")
    elif table_name == "budget":
        row.update(
            external_id=f"budget-{offset}",
            amount=Decimal("50.000000"),
            amount_micros=50_000_000,
            period="DAILY",
            explicitly_shared=True,
        )
    elif table_name == "campaign_group":
        row.update(
            account_id=refs["account_id"],
            external_id=f"group-{offset}",
            name="Group",
            status="ACTIVE",
        )
    elif table_name == "campaign":
        google = dataset.source == "google"
        row.update(
            account_id=refs["account_id"],
            external_id=f"{dataset.source}-campaign-{offset}",
            name="Campaign",
            status="active",
            raw_status="ENABLED" if google else "ACTIVE",
            campaign_group_id=refs.get("campaign_group_id"),
            budget_id=refs.get("budget_id"),
            daily_budget=Decimal("50.000000"),
            budget_period="DAILY" if google else None,
        )
    elif table_name == "ad_group":
        row.update(
            campaign_id=refs["campaign_id"],
            external_id=f"ad-group-{offset}",
            name="Ad group",
            status="ENABLED",
        )
    elif table_name == "keyword":
        row.update(
            ad_group_id=refs["ad_group_id"],
            criterion_id=str(1001 + offset),
            text="crm software",
            match_type="PHRASE",
            status="ENABLED",
            quality_score=7,
        )
    elif table_name == "creative":
        row.update(
            campaign_id=refs["campaign_id"],
            urn=f"urn:li:sponsoredCreative:{offset}",
            intended_status="ACTIVE",
        )
    elif table_name == "lead_form":
        row.update(canonical_urn=f"urn:li:leadGenForm:{offset}", name="Form", current_version=1)
    elif table_name == "lead_form_version":
        row.update(lead_form_id=refs["lead_form_id"], version=1 + offset, first_seen_at=AT)
    elif table_name == "entity_snapshot":
        row.update(
            entity_type="campaign",
            entity_id=refs["subject_id"],
            captured_at=AT + timedelta(hours=offset),
            status="active",
            budget_id=refs.get("budget_id"),
        )
    elif table_name == "decision":
        budget_id = refs.get("budget_id")
        row.update(
            subject_type="campaign",
            subject_id=refs["subject_id"],
            shape="own_budget" if budget_id is None else "shared_member",
            status="active",
            budget_id=budget_id,
            decided_at=AT + timedelta(hours=offset),
            origin="baseline",
        )
    elif table_name == "discrepancy":
        row.update(
            subject_type="campaign",
            subject_id=refs["subject_id"],
            kind=("status", "budget", "untracked", "missing")[offset],
            decided_value={"status": "active"},
            platform_value={"status": "paused"},
            snapshot_id=refs.get("snapshot_id"),
            decision_id=refs.get("decision_id"),
            detected_at=AT,
            state="open",
        )
    else:
        raise AssertionError(f"no row builder for {table_name}")
    return row


def _seed_dataset(connection: Connection, source: str, state: str) -> SeededDataset:
    """Insert a primary dataset of ``source`` in ``state`` with one row of each seeded table.

    A Google dataset gets an account, a shared budget, a campaign drawing from it, an ad
    group, a keyword, a campaign snapshot naming the budget, a ``shared_member`` decision and
    a discrepancy citing both. A LinkedIn dataset gets an account, a campaign group, a
    campaign in it, a creative, a lead form and one form version. Every reference stays
    inside the new dataset.
    """
    dataset_id = _insert(
        connection,
        "source_dataset",
        {"source": source, "mode": "simulated", "state": state, "role": "primary"},
    )
    refs: dict[str, int] = {}
    rows: dict[str, frozenset[int]] = {}
    shell = SeededDataset(id=dataset_id, source=source, refs=refs, rows=rows)
    for table_name in SEEDED_TABLES[source]:
        new_id = _insert(connection, table_name, _child_row(table_name, shell, refs))
        rows[table_name] = frozenset({new_id})
        if table_name in PARENT_COLUMNS:
            refs[PARENT_COLUMNS[table_name]] = new_id
        if table_name == "campaign":
            refs["subject_id"] = new_id
    return SeededDataset(id=dataset_id, source=source, refs=dict(refs), rows=dict(rows))


@pytest.fixture
def world(connection: Connection) -> World:
    """Seed an active and a retired dataset for Google and for LinkedIn."""
    return {
        (source, label): _seed_dataset(connection, source, label)
        for source in ("google", "linkedin")
        for label in (ACTIVE, RETIRED)
    }


def _row_ids(connection: Connection, table_name: str, dataset_id: int) -> frozenset[int]:
    """Return the ids of the ``table_name`` rows in ``dataset_id``."""
    table = _table(table_name)
    statement = sa.select(table.c.id).where(table.c.dataset_id == dataset_id)
    return frozenset(connection.scalars(statement))


def _purge(connection: Connection, dataset_id: int) -> None:
    """Purge one dataset exactly as the nightly cleanup does: delete its ``source_dataset`` row."""
    source_dataset = _table("source_dataset")
    connection.execute(sa.delete(source_dataset).where(source_dataset.c.id == dataset_id))


def _foreign_keys(connection: Connection, schema: str) -> dict[str, dict[str, ForeignKeyRow]]:
    """Return every foreign key of the private schema, keyed by table and constraint name."""
    found: dict[str, dict[str, ForeignKeyRow]] = {}
    for name, table, columns, referred, referred_columns, action, match in connection.execute(
        _FOREIGN_KEYS_SQL, {"schema": schema}
    ):
        found.setdefault(table, {})[name] = (
            tuple(columns),
            referred,
            tuple(referred_columns),
            action,
            match,
        )
    return found


@pytest.mark.db
def test_every_intra_dataset_key_is_a_dataset_qualified_cascade(
    connection: Connection, database: tuple[Engine, URL, str]
) -> None:
    """Each child table holds exactly its ``source_dataset`` key, its composite parent keys
    (named after the parent column, ``ON DELETE CASCADE``, ``MATCH SIMPLE``) and, on
    ``decision``, the ``SET NULL`` key into run history: no single-column parent key is
    left."""
    _, _, schema = database
    found = _foreign_keys(connection, schema)

    for child in sorted({table for table, _, _, _ in EDGES}):
        expected: dict[str, ForeignKeyRow] = {
            f"fk_{child}_dataset_id_source_dataset": (
                ("dataset_id",),
                "source_dataset",
                ("id",),
                "c",
                "s",
            ),
        }
        for table, column, parent, _ in EDGES:
            if table == child:
                expected[f"fk_{child}_{column}_{parent}"] = (
                    (column, "dataset_id"),
                    parent,
                    ("id", "dataset_id"),
                    "c",
                    "s",
                )
        if child == "decision":
            expected["fk_decision_recommendation_id_recommendation"] = (
                ("recommendation_id",),
                "recommendation",
                ("id",),
                "n",
                "s",
            )
        assert found[child] == expected, child


@pytest.mark.db
def test_every_parent_declares_the_dataset_id_id_unique(
    connection: Connection, database: tuple[Engine, URL, str]
) -> None:
    """Each parent of an edge carries ``uq_<table>_dataset_id_id`` on (``dataset_id``,
    ``id``), the key its children's composite foreign keys reference."""
    _, _, schema = database
    found: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, table, columns in connection.execute(_UNIQUES_SQL, {"schema": schema}):
        found.setdefault(table, {})[name] = tuple(columns)

    for parent in ANCHORED_PARENTS:
        assert found[parent][f"uq_{parent}_dataset_id_id"] == ("dataset_id", "id"), parent


@pytest.mark.db
@pytest.mark.parametrize(("table_name", "column", "parent", "source"), EDGES, ids=EDGE_IDS)
def test_a_parent_of_another_dataset_is_rejected(
    connection: Connection,
    world: World,
    table_name: str,
    column: str,
    parent: str,
    source: str,
) -> None:
    """A same-dataset reference inserts; the same row pointing at the retired dataset's
    parent fails with a foreign-key violation (23503) of the edge's composite key."""
    active = world[source, ACTIVE]
    retired = world[source, RETIRED]

    accepted = _insert(
        connection, table_name, _child_row(table_name, active, active.refs, offset=1)
    )
    assert _row_ids(connection, table_name, active.id) == active.rows[table_name] | {accepted}

    foreign = {**active.refs, column: retired.refs[column]}
    statement = sa.insert(_table(table_name)).values(
        _child_row(table_name, active, foreign, offset=2)
    )
    with pytest.raises(IntegrityError) as caught, connection.begin_nested():
        connection.execute(statement)

    violation = caught.value.orig
    assert isinstance(violation, pg_errors.ForeignKeyViolation)
    assert violation.sqlstate == "23503"
    assert violation.diag.constraint_name == f"fk_{table_name}_{column}_{parent}"
    assert _row_ids(connection, table_name, active.id) == active.rows[table_name] | {accepted}


@pytest.mark.db
@pytest.mark.parametrize(
    ("table_name", "column", "parent", "source"),
    NULLABLE_EDGES,
    ids=[f"{table}.{column}" for table, column, _, _ in NULLABLE_EDGES],
)
def test_a_null_parent_is_accepted(
    connection: Connection,
    world: World,
    table_name: str,
    column: str,
    parent: str,
    source: str,
) -> None:
    """A nullable key stays nullable: ``MATCH SIMPLE`` leaves a ``NULL`` unchecked, so the
    row inserts with that column ``NULL``."""
    active = world[source, ACTIVE]
    values = _child_row(table_name, active, {**active.refs, column: None}, offset=1)
    assert values[column] is None

    row_id = _insert(connection, table_name, values)

    table = _table(table_name)
    stored = connection.execute(sa.select(table.c[column]).where(table.c.id == row_id)).one()
    assert stored[0] is None


@pytest.mark.db
def test_purging_a_retired_dataset_keeps_every_active_row(
    connection: Connection, world: World
) -> None:
    """Purging the retired datasets removes every one of their rows and leaves every row of
    the active datasets intact, in every table the twelve edges connect."""
    for source in ("google", "linkedin"):
        _purge(connection, world[source, RETIRED].id)

    for (source, label), dataset in world.items():
        for table_name in SEEDED_TABLES[source]:
            expected = dataset.rows[table_name] if label == ACTIVE else frozenset()
            assert _row_ids(connection, table_name, dataset.id) == expected, (
                source,
                label,
                table_name,
            )


@pytest.mark.db
def test_deleting_a_parent_cascades_only_within_its_dataset(
    connection: Connection, world: World
) -> None:
    """Deleting the active Google budget removes, through the composite keys, the campaign
    drawing from it with its ad group, keyword, snapshot, decision and discrepancy, and
    nothing of the retired dataset."""
    budget = _table("budget")
    active = world["google", ACTIVE]
    retired = world["google", RETIRED]

    connection.execute(sa.delete(budget).where(budget.c.id == active.refs["budget_id"]))

    assert _row_ids(connection, "ad_account", active.id) == active.rows["ad_account"]
    for table_name in SEEDED_TABLES["google"]:
        if table_name != "ad_account":
            assert _row_ids(connection, table_name, active.id) == frozenset(), table_name
        assert _row_ids(connection, table_name, retired.id) == retired.rows[table_name]


# ---------------------------------------------------------------------------------------
# db/repositories.upsert_entities: the write path of every entity sync
# ---------------------------------------------------------------------------------------

UPSERT_EDGES: tuple[tuple[type[Base], str, str], ...] = (
    (CampaignGroup, "account_id", "ad_account"),
    (Campaign, "account_id", "ad_account"),
    (Campaign, "campaign_group_id", "campaign_group"),
    (Campaign, "budget_id", "budget"),
    (AdGroup, "campaign_id", "campaign"),
    (Keyword, "ad_group_id", "ad_group"),
    (Creative, "campaign_id", "campaign"),
    (LeadFormVersion, "lead_form_id", "lead_form"),
)
"""The edges an entity sync writes through ``upsert_entities``: model, column, parent."""


async def _dataset(session: AsyncSession, state: str) -> int:
    """Insert a writable primary Google dataset (``active`` or ``building``) and return its id."""
    dataset = SourceDataset(source="google", mode="simulated", state=state, role="primary")
    session.add(dataset)
    await session.flush()
    return dataset.id


async def _upsert_one(
    session: AsyncSession, model: type[Base], row: Mapping[str, Any], dataset_id: int
) -> int:
    """Upsert one entity row into ``dataset_id`` and return its id."""
    ids = await upsert_entities(session, model, [row], dataset_id)
    assert len(ids) == 1
    return next(iter(ids.values()))


async def _parents(session: AsyncSession, dataset_id: int) -> dict[str, int]:
    """Write one row of every parent table into ``dataset_id`` through ``upsert_entities``.

    Returns:
        The id of each parent row, keyed by the column its children reference it by.
    """
    refs: dict[str, int] = {}
    refs["account_id"] = await _upsert_one(
        session,
        AdAccount,
        {"external_id": "1234567890", "name": "Account", "currency": "USD"},
        dataset_id,
    )
    refs["budget_id"] = await _upsert_one(
        session,
        Budget,
        {
            "external_id": "9001",
            "amount": Decimal("50.000000"),
            "amount_micros": 50_000_000,
            "period": "DAILY",
            "explicitly_shared": True,
        },
        dataset_id,
    )
    refs["campaign_group_id"] = await _upsert_one(
        session,
        CampaignGroup,
        {
            "account_id": refs["account_id"],
            "external_id": "group",
            "name": "Group",
            "status": "ACTIVE",
        },
        dataset_id,
    )
    refs["campaign_id"] = await _upsert_one(
        session, Campaign, _upsert_row(Campaign, refs, "parent"), dataset_id
    )
    refs["ad_group_id"] = await _upsert_one(
        session, AdGroup, _upsert_row(AdGroup, refs, "parent"), dataset_id
    )
    refs["lead_form_id"] = await _upsert_one(
        session,
        LeadForm,
        {"canonical_urn": "urn:li:leadGenForm:1", "name": "Form", "current_version": 1},
        dataset_id,
    )
    return refs


def _upsert_row(model: type[Base], refs: Mapping[str, int], key: str) -> dict[str, Any]:
    """Build an upsert row of ``model`` whose parent keys come from ``refs``.

    ``key`` makes the row's natural key distinct from the other rows written in the test.
    """
    if model is CampaignGroup:
        return {
            "account_id": refs["account_id"],
            "external_id": f"group-{key}",
            "name": "Group",
            "status": "ACTIVE",
        }
    if model is Campaign:
        return {
            "account_id": refs["account_id"],
            "external_id": f"campaign-{key}",
            "name": "Campaign",
            "status": "active",
            "raw_status": "ENABLED",
            "campaign_group_id": refs["campaign_group_id"],
            "budget_id": refs["budget_id"],
            "daily_budget": Decimal("50.000000"),
            "budget_period": "DAILY",
        }
    if model is AdGroup:
        return {
            "campaign_id": refs["campaign_id"],
            "external_id": f"ad-group-{key}",
            "name": "Ad group",
            "status": "ENABLED",
        }
    if model is Keyword:
        return {
            "ad_group_id": refs["ad_group_id"],
            "criterion_id": f"criterion-{key}",
            "text": "crm software",
            "match_type": "PHRASE",
            "status": "ENABLED",
        }
    if model is Creative:
        return {
            "campaign_id": refs["campaign_id"],
            "urn": f"urn:li:sponsoredCreative:{key}",
            "intended_status": "ACTIVE",
        }
    if model is LeadFormVersion:
        return {
            "lead_form_id": refs["lead_form_id"],
            "version": 2 if key == "own" else 3,
            "first_seen_at": AT,
        }
    raise AssertionError(f"no upsert row builder for {model.__name__}")


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("model", "column", "parent"),
    UPSERT_EDGES,
    ids=[f"{model.__tablename__}.{column}" for model, column, _ in UPSERT_EDGES],
)
async def test_upsert_entities_rejects_a_parent_of_another_dataset(
    session: AsyncSession, model: type[Base], column: str, parent: str
) -> None:
    """``upsert_entities`` checks only the written row's own provenance; the composite key
    makes the database reject a parent id that belongs to another dataset, so an entity
    sync can never link a building dataset's rows to the active dataset's parents."""
    active = await _dataset(session, "active")
    building = await _dataset(session, "building")
    active_refs = await _parents(session, active)
    building_refs = await _parents(session, building)

    own = await _upsert_one(session, model, _upsert_row(model, building_refs, "own"), building)
    table = model.__table__
    assert isinstance(table, sa.Table)
    stored = (await session.execute(sa.select(table.c[column]).where(table.c.id == own))).one()
    assert stored[0] == building_refs[column]

    foreign = {**building_refs, column: active_refs[column]}
    with pytest.raises(IntegrityError) as caught:
        async with session.begin_nested():
            await upsert_entities(
                session, model, [_upsert_row(model, foreign, "foreign")], building
            )

    violation = caught.value.orig
    assert isinstance(violation, pg_errors.ForeignKeyViolation)
    assert violation.sqlstate == "23503"
    assert violation.diag.constraint_name == f"fk_{model.__tablename__}_{column}_{parent}"
