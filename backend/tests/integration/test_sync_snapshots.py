"""Tests for ``ads_engine.sync.snapshots``: snapshots, baselines and C0 discrepancy detection.

The pure parts need no database: the :class:`SnapshotRow` builders and their validation,
:class:`EnumerationResult` invariants, :class:`DetectionReport`, and
:func:`~ads_engine.decisions.verified_match`, the match detection closes discrepancies on.

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
from collections import Counter
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypedDict

import pytest
from sqlalchemy import create_engine, event, func, select, update
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
    BudgetPeriod,
    DecisionPatch,
    DecisionShape,
    DecisionSource,
    PlatformStateUnknown,
    SubjectRef,
    SubjectState,
    auto_resolve,
    auto_resolve_absent,
    compare,
    load_subject_state,
    raise_discrepancy,
    record_decision,
    resolve_discrepancy,
    verified_match,
    write_baseline,
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


# Storage bounds: entity_snapshot's money columns are numeric(18, 6); its ids and
# budget_amount_micros are bigint. A validated row must be stored exactly as observed.

LARGEST_AMOUNT = Decimal("999999999999.999999")
"""The largest amount ``numeric(18, 6)`` stores exactly."""

LARGEST_MICROS = 999_999_999_999_999_999
""":data:`LARGEST_AMOUNT` in micros (``10**18 - 1``)."""

BIGINT_MAX = 2**63 - 1


def _money_rows(amount: Decimal) -> list[SnapshotRow]:
    """Build every row that carries ``amount`` in ``budget_daily`` and ``budget_total``."""
    return [
        SnapshotRow.campaign(
            source="linkedin",
            entity_id=1,
            status="active",
            raw_status="ACTIVE",
            daily_budget=amount,
            total_budget=amount,
        ),
        SnapshotRow.campaign_group(
            entity_id=1, status="ACTIVE", daily_budget=amount, total_budget=amount
        ),
        SnapshotRow(
            source="google",
            entity_type="budget",
            entity_id=1,
            budget_daily=amount,
            budget_total=amount,
        ),
    ]


@pytest.mark.parametrize(
    "amount",
    [
        Decimal(0),
        Decimal("0.000001"),
        Decimal("1.2345670000"),
        Decimal("12.3456780000000000000000000000"),
        Decimal("1E+3"),
        LARGEST_AMOUNT,
    ],
)
def test_amounts_numeric_18_6_stores_exactly_are_kept_as_observed(amount: Decimal) -> None:
    for row in _money_rows(amount):
        assert row.budget_daily is amount
        assert row.budget_total is amount
        assert row.to_row(1, T0)["budget_daily"] is amount


@pytest.mark.parametrize(
    ("amount", "match"),
    [
        (Decimal("1.2345678"), "finer than six decimal places"),
        (Decimal("0.0000001"), "finer than six decimal places"),
        (Decimal("12.3456780000000000000000000001"), "finer than six decimal places"),
        (Decimal("999999999999.9999995"), "finer than six decimal places"),
        (Decimal(10**12), r"at least 10\*\*12"),
        (Decimal("1E+12"), r"at least 10\*\*12"),
        (Decimal("1000000000000.000000"), r"at least 10\*\*12"),
        (Decimal(2**63), r"at least 10\*\*12"),
        (Decimal("-0.01"), "non-negative"),
        (Decimal("NaN"), "finite"),
        (Decimal("sNaN"), "finite"),
        (Decimal("Infinity"), "finite"),
    ],
)
def test_amounts_numeric_18_6_cannot_store_exactly_are_refused(amount: Decimal, match: str) -> None:
    for column in ("budget_daily", "budget_total"):
        with pytest.raises(ValueError, match=match):
            SnapshotRow(source="google", entity_type="budget", entity_id=1, **{column: amount})  # type: ignore[arg-type]
    for argument in ("daily_budget", "total_budget"):
        with pytest.raises(ValueError, match=match):
            SnapshotRow.campaign(
                source="linkedin",
                entity_id=1,
                status="active",
                raw_status="ACTIVE",
                **{argument: amount},  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match=match):
            SnapshotRow.campaign_group(entity_id=1, status="ACTIVE", **{argument: amount})  # type: ignore[arg-type]


def test_a_float_amount_is_still_refused() -> None:
    with pytest.raises(ValueError, match="Decimal"):
        SnapshotRow.campaign_group(entity_id=1, status="ACTIVE", daily_budget=2.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("period", ["DAILY", "CUSTOM_PERIOD"])
def test_the_largest_storable_micros_yield_the_largest_storable_amount(period: str) -> None:
    campaign = SnapshotRow.campaign(
        source="google",
        entity_id=1,
        status="active",
        raw_status="ENABLED",
        budget_id=2,
        budget_period=period,
        budget_amount_micros=LARGEST_MICROS,
    )
    budget = SnapshotRow.budget(
        entity_id=2, period=period, amount_micros=LARGEST_MICROS, explicitly_shared=True
    )
    direct = SnapshotRow(
        source="google", entity_type="budget", entity_id=2, budget_amount_micros=LARGEST_MICROS
    )
    assert direct.budget_amount_micros == LARGEST_MICROS
    for row in (campaign, budget):
        assert row.budget_amount_micros == LARGEST_MICROS
        amount = row.budget_daily if period == "DAILY" else row.budget_total
        assert amount == LARGEST_AMOUNT
        assert str(amount) == "999999999999.999999"


@pytest.mark.parametrize("micros", [10**18, BIGINT_MAX, 2**63])
def test_micros_beyond_the_money_range_are_refused(micros: int) -> None:
    match = r"below 10\*\*18"
    with pytest.raises(ValueError, match=match):
        SnapshotRow.campaign(
            source="google",
            entity_id=1,
            status="active",
            raw_status="ENABLED",
            budget_id=2,
            budget_period="DAILY",
            budget_amount_micros=micros,
        )
    with pytest.raises(ValueError, match=match):
        SnapshotRow.budget(
            entity_id=2, period="CUSTOM_PERIOD", amount_micros=micros, explicitly_shared=False
        )
    for entity_type in ("campaign", "budget"):
        with pytest.raises(ValueError, match=match):
            SnapshotRow(
                source="google",
                entity_type=entity_type,
                entity_id=1,
                status="active" if entity_type == "campaign" else None,
                budget_amount_micros=micros,
            )


def test_ids_up_to_the_bigint_maximum_are_accepted() -> None:
    campaign = SnapshotRow.campaign(
        source="google",
        entity_id=BIGINT_MAX,
        status="active",
        raw_status="ENABLED",
        budget_id=BIGINT_MAX,
        budget_period="DAILY",
        budget_amount_micros=1,
    )
    assert (campaign.entity_id, campaign.budget_id) == (BIGINT_MAX, BIGINT_MAX)
    assert campaign.to_row(BIGINT_MAX, T0)["dataset_id"] == BIGINT_MAX
    keyword = SnapshotRow.keyword(
        entity_id=BIGINT_MAX, status="ENABLED", match_type="EXACT", quality_score=None
    )
    assert keyword.entity_id == BIGINT_MAX
    enumeration = EnumerationResult(
        source="google",
        complete=True,
        present_campaign_ids=frozenset({BIGINT_MAX}),
        present_budget_ids=frozenset({BIGINT_MAX}),
    )
    assert enumeration.present_campaign_ids == frozenset({BIGINT_MAX})


def test_ids_beyond_bigint_are_refused() -> None:
    beyond = 2**63
    with pytest.raises(ValueError, match=r"entity_id.*beyond bigint"):
        SnapshotRow(source="google", entity_type="campaign", entity_id=beyond, status="active")
    with pytest.raises(ValueError, match=r"entity_id.*beyond bigint"):
        SnapshotRow.keyword(entity_id=beyond, status="ENABLED", match_type="EXACT", quality_score=7)
    with pytest.raises(ValueError, match=r"budget_id.*beyond bigint"):
        SnapshotRow.campaign(
            source="google",
            entity_id=1,
            status="active",
            raw_status="ENABLED",
            budget_id=beyond,
            budget_period="DAILY",
            budget_amount_micros=1,
        )
    row = SnapshotRow.creative(entity_id=1, intended_status="ACTIVE", lead_form_urn=None)
    with pytest.raises(ValueError, match=r"dataset_id.*beyond bigint"):
        row.to_row(beyond, T0)


@pytest.mark.parametrize(
    "name",
    [
        "present_campaign_ids",
        "present_budget_ids",
        "removed_campaign_ids",
        "missing_campaign_ids",
        "missing_budget_ids",
    ],
)
def test_enumeration_ids_beyond_bigint_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match=rf"{name}.*beyond bigint"):
        EnumerationResult(source="google", complete=True, **{name: frozenset({2**63})})  # type: ignore[arg-type]


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
# Pure: decisions.verified_match, the match auto_resolve closes on
# ---------------------------------------------------------------------------------------

CAMPAIGN = SubjectRef(type="campaign", id=10)
SHARED = SubjectRef(type="budget", id=5)
OWN_BUDGET_ID = 4
"""The id of the budget, not explicitly shared, that :data:`CAMPAIGN` draws from."""


def _subject(
    ref: SubjectRef,
    *,
    source: DecisionSource = "google",
    period: BudgetPeriod | None = "DAILY",
    budget_id: int | None = None,
) -> SubjectState:
    """Return a subject's state as ``load_subject_state`` builds it, with no database."""
    shape: DecisionShape = "own_budget"
    if ref.type == "budget":
        shape = "budget"
    elif budget_id is not None:
        shape = "shared_member"
    return SubjectState(
        ref=ref,
        dataset_id=1,
        dataset_state="active",
        source=source,
        currency="USD",
        name=f"{ref.type} {ref.id}",
        shape=shape,
        has_daily_budget=shape != "shared_member" and period == "DAILY",
        has_total_budget=False,
        budget_period=period,
        budget_id=budget_id,
        budget_external_id=None if budget_id is None else "502",
        latest_snapshot=None,
        current_decision=None,
    )


