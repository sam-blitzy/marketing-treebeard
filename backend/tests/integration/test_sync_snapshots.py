"""Tests for ``ads_engine.sync.snapshots``: snapshots, baselines and C0 discrepancy detection.

The pure parts need no database: the :class:`SnapshotRow` builders and their validation,
:class:`EnumerationResult` invariants and :class:`DetectionReport`.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds every table of the model metadata in it, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. Each test builds its own datasets; a test that
needs an active dataset first retires the source's previous active primary, because at most
one may be active per source. An unreachable database fails the database tests; it never
skips them.

The scenarios follow the C0 rows of AAP 0.11.2 (``acceptance/test_campaign_rules.py``): no
discrepancy right after baseline; a 1-micro Google and a one-minor-unit LinkedIn difference;
status mismatch; ``untracked`` only after the baseline is sealed; a shared-budget amount
change raising one budget-subject discrepancy and none on members; a member moved to another
budget; Q-G2b ``REMOVED`` snapshotted as ``ended``; ``missing`` only after a complete
enumeration; tombstones; ``awaiting_platform`` auto-resolution.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import (
    AdAccount,
    Budget,
    Campaign,
    ConversionAction,
    Keyword,
)
from ads_engine.db.models.analysis import AnalysisRequest
from ads_engine.db.models.decisions import Decision, Discrepancy
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.db.repositories import DatasetNotWritable
from ads_engine.decisions import (
    DecisionPatch,
    SubjectRef,
    record_decision,
    resolve_discrepancy,
)
from ads_engine.sync import snapshots
from ads_engine.sync.freshness import REASON_INTERRUPTED, FreshnessOutcome, record_freshness
from ads_engine.sync.snapshots import (
    CONVERSION_ACTION_PAYLOAD_KEYS,
    DetectionReport,
    EnumerationResult,
    SnapshotRow,
    baseline_sealed,
    detect_discrepancies,
    ensure_baseline,
    write_snapshots,
)

type Sessions = async_sessionmaker[AsyncSession]

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The first sync; later syncs are whole hours after it."""

OWN_MICROS = 100_000_000
SHARED_MICROS = 50_000_000


def _at(hour: float) -> datetime:
    """Return the instant ``hour`` hours after :data:`T0`."""
    return T0 + timedelta(hours=hour)


# ---------------------------------------------------------------------------------------
# Pure: SnapshotRow builders and validation
# ---------------------------------------------------------------------------------------


def test_conversion_action_payload_keys_are_the_b4_contract_keys() -> None:
    assert CONVERSION_ACTION_PAYLOAD_KEYS == (
        "status",
        "include_in_conversions_metric",
        "counting_type",
        "attribution_model",
    )


def test_google_campaign_derives_the_period_amount_from_micros() -> None:
    daily = SnapshotRow.campaign(
        source="google",
        entity_id=3,
        status="active",
        raw_status="ENABLED",
        budget_id=9,
        budget_period="DAILY",
        budget_amount_micros=12_345_678,
        explicitly_shared=False,
    )
    assert daily.budget_daily == Decimal("12.345678")
    assert daily.budget_total is None
    assert daily.budget_amount_micros == 12_345_678
    assert daily.budget_id == 9
    assert dict(daily.payload) == {
        "raw_status": "ENABLED",
        "explicitly_shared": False,
        "budget_period": "DAILY",
    }
    lifetime = SnapshotRow.campaign(
        source="google",
        entity_id=3,
        status="paused",
        raw_status="PAUSED",
        budget_id=9,
        budget_period="CUSTOM_PERIOD",
        budget_amount_micros=1_000_000,
    )
    assert (lifetime.budget_daily, lifetime.budget_total) == (None, Decimal("1.000000"))


def test_google_campaign_without_budget_and_with_unknown_amount() -> None:
    bare = SnapshotRow.campaign(source="google", entity_id=1, status="ended", raw_status="REMOVED")
    assert (bare.budget_id, bare.budget_amount_micros, bare.budget_daily) == (None, None, None)
    unknown = SnapshotRow.campaign(
        source="google",
        entity_id=1,
        status="active",
        raw_status="ENABLED",
        budget_id=2,
        budget_period="DAILY",
        budget_amount_micros=None,
        explicitly_shared=True,
    )
    assert (unknown.budget_daily, unknown.budget_total, unknown.budget_amount_micros) == (
        None,
        None,
        None,
    )
    assert unknown.payload["explicitly_shared"] is True


def test_linkedin_campaign_keeps_its_own_budgets_and_group() -> None:
    row = SnapshotRow.campaign(
        source="linkedin",
        entity_id=5,
        status="active",
        raw_status="ACTIVE",
        daily_budget=Decimal("25.00"),
        total_budget=Decimal("900.00"),
        campaign_group_urn="urn:li:sponsoredCampaignGroup:77",
    )
    assert (row.budget_daily, row.budget_total) == (Decimal("25.00"), Decimal("900.00"))
    assert row.budget_amount_micros is None
    assert row.budget_id is None
    assert row.payload["campaign_group"] == "urn:li:sponsoredCampaignGroup:77"
    assert row.payload["raw_status"] == "ACTIVE"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": "linkedin", "budget_id": 4},
        {"source": "linkedin", "budget_amount_micros": 1},
        {"source": "linkedin", "budget_period": "DAILY"},
        {"source": "linkedin", "explicitly_shared": True},
        {"source": "google", "daily_budget": Decimal(1)},
        {"source": "google", "total_budget": Decimal(1)},
        {"source": "google", "campaign_group_urn": "urn:li:sponsoredCampaignGroup:1"},
        {"source": "google", "budget_period": "DAILY"},
        {"source": "google", "budget_amount_micros": 5},
        {"source": "google", "budget_id": 4, "budget_amount_micros": 5},
        {"source": "google", "budget_id": 4, "budget_period": "WEEKLY"},
        {"source": "google", "budget_id": 4, "budget_period": "DAILY", "budget_amount_micros": -1},
        {"source": "google", "status": "ENABLED"},
        {"source": "google", "raw_status": " "},
        {"source": "hubspot"},
        {"source": "linkedin", "daily_budget": 2.5},
        {"source": "linkedin", "daily_budget": Decimal("-1")},
        {"source": "linkedin", "daily_budget": Decimal("NaN")},
        {"source": "google", "entity_id": 0},
        {"source": "google", "entity_id": True},
        {"source": "google", "budget_id": 4, "explicitly_shared": 1},
    ],
)
def test_campaign_builder_rejects_invalid_arguments(kwargs: dict[str, object]) -> None:
    arguments: dict[str, object] = {
        "entity_id": 1,
        "status": "active",
        "raw_status": "ENABLED",
    }
    arguments.update(kwargs)
    with pytest.raises(ValueError):  # noqa: PT011 - every case is a ValueError by contract
        SnapshotRow.campaign(**arguments)  # type: ignore[arg-type]


