"""Tests for the value guards and the lock order of the ``db/repositories`` write path.

Covered:

* **Non-finite amounts**: a ``Decimal`` ``NaN``, ``sNaN``, ``Infinity`` or ``-Infinity`` is
  refused with ``ValueError`` naming ``<table>.<column>`` by :func:`upsert_facts`,
  :func:`upsert_entities` and :func:`append_snapshots`, before any statement, so nothing of
  the batch is stored. PostgreSQL would store a ``numeric`` ``NaN``, sort it above every
  number and turn every window ``SUM`` into ``NaN`` (AAP 0.5.2: money and conversions are
  ``numeric(18, 6)`` under a non-negative check).
* **Native days and instants**: a ``datetime`` for a ``date`` column (``date``,
  ``window_start``, ``window_end``), tz-aware or naive, is refused instead of being cast to
  its day in the session's UTC zone, because native days are never rebucketed (AAP 0.5.2,
  0.5.4.1); a bare ``date`` for a ``timestamptz`` column is refused instead of being read as
  UTC midnight. Native days and tz-aware instants are stored unchanged.
* **Natural-key write order**: whatever the caller's order, :func:`upsert_facts` and
  :func:`upsert_entities` send their rows in ascending natural-key order, a batch of one
  column set as one statement and a mixed batch as consecutive runs of one column set,
  observed in the parameters each ``INSERT`` sends. The sort key never compares values of
  two types. A repeated key still keeps its last occurrence, and the returned count and
  id map are unchanged.
* **Concurrent writers**: two transactions upserting the same 600 daily rows, or the same
  600 campaigns, one in forward and one in reverse order, started together round after
  round, all commit: none fails with SQLSTATE 40P01 (``deadlock_detected``), and each key
  is stored once.

The tests run against the PostgreSQL 18 test database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``). The module creates a private schema, builds every table
of the model metadata in it, points every connection's ``search_path`` at it alone, and
drops it with ``CASCADE`` at teardown, so the ``public`` schema and every other schema stay
untouched. Each test starts from empty dataset tables. An unreachable database fails the
tests; it never skips them.
"""

import asyncio
import itertools
import math
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import Campaign, CampaignGroup
from ads_engine.db.models.facts import FactCampaignDaily, FactSegmentWindow
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.repositories import (
    Row,
    _model_info,
    _prepare,
    append_snapshots,
    upsert_entities,
    upsert_facts,
)

type Sessions = async_sessionmaker[AsyncSession]

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The observation instant of every snapshot and entity instant written here."""

DAY = date(2026, 9, 10)
"""The first native day of the daily fact rows written here."""

NEW_YORK = ZoneInfo("America/New_York")
"""A Google account time zone behind UTC."""

LOS_ANGELES = ZoneInfo("America/Los_Angeles")
"""Another time zone behind UTC, for segment window instants."""

NON_FINITE: tuple[str, ...] = ("NaN", "-NaN", "sNaN", "Infinity", "-Infinity")
"""Every non-finite ``Decimal`` spelling: quiet and signalling ``NaN`` and both infinities."""


# ---------------------------------------------------------------------------------------
# Database fixtures
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
            "for the repository write-guard tests"
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
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding every table of the metadata; drop it afterwards."""
    url = _database_url()
    schema = f"repository_guards_test_{uuid.uuid4().hex}"
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
        yield url, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
async def sessions(database: tuple[URL, str]) -> AsyncIterator[Sessions]:
    """Yield a session factory on empty dataset tables of the private schema.

    The engine is created and disposed inside this test's loop. Truncating
    ``source_dataset`` cascades to every dataset-scoped table.
    """
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("TRUNCATE source_dataset CASCADE"))
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------
# Scenario builders and readers
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    """One active Google and one active LinkedIn dataset with an account and campaigns each.

    Attributes:
        google: The Google dataset's id.
        google_account: The Google account's id.
        google_campaigns: The Google campaigns' ids, in creation order.
        linkedin: The LinkedIn dataset's id.
        linkedin_account: The LinkedIn account's id.
        linkedin_campaign: The LinkedIn campaign's id.
    """

    google: int
    google_account: int
    google_campaigns: tuple[int, ...]
    linkedin: int
    linkedin_account: int
    linkedin_campaign: int