def _entity_snapshot(
    ref: SubjectRef,
    *,
    at: datetime,
    status: str | None = "active",
    micros: int | None = OWN_MICROS,
    budget_id: int | None = OWN_BUDGET_ID,
    source: str = "google",
    daily: Decimal | None = None,
) -> EntitySnapshot:
    """Return an unsaved snapshot, free to carry values the row builders refuse."""
    return EntitySnapshot(
        id=900,
        source=source,
        dataset_id=1,
        entity_type=ref.type,
        entity_id=ref.id,
        captured_at=at,
        status=status,
        budget_daily=daily,
        budget_total=None,
        budget_amount_micros=micros,
        budget_id=budget_id,
        quality_score=None,
        payload={},
    )


def _decision(
    ref: SubjectRef,
    *,
    shape: DecisionShape = "own_budget",
    status: str | None = "active",
    daily: Decimal | None = Decimal("100"),
    budget_id: int | None = None,
    tombstone: bool = False,
    at: datetime = T0,
    source: str = "google",
) -> Decision:
    """Return an unsaved decision row."""
    return Decision(
        id=800,
        source=source,
        dataset_id=1,
        subject_type=ref.type,
        subject_id=ref.id,
        shape=shape,
        status=status,
        daily_budget=daily,
        total_budget=None,
        budget_id=budget_id,
        tombstone=tombstone,
        decided_at=at,
        origin="baseline",
        patch={},
    )


def test_verified_match_needs_the_status_amount_and_period_known() -> None:
    state = _subject(CAMPAIGN)
    decision = _decision(CAMPAIGN)
    known = _entity_snapshot(CAMPAIGN, at=_at(3))
    assert compare(known, decision, state=state) == []
    assert verified_match(known, decision, state=state) is True
    # compare() raises nothing for a value the snapshot does not report; that is no match.
    unknown_status = _entity_snapshot(CAMPAIGN, at=_at(3), status=None)
    omitted_amount = _entity_snapshot(CAMPAIGN, at=_at(3), micros=None)
    unknown_period = _subject(CAMPAIGN, period=None)
    for snapshot, subject in (
        (unknown_status, state),
        (omitted_amount, state),
        (known, unknown_period),
    ):
        assert compare(snapshot, decision, state=subject) == []
        assert verified_match(snapshot, decision, state=subject) is False
    other = _entity_snapshot(SubjectRef(type="campaign", id=11), at=_at(3))
    with pytest.raises(ValueError, match="describes"):
        verified_match(other, decision, state=state)


def test_verified_match_on_a_shared_budget_needs_its_amount_and_period() -> None:
    decision = _decision(SHARED, shape="budget", status=None, daily=Decimal("50"))
    state = _subject(SHARED)
    known = _entity_snapshot(SHARED, at=_at(3), status=None, micros=SHARED_MICROS, budget_id=None)
    omitted = _entity_snapshot(SHARED, at=_at(3), status=None, micros=None, budget_id=None)
    assert verified_match(known, decision, state=state) is True
    assert compare(omitted, decision, state=state) == []
    assert verified_match(omitted, decision, state=state) is False
    assert compare(known, decision, state=_subject(SHARED, period=None)) == []
    assert verified_match(known, decision, state=_subject(SHARED, period=None)) is False


def test_verified_match_on_a_shared_member_needs_only_its_assignment() -> None:
    member = SubjectRef(type="campaign", id=12)
    state = _subject(member, budget_id=SHARED.id)
    decision = _decision(member, shape="shared_member", daily=None, budget_id=SHARED.id)
    # The shared amount belongs to the budget subject, so an omitted one does not matter.
    assigned = _entity_snapshot(member, at=_at(3), micros=None, budget_id=SHARED.id)
    assert verified_match(assigned, decision, state=state) is True
    unnamed = _entity_snapshot(member, at=_at(3), micros=None, budget_id=None)
    assert verified_match(unnamed, decision, state=state) is False