def test_budget_builder_carries_period_amount_and_no_status() -> None:
    daily = SnapshotRow.budget(entity_id=7, period="DAILY", amount_micros=1, explicitly_shared=True)
    assert daily.status is None
    assert daily.budget_daily == Decimal("0.000001")
    assert daily.budget_total is None
    assert dict(daily.payload) == {"period": "DAILY", "explicitly_shared": True}
    custom = SnapshotRow.budget(
        entity_id=7, period="CUSTOM_PERIOD", amount_micros=None, explicitly_shared=False
    )
    assert (custom.budget_daily, custom.budget_total, custom.budget_amount_micros) == (
        None,
        None,
        None,
    )
    with pytest.raises(ValueError, match="period"):
        SnapshotRow.budget(entity_id=7, period="MONTHLY", amount_micros=1, explicitly_shared=True)
    with pytest.raises(ValueError, match="explicitly_shared"):
        SnapshotRow.budget(entity_id=7, period="DAILY", amount_micros=1, explicitly_shared=1)  # type: ignore[arg-type]


def test_keyword_builder_keeps_quality_score_including_none() -> None:
    row = SnapshotRow.keyword(entity_id=8, status="ENABLED", match_type="EXACT", quality_score=7)
    assert row.quality_score == 7
    assert row.status == "ENABLED"
    assert dict(row.payload) == {"status": "ENABLED", "match_type": "EXACT"}
    assert (
        SnapshotRow.keyword(entity_id=8, status="ENABLED", match_type="BROAD", quality_score=None)
    ).quality_score is None
    for bad in (0, 11, True):
        with pytest.raises(ValueError, match="quality_score"):
            SnapshotRow.keyword(
                entity_id=8, status="ENABLED", match_type="EXACT", quality_score=bad
            )


def test_creative_and_campaign_group_builders() -> None:
    creative = SnapshotRow.creative(
        entity_id=4, intended_status="ACTIVE", lead_form_urn="urn:li:leadGenForm:9"
    )
    assert creative.source == "linkedin"
    assert creative.status == "ACTIVE"
    assert dict(creative.payload) == {"lead_form_urn": "urn:li:leadGenForm:9"}
    assert (
        SnapshotRow.creative(entity_id=4, intended_status=None, lead_form_urn=None).status is None
    )
    start = datetime(2026, 9, 1, tzinfo=UTC)
    group = SnapshotRow.campaign_group(
        entity_id=2, status="ACTIVE", daily_budget=Decimal(10), run_start=start
    )
    assert group.budget_daily == Decimal(10)
    assert dict(group.payload) == {"run_start": start.isoformat(), "run_end": None}
    with pytest.raises(ValueError, match="run_end"):
        SnapshotRow.campaign_group(entity_id=2, status="ACTIVE", run_end=datetime(2026, 9, 1))  # noqa: DTZ001


def test_conversion_action_builder_carries_every_contract_key() -> None:
    row = SnapshotRow.conversion_action(
        entity_id=6,
        status="ENABLED",
        include_in_conversions_metric=True,
        counting_type="ONE_PER_CLICK",
        attribution_model="GOOGLE_ADS_LAST_CLICK",
    )
    assert set(row.payload) == set(CONVERSION_ACTION_PAYLOAD_KEYS)
    assert row.payload["attribution_model"] == "GOOGLE_ADS_LAST_CLICK"
    unknown = SnapshotRow.conversion_action(
        entity_id=6,
        status="REMOVED",
        include_in_conversions_metric=False,
        counting_type=None,
        attribution_model=None,
    )
    assert unknown.payload["counting_type"] is None
    with pytest.raises(ValueError, match="include_in_conversions_metric"):
        SnapshotRow.conversion_action(
            entity_id=6,
            status="ENABLED",
            include_in_conversions_metric="yes",  # type: ignore[arg-type]
            counting_type=None,
            attribution_model=None,
        )


def test_direct_conversion_action_row_without_contract_keys_is_refused() -> None:
    with pytest.raises(ValueError, match="attribution_model"):
        SnapshotRow(
            source="google",
            entity_type="conversion_action",
            entity_id=1,
            status="ENABLED",
            payload={
                "status": "ENABLED",
                "include_in_conversions_metric": True,
                "counting_type": "ONE_PER_CLICK",
            },
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"entity_type": "ad_group"}, "entity_type"),
        ({"entity_type": "keyword", "source": "linkedin"}, "comes from"),
        ({"entity_type": "creative", "source": "google"}, "comes from"),
        ({"budget_amount_micros": 5, "source": "linkedin"}, "budget_amount_micros"),
        ({"entity_type": "keyword", "budget_id": 3}, "budget_id"),
        ({"quality_score": 5}, "quality_score"),
        ({"payload": {"spend": 1.5}}, "float"),
        ({"payload": {"spend": Decimal(1)}}, "Decimal"),
        ({"payload": {1: "x"}}, "key"),
        ({"payload": ["x"]}, "mapping"),
        ({"status": 3, "entity_type": "keyword"}, "status"),
    ],
)
def test_direct_rows_are_validated(kwargs: dict[str, object], match: str) -> None:
    arguments: dict[str, object] = {
        "source": "google",
        "entity_type": "campaign",
        "entity_id": 1,
        "status": "active",
    }
    arguments.update(kwargs)
    if arguments["entity_type"] != "campaign" and "status" not in kwargs:
        arguments["status"] = "ENABLED"
    with pytest.raises(ValueError, match=match):
        SnapshotRow(**arguments)  # type: ignore[arg-type]


