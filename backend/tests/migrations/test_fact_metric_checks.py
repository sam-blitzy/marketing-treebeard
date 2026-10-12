"""Migration tests: the fact metric checks of revision 0001 admit only finite values of 0 or more.

AAP 0.5.2 puts a non-negative check on the metrics of the five fact tables. PostgreSQL's
``numeric`` type stores ``NaN`` and sorts it above every number, so ``'NaN' >= 0`` is true
and a check of ``>= 0`` alone stores a ``NaN`` that turns every window ``SUM`` of the column
into ``NaN``. Each ``ck_<table>_non_negative`` check therefore also keeps every ``numeric``
metric from ``NaN``. On a schema built by ``alembic upgrade head`` these tests show, for each
of the 16 ``numeric`` metric columns, that an update or an insert of ``NaN`` is refused with
SQLSTATE 23514 (``check_violation``), that a negative value still is, that ``Infinity`` is
refused by the ``numeric(18, 6)`` type itself (22003), and that 0 and, where the column is
nullable, ``NULL`` ("unavailable") are stored.

``alembic check`` does not compare ``CHECK`` texts, so the model/migration parity of these
five constraints is proved here instead: ``pg_get_constraintdef`` of each constraint in the
migrated schema equals its definition in a schema built by ``Base.metadata.create_all``.

The tests run against the PostgreSQL 18 test database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``). The module creates two private schemas, migrates one
through ``migrations/env.py``'s ``connection`` attribute and builds the other from the model
metadata, points every connection's ``search_path`` at one schema alone, and drops both with
``CASCADE`` at teardown, so the ``public`` schema and every other schema stay untouched. Each
test runs inside one transaction that is rolled back afterwards. An unreachable database
fails the tests; it never skips them.
"""

import os
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.db.base import Base

pytestmark = pytest.mark.db

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
"""``backend/alembic.ini``, whose ``script_location`` is resolved relative to the file."""