def test_verified_match_never_matches_a_tombstone() -> None:
    state = _subject(CAMPAIGN)
    tombstone = _decision(CAMPAIGN, status="ended", tombstone=True, at=_at(4))
    before = _entity_snapshot(CAMPAIGN, at=_at(4), status="ended")
    after = _entity_snapshot(CAMPAIGN, at=_at(6), status="ended")
    assert compare(before, tombstone, state=state) == []
    assert verified_match(before, tombstone, state=state) is False
    assert [mismatch.kind for mismatch in compare(after, tombstone, state=state)] == ["status"]
    assert verified_match(after, tombstone, state=state) is False
    budget_tombstone = _decision(
        SHARED, shape="budget", status=None, daily=Decimal("50"), tombstone=True, at=_at(4)
    )
    returned = _entity_snapshot(
        SHARED, at=_at(6), status=None, micros=SHARED_MICROS, budget_id=None
    )
    assert [m.kind for m in compare(returned, budget_tombstone, state=_subject(SHARED))] == [
        "budget"
    ]
    assert verified_match(returned, budget_tombstone, state=_subject(SHARED)) is False


def test_verified_match_reads_budgets_the_snapshot_cannot_carry() -> None:
    # A LinkedIn amount lives on the campaign row: absent there means no such budget.
    linkedin = _subject(CAMPAIGN, source="linkedin", period=None)
    bare = _entity_snapshot(CAMPAIGN, at=_at(3), source="linkedin", micros=None, budget_id=None)
    no_budget = _decision(CAMPAIGN, source="linkedin", daily=None)
    assert verified_match(bare, no_budget, state=linkedin) is True
    budgeted = _decision(CAMPAIGN, source="linkedin", daily=Decimal("25.00"))
    assert verified_match(bare, budgeted, state=linkedin) is False
    # A Google campaign naming no budget has no amount to verify, unless one was decided.
    google = _subject(CAMPAIGN, period=None)
    unbudgeted = _entity_snapshot(CAMPAIGN, at=_at(3), micros=None, budget_id=None)
    assert verified_match(unbudgeted, _decision(CAMPAIGN, daily=None), state=google) is True
    assert compare(unbudgeted, _decision(CAMPAIGN), state=google) == []
    assert verified_match(unbudgeted, _decision(CAMPAIGN), state=google) is False


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