def test_payload_is_frozen_and_thawed_for_jsonb() -> None:
    source = {"nested": {"values": [1, "a", None, True]}}
    row = SnapshotRow(
        source="linkedin", entity_type="creative", entity_id=1, status=None, payload=source
    )
    source["nested"]["values"].append("later")
    with pytest.raises(TypeError):
        row.payload["extra"] = 1  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        row.status = "PAUSED"  # type: ignore[misc]
    stored = row.to_row(11, T0)
    assert stored["payload"] == {"nested": {"values": [1, "a", None, True]}}
    assert stored["dataset_id"] == 11
    assert stored["captured_at"] == T0
    assert row.key == ("creative", 1)
    with pytest.raises(ValueError, match="captured_at"):
        row.to_row(11, datetime(2026, 10, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="dataset_id"):
        row.to_row(0, T0)


def test_entity_type_tables_must_cover_entity_types(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots._check_tables()
    monkeypatch.setattr(snapshots, "_ENTITY_MODELS", {"campaign": Campaign})
    with pytest.raises(RuntimeError, match="_ENTITY_MODELS"):
        snapshots._check_tables()
    monkeypatch.undo()
    stray = dict(snapshots._ENTITY_SOURCES)
    stray["campaign"] = frozenset({"bing"})
    monkeypatch.setattr(snapshots, "_ENTITY_SOURCES", stray)
    with pytest.raises(RuntimeError, match="unknown"):
        snapshots._check_tables()


# ---------------------------------------------------------------------------------------
# Pure: EnumerationResult and DetectionReport
# ---------------------------------------------------------------------------------------


def test_enumeration_normalises_ids_to_frozensets() -> None:
    result = EnumerationResult(
        source="google",
        complete=True,
        present_campaign_ids=[1, 2],  # type: ignore[arg-type]
        missing_campaign_ids={3},  # type: ignore[arg-type]
    )
    assert result.present_campaign_ids == frozenset({1, 2})
    assert isinstance(result.missing_campaign_ids, frozenset)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"source": "hubspot"}, "google or linkedin"),
        ({"complete": False, "missing_campaign_ids": frozenset({1})}, "safe absence"),
        ({"complete": False, "missing_budget_ids": frozenset({1})}, "safe absence"),
        ({"source": "linkedin", "present_budget_ids": frozenset({1})}, "LinkedIn"),
        ({"source": "linkedin", "removed_campaign_ids": frozenset({1})}, "LinkedIn"),
        (
            {"present_campaign_ids": frozenset({1}), "missing_campaign_ids": frozenset({1})},
            "both",
        ),
        (
            {"present_campaign_ids": frozenset({1}), "removed_campaign_ids": frozenset({1})},
            "both",
        ),
        ({"present_budget_ids": frozenset({2}), "missing_budget_ids": frozenset({2})}, "both"),
        ({"present_campaign_ids": frozenset({0})}, "positive"),
        ({"present_campaign_ids": "12"}, "collection"),
        ({"complete": 1}, "bool"),
        ({"reason": "  "}, "blank"),
    ],
)
def test_enumeration_invariants(kwargs: dict[str, object], match: str) -> None:
    arguments: dict[str, object] = {"source": "google", "complete": True}
    arguments.update(kwargs)
    with pytest.raises(ValueError, match=match):
        EnumerationResult(**arguments)  # type: ignore[arg-type]


def test_detection_report_totals_its_raised_counts() -> None:
    report = DetectionReport(
        raised={"status": 1, "budget": 2, "untracked": 0, "missing": 1},
        refreshed=0,
        auto_resolved=0,
        checked=4,
        skipped=0,
    )
    assert report.total_raised == 4


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
            "for the snapshot and discrepancy tests"
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
    schema = f"snapshots_test_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
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
    """Yield a session factory on an engine created and disposed inside this test's loop."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------------------


async def _dataset(session: AsyncSession, source: str, state: str = "active") -> int:
    """Insert a primary dataset; an active one first retires the source's previous one."""
    if state == "active":
        await session.execute(
            update(SourceDataset)
            .where(
                SourceDataset.source == source,
                SourceDataset.state == "active",
                SourceDataset.role == "primary",
            )
            .values(state="retired", retired_at=T0)
        )
    dataset = SourceDataset(source=source, mode="simulated", state=state, role="primary")
    session.add(dataset)
    await session.flush()
    return dataset.id


@dataclass(frozen=True)
class GoogleWorld:
    """A Google dataset: one own-budget campaign and two members of one shared budget."""

    dataset_id: int
    account_id: int
    own_budget: int
    shared_budget: int
    other_shared: int
    own: int
    member_a: int
    member_b: int


