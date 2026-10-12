"""Unknown platform values in ``ad_account``, ``budget``, ``conversion_action`` and snapshots.

The Google parser hands over three values that may legitimately be unknown, and each target
column stores the unknown as ``NULL`` rather than rejecting the row or inventing a value:

* ``ad_account.name``: Google omits ``customer.descriptive_name`` for an unnamed account.
  :attr:`~ads_engine.db.models.accounts.AdAccount.display_name` falls back to the account id
  for display, and the stored name stays ``NULL``.
* ``budget.amount`` and ``budget.amount_micros``: an identified budget whose own-period
  amount is absent keeps its identity, period and sharedness with both amounts ``NULL``.
  ``ck_budget_amount_pair`` keeps the two unknown together.
* ``conversion_action.counting_type``: an omitted counting type is stored as ``NULL`` and
  kept as JSON ``null`` in the conversion action's ``entity_snapshot`` payload.

The metadata tests need no database. The round trips run against real PostgreSQL 18, because
only the server proves what a column and a constraint accept. The module creates a private
schema in the database named by ``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``),
builds in it only the tables under test and the tables their foreign keys reach, pins every
connection's ``search_path`` to it, and drops it with ``CASCADE`` at teardown, so ``public``
and every other schema stay untouched. Each test runs in one transaction that is rolled back
afterwards; expected violations run inside a savepoint. An unreachable database fails the
tests; it never skips them.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import (
    Column,
    Connection,
    Engine,
    Numeric,
    Table,
    create_engine,
    insert,
    select,
    text,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CheckConstraint, CreateSchema, DropSchema

from ads_engine.db.base import Base
from ads_engine.db.models import accounts, ops, snapshots

CHECK_VIOLATION = "23514"

AMOUNT_PAIR = "ck_budget_amount_pair"

CAPTURED_AT = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)

AD_ACCOUNT: Table = Base.metadata.tables[accounts.AdAccount.__tablename__]
BUDGET: Table = Base.metadata.tables[accounts.Budget.__tablename__]
CONVERSION_ACTION: Table = Base.metadata.tables[accounts.ConversionAction.__tablename__]
ENTITY_SNAPSHOT: Table = Base.metadata.tables[snapshots.EntitySnapshot.__tablename__]


# ---------------------------------------------------------------------------------------
# Database: a private schema per module, one rolled-back transaction per test
# ---------------------------------------------------------------------------------------


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the unknown-value tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


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


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[Engine, str]]:
    """Create a private schema holding the tables under test; drop it afterwards.

    Yields:
        An engine whose connections use only the private schema, and that schema's name.
    """
    url = _database_url()
    schema = f"accounts_unknown_test_{uuid.uuid4().hex}"
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={"options": f"-c search_path={schema} -c TimeZone=UTC"},
    )
    tables = _with_foreign_key_targets(AD_ACCOUNT, BUDGET, CONVERSION_ACTION, ENTITY_SNAPSHOT)
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection, tables=tables)
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


@pytest.fixture
def google_dataset(connection: Connection) -> int:
    """Insert an active simulated primary Google ``source_dataset`` row and return its id."""
    statement = (
        insert(ops.SourceDataset)
        .values(source="google", mode="simulated", state="active", role="primary")
        .returning(ops.SourceDataset.id)
    )
    return int(connection.execute(statement).scalar_one())


def _insert_budget(connection: Connection, dataset_id: int, **values: Any) -> int:
    """Insert one Google ``budget`` row of ``dataset_id`` and return its id."""
    statement = (
        insert(accounts.Budget)
        .values(source="google", dataset_id=dataset_id, **values)
        .returning(accounts.Budget.id)
    )
    return int(connection.execute(statement).scalar_one())


def _insert_conversion_action(connection: Connection, dataset_id: int, **values: Any) -> int:
    """Insert one Google ``conversion_action`` row of ``dataset_id`` and return its id."""
    statement = (
        insert(accounts.ConversionAction)
        .values(
            source="google",
            dataset_id=dataset_id,
            external_id="7001",
            name="Demo request",
            status="ENABLED",
            include_in_conversions_metric=True,
            **values,
        )
        .returning(accounts.ConversionAction.id)
    )
    return int(connection.execute(statement).scalar_one())


def _violation(error: IntegrityError) -> tuple[str | None, str | None]:
    """Return the SQLSTATE and the constraint name PostgreSQL reported for ``error``."""
    original = error.orig
    assert isinstance(original, psycopg.Error), original
    return original.sqlstate, original.diag.constraint_name


def _check_sql(table: Table, name: str) -> str:
    """Return the SQL text of the ``CHECK`` constraint ``name`` declared on ``table``."""
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return str(constraint.sqltext)
    pytest.fail(f"{table.name} declares no CHECK named {name}")


def _type_shape(column: Column[Any]) -> tuple[str, int | None, int | None]:
    """Return ``column``'s type class name, with precision and scale for a ``Numeric``."""
    type_ = column.type
    if isinstance(type_, Numeric):
        return type(type_).__name__, type_.precision, type_.scale
    return type(type_).__name__, None, None