@pytest.mark.anyio
async def test_write_snapshots_stores_the_largest_storable_amounts_exactly(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions, state="building")
    rows = [
        SnapshotRow.campaign(
            source="google",
            entity_id=world.own,
            status="active",
            raw_status="ENABLED",
            budget_id=world.own_budget,
            budget_period="DAILY",
            budget_amount_micros=LARGEST_MICROS,
        ),
        SnapshotRow.budget(
            entity_id=world.shared_budget,
            period="CUSTOM_PERIOD",
            amount_micros=LARGEST_MICROS,
            explicitly_shared=True,
        ),
    ]
    async with sessions() as session, session.begin():
        ids = await write_snapshots(session, world.dataset_id, rows, captured_at=_at(0))
    async with sessions() as session:
        campaign = await session.get(EntitySnapshot, ids[("campaign", world.own)])
        budget = await session.get(EntitySnapshot, ids[("budget", world.shared_budget)])
    assert campaign is not None
    assert budget is not None
    assert (campaign.budget_daily, campaign.budget_total, campaign.budget_amount_micros) == (
        LARGEST_AMOUNT,
        None,
        LARGEST_MICROS,
    )
    assert (budget.budget_daily, budget.budget_total, budget.budget_amount_micros) == (
        None,
        LARGEST_AMOUNT,
        LARGEST_MICROS,
    )
    assert str(campaign.budget_daily) == str(budget.budget_total) == "999999999999.999999"


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
        resolved = await resolve_discrepancy(
            missing.id, "acknowledge_removal", session=session, now=_at(4)
        )
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
async def test_tombstoned_campaign_returned_as_removed_raises_status(sessions: Sessions) -> None:
    # AAP 0.5.4.4: "If a complete enumeration later returns it, a status discrepancy is
    # raised against the tombstone" - the return is the reappearance, even when Q-G2b
    # returns the campaign as REMOVED and it is snapshotted as ended.
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    absent = _google_rows(world, without=frozenset({world.own}))
    await _sync(
        sessions, world.dataset_id, absent, at=_at(3), missing_campaigns=frozenset({world.own})
    )
    (missing,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        await resolve_discrepancy(missing.id, "acknowledge_removal", session=session, now=_at(4))
        await session.execute(
            update(Campaign)
            .where(Campaign.id == world.own)
            .values(status="ended", raw_status="REMOVED")
        )
    still_absent = await _sync(
        sessions, world.dataset_id, absent, at=_at(6), missing_campaigns=frozenset({world.own})
    )
    assert still_absent.report.total_raised == 0
    assert await _open(sessions, world.dataset_id) == []
    removed_row = SnapshotRow.campaign(
        source="google",
        entity_id=world.own,
        status="ended",
        raw_status="REMOVED",
        budget_id=world.own_budget,
        budget_period="DAILY",
        budget_amount_micros=OWN_MICROS,
    )
    returned = [*absent, removed_row]
    removed = frozenset({world.own})
    partial = await _sync(
        sessions, world.dataset_id, returned, at=_at(9), complete=False, removed=removed
    )
    assert partial.report.total_raised == 0
    assert partial.report.skipped == 1
    assert await _open(sessions, world.dataset_id) == []
    complete = await _sync(sessions, world.dataset_id, returned, at=_at(12), removed=removed)
    assert dict(complete.report.raised) == {"status": 1, "budget": 0, "untracked": 0, "missing": 0}
    (row,) = await _open(sessions, world.dataset_id)
    tombstone = (await _decisions(sessions, world.dataset_id))[-1]
    assert tombstone.tombstone is True
    assert (row.subject_type, row.subject_id, row.kind, row.state) == (
        "campaign",
        world.own,
        "status",
        "open",
    )
    assert row.decided_value == {"status": "ended", "tombstone": True}
    assert row.platform_value == {"status": "ended"}
    assert row.snapshot_id == complete.snapshot_ids[("campaign", world.own)]
    assert row.decision_id == tombstone.id
    async with sessions() as session, session.begin():
        await resolve_discrepancy(row.id, "platform_correct", session=session, now=_at(13))
    adopted = (await _decisions(sessions, world.dataset_id))[-1]
    assert (adopted.status, adopted.tombstone, adopted.origin) == ("ended", False, "resolution")
    after = await _sync(sessions, world.dataset_id, returned, at=_at(15), removed=removed)
    assert after.report.total_raised == 0
    assert await _open(sessions, world.dataset_id) == []


@pytest.mark.anyio
async def test_tombstoned_shared_budget_returned_raises_budget(sessions: Sessions) -> None:
    # A budget subject has no status (AAP 0.5.2), so its reappearance is a budget item.
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    absent = [row for row in _google_rows(world) if row.key != ("budget", world.other_shared)]
    gone = frozenset({world.other_shared})
    await _sync(sessions, world.dataset_id, absent, at=_at(3), missing_budgets=gone)
    (missing,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        await resolve_discrepancy(missing.id, "acknowledge_removal", session=session, now=_at(4))
    still_absent = await _sync(sessions, world.dataset_id, absent, at=_at(6), missing_budgets=gone)
    assert still_absent.report.total_raised == 0
    assert await _open(sessions, world.dataset_id) == []
    returned = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(9))
    assert dict(returned.report.raised) == {"status": 0, "budget": 1, "untracked": 0, "missing": 0}
    (row,) = await _open(sessions, world.dataset_id)
    assert (row.subject_type, row.subject_id, row.kind) == ("budget", world.other_shared, "budget")
    assert row.decided_value is not None
    assert row.platform_value is not None
    assert row.decided_value["tombstone"] is True
    assert "status" not in row.decided_value
    assert "status" not in row.platform_value
    assert row.platform_value["amount_micros"] == 70_000_000


@pytest.mark.anyio
async def test_awaiting_platform_auto_resolves_when_the_platform_matches(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    await _sync(sessions, world.dataset_id, _google_rows(world, own_status="paused"), at=_at(3))
    (row,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        waiting = await resolve_discrepancy(row.id, "decision_correct", session=session, now=_at(4))
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
async def test_awaiting_budget_stays_open_while_google_omits_the_amount(
    sessions: Sessions,
) -> None:
    # An omitted amount is unknown, never a match: closing needs the decided amount observed.
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    differing = _google_rows(world, own_micros=OWN_MICROS + 1, shared_micros=60_000_000)
    raised = await _sync(sessions, world.dataset_id, differing, at=_at(3))
    assert raised.report.raised["budget"] == 2
    rows = await _open(sessions, world.dataset_id)
    assert _kinds(rows) == [
        ("campaign", world.own, "budget"),
        ("budget", world.shared_budget, "budget"),
    ]
    async with sessions() as session, session.begin():
        for row in rows:
            waiting = await resolve_discrepancy(
                row.id, "decision_correct", session=session, now=_at(4)
            )
            assert waiting.state == "awaiting_platform"
    unreported = {("campaign", world.own), ("budget", world.shared_budget)}
    omitted = [
        SnapshotRow.campaign(
            source="google",
            entity_id=world.own,
            status="active",
            raw_status="ENABLED",
            budget_id=world.own_budget,
            budget_period="DAILY",
            budget_amount_micros=None,
        ),
        SnapshotRow.budget(
            entity_id=world.shared_budget,
            period="DAILY",
            amount_micros=None,
            explicitly_shared=True,
        ),
        *(row for row in _google_rows(world) if row.key not in unreported),
    ]
    blind = await _sync(sessions, world.dataset_id, omitted, at=_at(6))
    assert blind.report.total_raised == 0
    assert blind.report.auto_resolved == 0
    still = await _open(sessions, world.dataset_id)
    assert [(row.id, row.state) for row in still] == [(row.id, "awaiting_platform") for row in rows]
    matched = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(9))
    assert matched.report.auto_resolved == 2
    assert await _open(sessions, world.dataset_id) == []
    async with sessions() as session:
        closed = [await session.get(Discrepancy, row.id) for row in rows]
    assert [(row.state, row.resolution, row.resolved_at) for row in closed if row] == [
        ("resolved", "decision_correct", _at(9)),
        ("resolved", "decision_correct", _at(9)),
    ]


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
        await resolve_discrepancy(missing.id, "decision_correct", session=session, now=_at(4))
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
            SubjectRef(type="campaign", id=world.own),
            DecisionPatch(status="paused"),
            origin="user",
            session=session,
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


# ---------------------------------------------------------------------------------------
# Database: set-based baseline and detection
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ManyWorld:
    """A Google dataset with ``count`` own-budget campaigns and two shared budgets."""

    dataset_id: int
    campaigns: tuple[int, ...]
    budgets: tuple[int, ...]
    members: tuple[int, ...]
    shared: tuple[int, ...]


async def _many_world(sessions: Sessions, count: int) -> ManyWorld:
    """Commit an active Google dataset with ``count`` own-budget campaigns, each on its own
    budget, plus two explicitly shared budgets with two members each."""
    async with sessions() as session, session.begin():
        dataset_id = await _dataset(session, "google")
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
        budgets = [
            await _budget(session, dataset_id, f"9{index}", OWN_MICROS, shared=False)
            for index in range(count)
        ]
        shared = [
            await _budget(session, dataset_id, f"8{index}", SHARED_MICROS, shared=True)
            for index in range(2)
        ]
        campaigns = [
            await _google_campaign(
                session, dataset_id, account.id, f"c{index}", budget, own_micros=OWN_MICROS
            )
            for index, budget in enumerate(budgets)
        ]
        members = [
            await _google_campaign(session, dataset_id, account.id, f"m{index}", shared[index % 2])
            for index in range(4)
        ]
        return ManyWorld(
            dataset_id=dataset_id,
            campaigns=tuple(campaigns),
            budgets=tuple(budgets),
            members=tuple(members),
            shared=tuple(shared),
        )


def _many_rows(world: ManyWorld, *, paused: frozenset[int] = frozenset()) -> list[SnapshotRow]:
    """Return one sync's snapshot rows for a :class:`ManyWorld`."""
    rows = [
        SnapshotRow.campaign(
            source="google",
            entity_id=campaign,
            status="paused" if campaign in paused else "active",
            raw_status="PAUSED" if campaign in paused else "ENABLED",
            budget_id=budget,
            budget_period="DAILY",
            budget_amount_micros=OWN_MICROS,
        )
        for campaign, budget in zip(world.campaigns, world.budgets, strict=True)
    ]
    rows.extend(
        SnapshotRow.campaign(
            source="google",
            entity_id=member,
            status="active",
            raw_status="ENABLED",
            budget_id=world.shared[index % 2],
            budget_period="DAILY",
            budget_amount_micros=SHARED_MICROS,
            explicitly_shared=True,
        )
        for index, member in enumerate(world.members)
    )
    rows.extend(
        SnapshotRow.budget(
            entity_id=budget, period="DAILY", amount_micros=OWN_MICROS, explicitly_shared=False
        )
        for budget in world.budgets
    )
    rows.extend(
        SnapshotRow.budget(
            entity_id=budget, period="DAILY", amount_micros=SHARED_MICROS, explicitly_shared=True
        )
        for budget in world.shared
    )
    return rows


async def _requests(session: AsyncSession) -> Counter[str]:
    """Return the number of ``analysis_request`` rows per reason."""
    result = await session.execute(
        select(AnalysisRequest.reason, func.count()).group_by(AnalysisRequest.reason)
    )
    counts: Counter[str] = Counter()
    for reason, total in result:
        counts[reason] = total
    return counts


@dataclass(frozen=True)
class CountedSync:
    """One entity sync with the statements its baseline and detection steps sent."""

    result: SyncResult
    baseline_statements: int
    detection_statements: int
    requests: Counter[str]


async def _counted_sync(
    sessions: Sessions, dataset_id: int, rows: Sequence[SnapshotRow], *, at: datetime
) -> CountedSync:
    """Run one complete Google entity sync, counting the statements of baseline and detection
    and the analysis requests the sync queued."""
    engine = sessions.kw["bind"]
    step: list[str] = ["idle"]
    counts: Counter[str] = Counter()

    def count(*_: object) -> None:
        counts[step[0]] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        async with sessions() as session, session.begin():
            before = await _requests(session)
            sealed = await baseline_sealed(session, dataset_id)
            ids = await write_snapshots(session, dataset_id, rows, captured_at=at)
            step[0] = "baseline"
            baselines = await ensure_baseline(session, dataset_id, captured_at=at, sealed=sealed)
            step[0] = "idle"
            enumeration = EnumerationResult(
                source="google",
                complete=True,
                present_campaign_ids=frozenset(
                    row.entity_id for row in rows if row.entity_type == "campaign"
                ),
                present_budget_ids=frozenset(
                    row.entity_id for row in rows if row.entity_type == "budget"
                ),
            )
            step[0] = "detection"
            report = await detect_discrepancies(
                session, dataset_id, enumeration, ids, sealed=sealed, now=at
            )
            step[0] = "idle"
            after = await _requests(session)
            await record_freshness(
                session,
                dataset_id,
                "google.entities",
                FreshnessOutcome.success(
                    at, covered_from=None, covered_through=None, complete=True
                ),
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    return CountedSync(
        result=SyncResult(sealed=sealed, baselines=baselines, report=report, snapshot_ids=ids),
        baseline_statements=counts["baseline"],
        detection_statements=counts["detection"],
        requests=after - before,
    )


@pytest.mark.anyio
async def test_baseline_and_detection_statements_do_not_grow_with_the_subjects(
    sessions: Sessions,
) -> None:
    runs: dict[int, list[CountedSync]] = {}
    for count in (10, 200):
        world = await _many_world(sessions, count)
        paused = frozenset(world.campaigns[:5])
        runs[count] = [
            await _counted_sync(sessions, world.dataset_id, _many_rows(world), at=_at(0)),
            await _counted_sync(sessions, world.dataset_id, _many_rows(world), at=_at(3)),
            await _counted_sync(
                sessions, world.dataset_id, _many_rows(world, paused=paused), at=_at(6)
            ),
            await _counted_sync(
                sessions, world.dataset_id, _many_rows(world, paused=paused), at=_at(9)
            ),
            await _counted_sync(sessions, world.dataset_id, _many_rows(world), at=_at(12)),
        ]
        first, unchanged, changed, again, restored = runs[count]
        assert (first.result.sealed, first.result.baselines) == (False, count + 6)
        assert first.result.report.total_raised == 0
        assert unchanged.result.report.total_raised == 0
        assert unchanged.result.report.checked == count + 6
        assert changed.result.report.raised["status"] == 5
        assert changed.requests["discrepancy_created"] == 1
        assert (again.result.report.total_raised, again.result.report.refreshed) == (0, 5)
        assert again.requests["discrepancy_created"] == 0
        assert restored.result.report.auto_resolved == 5
        assert restored.requests["discrepancy_resolved"] == 1
        assert await _open(sessions, world.dataset_id) == []
    small = [(run.baseline_statements, run.detection_statements) for run in runs[10]]
    large = [(run.baseline_statements, run.detection_statements) for run in runs[200]]
    assert small == large
    # Reads are a fixed handful; writes follow only the rows created or changed.
    first, unchanged, changed, again, restored = runs[200]
    # Each pass includes the three statements that lock the dataset, its campaigns and its
    # shared budgets before anything else is read.
    assert first.baseline_statements <= 20
    assert unchanged.detection_statements <= 18
    assert changed.detection_statements - unchanged.detection_statements <= 5 * 3 + 1
    assert restored.detection_statements - unchanged.detection_statements <= 5 + 1


# ---------------------------------------------------------------------------------------
# Database: a baseline deferred while the baseline was open
# ---------------------------------------------------------------------------------------


def _shared_unreported(world: GoogleWorld) -> list[SnapshotRow]:
    """Return the world's rows with the shared budget's amount omitted by Google."""
    return [
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


@pytest.mark.anyio
async def test_a_baseline_deferred_by_the_sealing_sync_is_written_once_known(
    sessions: Sessions,
) -> None:
    # The first, complete sync seals the baseline although Google omitted the shared
    # budget's amount; the budget was observed with the baseline population, so it is
    # baselined once an amount is reported and is never untracked (AAP 0.5.4.4).
    world = await _google_world(sessions)
    first = await _sync(sessions, world.dataset_id, _shared_unreported(world), at=_at(0))
    assert (first.sealed, first.baselines, first.report.skipped) == (False, 4, 1)
    still_unknown = await _sync(sessions, world.dataset_id, _shared_unreported(world), at=_at(3))
    assert (still_unknown.sealed, still_unknown.baselines) == (True, 0)
    assert still_unknown.report.total_raised == 0
    assert still_unknown.report.skipped == 1
    assert await _open(sessions, world.dataset_id) == []
    known = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(6))
    assert (known.sealed, known.baselines) == (True, 1)
    assert known.report.total_raised == 0
    assert known.report.skipped == 0
    assert await _open(sessions, world.dataset_id) == []
    baseline = [
        row
        for row in await _decisions(sessions, world.dataset_id)
        if (row.subject_type, row.subject_id) == ("budget", world.shared_budget)
    ]
    assert [(row.origin, row.shape, row.daily_budget, row.decided_at) for row in baseline] == [
        ("baseline", "budget", Decimal("50.000000"), _at(6))
    ]
    changed = await _sync(
        sessions, world.dataset_id, _google_rows(world, shared_micros=60_000_000), at=_at(9)
    )
    assert dict(changed.report.raised) == {"status": 0, "budget": 1, "untracked": 0, "missing": 0}


def _own_unreported(world: GoogleWorld) -> list[SnapshotRow]:
    """Return the world's rows with the own-budget campaign's DAILY amount omitted by Google.

    The campaign's snapshot still names its budget and period; Google omits the amount on
    the campaign's row and on its budget's row alike.
    """
    unreported = {("campaign", world.own), ("budget", world.own_budget)}
    return [
        SnapshotRow.campaign(
            source="google",
            entity_id=world.own,
            status="active",
            raw_status="ENABLED",
            budget_id=world.own_budget,
            budget_period="DAILY",
            budget_amount_micros=None,
        ),
        SnapshotRow.budget(
            entity_id=world.own_budget, period="DAILY", amount_micros=None, explicitly_shared=False
        ),
        *(row for row in _google_rows(world) if row.key not in unreported),
    ]


async def _own_decisions(sessions: Sessions, world: GoogleWorld) -> list[Decision]:
    """Return the own-budget campaign's decisions in insertion order."""
    return [
        row
        for row in await _decisions(sessions, world.dataset_id)
        if (row.subject_type, row.subject_id) == ("campaign", world.own)
    ]


NOTHING_RAISED = {"status": 0, "budget": 0, "untracked": 0, "missing": 0}
"""A detection report's ``raised`` counts when no discrepancy of any kind was raised."""


@pytest.mark.anyio
async def test_baseline_waits_for_an_own_budget_amount_google_omitted(sessions: Sessions) -> None:
    # The omitted amount is unknown, never absent. A baseline without it would
    # make the unchanged amount, once reported, a false C0 budget discrepancy.
    world = await _google_world(sessions)
    first = await _sync(
        sessions, world.dataset_id, _own_unreported(world), at=_at(0), complete=False
    )
    assert (first.sealed, first.baselines, first.report.skipped) == (False, 4, 1)
    assert dict(first.report.raised) == NOTHING_RAISED
    assert await _own_decisions(sessions, world) == []
    assert await _open(sessions, world.dataset_id) == []
    known = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(3))
    assert (known.sealed, known.baselines, known.report.skipped) == (False, 1, 0)
    assert dict(known.report.raised) == NOTHING_RAISED
    assert await _open(sessions, world.dataset_id) == []
    assert [
        (row.origin, row.shape, row.status, row.daily_budget, row.total_budget, row.decided_at)
        for row in await _own_decisions(sessions, world)
    ] == [("baseline", "own_budget", "active", Decimal("100.000000"), None, _at(3))]
    sealed = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(6))
    assert (sealed.sealed, sealed.baselines) == (True, 0)
    assert dict(sealed.report.raised) == NOTHING_RAISED


@pytest.mark.anyio
async def test_an_own_budget_baseline_deferred_by_the_sealing_sync_is_written_once_known(
    sessions: Sessions,
) -> None:
    # The first, complete sync seals the baseline although Google omitted the own-budget
    # campaign's amount; the campaign was observed with the baseline population, so it is
    # baselined once the amount is reported and is never untracked (AAP 0.5.4.4).
    world = await _google_world(sessions)
    first = await _sync(sessions, world.dataset_id, _own_unreported(world), at=_at(0))
    assert (first.sealed, first.baselines, first.report.skipped) == (False, 4, 1)
    still_unknown = await _sync(sessions, world.dataset_id, _own_unreported(world), at=_at(3))
    assert (still_unknown.sealed, still_unknown.baselines) == (True, 0)
    assert dict(still_unknown.report.raised) == NOTHING_RAISED
    assert still_unknown.report.skipped == 1
    assert await _open(sessions, world.dataset_id) == []
    known = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(6))
    assert (known.sealed, known.baselines, known.report.skipped) == (True, 1, 0)
    assert dict(known.report.raised) == NOTHING_RAISED
    assert await _open(sessions, world.dataset_id) == []
    assert [
        (row.origin, row.shape, row.status, row.daily_budget, row.total_budget, row.decided_at)
        for row in await _own_decisions(sessions, world)
    ] == [("baseline", "own_budget", "active", Decimal("100.000000"), None, _at(6))]
    changed = await _sync(
        sessions, world.dataset_id, _google_rows(world, own_micros=OWN_MICROS + 1), at=_at(9)
    )
    assert dict(changed.report.raised) == {**NOTHING_RAISED, "budget": 1}