async def _budget(
    session: AsyncSession, dataset_id: int, external_id: str, micros: int | None, *, shared: bool
) -> int:
    """Insert a Google DAILY budget and return its id."""
    row = Budget(
        source="google",
        dataset_id=dataset_id,
        external_id=external_id,
        amount=None if micros is None else Decimal(micros) / Decimal(1_000_000),
        amount_micros=micros,
        period="DAILY",
        explicitly_shared=shared,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _google_campaign(
    session: AsyncSession,
    dataset_id: int,
    account_id: int,
    external_id: str,
    budget_id: int,
    *,
    own_micros: int | None = None,
) -> int:
    """Insert a Google campaign on a DAILY budget and return its id."""
    row = Campaign(
        source="google",
        dataset_id=dataset_id,
        account_id=account_id,
        external_id=external_id,
        name=f"Campaign {external_id}",
        status="active",
        raw_status="ENABLED",
        objective_or_channel="SEARCH",
        budget_id=budget_id,
        daily_budget=None if own_micros is None else Decimal(own_micros) / Decimal(1_000_000),
        budget_period="DAILY",
    )
    session.add(row)
    await session.flush()
    return row.id


async def _google_world(sessions: Sessions, *, state: str = "active") -> GoogleWorld:
    """Commit a Google dataset with its account, three budgets and three campaigns."""
    async with sessions() as session, session.begin():
        dataset_id = await _dataset(session, "google", state)
        account = AdAccount(
            source="google",
            dataset_id=dataset_id,
            external_id="1234567890",
            name="Acme",
            currency="USD",
            timezone="America/New_York",
        )
        session.add(account)
        await session.flush()
        own_budget = await _budget(session, dataset_id, "501", OWN_MICROS, shared=False)
        shared_budget = await _budget(session, dataset_id, "502", SHARED_MICROS, shared=True)
        other_shared = await _budget(session, dataset_id, "503", 70_000_000, shared=True)
        own = await _google_campaign(
            session, dataset_id, account.id, "11", own_budget, own_micros=OWN_MICROS
        )
        member_a = await _google_campaign(session, dataset_id, account.id, "12", shared_budget)
        member_b = await _google_campaign(session, dataset_id, account.id, "13", shared_budget)
        return GoogleWorld(
            dataset_id=dataset_id,
            account_id=account.id,
            own_budget=own_budget,
            shared_budget=shared_budget,
            other_shared=other_shared,
            own=own,
            member_a=member_a,
            member_b=member_b,
        )


def _google_rows(
    world: GoogleWorld,
    *,
    own_status: str = "active",
    own_micros: int = OWN_MICROS,
    shared_micros: int = SHARED_MICROS,
    member_b_budget: int | None = None,
    without: frozenset[int] = frozenset(),
) -> list[SnapshotRow]:
    """Return one sync's Google snapshot rows for the world's platform state."""
    member_b_budget = world.shared_budget if member_b_budget is None else member_b_budget
    rows = [
        SnapshotRow.campaign(
            source="google",
            entity_id=world.own,
            status=own_status,
            raw_status="ENABLED" if own_status == "active" else "PAUSED",
            budget_id=world.own_budget,
            budget_period="DAILY",
            budget_amount_micros=own_micros,
        ),
        SnapshotRow.campaign(
            source="google",
            entity_id=world.member_a,
            status="active",
            raw_status="ENABLED",
            budget_id=world.shared_budget,
            budget_period="DAILY",
            budget_amount_micros=shared_micros,
            explicitly_shared=True,
        ),
        SnapshotRow.campaign(
            source="google",
            entity_id=world.member_b,
            status="active",
            raw_status="ENABLED",
            budget_id=member_b_budget,
            budget_period="DAILY",
            budget_amount_micros=shared_micros
            if member_b_budget == world.shared_budget
            else 70_000_000,
            explicitly_shared=True,
        ),
        SnapshotRow.budget(
            entity_id=world.own_budget,
            period="DAILY",
            amount_micros=own_micros,
            explicitly_shared=False,
        ),
        SnapshotRow.budget(
            entity_id=world.shared_budget,
            period="DAILY",
            amount_micros=shared_micros,
            explicitly_shared=True,
        ),
        SnapshotRow.budget(
            entity_id=world.other_shared,
            period="DAILY",
            amount_micros=70_000_000,
            explicitly_shared=True,
        ),
    ]
    return [row for row in rows if row.entity_id not in without or row.entity_type == "budget"]


@dataclass(frozen=True)
class SyncResult:
    """What one simulated entity sync did."""

    sealed: bool
    baselines: int
    report: DetectionReport
    snapshot_ids: dict[tuple[str, int], int]


async def _sync(
    sessions: Sessions,
    dataset_id: int,
    rows: Sequence[SnapshotRow],
    *,
    at: datetime,
    source: str = "google",
    complete: bool = True,
    removed: frozenset[int] = frozenset(),
    missing_campaigns: frozenset[int] = frozenset(),
    missing_budgets: frozenset[int] = frozenset(),
) -> SyncResult:
    """Run the snapshot, baseline, detection and freshness steps of one entity sync."""
    async with sessions() as session, session.begin():
        sealed = await baseline_sealed(session, dataset_id)
        ids = await write_snapshots(session, dataset_id, rows, captured_at=at)
        baselines = await ensure_baseline(session, dataset_id, captured_at=at, sealed=sealed)
        campaigns = frozenset(row.entity_id for row in rows if row.entity_type == "campaign")
        budgets = frozenset(row.entity_id for row in rows if row.entity_type == "budget")
        enumeration = EnumerationResult(
            source=source,
            complete=complete,
            present_campaign_ids=campaigns - removed,
            present_budget_ids=budgets,
            removed_campaign_ids=removed,
            missing_campaign_ids=missing_campaigns,
            missing_budget_ids=missing_budgets,
            reason=None if complete else f"{REASON_INTERRUPTED}: page deferred",
        )
        report = await detect_discrepancies(
            session, dataset_id, enumeration, ids, sealed=sealed, now=at
        )
        await record_freshness(
            session,
            dataset_id,
            f"{source}.entities",
            FreshnessOutcome.success(
                at,
                covered_from=None,
                covered_through=None,
                complete=complete,
                reason=None if complete else f"{REASON_INTERRUPTED}: page deferred",
            ),
        )
    return SyncResult(sealed=sealed, baselines=baselines, report=report, snapshot_ids=ids)


async def _open(sessions: Sessions, dataset_id: int) -> list[Discrepancy]:
    """Return the dataset's non-resolved discrepancies, oldest first."""
    async with sessions() as session:
        result = await session.scalars(
            select(Discrepancy)
            .where(Discrepancy.dataset_id == dataset_id, Discrepancy.state != "resolved")
            .order_by(Discrepancy.id)
        )
        return list(result)


async def _decisions(sessions: Sessions, dataset_id: int) -> list[Decision]:
    """Return the dataset's decisions in insertion order."""
    async with sessions() as session:
        result = await session.scalars(
            select(Decision).where(Decision.dataset_id == dataset_id).order_by(Decision.id)
        )
        return list(result)


def _kinds(rows: Sequence[Discrepancy]) -> list[tuple[str, int, str]]:
    """Return ``(subject_type, subject_id, kind)`` per discrepancy."""
    return [(row.subject_type, row.subject_id, row.kind) for row in rows]


# ---------------------------------------------------------------------------------------
# Database: write_snapshots
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_write_snapshots_appends_one_row_per_entity_with_one_instant(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions, state="building")
    rows = _google_rows(world)
    async with sessions() as session, session.begin():
        first = await write_snapshots(session, world.dataset_id, rows, captured_at=_at(0))
        second = await write_snapshots(session, world.dataset_id, rows, captured_at=_at(3))
    assert set(first) == {row.key for row in rows}
    assert set(first.values()).isdisjoint(second.values())
    async with sessions() as session:
        stored = list(
            await session.scalars(
                select(EntitySnapshot)
                .where(EntitySnapshot.dataset_id == world.dataset_id)
                .order_by(EntitySnapshot.id)
            )
        )
    assert len(stored) == 2 * len(rows)
    assert {row.captured_at for row in stored} == {_at(0), _at(3)}
    by_id = {row.id: row for row in stored}
    own = by_id[first[("campaign", world.own)]]
    assert (own.status, own.budget_amount_micros, own.budget_daily, own.budget_id) == (
        "active",
        OWN_MICROS,
        Decimal("100.000000"),
        world.own_budget,
    )
    assert own.source == "google"
    shared = by_id[first[("budget", world.shared_budget)]]
    assert shared.status is None
    assert shared.payload == {"period": "DAILY", "explicitly_shared": True}


@pytest.mark.anyio
async def test_write_snapshots_stores_quality_scores_and_counting_contract(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions, state="building")
    async with sessions() as session, session.begin():
        from ads_engine.db.models.accounts import AdGroup

        ad_group = AdGroup(
            source="google",
            dataset_id=world.dataset_id,
            campaign_id=world.own,
            external_id="901",
            name="Group",
            status="ENABLED",
        )
        session.add(ad_group)
        await session.flush()
        keyword = Keyword(
            source="google",
            dataset_id=world.dataset_id,
            ad_group_id=ad_group.id,
            criterion_id="77",
            text="crm software",
            match_type="PHRASE",
            status="ENABLED",
            quality_score=None,
        )
        action = ConversionAction(
            source="google",
            dataset_id=world.dataset_id,
            external_id="31",
            name="Demo request",
            status="ENABLED",
            include_in_conversions_metric=True,
            counting_type="ONE_PER_CLICK",
            attribution_model="GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
        )
        session.add_all([keyword, action])
        await session.flush()
        ids = await write_snapshots(
            session,
            world.dataset_id,
            [
                SnapshotRow.keyword(
                    entity_id=keyword.id, status="ENABLED", match_type="PHRASE", quality_score=None
                ),
                SnapshotRow.conversion_action(
                    entity_id=action.id,
                    status="ENABLED",
                    include_in_conversions_metric=True,
                    counting_type="ONE_PER_CLICK",
                    attribution_model="GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
                ),
            ],
            captured_at=_at(0),
        )
        stored_action = await session.get(EntitySnapshot, ids[("conversion_action", action.id)])
        stored_keyword = await session.get(EntitySnapshot, ids[("keyword", keyword.id)])
    assert stored_action is not None
    assert stored_keyword is not None
    assert set(stored_action.payload) == set(CONVERSION_ACTION_PAYLOAD_KEYS)
    assert stored_action.payload["attribution_model"] == "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"
    assert stored_keyword.quality_score is None
    assert stored_keyword.payload == {"status": "ENABLED", "match_type": "PHRASE"}


@pytest.mark.anyio
async def test_write_snapshots_refuses_bad_input(sessions: Sessions) -> None:
    world = await _google_world(sessions, state="building")
    other = await _google_world(sessions, state="building")
    row = _google_rows(world)[0]
    async with sessions() as session:
        assert await write_snapshots(session, world.dataset_id, [], captured_at=_at(0)) == {}
        with pytest.raises(ValueError, match="more than once"):
            await write_snapshots(session, world.dataset_id, [row, row], captured_at=_at(0))
        with pytest.raises(ValueError, match="captured_at"):
            await write_snapshots(
                session,
                world.dataset_id,
                [row],
                captured_at=datetime(2026, 10, 1),  # noqa: DTZ001
            )
        foreign = SnapshotRow.campaign(
            source="google", entity_id=other.own, status="active", raw_status="ENABLED"
        )
        with pytest.raises(ValueError, match="not campaign rows of dataset"):
            await write_snapshots(session, world.dataset_id, [foreign], captured_at=_at(0))
        linkedin = SnapshotRow.campaign(
            source="linkedin", entity_id=world.own, status="active", raw_status="ACTIVE"
        )
        with pytest.raises(ValueError, match="source"):
            await write_snapshots(session, world.dataset_id, [linkedin], captured_at=_at(0))
        await session.rollback()
    async with sessions() as session, session.begin():
        await session.execute(
            update(SourceDataset)
            .where(SourceDataset.id == world.dataset_id)
            .values(state="retired", retired_at=_at(0))
        )
    async with sessions() as session:
        with pytest.raises(DatasetNotWritable):
            await write_snapshots(session, world.dataset_id, [row], captured_at=_at(1))


# ---------------------------------------------------------------------------------------
# Database: baseline
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_baseline_sealed_reads_the_entities_freshness_row(sessions: Sessions) -> None:
    world = await _google_world(sessions, state="building")
    async with sessions() as session, session.begin():
        assert await baseline_sealed(session, world.dataset_id) is False
        await record_freshness(
            session,
            world.dataset_id,
            "google.entities",
            FreshnessOutcome.success(
                _at(0), covered_from=None, covered_through=None, complete=False, reason="x"
            ),
        )
        assert await baseline_sealed(session, world.dataset_id) is False
        await record_freshness(
            session,
            world.dataset_id,
            "google.entities",
            FreshnessOutcome.success(
                _at(1), covered_from=None, covered_through=None, complete=True
            ),
        )
        assert await baseline_sealed(session, world.dataset_id) is True
        hubspot = await _dataset(session, "hubspot", "building")
        with pytest.raises(ValueError, match="hubspot"):
            await baseline_sealed(session, hubspot)
        with pytest.raises(ValueError, match="does not exist"):
            await baseline_sealed(session, 987_654_321)


@pytest.mark.anyio
async def test_first_sync_writes_one_baseline_per_shape_and_raises_nothing(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    first = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    assert first.sealed is False
    assert first.baselines == 5
    assert first.report.total_raised == 0
    assert first.report.checked == 5
    decisions = await _decisions(sessions, world.dataset_id)
    shapes = {(row.subject_type, row.subject_id): row for row in decisions}
    assert set(shapes) == {
        ("campaign", world.own),
        ("campaign", world.member_a),
        ("campaign", world.member_b),
        ("budget", world.shared_budget),
        ("budget", world.other_shared),
    }
    assert ("budget", world.own_budget) not in shapes
    own = shapes[("campaign", world.own)]
    assert (own.shape, own.status, own.daily_budget, own.origin) == (
        "own_budget",
        "active",
        Decimal("100.000000"),
        "baseline",
    )
    assert own.decided_at == _at(0)
    member = shapes[("campaign", world.member_a)]
    assert (member.shape, member.budget_id, member.daily_budget) == (
        "shared_member",
        world.shared_budget,
        None,
    )
    budget = shapes[("budget", world.shared_budget)]
    assert (budget.shape, budget.status, budget.daily_budget) == (
        "budget",
        None,
        Decimal("50.000000"),
    )
    second = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(3))
    assert second.sealed is True
    assert second.baselines == 0
    assert second.report.total_raised == 0
    assert await _open(sessions, world.dataset_id) == []
    async with sessions() as session:
        again = await ensure_baseline(session, world.dataset_id, captured_at=_at(0), sealed=False)
    assert again == 0


@pytest.mark.anyio
async def test_baseline_waits_for_a_shared_budget_amount_google_omitted(sessions: Sessions) -> None:
    world = await _google_world(sessions)
    rows = [
        row
        if row.key != ("budget", world.shared_budget)
        else SnapshotRow.budget(
            entity_id=world.shared_budget,
            period="DAILY",
            amount_micros=None,
            explicitly_shared=True,
        )
        for row in _google_rows(world)
    ]
    first = await _sync(sessions, world.dataset_id, rows, at=_at(0), complete=False)
    assert first.baselines == 4
    assert first.report.skipped == 1
    assert first.report.total_raised == 0
    second = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(3))
    assert second.sealed is False
    assert second.baselines == 1
    subjects = {
        (row.subject_type, row.subject_id) for row in await _decisions(sessions, world.dataset_id)
    }
    assert ("budget", world.shared_budget) in subjects