AT = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The ``fetched_at`` instant of every seeded fact row."""

DAY = date(2026, 9, 10)
"""The native day of every seeded daily fact row."""

NUMERIC_METRICS: tuple[tuple[str, str], ...] = (
    ("fact_campaign_daily", "cost"),
    ("fact_campaign_daily", "conversions"),
    ("fact_campaign_daily", "conversions_value"),
    ("fact_campaign_daily", "budget_lost_is"),
    ("fact_campaign_daily", "impression_share"),
    ("fact_creative_daily", "cost"),
    ("fact_creative_daily", "conversions"),
    ("fact_creative_daily", "conversions_value"),
    ("fact_keyword_daily", "cost"),
    ("fact_keyword_daily", "conversions"),
    ("fact_keyword_daily", "conversions_value"),
    ("fact_keyword_daily", "average_cpc"),
    ("fact_search_term_daily", "cost"),
    ("fact_search_term_daily", "conversions"),
    ("fact_search_term_daily", "conversions_value"),
    ("fact_segment_window", "cost"),
)
"""Every ``numeric`` metric column a ``ck_<table>_non_negative`` check covers."""

FACT_TABLES: tuple[str, ...] = tuple(dict.fromkeys(table for table, _ in NUMERIC_METRICS))
"""The five fact tables, in :data:`NUMERIC_METRICS` order."""

CHECK_VIOLATION = "23514"
"""SQLSTATE ``check_violation``."""

NUMERIC_VALUE_OUT_OF_RANGE = "22003"
"""SQLSTATE ``numeric_value_out_of_range``."""

_NON_NEGATIVE_DEFINITIONS = sa.text(
    "SELECT con.conname, pg_get_constraintdef(con.oid) "
    "FROM pg_constraint AS con "
    "JOIN pg_class AS cls ON cls.oid = con.conrelid "
    "JOIN pg_namespace AS nsp ON nsp.oid = cls.relnamespace "
    "WHERE nsp.nspname = :schema AND con.contype = 'c' "
    "AND con.conname LIKE 'ck\\_%\\_non\\_negative'"
)


@dataclass(frozen=True)
class Schemas:
    """The two private schemas of this module.

    Attributes:
        engine: An engine whose connections see only the migrated schema.
        migrated: The schema built by ``alembic upgrade head``.
        modelled: The schema built by ``Base.metadata.create_all``.
    """

    engine: Engine
    migrated: str
    modelled: str


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the fact metric check tests"
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


def _upgrade(connection: Connection) -> None:
    """Run ``alembic upgrade head`` through ``connection``, inside its open transaction."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["configure_logger"] = False
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def schemas() -> Iterator[Schemas]:
    """Migrate one private schema and build another from the models; drop both afterwards.

    Both are built in one transaction, so a failure leaves neither behind.
    """
    url = _database_url()
    token = uuid.uuid4().hex
    migrated, modelled = f"fact_checks_alembic_{token}", f"fact_checks_models_{token}"
    engine = sa.create_engine(url, poolclass=NullPool, connect_args=_connect_args(migrated))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(migrated))
            _upgrade(connection)
            connection.execute(CreateSchema(modelled))
            # Until this transaction ends, unqualified names resolve to the modelled schema.
            connection.execute(sa.text(f'SET LOCAL search_path TO "{modelled}"'))
            Base.metadata.create_all(connection)
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield Schemas(engine=engine, migrated=migrated, modelled=modelled)
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(migrated, cascade=True, if_exists=True))
            connection.execute(DropSchema(modelled, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
def connection(schemas: Schemas) -> Iterator[Connection]:
    """Yield a connection to the migrated schema inside a transaction rolled back afterwards."""
    with schemas.engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _table(name: str) -> sa.Table:
    """Return the ``Base.metadata`` table named ``name``; it resolves to the migrated schema."""
    return Base.metadata.tables[name]


def _insert(connection: Connection, table_name: str, values: Mapping[str, object]) -> int:
    """Insert one row into ``table_name`` and return its generated ``id``."""
    table = _table(table_name)
    return int(connection.execute(sa.insert(table).values(values).returning(table.c.id)).one()[0])


def _scope(dataset_id: int, source: str) -> dict[str, object]:
    """Return the provenance columns of a row in ``dataset_id``."""
    return {"source": source, "dataset_id": dataset_id}


def _fact_rows(connection: Connection) -> dict[str, dict[str, object]]:
    """Seed a Google and a LinkedIn dataset with the entities the fact tables reference.

    Returns:
        One valid row per fact table, every ``numeric`` metric set to a finite positive
        value, not yet inserted.
    """
    google = _scope(
        _insert(
            connection,
            "source_dataset",
            {"source": "google", "mode": "simulated", "state": "active", "role": "primary"},
        ),
        "google",
    )
    linkedin = _scope(
        _insert(
            connection,
            "source_dataset",
            {"source": "linkedin", "mode": "simulated", "state": "active", "role": "primary"},
        ),
        "linkedin",
    )
    google_account = _insert(
        connection, "ad_account", {**google, "external_id": "g-1", "currency": "USD"}
    )
    linkedin_account = _insert(
        connection, "ad_account", {**linkedin, "external_id": "l-1", "currency": "USD"}
    )
    google_campaign = _insert(
        connection,
        "campaign",
        {
            **google,
            "account_id": google_account,
            "external_id": "g-c-1",
            "name": "Search",
            "status": "active",
            "raw_status": "ENABLED",
        },
    )
    linkedin_campaign = _insert(
        connection,
        "campaign",
        {
            **linkedin,
            "account_id": linkedin_account,
            "external_id": "l-c-1",
            "name": "Lead gen",
            "status": "active",
            "raw_status": "ACTIVE",
        },
    )
    ad_group = _insert(
        connection,
        "ad_group",
        {
            **google,
            "campaign_id": google_campaign,
            "external_id": "ag-1",
            "name": "Ad group",
            "status": "ENABLED",
        },
    )
    keyword = _insert(
        connection,
        "keyword",
        {
            **google,
            "ad_group_id": ad_group,
            "criterion_id": "1001",
            "text": "crm software",
            "match_type": "PHRASE",
            "status": "ENABLED",
        },
    )
    creative = _insert(
        connection,
        "creative",
        {**linkedin, "campaign_id": linkedin_campaign, "urn": "urn:li:sponsoredCreative:1"},
    )
    base = {"cost": Decimal("12.5"), "impressions": 100, "clicks": 4, "fetched_at": AT}
    conversions = {"conversions": Decimal("1.5"), "conversions_value": Decimal("30")}
    return {
        "fact_campaign_daily": {
            **google,
            **base,
            **conversions,
            "campaign_id": google_campaign,
            "date": DAY,
            "budget_lost_is": Decimal("0.12"),
            "impression_share": Decimal("0.4"),
        },
        "fact_creative_daily": {
            **linkedin,
            **base,
            **conversions,
            "creative_id": creative,
            "date": DAY,
        },
        "fact_keyword_daily": {
            **google,
            **base,
            **conversions,
            "keyword_id": keyword,
            "date": DAY,
            "average_cpc": Decimal("3.125"),
        },
        "fact_search_term_daily": {
            **google,
            **base,
            **conversions,
            "ad_group_id": ad_group,
            "campaign_id": google_campaign,
            "date": DAY,
            "search_term": "crm software free",
            "term_status": "NONE",
            "matched_criterion_id": "1001",
        },
        "fact_segment_window": {
            **linkedin,
            "campaign_id": linkedin_campaign,
            "facet": "seniority",
            "value": "urn:li:seniority:5",
            "window_start": date(2026, 9, 4),
            "window_end": date(2026, 10, 1),
            "impressions": 100,
            "clicks": 4,
            "cost": Decimal("12.5"),
            "fetched_at": AT,
        },
    }


def _next_key(table_name: str, row: Mapping[str, object]) -> dict[str, object]:
    """Return ``row`` moved to another natural key of the same table and entity."""
    if table_name == "fact_segment_window":
        return {**row, "value": "urn:li:seniority:6"}
    return {**row, "date": date(2026, 9, 11)}


def _sqlstate(connection: Connection, statement: sa.Executable) -> str | None:
    """Execute ``statement`` in a savepoint that is always rolled back.

    Returns:
        The SQLSTATE PostgreSQL refused the statement with, or ``None`` when it succeeded.
    """
    savepoint = connection.begin_nested()
    try:
        connection.execute(statement)
    except DBAPIError as exc:
        savepoint.rollback()
        refused = exc.orig
    else:
        savepoint.rollback()
        return None
    if not isinstance(refused, psycopg.Error):
        pytest.fail(f"expected a psycopg error, got {refused!r}")
    return refused.sqlstate


def _definitions(connection: Connection, schema: str) -> dict[str, str]:
    """Return ``pg_get_constraintdef`` of every ``ck_<table>_non_negative`` check in ``schema``."""
    result = connection.execute(_NON_NEGATIVE_DEFINITIONS, {"schema": schema})
    return {str(name): str(definition) for name, definition in result}


def _update(table_name: str, row_id: int, column: str, value: Decimal | None) -> sa.Update:
    """Return ``UPDATE <table> SET <column> = :value WHERE id = :row_id``."""
    table = _table(table_name)
    return sa.update(table).where(table.c.id == row_id).values({column: value})


@pytest.mark.parametrize(
    ("table_name", "column"), NUMERIC_METRICS, ids=[f"{t}.{c}" for t, c in NUMERIC_METRICS]
)
def test_a_nan_metric_is_refused_by_the_non_negative_check(
    connection: Connection, table_name: str, column: str
) -> None:
    """``NaN`` is refused with 23514 by an update of a stored row and by an insert."""
    row = _fact_rows(connection)[table_name]
    row_id = _insert(connection, table_name, row)

    assert _sqlstate(connection, _update(table_name, row_id, column, Decimal("NaN"))) == (
        CHECK_VIOLATION
    )
    inserted = sa.insert(_table(table_name)).values(
        {**_next_key(table_name, row), column: Decimal("NaN")}
    )
    assert _sqlstate(connection, inserted) == CHECK_VIOLATION
    # The same insert with a finite value is stored, so the refusal above is the NaN's alone.
    finite = sa.insert(_table(table_name)).values(_next_key(table_name, row))
    assert _sqlstate(connection, finite) is None


@pytest.mark.parametrize(
    ("table_name", "column"), NUMERIC_METRICS, ids=[f"{t}.{c}" for t, c in NUMERIC_METRICS]
)
def test_a_metric_keeps_every_finite_value_of_zero_or_more_and_null(
    connection: Connection, table_name: str, column: str
) -> None:
    """A negative value is still 23514 and ``Infinity`` is 22003; 0 and, on a nullable
    column, ``NULL`` are stored."""
    row = _fact_rows(connection)[table_name]
    row_id = _insert(connection, table_name, row)
    table = _table(table_name)

    assert _sqlstate(connection, _update(table_name, row_id, column, Decimal("-0.000001"))) == (
        CHECK_VIOLATION
    )
    assert _sqlstate(connection, _update(table_name, row_id, column, Decimal("Infinity"))) == (
        NUMERIC_VALUE_OUT_OF_RANGE
    )
    assert _sqlstate(connection, _update(table_name, row_id, column, Decimal("0"))) is None
    if table.c[column].nullable:
        assert _sqlstate(connection, _update(table_name, row_id, column, None)) is None

    connection.execute(_update(table_name, row_id, column, Decimal("0")))
    stored = connection.execute(sa.select(table.c[column]).where(table.c.id == row_id)).scalar()
    assert stored == Decimal("0")


def test_one_nan_row_can_no_longer_poison_a_window_sum(connection: Connection) -> None:
    """The finding's scenario: with one day stored, a second day of ``NaN`` cost is refused
    and the window ``SUM(cost)`` stays the finite total."""
    row = _fact_rows(connection)["fact_campaign_daily"]
    _insert(connection, "fact_campaign_daily", {**row, "cost": Decimal("10")})
    nan_day = sa.insert(_table("fact_campaign_daily")).values(
        {
            **_next_key("fact_campaign_daily", row),
            "cost": Decimal("NaN"),
            "conversions": Decimal("NaN"),
        }
    )

    assert _sqlstate(connection, nan_day) == CHECK_VIOLATION
    table = _table("fact_campaign_daily")
    assert connection.execute(sa.select(sa.func.sum(table.c.cost))).scalar() == Decimal("10")


def test_the_migrated_checks_match_the_models(schemas: Schemas, connection: Connection) -> None:
    """``pg_get_constraintdef`` of every ``ck_<table>_non_negative`` check is the same in the
    migrated schema and in the schema built from ``Base.metadata``, and each names ``NaN``."""
    migrated = _definitions(connection, schemas.migrated)
    modelled = _definitions(connection, schemas.modelled)

    assert set(migrated) == {f"ck_{table}_non_negative" for table in FACT_TABLES}
    assert migrated == modelled
    for table, column in NUMERIC_METRICS:
        assert f"({column} <> 'NaN'::numeric)" in migrated[f"ck_{table}_non_negative"]