@pytest.mark.anyio
async def test_a_tombstone_is_not_corrected_from_an_own_budget_amount_google_omitted(
    sessions: Sessions,
) -> None:
    # A tombstoned campaign's platform_correct writes the whole platform state, which
    # needs the amount; the status-only correction of a live decision does not apply.
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    absent = _google_rows(world, without=frozenset({world.own}))
    await _sync(
        sessions, world.dataset_id, absent, at=_at(3), missing_campaigns=frozenset({world.own})
    )
    (missing,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        await resolve_discrepancy(missing.id, "acknowledge_removal", session=session, now=_at(4))
    reappeared = await _sync(sessions, world.dataset_id, _own_unreported(world), at=_at(6))
    assert dict(reappeared.report.raised) == {**NOTHING_RAISED, "status": 1}
    (status,) = await _open(sessions, world.dataset_id)
    decided = [row.id for row in await _own_decisions(sessions, world)]

    with pytest.raises(PlatformStateUnknown) as refused:
        async with sessions() as session, session.begin():
            await resolve_discrepancy(status.id, "platform_correct", session=session, now=_at(7))

    assert (refused.value.status_code, refused.value.code) == (409, "platform_state_unknown")
    assert refused.value.details == {
        "snapshot_id": reappeared.snapshot_ids[("campaign", world.own)]
    }
    assert [row.id for row in await _own_decisions(sessions, world)] == decided
    assert [(row.id, row.state) for row in await _open(sessions, world.dataset_id)] == [
        (status.id, "open")
    ]
    known = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(9))
    assert known.report.refreshed == 1
    async with sessions() as session, session.begin():
        closed = await resolve_discrepancy(
            status.id, "platform_correct", session=session, now=_at(10)
        )
    assert (closed.state, closed.resolution) == ("resolved", "platform_correct")
    latest = (await _own_decisions(sessions, world))[-1]
    assert (latest.status, latest.daily_budget, latest.tombstone) == (
        "active",
        Decimal("100.000000"),
        False,
    )