@pytest.mark.anyio
async def test_ensure_baseline_refuses_hubspot_and_naive_instants(sessions: Sessions) -> None:
    async with sessions() as session, session.begin():
        hubspot = await _dataset(session, "hubspot", "building")
        with pytest.raises(ValueError, match="hubspot"):
            await ensure_baseline(session, hubspot, captured_at=_at(0), sealed=False)
        with pytest.raises(ValueError, match="captured_at"):
            await ensure_baseline(
                session,
                hubspot,
                captured_at=datetime(2026, 10, 1),  # noqa: DTZ001
                sealed=False,
            )


# ---------------------------------------------------------------------------------------
# Database: C0 detection
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_one_micro_difference_on_a_google_budget_raises_budget(sessions: Sessions) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    result = await _sync(
        sessions, world.dataset_id, _google_rows(world, own_micros=OWN_MICROS + 1), at=_at(3)
    )
    assert dict(result.report.raised) == {"status": 0, "budget": 1, "untracked": 0, "missing": 0}
    (row,) = await _open(sessions, world.dataset_id)
    assert (row.subject_type, row.subject_id, row.kind) == ("campaign", world.own, "budget")
    assert row.decided_value is not None
    assert row.platform_value is not None
    assert row.decided_value["amount_micros"] == OWN_MICROS
    assert row.platform_value["amount_micros"] == OWN_MICROS + 1
    assert row.snapshot_id == result.snapshot_ids[("campaign", world.own)]
    async with sessions() as session:
        requests = await session.scalar(
            select(func.count())
            .select_from(AnalysisRequest)
            .where(AnalysisRequest.reason == "discrepancy_created")
        )
    assert requests is not None
    assert requests >= 1