# The columns that may hold an unknown platform value, each with its declared type shape and
# PostgreSQL's own rendering of that type (``format_type``).
UNKNOWN_VALUE_COLUMNS = [
    pytest.param(AD_ACCOUNT, "name", ("Text", None, None), "text", id="ad_account.name"),
    pytest.param(BUDGET, "amount", ("Numeric", 18, 6), "numeric(18,6)", id="budget.amount"),
    pytest.param(
        BUDGET, "amount_micros", ("BigInteger", None, None), "bigint", id="budget.amount_micros"
    ),
    pytest.param(
        CONVERSION_ACTION,
        "counting_type",
        ("Text", None, None),
        "text",
        id="conversion_action.counting_type",
    ),
]


# ---------------------------------------------------------------------------------------
# Metadata: the declared shape the migration mirrors
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("table", "column", "shape", "pg_type"), UNKNOWN_VALUE_COLUMNS)
def test_unknown_value_columns_are_nullable_with_their_types_unchanged(
    table: Table, column: str, shape: tuple[str, int | None, int | None], pg_type: str
) -> None:
    """Each column that may hold an unknown platform value is nullable, at its planned type."""
    target = table.c[column]

    assert target.nullable is True
    assert _type_shape(target) == shape
    assert target.server_default is None


@pytest.mark.parametrize(
    ("table", "column"),
    [
        pytest.param(AD_ACCOUNT, "external_id", id="ad_account.external_id"),
        pytest.param(AD_ACCOUNT, "currency", id="ad_account.currency"),
        pytest.param(BUDGET, "external_id", id="budget.external_id"),
        pytest.param(BUDGET, "period", id="budget.period"),
        pytest.param(BUDGET, "explicitly_shared", id="budget.explicitly_shared"),
        pytest.param(CONVERSION_ACTION, "external_id", id="conversion_action.external_id"),
        pytest.param(CONVERSION_ACTION, "status", id="conversion_action.status"),
    ],
)
def test_identity_and_shape_columns_stay_required(table: Table, column: str) -> None:
    """Identity, budget period and sharedness stay ``NOT NULL`` beside the unknown values."""
    assert table.c[column].nullable is False


def test_the_amount_pair_check_renders_the_module_sql_verbatim() -> None:
    """``ck_budget_amount_pair`` carries exactly the module constant the migration copies."""
    expected = "(amount IS NULL) = (amount_micros IS NULL)"

    assert _check_sql(BUDGET, AMOUNT_PAIR) == accounts._AMOUNT_PAIR_SQL == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("Acme Ads", "Acme Ads", id="named"),
        pytest.param(None, "1234567890", id="unnamed"),
        pytest.param("", "1234567890", id="empty-name"),
    ],
)
def test_display_name_falls_back_to_the_account_id(name: str | None, expected: str) -> None:
    """``display_name`` is the stored name when non-empty, else the platform account id."""
    account = accounts.AdAccount(external_id="1234567890", name=name, currency="USD")

    assert account.display_name == expected
    assert account.name == name


# ---------------------------------------------------------------------------------------
# PostgreSQL round trips
# ---------------------------------------------------------------------------------------


