"""Tests that fact and lead rows reference only entities of their own dataset.

AAP 0.3.3 lets only ``lead_match`` link two datasets, and AAP 0.5.2 makes every foreign key
between rows of one dataset ``ON DELETE CASCADE``. Each entity reference of the five fact
tables and of ``lead`` is therefore a composite foreign key (``<entity>_id``,
``dataset_id``) to the entity's unique (``dataset_id``, ``id``). These tests show, on real
PostgreSQL, that a row cannot reference another dataset's entity, that purging one dataset
never deletes another dataset's rows, and that the catalog holds exactly the planned
constraint names, columns and delete actions.

The tests run against the PostgreSQL 18 test database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``). The module creates a private schema, builds only the
tables these tests touch in it, points every connection's ``search_path`` at it alone, and
drops it with ``CASCADE`` at teardown, so the ``public`` schema and every other schema stay
untouched. Each test runs inside one transaction that is rolled back afterwards. An
unreachable database fails the tests; it never skips them.
"""

import os
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from psycopg import errors as pg_errors
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, IntegrityError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

# Importing the model modules registers their tables on ``Base.metadata``.
from ads_engine.db.base import Base
from ads_engine.db.models import accounts, crm, facts, ops

ACTIVE = "active"
"""Label of the seeded dataset each source currently reads."""

RETIRED = "retired"
"""Label of the seeded dataset each source has retired and will purge."""

DAY = date(2026, 9, 30)
"""The native day of every seeded daily fact; variants are whole days after it."""