@pytest.mark.anyio
async def test_status_mismatch_raises_status_and_reruns_refresh_it(sessions: Sessions) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    paused = _google_rows(world, own_status="paused")
    first = await _sync(sessions, world.dataset_id, paused, at=_at(3))
    assert first.report.raised["status"] == 1
    again = await _sync(sessions, world.dataset_id, paused, at=_at(6))
    assert again.report.total_raised == 0
    assert again.report.refreshed == 1
    (row,) = await _open(sessions, world.dataset_id)
    assert row.kind == "status"
    assert row.decided_value == {"status": "active"}
    assert row.platform_value == {"status": "paused"}
    assert row.detected_at == _at(3)
    assert row.snapshot_id == again.snapshot_ids[("campaign", world.own)]


@pytest.mark.anyio
async def test_shared_amount_change_raises_one_budget_subject_discrepancy(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    result = await _sync(
        sessions, world.dataset_id, _google_rows(world, shared_micros=60_000_000), at=_at(3)
    )
    assert result.report.total_raised == 1
    assert _kinds(await _open(sessions, world.dataset_id)) == [
        ("budget", world.shared_budget, "budget")
    ]


@pytest.mark.anyio
async def test_member_moved_to_another_budget_raises_its_assignment(sessions: Sessions) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    async with sessions() as session, session.begin():
        await session.execute(
            update(Campaign)
            .where(Campaign.id == world.member_b)
            .values(budget_id=world.other_shared)
        )
    result = await _sync(
        sessions,
        world.dataset_id,
        _google_rows(world, member_b_budget=world.other_shared),
        at=_at(3),
    )
    assert result.report.total_raised == 1
    (row,) = await _open(sessions, world.dataset_id)
    assert (row.subject_type, row.subject_id, row.kind) == ("campaign", world.member_b, "budget")
    assert row.decided_value is not None
    assert row.platform_value is not None
    assert row.decided_value["budget_id"] == world.shared_budget
    assert row.platform_value["budget_id"] == world.other_shared


@pytest.mark.anyio
async def test_untracked_only_after_a_complete_entity_sync_seals_the_baseline(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    first = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0), complete=False)
    assert first.baselines == 5
    async with sessions() as session, session.begin():
        late = await _google_campaign(
            session,
            world.dataset_id,
            world.account_id,
            "14",
            world.own_budget,
            own_micros=OWN_MICROS,
        )
    late_row = SnapshotRow.campaign(
        source="google",
        entity_id=late,
        status="active",
        raw_status="ENABLED",
        budget_id=world.own_budget,
        budget_period="DAILY",
        budget_amount_micros=OWN_MICROS,
    )
    second = await _sync(sessions, world.dataset_id, [*_google_rows(world), late_row], at=_at(3))
    assert second.sealed is False
    assert second.baselines == 1
    assert second.report.total_raised == 0
    async with sessions() as session, session.begin():
        newer = await _google_campaign(
            session,
            world.dataset_id,
            world.account_id,
            "15",
            world.own_budget,
            own_micros=OWN_MICROS,
        )
    newer_row = SnapshotRow.campaign(
        source="google",
        entity_id=newer,
        status="paused",
        raw_status="PAUSED",
        budget_id=world.own_budget,
        budget_period="DAILY",
        budget_amount_micros=OWN_MICROS,
    )
    third = await _sync(
        sessions, world.dataset_id, [*_google_rows(world), late_row, newer_row], at=_at(6)
    )
    assert third.sealed is True
    assert third.baselines == 0
    assert dict(third.report.raised) == {"status": 0, "budget": 0, "untracked": 1, "missing": 0}
    (row,) = await _open(sessions, world.dataset_id)
    assert (row.subject_id, row.kind, row.decided_value, row.decision_id) == (
        newer,
        "untracked",
        None,
        None,
    )
    assert row.platform_value is not None
    assert row.platform_value["status"] == "paused"