async def _insert(session: AsyncSession, table_name: str, values: dict[str, Any]) -> int:
    """Insert one row into ``table_name`` and return its generated ``id``."""
    table = Base.metadata.tables[table_name]
    result = await session.execute(sa.insert(table).values(values).returning(table.c.id))
    return int(result.scalar_one())


def _campaign_row(account_id: int, number: int, **values: Any) -> dict[str, Any]:
    """Return a valid Google campaign row for :func:`upsert_entities`, without provenance."""
    return {
        "account_id": account_id,
        "external_id": f"google-campaign-{number:04d}",
        "name": f"Campaign {number}",
        "status": "active",
        "raw_status": "ENABLED",
        "daily_budget": Decimal("50"),
        "budget_period": "DAILY",
        **values,
    }


def _daily_row(campaign_id: int, day: date, **values: Any) -> dict[str, Any]:
    """Return a valid ``fact_campaign_daily`` row for :func:`upsert_facts`."""
    return {
        "campaign_id": campaign_id,
        "date": day,
        "cost": Decimal("12.5"),
        "impressions": 100,
        "clicks": 4,
        "conversions": Decimal("1.5"),
        **values,
    }


def _segment_row(campaign_id: int, **values: Any) -> dict[str, Any]:
    """Return a valid ``fact_segment_window`` row for :func:`upsert_facts`."""
    return {
        "campaign_id": campaign_id,
        "facet": "seniority",
        "value": "urn:li:seniority:5",
        "window_start": date(2026, 9, 4),
        "window_end": date(2026, 10, 1),
        "impressions": 100,
        "clicks": 4,
        "cost": Decimal("12.5"),
        "leads": 2,
        **values,
    }


def _group_row(world: World, **values: Any) -> dict[str, Any]:
    """Return a valid LinkedIn ``campaign_group`` row for :func:`upsert_entities`."""
    return {
        "account_id": world.linkedin_account,
        "external_id": "group-1",
        "name": "Group",
        "status": "ACTIVE",
        **values,
    }


def _snapshot_row(world: World, **values: Any) -> dict[str, Any]:
    """Return a valid campaign ``entity_snapshot`` row for :func:`append_snapshots`."""
    return {
        "dataset_id": world.google,
        "entity_type": "campaign",
        "entity_id": world.google_campaigns[0],
        "captured_at": AT,
        "status": "active",
        "budget_daily": Decimal("50"),
        **values,
    }


async def _world(sessions: Sessions, campaigns: int = 3) -> World:
    """Commit the datasets, accounts and campaigns the scenarios write facts against."""
    async with sessions() as session, session.begin():
        ids: dict[str, int] = {}
        for source in ("google", "linkedin"):
            ids[source] = await _insert(
                session,
                "source_dataset",
                {"source": source, "mode": "simulated", "state": "active", "role": "primary"},
            )
            ids[f"{source}_account"] = await _insert(
                session,
                "ad_account",
                {
                    "source": source,
                    "dataset_id": ids[source],
                    "external_id": f"{source}-account",
                    "currency": "USD",
                },
            )
        google_campaigns = [
            await _insert(
                session,
                "campaign",
                {
                    **_campaign_row(ids["google_account"], number),
                    "source": "google",
                    "dataset_id": ids["google"],
                },
            )
            for number in range(campaigns)
        ]
        linkedin_campaign = await _insert(
            session,
            "campaign",
            {
                "source": "linkedin",
                "dataset_id": ids["linkedin"],
                "account_id": ids["linkedin_account"],
                "external_id": "linkedin-campaign",
                "name": "Lead gen",
                "status": "active",
                "raw_status": "ACTIVE",
            },
        )
    return World(
        google=ids["google"],
        google_account=ids["google_account"],
        google_campaigns=tuple(google_campaigns),
        linkedin=ids["linkedin"],
        linkedin_account=ids["linkedin_account"],
        linkedin_campaign=linkedin_campaign,
    )


async def _count(sessions: Sessions, table_name: str) -> int:
    """Return the committed row count of ``table_name``."""
    async with sessions() as session:
        table = Base.metadata.tables[table_name]
        result = await session.execute(sa.select(sa.func.count()).select_from(table))
        return int(result.scalar_one())