FETCHED_AT = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""When every seeded fact was written, and the submission instant of every seeded lead."""

TABLES: tuple[str, ...] = (
    ops.SourceDataset.__tablename__,
    accounts.AdAccount.__tablename__,
    accounts.CampaignGroup.__tablename__,
    accounts.Budget.__tablename__,
    accounts.Campaign.__tablename__,
    accounts.AdGroup.__tablename__,
    accounts.Keyword.__tablename__,
    accounts.Creative.__tablename__,
    facts.FactCampaignDaily.__tablename__,
    facts.FactCreativeDaily.__tablename__,
    facts.FactKeywordDaily.__tablename__,
    facts.FactSearchTermDaily.__tablename__,
    facts.FactSegmentWindow.__tablename__,
    crm.Lead.__tablename__,
)
"""The tables built in the private schema: the children, their entities and those entities'
own foreign-key targets."""

CHILD_SOURCES: dict[str, str] = {
    "fact_campaign_daily": "google",
    "fact_creative_daily": "linkedin",
    "fact_keyword_daily": "google",
    "fact_search_term_daily": "google",
    "fact_segment_window": "linkedin",
    "lead": "linkedin",
}
"""Each child table and the source whose rows it is seeded with."""

CHILD_REFERENCES: dict[str, tuple[str, ...]] = {
    "fact_campaign_daily": ("campaign_id",),
    "fact_creative_daily": ("creative_id",),
    "fact_keyword_daily": ("keyword_id",),
    "fact_search_term_daily": ("ad_group_id", "campaign_id"),
    "fact_segment_window": ("campaign_id",),
    "lead": ("campaign_id", "creative_id"),
}
"""The entity-referencing columns of each child table."""

EDGES: tuple[tuple[str, str, str, str], ...] = (
    ("fact_campaign_daily", "campaign_id", "campaign", "ix_fact_campaign_daily_campaign_date"),
    ("fact_creative_daily", "creative_id", "creative", "ix_fact_creative_daily_creative_date"),
    ("fact_keyword_daily", "keyword_id", "keyword", "ix_fact_keyword_daily_keyword_date"),
    (
        "fact_search_term_daily",
        "ad_group_id",
        "ad_group",
        "ix_fact_search_term_daily_ad_group_date",
    ),
    (
        "fact_search_term_daily",
        "campaign_id",
        "campaign",
        "ix_fact_search_term_daily_campaign_date",
    ),
    ("fact_segment_window", "campaign_id", "campaign", "ix_fact_segment_window_campaign_id"),
    ("lead", "campaign_id", "campaign", "ix_lead_campaign_id"),
    ("lead", "creative_id", "creative", "ix_lead_creative_id"),
)
"""Every dataset-qualified edge: child table, entity column, entity table, and the index
whose leading column serves the cascade from the entity table."""

EDGE_IDS: list[str] = [f"{table}.{column}" for table, column, _, _ in EDGES]

ENTITY_COLUMNS: dict[str, str] = {
    "campaign": "campaign_id",
    "ad_group": "ad_group_id",
    "keyword": "keyword_id",
    "creative": "creative_id",
}
"""Each referenced entity table and the column its children reference it by."""

PARENT_UNIQUES: dict[str, dict[str, tuple[str, ...]]] = {
    "campaign": {
        "uq_campaign_dataset_id_account_id_external_id": (
            "dataset_id",
            "account_id",
            "external_id",
        ),
        "uq_campaign_dataset_id_id": ("dataset_id", "id"),
    },
    "ad_group": {
        "uq_ad_group_dataset_id_campaign_id_external_id": (
            "dataset_id",
            "campaign_id",
            "external_id",
        ),
        "uq_ad_group_dataset_id_id": ("dataset_id", "id"),
    },
    "keyword": {
        "uq_keyword_dataset_id_ad_group_id_criterion_id": (
            "dataset_id",
            "ad_group_id",
            "criterion_id",
        ),
        "uq_keyword_dataset_id_id": ("dataset_id", "id"),
    },
    "creative": {
        "uq_creative_dataset_id_urn": ("dataset_id", "urn"),
        "uq_creative_dataset_id_id": ("dataset_id", "id"),
    },
}
"""The complete unique-constraint set of each entity table: its unchanged natural key plus the
(``dataset_id``, ``id``) key that anchors the dataset-qualified child foreign keys."""

CHILD_UNIQUES: dict[str, dict[str, tuple[str, ...]]] = {
    "fact_campaign_daily": {
        "uq_fact_campaign_daily_dataset_id_campaign_id_date": (
            "dataset_id",
            "campaign_id",
            "date",
        ),
    },
    "fact_creative_daily": {
        "uq_fact_creative_daily_dataset_id_creative_id_date": (
            "dataset_id",
            "creative_id",
            "date",
        ),
    },
    "fact_keyword_daily": {
        "uq_fact_keyword_daily_dataset_id_keyword_id_date": ("dataset_id", "keyword_id", "date"),
    },
    "fact_search_term_daily": {
        "uq_fact_search_term_daily_natural": (
            "dataset_id",
            "ad_group_id",
            "matched_criterion_id",
            "search_term",
            "date",
        ),
    },
    "fact_segment_window": {
        "uq_fact_segment_window_natural": (
            "dataset_id",
            "campaign_id",
            "facet",
            "value",
            "window_start",
            "window_end",
        ),
    },
    "lead": {"uq_lead_dataset_id_external_id": ("dataset_id", "external_id")},
}
"""The natural-key unique constraint of each child table, unchanged by the composite keys."""

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
           con.confdeltype::text
    FROM pg_constraint AS con
    JOIN pg_class AS rel ON rel.oid = con.conrelid
    JOIN pg_namespace AS nsp ON nsp.oid = rel.relnamespace
    JOIN pg_class AS ref ON ref.oid = con.confrelid
    WHERE con.contype = 'f' AND nsp.nspname = :schema
    """
)
"""Every foreign key in the private schema: name, table, columns, referred table, referred
columns and ``confdeltype`` (``c`` is ``ON DELETE CASCADE``)."""

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

_LEADING_INDEX_COLUMNS_SQL = sa.text(
    """
    SELECT rel.relname, idx.relname, att.attname::text
    FROM pg_index AS i
    JOIN pg_class AS rel ON rel.oid = i.indrelid
    JOIN pg_class AS idx ON idx.oid = i.indexrelid
    JOIN pg_namespace AS nsp ON nsp.oid = rel.relnamespace
    JOIN pg_attribute AS att ON att.attrelid = i.indrelid AND att.attnum = i.indkey[0]
    WHERE nsp.nspname = :schema
    """
)
"""Every index in the private schema with the column it leads with."""


@dataclass(frozen=True)
class SeededDataset:
    """One seeded ``source_dataset`` row and the ids of its entities.

    Attributes:
        id: The ``source_dataset.id``.
        source: The dataset's source, which every seeded row of the dataset carries.
        refs: The id of each seeded entity, keyed by the column children reference it by.
    """

    id: int
    source: str
    refs: Mapping[str, int]