@pytest.mark.anyio
async def test_subjects_first_seen_as_subjects_after_the_seal_stay_untracked(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    async with sessions() as session, session.begin():
        later_shared = await _budget(session, world.dataset_id, "505", 40_000_000, shared=False)
    unshared_row = SnapshotRow.budget(
        entity_id=later_shared, period="DAILY", amount_micros=40_000_000, explicitly_shared=False
    )
    first = await _sync(sessions, world.dataset_id, [*_google_rows(world), unshared_row], at=_at(0))
    assert first.baselines == 5
    async with sessions() as session, session.begin():
        await session.execute(
            update(Budget).where(Budget.id == later_shared).values(explicitly_shared=True)
        )
        new_shared = await _budget(session, world.dataset_id, "506", 30_000_000, shared=True)
    rows = [
        *_google_rows(world),
        SnapshotRow.budget(
            entity_id=later_shared,
            period="DAILY",
            amount_micros=40_000_000,
            explicitly_shared=True,
        ),
        SnapshotRow.budget(
            entity_id=new_shared, period="DAILY", amount_micros=30_000_000, explicitly_shared=True
        ),
    ]
    second = await _sync(sessions, world.dataset_id, rows, at=_at(3))
    assert (second.sealed, second.baselines) == (True, 0)
    assert dict(second.report.raised) == {"status": 0, "budget": 0, "untracked": 2, "missing": 0}
    assert sorted(_kinds(await _open(sessions, world.dataset_id))) == [
        ("budget", later_shared, "untracked"),
        ("budget", new_shared, "untracked"),
    ]
    third = await _sync(sessions, world.dataset_id, rows, at=_at(6))
    assert (third.baselines, third.report.total_raised, third.report.refreshed) == (0, 0, 2)


# ---------------------------------------------------------------------------------------
# Database: the untracked row waiting after record_intended
# ---------------------------------------------------------------------------------------


def _late_campaign_row(campaign_id: int, budget_id: int, status: str, micros: int) -> SnapshotRow:
    """Return a Google own-budget campaign snapshot with the given status and amount."""
    return SnapshotRow.campaign(
        source="google",
        entity_id=campaign_id,
        status=status,
        raw_status="ENABLED" if status == "active" else "PAUSED",
        budget_id=budget_id,
        budget_period="DAILY",
        budget_amount_micros=micros,
    )


def _amounts(value: dict[str, object] | None) -> tuple[object, Decimal | None, object]:
    """Return a stored value's status, daily budget and micros, the budget as a Decimal."""
    assert value is not None
    daily = value["daily_budget"]
    assert daily is None or isinstance(daily, str)
    return value["status"], None if daily is None else Decimal(daily), value["amount_micros"]


@pytest.mark.anyio
async def test_the_waiting_untracked_row_shows_the_current_platform_and_decision(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    async with sessions() as session, session.begin():
        late_budget = await _budget(session, world.dataset_id, "507", OWN_MICROS, shared=False)
        late = await _google_campaign(
            session, world.dataset_id, world.account_id, "17", late_budget, own_micros=OWN_MICROS
        )

    def rows(status: str, micros: int) -> list[SnapshotRow]:
        return [
            *_google_rows(world),
            _late_campaign_row(late, late_budget, status, micros),
            SnapshotRow.budget(
                entity_id=late_budget, period="DAILY", amount_micros=micros, explicitly_shared=False
            ),
        ]

    seen = await _sync(sessions, world.dataset_id, rows("active", OWN_MICROS), at=_at(3))
    assert seen.report.raised["untracked"] == 1
    (untracked,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        waiting = await resolve_discrepancy(
            untracked.id,
            "record_intended",
            {"status": "paused", "daily_budget": "120"},
            session=session,
            now=_at(4),
        )
        intended_id = waiting.decision_id
    assert intended_id is not None

    # The budget converges to the intended 120 while the status still differs: the card
    # shows the platform's complete current state, not the stale 100.
    converged = await _sync(sessions, world.dataset_id, rows("active", 120_000_000), at=_at(6))
    assert (converged.report.total_raised, converged.report.refreshed) == (0, 1)
    (row,) = await _open(sessions, world.dataset_id)
    assert (row.id, row.kind, row.state, row.resolution, row.detected_at) == (
        untracked.id,
        "untracked",
        "awaiting_platform",
        "record_intended",
        _at(3),
    )
    assert _amounts(row.platform_value) == ("active", Decimal(120), 120_000_000)
    assert _amounts(row.decided_value) == ("paused", Decimal(120), 120_000_000)
    assert row.platform_value is not None
    assert row.platform_value["currency"] == "USD"
    assert row.snapshot_id == converged.snapshot_ids[("campaign", late)]
    assert row.decision_id == intended_id

    # A later ordinary decision replaces the intended state on the waiting card.
    async with sessions() as session, session.begin():
        later = await record_decision(
            SubjectRef(type="campaign", id=late),
            DecisionPatch(daily_budget=Decimal(130)),
            origin="user",
            session=session,
            decided_by="ops@example.com",
            now=_at(7),
        )
        later_id = later.id
    differing = await _sync(sessions, world.dataset_id, rows("active", 120_000_000), at=_at(9))
    assert (differing.report.total_raised, differing.report.refreshed) == (0, 2)
    (row,) = await _open(sessions, world.dataset_id)
    assert row.id == untracked.id
    assert _amounts(row.decided_value) == ("paused", Decimal(130), 130_000_000)
    assert _amounts(row.platform_value) == ("active", Decimal(120), 120_000_000)
    assert (row.decision_id, row.snapshot_id) == (
        later_id,
        differing.snapshot_ids[("campaign", late)],
    )

    # Called on its own, raise_discrepancy loads the subject and the compared snapshot.
    first_seen = seen.snapshot_ids[("campaign", late)]
    async with sessions() as session, session.begin():
        folded = await raise_discrepancy(
            session,
            SubjectRef(type="campaign", id=late),
            "status",
            decided_value={"status": "paused"},
            platform_value={"status": "active"},
            snapshot_id=first_seen,
            decision_id=later_id,
            detected_at=_at(10),
        )
        assert (folded.id, folded.snapshot_id, folded.decision_id) == (
            untracked.id,
            first_seen,
            later_id,
        )
        assert _amounts(folded.platform_value) == ("active", Decimal(100), OWN_MICROS)
        assert _amounts(folded.decided_value) == ("paused", Decimal(130), 130_000_000)
        assert folded.detected_at == _at(3)
        # A status comparison always names the snapshot it read; without one it is refused
        # before the waiting row changes.
        with pytest.raises(ValueError, match="a status discrepancy needs the snapshot"):
            await raise_discrepancy(
                session,
                SubjectRef(type="campaign", id=late),
                "status",
                decided_value={"status": "paused"},
                platform_value={"status": "ended"},
                snapshot_id=None,
                decision_id=intended_id,
                detected_at=_at(10),
            )
        assert (folded.snapshot_id, folded.decision_id) == (first_seen, later_id)
        assert _amounts(folded.platform_value) == ("active", Decimal(100), OWN_MICROS)
        # Measured against an older decision, the decided side is that decision and the
        # platform side the complete state the named snapshot observed.
        converged_seen = converged.snapshot_ids[("campaign", late)]
        older = await raise_discrepancy(
            session,
            SubjectRef(type="campaign", id=late),
            "status",
            decided_value={"status": "paused"},
            platform_value={"status": "active"},
            snapshot_id=converged_seen,
            decision_id=intended_id,
            detected_at=_at(10),
        )
        assert (older.id, older.snapshot_id, older.decision_id) == (
            untracked.id,
            converged_seen,
            intended_id,
        )
        assert _amounts(older.platform_value) == ("active", Decimal(120), 120_000_000)
        assert _amounts(older.decided_value) == ("paused", Decimal(120), 120_000_000)

    matched = await _sync(sessions, world.dataset_id, rows("paused", 130_000_000), at=_at(12))
    assert matched.report.auto_resolved == 1
    assert await _open(sessions, world.dataset_id) == []
    async with sessions() as session:
        closed = await session.get(Discrepancy, untracked.id)
    assert closed is not None
    assert (closed.state, closed.resolution, closed.resolved_at) == (
        "resolved",
        "record_intended",
        _at(12),
    )


# ---------------------------------------------------------------------------------------
# Database: a tombstoned subject absent again
# ---------------------------------------------------------------------------------------


async def _closed(
    sessions: Sessions, discrepancy_id: int
) -> tuple[str, str | None, datetime | None]:
    """Return a discrepancy's state, resolution and resolution instant."""
    async with sessions() as session:
        row = await session.get(Discrepancy, discrepancy_id)
    assert row is not None
    return row.state, row.resolution, row.resolved_at


@pytest.mark.anyio
async def test_renewed_absence_closes_the_reappearance_against_a_tombstone(
    sessions: Sessions,
) -> None:
    # The tombstone records that the campaign is gone, so a complete enumeration that no
    # longer returns it matches the tombstone: the reappearance item closes, open or
    # waiting after decision_correct, and no missing is raised.
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    absent = _google_rows(world, without=frozenset({world.own}))
    gone = frozenset({world.own})
    await _sync(sessions, world.dataset_id, absent, at=_at(3), missing_campaigns=gone)
    (missing,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        await resolve_discrepancy(missing.id, "acknowledge_removal", session=session, now=_at(4))
    back = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(6))
    assert back.report.raised["status"] == 1
    (reappeared,) = await _open(sessions, world.dataset_id)
    async with sessions() as session:
        before = await _requests(session)
    absent_again = await _sync(
        sessions, world.dataset_id, absent, at=_at(9), missing_campaigns=gone
    )
    assert absent_again.report.total_raised == 0
    assert (absent_again.report.auto_resolved, absent_again.report.skipped) == (1, 1)
    assert await _open(sessions, world.dataset_id) == []
    assert await _closed(sessions, reappeared.id) == ("resolved", None, _at(9))
    async with sessions() as session:
        after = await _requests(session)
    assert (after - before)["discrepancy_resolved"] == 1

    returned = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(12))
    assert returned.report.raised["status"] == 1
    (again,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        await resolve_discrepancy(again.id, "decision_correct", session=session, now=_at(13))
    partial = await _sync(sessions, world.dataset_id, absent, at=_at(15), complete=False)
    assert (partial.report.auto_resolved, partial.report.total_raised) == (0, 0)
    assert [row.state for row in await _open(sessions, world.dataset_id)] == ["awaiting_platform"]
    confirmed = await _sync(sessions, world.dataset_id, absent, at=_at(18), missing_campaigns=gone)
    assert (confirmed.report.auto_resolved, confirmed.report.total_raised) == (1, 0)
    assert await _closed(sessions, again.id) == ("resolved", "decision_correct", _at(18))
    assert await _open(sessions, world.dataset_id) == []


@pytest.mark.anyio
async def test_renewed_absence_closes_a_tombstoned_shared_budget_item(sessions: Sessions) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    absent = [row for row in _google_rows(world) if row.key != ("budget", world.other_shared)]
    gone = frozenset({world.other_shared})
    await _sync(sessions, world.dataset_id, absent, at=_at(3), missing_budgets=gone)
    (missing,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        await resolve_discrepancy(missing.id, "acknowledge_removal", session=session, now=_at(4))
    back = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(6))
    assert back.report.raised["budget"] == 1
    (reappeared,) = await _open(sessions, world.dataset_id)
    absent_again = await _sync(sessions, world.dataset_id, absent, at=_at(9), missing_budgets=gone)
    assert (absent_again.report.auto_resolved, absent_again.report.total_raised) == (1, 0)
    assert await _closed(sessions, reappeared.id) == ("resolved", None, _at(9))
    assert await _open(sessions, world.dataset_id) == []


# ---------------------------------------------------------------------------------------
# Database: the detection-side helpers without preloaded input
# ---------------------------------------------------------------------------------------


def test_a_discrepancy_of_an_unknown_subject_type_is_refused() -> None:
    row = Discrepancy(id=7, subject_type="keyword", subject_id=1, kind="status")
    with pytest.raises(ValueError, match="unknown subject type"):
        snapshots._discrepancy_subject(row)


class Comparison(TypedDict):
    """The comparison keywords of :func:`raise_discrepancy`, typed as its signature takes them."""

    decided_value: Mapping[str, Any] | None
    platform_value: Mapping[str, Any] | None
    snapshot_id: int | None
    decision_id: int | None
    detected_at: datetime


@pytest.mark.anyio
async def test_detection_helpers_work_standalone_and_check_preloaded_input(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    first = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    own = SubjectRef(type="campaign", id=world.own)
    member = SubjectRef(type="campaign", id=world.member_a)
    snapshot_id = first.snapshot_ids[("campaign", world.own)]
    async with sessions() as session, session.begin():
        own_state = await load_subject_state(session, own)
        member_state = await load_subject_state(session, member)
        decision = own_state.current_decision
        snapshot = own_state.latest_snapshot
        assert decision is not None
        assert snapshot is not None
        assert snapshot.id == snapshot_id
        values: Comparison = {
            "decided_value": {"status": "active"},
            "platform_value": {"status": "paused"},
            "snapshot_id": snapshot_id,
            "decision_id": decision.id,
            "detected_at": _at(1),
        }
        with pytest.raises(ValueError, match="state describes"):
            await raise_discrepancy(session, own, "status", **values, state=member_state)
        with pytest.raises(ValueError, match="state describes"):
            await write_baseline(session, own, snapshot, now=_at(1), state=member_state)
        before = await _requests(session)
        row = await raise_discrepancy(session, own, "status", **values)
        assert (row.state, row.kind, row.detected_at) == ("open", "status", _at(1))
        with pytest.raises(ValueError, match="belongs to campaign"):
            await raise_discrepancy(session, member, "status", **values, unresolved=[row])
        with pytest.raises(ValueError, match="belongs to campaign"):
            await auto_resolve_absent(session, member, decision, now=_at(2))
        assert await auto_resolve_absent(session, own, decision, now=_at(2)) == []
        closed = await auto_resolve(session, own, snapshot, decision, now=_at(2))
        assert [item.id for item in closed] == [row.id]
        assert (row.state, row.resolved_at) == ("resolved", _at(2))
        requests = await _requests(session) - before
        assert (requests["discrepancy_created"], requests["discrepancy_resolved"]) == (1, 1)
        await session.rollback()

    # A tombstone's renewed absence closes its status item standalone, queueing its own rerun.
    absent = _google_rows(world, without=frozenset({world.own}))
    gone = frozenset({world.own})
    await _sync(sessions, world.dataset_id, absent, at=_at(3), missing_campaigns=gone)
    (missing,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        await resolve_discrepancy(missing.id, "acknowledge_removal", session=session, now=_at(4))
    await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(6))
    (reappeared,) = await _open(sessions, world.dataset_id)
    async with sessions() as session, session.begin():
        tombstone = (await load_subject_state(session, own)).current_decision
        assert tombstone is not None
        assert tombstone.tombstone is True
        before = await _requests(session)
        closed = await auto_resolve_absent(session, own, tombstone, now=_at(7))
        assert [item.id for item in closed] == [reappeared.id]
        assert (await _requests(session) - before)["discrepancy_resolved"] == 1
    assert await _open(sessions, world.dataset_id) == []