# ---------------------------------------------------------------------------------------
# Non-finite amounts
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("spelling", NON_FINITE)
@pytest.mark.parametrize("column", ["cost", "conversions"])
async def test_upsert_facts_refuses_a_non_finite_amount_and_stores_nothing(
    sessions: Sessions, column: str, spelling: str
) -> None:
    """A non-finite money or conversion value anywhere in the batch refuses the whole batch."""
    world = await _world(sessions)
    campaign = world.google_campaigns[0]
    rows: list[Row] = [
        _daily_row(campaign, DAY),
        _daily_row(campaign, date(2026, 9, 11), **{column: Decimal(spelling)}),
    ]

    with pytest.raises(
        ValueError,
        match=rf"^fact_campaign_daily\.{column}: non-finite Decimal .+ is not accepted",
    ):
        async with sessions() as session, session.begin():
            await upsert_facts(FactCampaignDaily, rows, world.google, session=session)

    assert await _count(sessions, "fact_campaign_daily") == 0


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("spelling", NON_FINITE)
async def test_upsert_facts_refuses_a_non_finite_segment_cost(
    sessions: Sessions, spelling: str
) -> None:
    """``fact_segment_window.cost`` is guarded like the daily tables' amounts."""
    world = await _world(sessions)
    rows: list[Row] = [_segment_row(world.linkedin_campaign, cost=Decimal(spelling))]

    with pytest.raises(ValueError, match=r"^fact_segment_window\.cost: non-finite Decimal"):
        async with sessions() as session, session.begin():
            await upsert_facts(FactSegmentWindow, rows, world.linkedin, session=session)

    assert await _count(sessions, "fact_segment_window") == 0


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("spelling", NON_FINITE)
async def test_upsert_entities_refuses_a_non_finite_budget_and_stores_nothing(
    sessions: Sessions, spelling: str
) -> None:
    """A non-finite campaign budget refuses the batch; the stored campaigns are unchanged."""
    world = await _world(sessions)
    rows: list[Row] = [
        _campaign_row(world.google_account, 100),
        _campaign_row(world.google_account, 0, daily_budget=Decimal(spelling)),
    ]

    with pytest.raises(ValueError, match=r"^campaign\.daily_budget: non-finite Decimal"):
        async with sessions() as session, session.begin():
            await upsert_entities(session, Campaign, rows, world.google)

    # The Google campaigns and the LinkedIn one: the new campaign of the batch is not stored.
    assert await _count(sessions, "campaign") == len(world.google_campaigns) + 1
    async with sessions() as session:
        budgets = await session.scalars(
            sa.select(Campaign.daily_budget).where(Campaign.dataset_id == world.google)
        )
        assert set(budgets) == {Decimal("50")}


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("spelling", NON_FINITE)
async def test_append_snapshots_refuses_a_non_finite_budget_and_stores_nothing(
    sessions: Sessions, spelling: str
) -> None:
    """A non-finite observed budget refuses the batch before any snapshot is inserted."""
    world = await _world(sessions)
    rows: list[Row] = [
        _snapshot_row(world),
        _snapshot_row(world, budget_daily=None, budget_total=Decimal(spelling)),
    ]

    with pytest.raises(ValueError, match=r"^entity_snapshot\.budget_total: non-finite Decimal"):
        async with sessions() as session, session.begin():
            await append_snapshots(rows, session=session)

    assert await _count(sessions, "entity_snapshot") == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_finite_amounts_are_still_stored_exactly(sessions: Sessions) -> None:
    """Zero and finite positive amounts pass the guard and are stored unchanged."""
    world = await _world(sessions)
    campaign = world.google_campaigns[0]
    rows: list[Row] = [
        _daily_row(campaign, DAY, cost=Decimal("0"), conversions=Decimal("0.333333")),
        _daily_row(campaign, date(2026, 9, 11), cost=Decimal("1234.5"), conversions=None),
    ]

    async with sessions() as session, session.begin():
        assert await upsert_facts(FactCampaignDaily, rows, world.google, session=session) == 2

    async with sessions() as session:
        stored = await session.execute(
            sa.select(FactCampaignDaily.cost, FactCampaignDaily.conversions).order_by(
                FactCampaignDaily.date
            )
        )
        assert [tuple(row) for row in stored] == [
            (Decimal("0"), Decimal("0.333333")),
            (Decimal("1234.5"), None),
        ]