type World = dict[tuple[str, str], SeededDataset]


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the dataset lineage tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[Engine, str]]:
    """Create a private schema holding only the lineage tables; drop it afterwards.

    Yields:
        An engine whose connections see only the private schema, and the schema's name.
    """
    url = _database_url()
    schema = f"lineage_test_{uuid.uuid4().hex}"
    engine = sa.create_engine(
        url,
        poolclass=NullPool,
        connect_args={"options": f"-c search_path={schema} -c TimeZone=UTC"},
    )
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(
                connection, tables=[Base.metadata.tables[name] for name in TABLES]
            )
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield engine, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
def connection(database: tuple[Engine, str]) -> Iterator[Connection]:
    """Yield a connection inside a transaction that is rolled back after the test."""
    engine, _ = database
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _table(name: str) -> sa.Table:
    """Return the ``Base.metadata`` table named ``name``."""
    return Base.metadata.tables[name]


def _insert(connection: Connection, table_name: str, values: Mapping[str, object]) -> int:
    """Insert one row into ``table_name`` and return its generated ``id``."""
    table = _table(table_name)
    return int(connection.execute(sa.insert(table).values(values).returning(table.c.id)).one()[0])


def _seed_dataset(connection: Connection, source: str, state: str) -> SeededDataset:
    """Insert a primary dataset of ``source`` in ``state`` with one entity of each kind.

    A Google dataset gets an account, a campaign, an ad group and a keyword; a LinkedIn
    dataset gets an account, a campaign and a creative. Every row carries ``source`` and the
    new dataset's id, and every value satisfies its table's ``CHECK`` constraints.
    """
    dataset_id = _insert(
        connection,
        "source_dataset",
        {"source": source, "mode": "simulated", "state": state, "role": "primary"},
    )
    scope = {"source": source, "dataset_id": dataset_id}
    account_id = _insert(
        connection,
        "ad_account",
        {**scope, "external_id": f"{source}-account", "name": "Account", "currency": "USD"},
    )
    campaign_id = _insert(
        connection,
        "campaign",
        {
            **scope,
            "account_id": account_id,
            "external_id": f"{source}-campaign",
            "name": "Campaign",
            "status": "active",
            "raw_status": "ENABLED" if source == "google" else "ACTIVE",
            "daily_budget": Decimal("100.000000"),
            "budget_period": "DAILY" if source == "google" else None,
        },
    )
    refs = {"campaign_id": campaign_id}
    if source == "google":
        ad_group_id = _insert(
            connection,
            "ad_group",
            {
                **scope,
                "campaign_id": campaign_id,
                "external_id": "ad-group",
                "name": "Ad group",
                "status": "ENABLED",
            },
        )
        refs["ad_group_id"] = ad_group_id
        refs["keyword_id"] = _insert(
            connection,
            "keyword",
            {
                **scope,
                "ad_group_id": ad_group_id,
                "criterion_id": "1001",
                "text": "crm software",
                "match_type": "PHRASE",
                "status": "ENABLED",
                "quality_score": 7,
            },
        )
    else:
        refs["creative_id"] = _insert(
            connection,
            "creative",
            {
                **scope,
                "campaign_id": campaign_id,
                "urn": "urn:li:sponsoredCreative:1",
                "intended_status": "ACTIVE",
            },
        )
    return SeededDataset(id=dataset_id, source=source, refs=refs)


@pytest.fixture
def world(connection: Connection) -> World:
    """Seed an active and a retired dataset for Google and for LinkedIn."""
    return {
        (source, label): _seed_dataset(connection, source, label)
        for source in ("google", "linkedin")
        for label in (ACTIVE, RETIRED)
    }


def _child_row(
    table_name: str,
    dataset: SeededDataset,
    refs: Mapping[str, int | None],
    offset: int = 0,
) -> dict[str, object]:
    """Build a valid ``table_name`` row in ``dataset`` that references ``refs``.

    ``offset`` moves the row's natural key (its day, window or external id), so a second
    row of the same table never collides with the first on a unique constraint.
    """
    day = DAY + timedelta(days=offset)
    row: dict[str, object] = {"source": dataset.source, "dataset_id": dataset.id}
    row.update({column: refs[column] for column in CHILD_REFERENCES[table_name]})
    if table_name == "lead":
        row.update(
            external_id=f"lead-{offset}",
            submitted_at=FETCHED_AT + timedelta(days=offset),
            lead_form_urn="urn:li:leadGenForm:1",
            lead_form_version=1,
        )
        return row
    if table_name == "fact_segment_window":
        row.update(
            facet="seniority",
            value="urn:li:seniority:5",
            window_start=day - timedelta(days=27),
            window_end=day,
            impressions=1000,
            clicks=12,
            cost=Decimal("80.000000"),
            leads=3,
            fetched_at=FETCHED_AT,
        )
        return row
    row.update(
        date=day,
        cost=Decimal("25.500000"),
        impressions=400,
        clicks=9,
        fetched_at=FETCHED_AT,
    )
    if table_name == "fact_search_term_daily":
        row.update(search_term="crm software", term_status="NONE", matched_criterion_id="1001")
    return row