@pytest.mark.db
def test_the_catalog_holds_the_amount_pair_check(
    connection: Connection, database: tuple[Engine, str]
) -> None:
    """PostgreSQL holds ``ck_budget_amount_pair`` on ``budget`` with the module's expression."""
    _, schema = database

    row = (
        connection.execute(
            text(
                "SELECT con.contype, cls.relname AS table_name, "
                "pg_get_constraintdef(con.oid) AS definition "
                "FROM pg_constraint con "
                "JOIN pg_class cls ON cls.oid = con.conrelid "
                "JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace "
                "WHERE nsp.nspname = :schema AND con.conname = :name"
            ),
            {"schema": schema, "name": AMOUNT_PAIR},
        )
        .mappings()
        .one()
    )

    assert (row["contype"], row["table_name"]) == ("c", "budget")
    assert row["definition"] == "CHECK (((amount IS NULL) = (amount_micros IS NULL)))"


@pytest.mark.db
@pytest.mark.parametrize(("table", "column", "shape", "pg_type"), UNKNOWN_VALUE_COLUMNS)
def test_the_catalog_holds_the_unknown_value_columns_as_nullable(
    connection: Connection,
    database: tuple[Engine, str],
    table: Table,
    column: str,
    shape: tuple[str, int | None, int | None],
    pg_type: str,
) -> None:
    """PostgreSQL created each unknown-value column without ``NOT NULL``, at its planned type."""
    _, schema = database

    row = (
        connection.execute(
            text(
                "SELECT format_type(att.atttypid, att.atttypmod) AS pg_type, "
                "att.attnotnull AS not_null "
                "FROM pg_attribute att "
                "JOIN pg_class cls ON cls.oid = att.attrelid "
                "JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace "
                "WHERE nsp.nspname = :schema AND cls.relname = :table "
                "AND att.attname = :column AND NOT att.attisdropped"
            ),
            {"schema": schema, "table": table.name, "column": column},
        )
        .mappings()
        .one()
    )

    assert (row["pg_type"], row["not_null"]) == (pg_type, False)


@pytest.mark.db
@pytest.mark.parametrize(
    ("name", "expected_display"),
    [
        pytest.param(None, "1234567890", id="unnamed"),
        pytest.param("Acme Ads", "Acme Ads", id="named"),
    ],
)
def test_an_account_round_trips_its_name_and_display_name(
    connection: Connection, google_dataset: int, name: str | None, expected_display: str
) -> None:
    """An unnamed account is stored with ``name`` NULL, never a string, and displays its id."""
    account_id = connection.execute(
        insert(accounts.AdAccount)
        .values(
            source="google",
            dataset_id=google_dataset,
            external_id="1234567890",
            name=name,
            currency="USD",
            timezone="America/New_York",
        )
        .returning(accounts.AdAccount.id)
    ).scalar_one()

    name_is_null: bool = connection.execute(
        text("SELECT name IS NULL FROM ad_account WHERE id = :id"), {"id": account_id}
    ).scalar_one()
    with Session(bind=connection) as session:
        loaded = session.get_one(accounts.AdAccount, account_id)

        assert name_is_null is (name is None)
        assert loaded.name == name
        assert loaded.display_name == expected_display


@pytest.mark.db
@pytest.mark.parametrize(
    ("period", "explicitly_shared"),
    [
        pytest.param("DAILY", True, id="shared-daily"),
        pytest.param("CUSTOM_PERIOD", False, id="lifetime"),
    ],
)
def test_a_budget_with_an_unknown_amount_keeps_its_identity_period_and_sharedness(
    connection: Connection, google_dataset: int, period: str, explicitly_shared: bool
) -> None:
    """Both amounts are stored NULL, never zero, while the budget's other fields stay intact."""
    budget_id = _insert_budget(
        connection,
        google_dataset,
        external_id="9001",
        amount=None,
        amount_micros=None,
        period=period,
        explicitly_shared=explicitly_shared,
    )

    stored = connection.execute(
        select(
            accounts.Budget.dataset_id,
            accounts.Budget.external_id,
            accounts.Budget.amount,
            accounts.Budget.amount_micros,
            accounts.Budget.period,
            accounts.Budget.explicitly_shared,
        ).where(accounts.Budget.id == budget_id)
    ).one()

    assert tuple(stored) == (google_dataset, "9001", None, None, period, explicitly_shared)