# ---------------------------------------------------------------------------------------
# Native days and instants
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 9, 10, 23, 0, tzinfo=NEW_YORK),
        datetime(2026, 9, 10, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 12, 0, tzinfo=UTC).replace(tzinfo=None),
    ],
    ids=["new-york-evening", "utc-midnight", "naive"],
)
async def test_upsert_facts_refuses_a_datetime_for_a_native_day(
    sessions: Sessions, value: datetime
) -> None:
    """A ``datetime`` for ``fact_campaign_daily.date`` is refused, never cast to its UTC day."""
    world = await _world(sessions)
    rows: list[Row] = [_daily_row(world.google_campaigns[0], value)]

    with pytest.raises(
        ValueError,
        match=r"^fact_campaign_daily\.date: datetime .+ is not accepted for a date column",
    ):
        async with sessions() as session, session.begin():
            await upsert_facts(FactCampaignDaily, rows, world.google, session=session)

    assert await _count(sessions, "fact_campaign_daily") == 0


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("column", ["window_start", "window_end"])
async def test_upsert_facts_refuses_a_datetime_for_a_segment_window_day(
    sessions: Sessions, column: str
) -> None:
    """``window_start`` and ``window_end`` are native days too, so a ``datetime`` is refused."""
    world = await _world(sessions)
    day = {"window_start": date(2026, 9, 4), "window_end": date(2026, 10, 1)}[column]
    instant = datetime(day.year, day.month, day.day, 22, 0, tzinfo=LOS_ANGELES)
    rows: list[Row] = [_segment_row(world.linkedin_campaign, **{column: instant})]

    with pytest.raises(
        ValueError,
        match=rf"^fact_segment_window\.{column}: datetime .+ is not accepted for a date column",
    ):
        async with sessions() as session, session.begin():
            await upsert_facts(FactSegmentWindow, rows, world.linkedin, session=session)

    assert await _count(sessions, "fact_segment_window") == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_upsert_entities_refuses_a_bare_date_for_an_instant(sessions: Sessions) -> None:
    """A bare ``date`` for a ``timestamptz`` column would be read as UTC midnight: refused."""
    world = await _world(sessions)
    rows: list[Row] = [_group_row(world, run_start=date(2026, 9, 1))]

    with pytest.raises(
        ValueError,
        match=r"^campaign_group\.run_start: date .+ is not accepted for a timestamptz column",
    ):
        async with sessions() as session, session.begin():
            await upsert_entities(session, CampaignGroup, rows, world.linkedin)

    assert await _count(sessions, "campaign_group") == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_native_days_and_instants_are_stored_unchanged(sessions: Sessions) -> None:
    """Native days keep their date whatever the session's time zone, and a tz-aware instant
    keeps its instant."""
    world = await _world(sessions)
    run_start = datetime(2026, 9, 1, 8, 30, tzinfo=NEW_YORK)

    async with sessions() as session, session.begin():
        # Fourteen hours ahead of UTC: a day cast through an instant would move here.
        await session.execute(sa.text("SET LOCAL TIME ZONE 'Pacific/Kiritimati'"))
        await upsert_facts(
            FactCampaignDaily,
            [_daily_row(world.google_campaigns[0], DAY)],
            world.google,
            session=session,
        )
        await upsert_facts(
            FactSegmentWindow,
            [_segment_row(world.linkedin_campaign)],
            world.linkedin,
            session=session,
        )
        await upsert_entities(
            session, CampaignGroup, [_group_row(world, run_start=run_start)], world.linkedin
        )

    async with sessions() as session:
        assert list(await session.scalars(sa.select(FactCampaignDaily.date))) == [DAY]
        windows = await session.execute(
            sa.select(FactSegmentWindow.window_start, FactSegmentWindow.window_end)
        )
        assert [tuple(window) for window in windows] == [(date(2026, 9, 4), date(2026, 10, 1))]
        stored = await session.scalar(sa.select(CampaignGroup.run_start))
        assert stored == run_start
        assert stored is not None
        assert stored.utcoffset() is not None