def _row_ids(connection: Connection, table_name: str, dataset_id: int) -> set[int]:
    """Return the ids of the ``table_name`` rows in ``dataset_id``."""
    table = _table(table_name)
    statement = sa.select(table.c.id).where(table.c.dataset_id == dataset_id)
    return set(connection.scalars(statement))


def _populate(connection: Connection, world: World) -> dict[tuple[str, str], int]:
    """Insert one same-dataset row of every child table into both datasets of its source.

    Returns:
        The id of each inserted row, keyed by (child table, dataset label).
    """
    ids: dict[tuple[str, str], int] = {}
    for table_name, source in CHILD_SOURCES.items():
        for label in (ACTIVE, RETIRED):
            dataset = world[source, label]
            ids[table_name, label] = _insert(
                connection, table_name, _child_row(table_name, dataset, dataset.refs)
            )
    return ids


def _purge(connection: Connection, dataset_id: int) -> None:
    """Purge one dataset exactly as the nightly cleanup does: delete its ``source_dataset`` row."""
    source_dataset = _table("source_dataset")
    connection.execute(sa.delete(source_dataset).where(source_dataset.c.id == dataset_id))


@pytest.mark.db
@pytest.mark.parametrize(("table_name", "column", "referred_table", "index"), EDGES, ids=EDGE_IDS)
def test_a_reference_to_another_datasets_entity_is_rejected(
    connection: Connection,
    world: World,
    table_name: str,
    column: str,
    referred_table: str,
    index: str,
) -> None:
    """A same-dataset reference inserts; the same row pointing at the retired dataset's
    entity fails with a foreign-key violation (23503) of the composite key."""
    active = world[CHILD_SOURCES[table_name], ACTIVE]
    retired = world[CHILD_SOURCES[table_name], RETIRED]

    accepted = _insert(connection, table_name, _child_row(table_name, active, active.refs))
    assert _row_ids(connection, table_name, active.id) == {accepted}

    foreign = {**active.refs, column: retired.refs[column]}
    statement = sa.insert(_table(table_name)).values(
        _child_row(table_name, active, foreign, offset=1)
    )
    with pytest.raises(IntegrityError) as caught, connection.begin_nested():
        connection.execute(statement)

    violation = caught.value.orig
    assert isinstance(violation, pg_errors.ForeignKeyViolation)
    assert violation.sqlstate == "23503"
    assert violation.diag.constraint_name == f"fk_{table_name}_{column}_{referred_table}"
    assert _row_ids(connection, table_name, active.id) == {accepted}


@pytest.mark.db
def test_a_lead_without_a_creative_is_accepted(connection: Connection, world: World) -> None:
    """``lead.creative_id`` stays nullable: ``MATCH SIMPLE`` leaves a ``NULL`` unchecked."""
    active = world["linkedin", ACTIVE]
    lead_id = _insert(
        connection, "lead", _child_row("lead", active, {**active.refs, "creative_id": None})
    )

    lead = _table("lead")
    stored = connection.execute(
        sa.select(lead.c.campaign_id, lead.c.creative_id).where(lead.c.id == lead_id)
    ).one()
    assert tuple(stored) == (active.refs["campaign_id"], None)


@pytest.mark.db
def test_purging_a_retired_dataset_keeps_every_active_row(
    connection: Connection, world: World
) -> None:
    """Purging the retired datasets removes their rows and entities and leaves every fact
    and lead of the active datasets, and the entities they reference, intact."""
    ids = _populate(connection, world)

    for source in ("google", "linkedin"):
        _purge(connection, world[source, RETIRED].id)

    for table_name, source in CHILD_SOURCES.items():
        assert _row_ids(connection, table_name, world[source, ACTIVE].id) == {
            ids[table_name, ACTIVE]
        }
        assert _row_ids(connection, table_name, world[source, RETIRED].id) == set()
    for (source, label), dataset in world.items():
        for entity_table, column in ENTITY_COLUMNS.items():
            if column not in dataset.refs:
                continue
            expected = {dataset.refs[column]} if label == ACTIVE else set()
            assert _row_ids(connection, entity_table, dataset.id) == expected, (source, label)