@pytest.mark.anyio
async def test_removed_campaign_is_compared_as_ended(sessions: Sessions) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    async with sessions() as session, session.begin():
        await session.execute(
            update(Campaign)
            .where(Campaign.id == world.own)
            .values(status="ended", raw_status="REMOVED")
        )
    removed_row = SnapshotRow.campaign(
        source="google",
        entity_id=world.own,
        status="ended",
        raw_status="REMOVED",
        budget_id=world.own_budget,
        budget_period="DAILY",
        budget_amount_micros=OWN_MICROS,
    )
    rows = [*_google_rows(world, without=frozenset({world.own})), removed_row]
    result = await _sync(
        sessions, world.dataset_id, rows, at=_at(3), removed=frozenset({world.own})
    )
    assert result.report.raised["status"] == 1
    (row,) = await _open(sessions, world.dataset_id)
    assert row.platform_value == {"status": "ended"}
    async with sessions() as session:
        snapshot = await session.get(EntitySnapshot, result.snapshot_ids[("campaign", world.own)])
    assert snapshot is not None
    assert snapshot.status == "ended"
    assert snapshot.payload["raw_status"] == "REMOVED"


@pytest.mark.anyio
async def test_missing_only_after_a_complete_enumeration(sessions: Sessions) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    absent = _google_rows(world, without=frozenset({world.own}))
    cut_short = await _sync(sessions, world.dataset_id, absent, at=_at(3), complete=False)
    assert cut_short.report.total_raised == 0
    assert await _open(sessions, world.dataset_id) == []
    complete = await _sync(
        sessions,
        world.dataset_id,
        absent,
        at=_at(6),
        missing_campaigns=frozenset({world.own}),
    )
    assert dict(complete.report.raised) == {"status": 0, "budget": 0, "untracked": 0, "missing": 1}
    (row,) = await _open(sessions, world.dataset_id)
    assert (row.subject_id, row.kind, row.snapshot_id, row.platform_value) == (
        world.own,
        "missing",
        None,
        None,
    )
    assert row.decided_value is not None
    assert row.decided_value["status"] == "active"