# ---------------------------------------------------------------------------------------
# Natural-key write order
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Statement:
    """One ``INSERT`` a session sent: the columns it supplies and its rows' natural keys.

    Attributes:
        columns: The column names of the ``INSERT`` column list, in statement order.
        keys: The natural-key tuple of each row, in the order the rows were sent.
    """

    columns: tuple[str, ...]
    keys: tuple[tuple[Any, ...], ...]


_POSITIONED = re.compile(r"(?P<column>.+)__(?P<position>\d+)")
"""An ``insertmanyvalues`` parameter name: the column and the row's position in the batch."""


def _sent_rows(parameters: object) -> list[dict[str, Any]]:
    """Return the rows of one cursor execution, as column-name dicts in the order sent.

    ``executemany`` sends a sequence of row dicts and a single row is sent as its own dict.
    An ``INSERT`` rendered by ``insertmanyvalues`` (one with ``RETURNING``) sends one dict
    whose keys carry each row's position as a ``__<n>`` suffix.
    """
    if isinstance(parameters, list | tuple):
        return [dict(row) for row in parameters]
    assert isinstance(parameters, dict)
    suffixed = {str(name): _POSITIONED.fullmatch(str(name)) for name in parameters}
    if not any(suffixed.values()):
        return [dict(parameters)]
    rows: dict[int, dict[str, Any]] = {}
    for name, match in suffixed.items():
        assert match is not None, f"parameter {name!r} carries no row position"
        rows.setdefault(int(match["position"]), {})[match["column"]] = parameters[name]
    return [rows[position] for position in sorted(rows)]