@pytest.mark.db
def test_purging_the_active_dataset_removes_its_rows_without_error(
    connection: Connection, world: World
) -> None:
    """Purging the active datasets cascades through both foreign-key paths without a
    violation and leaves the other datasets' rows in place."""
    ids = _populate(connection, world)

    for source in ("google", "linkedin"):
        _purge(connection, world[source, ACTIVE].id)

    for table_name, source in CHILD_SOURCES.items():
        assert _row_ids(connection, table_name, world[source, ACTIVE].id) == set()
        assert _row_ids(connection, table_name, world[source, RETIRED].id) == {
            ids[table_name, RETIRED]
        }


@pytest.mark.db
@pytest.mark.parametrize(("entity_table", "column"), list(ENTITY_COLUMNS.items()))
def test_deleting_an_entity_cascades_to_its_own_datasets_rows(
    connection: Connection, world: World, entity_table: str, column: str
) -> None:
    """Deleting an active entity deletes, through the composite key, every active row that
    references it and no row of the retired dataset."""
    ids = _populate(connection, world)
    entity = _table(entity_table)

    for source in ("google", "linkedin"):
        active = world[source, ACTIVE]
        if column in active.refs:
            connection.execute(sa.delete(entity).where(entity.c.id == active.refs[column]))

    for table_name, references in CHILD_REFERENCES.items():
        source = CHILD_SOURCES[table_name]
        if column in references:
            assert _row_ids(connection, table_name, world[source, ACTIVE].id) == set()
        assert _row_ids(connection, table_name, world[source, RETIRED].id) == {
            ids[table_name, RETIRED]
        }


@pytest.mark.db
def test_child_foreign_keys_are_the_dataset_qualified_cascades(
    connection: Connection, database: tuple[Engine, str]
) -> None:
    """Each child table has exactly its ``source_dataset`` key and its composite entity keys,
    named by the convention after the entity column, all ``ON DELETE CASCADE``."""
    _, schema = database
    found: dict[str, dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]]] = {}
    for name, table_name, columns, referred, referred_columns, action in connection.execute(
        _FOREIGN_KEYS_SQL, {"schema": schema}
    ):
        found.setdefault(table_name, {})[name] = (
            tuple(columns),
            referred,
            tuple(referred_columns),
            action,
        )

    for table_name in CHILD_SOURCES:
        expected: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
            f"fk_{table_name}_dataset_id_source_dataset": (
                ("dataset_id",),
                "source_dataset",
                ("id",),
                "c",
            ),
        }
        for edge_table, column, referred_table, _ in EDGES:
            if edge_table == table_name:
                expected[f"fk_{table_name}_{column}_{referred_table}"] = (
                    (column, "dataset_id"),
                    referred_table,
                    ("id", "dataset_id"),
                    "c",
                )
        assert found[table_name] == expected


@pytest.mark.db
def test_unique_keys_anchor_the_composite_keys_and_natural_keys_are_unchanged(
    connection: Connection, database: tuple[Engine, str]
) -> None:
    """Each entity table adds ``uq_<table>_dataset_id_id`` beside its unchanged natural key,
    and each child table keeps exactly its natural key."""
    _, schema = database
    found: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, table_name, columns in connection.execute(_UNIQUES_SQL, {"schema": schema}):
        found.setdefault(table_name, {})[name] = tuple(columns)

    for table_name, expected in {**PARENT_UNIQUES, **CHILD_UNIQUES}.items():
        assert found[table_name] == expected


@pytest.mark.db
def test_every_composite_key_leads_an_index_for_its_cascade(
    connection: Connection, database: tuple[Engine, str]
) -> None:
    """The entity column of every composite key leads the named index that serves the
    cascade's lookup when the entity row is deleted."""
    _, schema = database
    leading = {
        (table_name, index_name): column
        for table_name, index_name, column in connection.execute(
            _LEADING_INDEX_COLUMNS_SQL, {"schema": schema}
        )
    }

    for table_name, column, _, index_name in EDGES:
        assert leading.get((table_name, index_name)) == column