@pytest.mark.db
def test_a_budget_with_a_known_amount_keeps_both_units_exactly(
    connection: Connection, google_dataset: int
) -> None:
    """A known amount is stored in both units at native precision, as before."""
    budget_id = _insert_budget(
        connection,
        google_dataset,
        external_id="9002",
        amount=Decimal("50.000001"),
        amount_micros=50_000_001,
        period="DAILY",
    )

    stored = connection.execute(
        select(accounts.Budget.amount, accounts.Budget.amount_micros).where(
            accounts.Budget.id == budget_id
        )
    ).one()

    assert tuple(stored) == (Decimal("50.000001"), 50_000_001)


@pytest.mark.db
@pytest.mark.parametrize(
    ("amount", "amount_micros"),
    [
        pytest.param(Decimal("50.000000"), None, id="micros-unknown"),
        pytest.param(None, 50_000_000, id="amount-unknown"),
    ],
)
def test_a_budget_with_only_one_unknown_amount_unit_is_rejected(
    connection: Connection,
    google_dataset: int,
    amount: Decimal | None,
    amount_micros: int | None,
) -> None:
    """``ck_budget_amount_pair`` rejects an amount known in one unit and unknown in the other."""
    with pytest.raises(IntegrityError) as caught, connection.begin_nested():
        _insert_budget(
            connection,
            google_dataset,
            external_id="9003",
            amount=amount,
            amount_micros=amount_micros,
            period="DAILY",
        )

    assert _violation(caught.value) == (CHECK_VIOLATION, AMOUNT_PAIR)
    assert connection.execute(select(accounts.Budget.id)).all() == []


@pytest.mark.db
def test_a_conversion_action_with_an_unknown_counting_type_is_stored_null(
    connection: Connection, google_dataset: int
) -> None:
    """An omitted counting type is stored NULL: no ``ONE_PER_CLICK`` or default is invented."""
    action_id = _insert_conversion_action(
        connection, google_dataset, counting_type=None, attribution_model=None
    )

    stored = connection.execute(
        text(
            "SELECT counting_type IS NULL AS counting_unknown, "
            "attribution_model IS NULL AS attribution_unknown "
            "FROM conversion_action WHERE id = :id"
        ),
        {"id": action_id},
    ).one()

    assert tuple(stored) == (True, True)


@pytest.mark.db
def test_a_snapshot_payload_keeps_an_unknown_counting_type_as_json_null(
    connection: Connection, google_dataset: int
) -> None:
    """The conversion action's snapshot keeps the ``counting_type`` key with a JSON null."""
    action_id = _insert_conversion_action(
        connection, google_dataset, counting_type=None, attribution_model="GOOGLE_ADS_LAST_CLICK"
    )
    payload = {
        "include_in_conversions_metric": True,
        "counting_type": None,
        "attribution_model": "GOOGLE_ADS_LAST_CLICK",
    }
    snapshot_id = connection.execute(
        insert(snapshots.EntitySnapshot)
        .values(
            source="google",
            dataset_id=google_dataset,
            entity_type="conversion_action",
            entity_id=action_id,
            captured_at=CAPTURED_AT,
            status="ENABLED",
            payload=payload,
        )
        .returning(snapshots.EntitySnapshot.id)
    ).scalar_one()

    stored: dict[str, Any] = connection.execute(
        select(snapshots.EntitySnapshot.payload).where(snapshots.EntitySnapshot.id == snapshot_id)
    ).scalar_one()
    json_type = connection.execute(
        text(
            # ``->`` yields SQL NULL for an absent key and JSON null for a present null value.
            "SELECT (payload -> 'counting_type') IS NOT NULL AS has_key, "
            "jsonb_typeof(payload -> 'counting_type') AS json_type "
            "FROM entity_snapshot WHERE id = :id"
        ),
        {"id": snapshot_id},
    ).one()

    assert stored == payload
    assert stored["counting_type"] is None
    assert tuple(json_type) == (True, "null")