@asynccontextmanager
async def _inserts(
    session: AsyncSession, table_name: str, key: tuple[str, ...]
) -> AsyncIterator[list[Statement]]:
    """Collect every ``INSERT INTO <table_name>`` the session's connection sends in the block."""
    connection = (await session.connection()).sync_connection
    assert connection is not None
    sent: list[Statement] = []
    prefix = f"INSERT INTO {table_name} ("

    def record(
        _connection: object, _cursor: object, statement: str, parameters: object, *_: object
    ) -> None:
        if statement.startswith(prefix):
            columns = statement[len(prefix) : statement.index(")")].split(", ")
            rows = _sent_rows(parameters)
            sent.append(
                Statement(
                    columns=tuple(columns),
                    keys=tuple(tuple(row[name] for name in key) for row in rows),
                )
            )

    event.listen(connection, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        event.remove(connection, "before_cursor_execute", record)


def _daily_rows(world: World, days: int) -> list[dict[str, Any]]:
    """Return one ``fact_campaign_daily`` row per Google campaign and day, in ascending key."""
    return [
        _daily_row(campaign, DAY + timedelta(days=offset))
        for campaign in world.google_campaigns
        for offset in range(days)
    ]


def _orders(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Return ``rows`` reversed and deterministically scrambled, as two caller orders.

    The scrambled order steps through ``rows`` by a stride coprime with their count, so it
    visits every row once and keeps almost no neighbour of the ascending order.
    """
    count = len(rows)
    stride = next(step for step in range(count // 3 + 1, count + 1) if math.gcd(step, count) == 1)
    shuffled = [rows[(position * stride) % count] for position in range(count)]
    return {"reversed": rows[::-1], "shuffled": shuffled}


FACT_KEY: tuple[str, ...] = ("dataset_id", "campaign_id", "date")
"""``FactCampaignDaily.__natural_key__``."""

CAMPAIGN_KEY: tuple[str, ...] = ("dataset_id", "account_id", "external_id")
"""``Campaign.__natural_key__``."""


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("order", ["reversed", "shuffled"])
async def test_upsert_facts_sends_rows_in_ascending_natural_key_order(
    sessions: Sessions, order: str
) -> None:
    """Whatever the caller's order, a homogeneous batch is one statement whose rows run in
    ascending natural-key order, and a repeated key still keeps its last occurrence."""
    world = await _world(sessions)
    ascending = _daily_rows(world, days=20)
    rows: list[Row] = list(_orders(ascending)[order])
    restated = {**ascending[7], "cost": Decimal("99")}
    rows.append(restated)

    async with (
        sessions() as session,
        session.begin(),
        _inserts(session, "fact_campaign_daily", FACT_KEY) as sent,
    ):
        written = await upsert_facts(FactCampaignDaily, rows, world.google, session=session)

    assert written == len(ascending)
    expected = [(world.google, row["campaign_id"], row["date"]) for row in ascending]
    assert [statement.keys for statement in sent] == [tuple(expected)]
    async with sessions() as session:
        costs = await session.scalars(
            sa.select(FactCampaignDaily.cost).where(
                FactCampaignDaily.campaign_id == restated["campaign_id"],
                FactCampaignDaily.date == restated["date"],
            )
        )
        assert list(costs) == [Decimal("99")]


@pytest.mark.anyio
@pytest.mark.db
async def test_a_heterogeneous_batch_runs_consecutive_column_sets_in_key_order(
    sessions: Sessions,
) -> None:
    """Rows supplying different column sets are sent as consecutive runs of one column set
    each, so the whole batch still takes its row locks in ascending natural-key order."""
    world = await _world(sessions)
    ascending = [
        # Every third row omits ``conversions``: it is neither inserted as NULL nor overwritten.
        {name: value for name, value in row.items() if name != "conversions" or number % 3 != 1}
        for number, row in enumerate(_daily_rows(world, days=4))
    ]
    rows: list[Row] = list(_orders(ascending)["shuffled"])

    async with (
        sessions() as session,
        session.begin(),
        _inserts(session, "fact_campaign_daily", FACT_KEY) as sent,
    ):
        written = await upsert_facts(FactCampaignDaily, rows, world.google, session=session)

    assert written == len(ascending)
    runs = [
        (supplies_conversions, len(list(members)))
        for supplies_conversions, members in itertools.groupby(
            ascending, key=lambda row: "conversions" in row
        )
    ]
    assert len(runs) > 2
    assert [("conversions" in st.columns, len(st.keys)) for st in sent] == runs
    expected = [(world.google, row["campaign_id"], row["date"]) for row in ascending]
    assert [key for statement in sent for key in statement.keys] == expected


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("order", ["reversed", "shuffled"])
async def test_upsert_entities_sends_rows_in_ascending_natural_key_order(
    sessions: Sessions, order: str
) -> None:
    """Entity rows are sent in ascending natural-key order, and the returned map still holds
    every row's stored id."""
    world = await _world(sessions, campaigns=0)
    ascending = [_campaign_row(world.google_account, number) for number in range(30)]
    rows: list[Row] = list(_orders(ascending)[order])

    async with (
        sessions() as session,
        session.begin(),
        _inserts(session, "campaign", CAMPAIGN_KEY) as sent,
    ):
        ids = await upsert_entities(session, Campaign, rows, world.google)

    expected = [(world.google, world.google_account, row["external_id"]) for row in ascending]
    assert [key for statement in sent for key in statement.keys] == expected
    async with sessions() as session:
        stored = await session.execute(
            sa.select(
                Campaign.dataset_id, Campaign.account_id, Campaign.external_id, Campaign.id
            ).where(Campaign.dataset_id == world.google)
        )
        assert ids == {
            (dataset, account, external): id_ for dataset, account, external, id_ in stored
        }
    assert set(ids) == set(expected)


def test_the_natural_key_order_never_compares_values_of_different_types() -> None:
    """Values of different types in one key column are ordered by type first, so sorting a
    batch never raises ``TypeError``; equal-typed values keep their natural order."""
    info = _model_info(FactCampaignDaily)
    dataset = SourceDataset(id=1, source="google")
    rows: list[Row] = [
        _daily_row(5, DAY),
        {**_daily_row(4, DAY), "campaign_id": "4"},
        _daily_row(3, DAY + timedelta(days=1)),
        _daily_row(3, DAY),
    ]

    prepared = _prepare(rows, info, dataset)

    assert [(row["campaign_id"], row["date"]) for row in prepared] == [
        (3, DAY),
        (3, DAY + timedelta(days=1)),
        (5, DAY),
        ("4", DAY),
    ]


# ---------------------------------------------------------------------------------------
# Concurrent writers of overlapping keys
# ---------------------------------------------------------------------------------------


async def _writer(
    sessions: Sessions,
    write: Callable[[AsyncSession, int], Awaitable[object]],
    iterations: int,
    barrier: asyncio.Barrier,
) -> int:
    """Run ``write`` in a transaction of its own per iteration, each started with the rival.

    Returns:
        The number of committed iterations.

    Raises:
        BaseException: Whatever an iteration raised; the barrier is aborted first, so the
            rival writer stops instead of waiting for this one.
    """
    committed = 0
    try:
        for iteration in range(iterations):
            await barrier.wait()
            async with sessions() as session, session.begin():
                await write(session, iteration)
            committed += 1
    except BaseException:
        await barrier.abort()
        raise
    return committed


def _failures(results: list[int | BaseException]) -> list[str]:
    """Describe every writer that raised, with the SQLSTATE of a database error."""
    described: list[str] = []
    for result in results:
        if isinstance(result, BaseException):
            state = None
            if isinstance(result, DBAPIError) and isinstance(result.orig, psycopg.Error):
                state = result.orig.sqlstate
            described.append(f"{type(result).__name__} (SQLSTATE {state})")
    return described


@pytest.mark.anyio
@pytest.mark.db
async def test_fact_writers_of_the_same_keys_in_opposite_orders_never_deadlock(
    sessions: Sessions,
) -> None:
    """Two transactions upserting the same 600 daily rows, one in forward and one in reverse
    order, started together for many rounds, all commit: no 40P01, and the table holds each
    key once with one writer's values."""
    world = await _world(sessions)
    ascending = _daily_rows(world, days=200)
    iterations = 12
    barrier = asyncio.Barrier(2)

    def facts_writer(order: list[dict[str, Any]], offset: int) -> Callable[..., Awaitable[int]]:
        async def write(session: AsyncSession, iteration: int) -> int:
            cost = Decimal(offset + iteration)
            rows: list[Row] = [{**row, "cost": cost} for row in order]
            return await upsert_facts(FactCampaignDaily, rows, world.google, session=session)

        return write

    results = await asyncio.gather(
        _writer(sessions, facts_writer(ascending, 0), iterations, barrier),
        _writer(sessions, facts_writer(ascending[::-1], 1000), iterations, barrier),
        return_exceptions=True,
    )

    assert _failures(list(results)) == []
    assert results == [iterations, iterations]
    async with sessions() as session:
        counts = await session.execute(
            sa.select(
                sa.func.count(),
                sa.func.count(
                    sa.distinct(sa.tuple_(FactCampaignDaily.campaign_id, FactCampaignDaily.date))
                ),
                sa.func.count(sa.distinct(FactCampaignDaily.cost)),
                sa.func.count(sa.distinct(FactCampaignDaily.fetched_at)),
            )
        )
        assert tuple(counts.one()) == (len(ascending), len(ascending), 1, 1)


@pytest.mark.anyio
@pytest.mark.db
async def test_entity_writers_of_the_same_keys_in_opposite_orders_never_deadlock(
    sessions: Sessions,
) -> None:
    """Two transactions upserting the same 600 campaigns in opposite orders, started together
    for several rounds, all commit with no 40P01 and leave each campaign stored once."""
    world = await _world(sessions, campaigns=0)
    ascending = [_campaign_row(world.google_account, number) for number in range(600)]
    iterations = 8
    barrier = asyncio.Barrier(2)

    def entities_writer(order: list[dict[str, Any]]) -> Callable[..., Awaitable[int]]:
        async def write(session: AsyncSession, iteration: int) -> int:
            rows: list[Row] = [{**row, "name": f"Round {iteration}"} for row in order]
            return len(await upsert_entities(session, Campaign, rows, world.google))

        return write

    results = await asyncio.gather(
        _writer(sessions, entities_writer(ascending), iterations, barrier),
        _writer(sessions, entities_writer(ascending[::-1]), iterations, barrier),
        return_exceptions=True,
    )

    assert _failures(list(results)) == []
    assert results == [iterations, iterations]
    assert await _count(sessions, "campaign") == len(ascending) + 1