@pytest.mark.anyio
async def test_missing_shared_budget_raises_on_the_budget_subject_only(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    rows = [row for row in _google_rows(world) if row.key != ("budget", world.other_shared)]
    result = await _sync(
        sessions,
        world.dataset_id,
        rows,
        at=_at(3),
        missing_budgets=frozenset({world.other_shared}),
    )
    # The shared budget was baselined from its first snapshot, so it is tracked; a budget
    # that is not explicitly shared is never a subject and is ignored if named.
    assert result.report.raised["missing"] == 1
    assert _kinds(await _open(sessions, world.dataset_id)) == [
        ("budget", world.other_shared, "missing")
    ]


@pytest.mark.anyio
async def test_tombstone_suppresses_missing_until_the_campaign_reappears(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    absent = _google_rows(world, without=frozenset({world.own}))
    await _sync(
        sessions, world.dataset_id, absent, at=_at(3), missing_campaigns=frozenset({world.own})
    )
    (missing,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        resolved = await resolve_discrepancy(session, missing.id, "acknowledge_removal", now=_at(4))
    assert resolved.state == "resolved"
    still_absent = await _sync(
        sessions, world.dataset_id, absent, at=_at(6), missing_campaigns=frozenset({world.own})
    )
    assert still_absent.report.total_raised == 0
    assert still_absent.report.skipped == 1
    assert await _open(sessions, world.dataset_id) == []
    returned_partially = await _sync(
        sessions, world.dataset_id, _google_rows(world), at=_at(9), complete=False
    )
    assert returned_partially.report.total_raised == 0
    reappeared = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(12))
    assert reappeared.report.raised["status"] == 1
    (row,) = await _open(sessions, world.dataset_id)
    assert row.kind == "status"
    assert row.decided_value == {"status": "ended", "tombstone": True}
    assert row.platform_value == {"status": "active"}


@pytest.mark.anyio
async def test_awaiting_platform_auto_resolves_when_the_platform_matches(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    await _sync(sessions, world.dataset_id, _google_rows(world, own_status="paused"), at=_at(3))
    (row,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        waiting = await resolve_discrepancy(session, row.id, "decision_correct", now=_at(4))
    assert waiting.state == "awaiting_platform"
    unchanged = await _sync(
        sessions, world.dataset_id, _google_rows(world, own_status="paused"), at=_at(6)
    )
    assert unchanged.report.auto_resolved == 0
    fixed = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(9))
    assert fixed.report.auto_resolved == 1
    assert await _open(sessions, world.dataset_id) == []
    async with sessions() as session:
        closed = await session.get(Discrepancy, row.id)
    assert closed is not None
    assert (closed.state, closed.resolution, closed.resolved_at) == (
        "resolved",
        "decision_correct",
        _at(9),
    )


@pytest.mark.anyio
async def test_missing_after_decision_correct_closes_on_a_complete_return(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    absent = _google_rows(world, without=frozenset({world.own}))
    await _sync(
        sessions, world.dataset_id, absent, at=_at(3), missing_campaigns=frozenset({world.own})
    )
    (missing,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        await resolve_discrepancy(session, missing.id, "decision_correct", now=_at(4))
    partial = await _sync(
        sessions, world.dataset_id, _google_rows(world), at=_at(6), complete=False
    )
    assert partial.report.auto_resolved == 0
    complete = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(9))
    assert complete.report.auto_resolved == 1
    assert await _open(sessions, world.dataset_id) == []


@pytest.mark.anyio
async def test_a_snapshot_older_than_the_decision_is_not_judged(sessions: Sessions) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    async with sessions() as session, session.begin():
        sealed = await baseline_sealed(session, world.dataset_id)
        ids = await write_snapshots(
            session, world.dataset_id, _google_rows(world), captured_at=_at(3)
        )
        # The user pauses the campaign in Google and records it while this sync is running.
        await record_decision(
            session,
            SubjectRef(type="campaign", id=world.own),
            DecisionPatch(status="paused"),
            origin="user",
            decided_by="ops@example.com",
            now=_at(3.5),
        )
        enumeration = EnumerationResult(
            source="google",
            complete=True,
            present_campaign_ids=frozenset({world.own, world.member_a, world.member_b}),
            present_budget_ids=frozenset(
                {world.own_budget, world.shared_budget, world.other_shared}
            ),
        )
        report = await detect_discrepancies(
            session, world.dataset_id, enumeration, ids, sealed=sealed, now=_at(3.6)
        )
    assert report.total_raised == 0
    assert report.skipped == 1
    later = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(6))
    assert later.report.raised["status"] == 1


async def _linkedin_world(sessions: Sessions) -> tuple[int, int, int]:
    """Commit a LinkedIn dataset with one account and one campaign; return their ids."""
    async with sessions() as session, session.begin():
        dataset_id = await _dataset(session, "linkedin")
        account = AdAccount(
            source="linkedin",
            dataset_id=dataset_id,
            external_id="508",
            name="Acme LinkedIn",
            currency="USD",
            timezone="UTC",
        )
        session.add(account)
        await session.flush()
        campaign = Campaign(
            source="linkedin",
            dataset_id=dataset_id,
            account_id=account.id,
            external_id="urn:li:sponsoredCampaign:61",
            name="White paper A",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
            daily_budget=Decimal("25.00"),
        )
        session.add(campaign)
        await session.flush()
        return dataset_id, account.id, campaign.id


def _linkedin_row(campaign_id: int, daily: str) -> SnapshotRow:
    """Return a LinkedIn campaign snapshot with the given daily budget."""
    return SnapshotRow.campaign(
        source="linkedin",
        entity_id=campaign_id,
        status="active",
        raw_status="ACTIVE",
        daily_budget=Decimal(daily),
        campaign_group_urn="urn:li:sponsoredCampaignGroup:5",
    )


@pytest.mark.anyio
async def test_linkedin_minor_unit_difference_and_safe_absence(sessions: Sessions) -> None:
    dataset_id, account_id, campaign_id = await _linkedin_world(sessions)
    first = await _sync(
        sessions, dataset_id, [_linkedin_row(campaign_id, "25.00")], at=_at(0), source="linkedin"
    )
    assert (first.baselines, first.report.total_raised) == (1, 0)
    (baseline,) = await _decisions(sessions, dataset_id)
    assert (baseline.shape, baseline.daily_budget, baseline.total_budget) == (
        "own_budget",
        Decimal("25.00"),
        None,
    )
    cent = await _sync(
        sessions, dataset_id, [_linkedin_row(campaign_id, "25.01")], at=_at(3), source="linkedin"
    )
    assert dict(cent.report.raised) == {"status": 0, "budget": 1, "untracked": 0, "missing": 0}
    (row,) = await _open(sessions, dataset_id)
    assert row.platform_value is not None
    assert Decimal(row.platform_value["daily_budget"]) == Decimal("25.01")
    assert "amount_micros" not in row.platform_value

    async with sessions() as session, session.begin():
        stray = Campaign(
            source="linkedin",
            dataset_id=dataset_id,
            account_id=account_id,
            external_id="urn:li:sponsoredCampaign:62",
            name="Never baselined",
            status="paused",
            raw_status="PAUSED",
            objective_or_channel="LEAD_GENERATION",
        )
        session.add(stray)
        await session.flush()
        stray_id = stray.id
    cut_short = await _sync(sessions, dataset_id, [], at=_at(6), source="linkedin", complete=False)
    assert cut_short.report.raised["missing"] == 0
    assert cut_short.report.checked == 0
    complete = await _sync(
        sessions,
        dataset_id,
        [],
        at=_at(9),
        source="linkedin",
        missing_campaigns=frozenset({campaign_id, stray_id}),
    )
    assert complete.report.raised["missing"] == 1
    assert complete.report.skipped == 1
    assert sorted(kind for _, _, kind in _kinds(await _open(sessions, dataset_id))) == [
        "budget",
        "missing",
    ]


@pytest.mark.anyio
async def test_detection_refuses_inconsistent_input(sessions: Sessions) -> None:
    world = await _google_world(sessions, state="building")
    async with sessions() as session:
        ids = await write_snapshots(
            session, world.dataset_id, _google_rows(world), captured_at=_at(0)
        )
        linkedin = EnumerationResult(source="linkedin", complete=True)
        with pytest.raises(ValueError, match="describes linkedin"):
            await detect_discrepancies(
                session, world.dataset_id, linkedin, ids, sealed=False, now=_at(0)
            )
        present = EnumerationResult(
            source="google", complete=True, present_campaign_ids=frozenset({world.own})
        )
        with pytest.raises(ValueError, match="no snapshot"):
            await detect_discrepancies(
                session, world.dataset_id, present, {}, sealed=False, now=_at(0)
            )
        with pytest.raises(ValueError, match="now"):
            await detect_discrepancies(
                session,
                world.dataset_id,
                present,
                ids,
                sealed=False,
                now=datetime(2026, 10, 1),  # noqa: DTZ001
            )
        other = await _google_world(sessions, state="building")
        foreign = EnumerationResult(
            source="google", complete=True, present_campaign_ids=frozenset({other.own})
        )
        with pytest.raises(ValueError, match="belongs to dataset"):
            await detect_discrepancies(
                session,
                world.dataset_id,
                foreign,
                {("campaign", other.own): ids[("campaign", world.own)]},
                sealed=False,
                now=_at(0),
            )
        wrong = {("campaign", world.own): ids[("campaign", world.member_a)]}
        with pytest.raises(ValueError, match="is not a snapshot of"):
            await detect_discrepancies(
                session, world.dataset_id, present, wrong, sealed=False, now=_at(0)
            )
        await session.rollback()
