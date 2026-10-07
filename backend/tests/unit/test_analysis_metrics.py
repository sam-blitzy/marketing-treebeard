"""Unit tests for the pure half of ``ads_engine.analysis.metrics`` (AAP 0.5.4.1, 0.5.4.2, 0.5.4.5).

They pin outcome by source, window totals, efficiency and baselines; budget-day classification
from observations (whole-day known, changed intraday, unobserved, at most
``budget.max_observation_gap_hours`` apart); own-budget and shared-budget histories with dated
membership and budget-active days; the cap-constrained, high-utilization and under-utilized
gates with their "observed of required" shortfalls; B2, the G1 member predicate and the G1
set; and the B4 Google counting contract.

Contexts are built in memory, either by hand from ``analysis.context`` types or from the
scenario catalogue (``SCENARIOS[name].build_context()``), whose expectations these tests hold
the metrics predicates to. The SQL half (``QueryScope``, ``aggregate``, ``load_context``) is
covered by ``tests/integration/test_analysis_metrics_load.py``. Pure: no database and no
network.
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ads_engine.analysis.context import (
    AccountInfo,
    AnalysisContext,
    BudgetInfo,
    BudgetPeriod,
    CampaignInfo,
    CampaignStatus,
    ConversionActionInfo,
    DatasetRef,
    DayFact,
    Freshness,
    Observation,
    SubjectKey,
    objective_group,
)
from ads_engine.analysis.metrics import (
    B2_THRESHOLD_KEYS,
    BUDGET_THRESHOLD_KEYS,
    LAST_CLICK_MODEL,
    BudgetHistory,
    GateConfig,
    Totals,
    assess_b2,
    baseline_cpo,
    budget_amount_value,
    budget_history,
    budget_subjects,
    campaign_budget_history,
    campaign_state_value,
    cap_constrained,
    classify_days,
    cost_per_outcome,
    counting_contract,
    expected_outcomes,
    high_utilization,
    member_poor_efficiency,
    membership_value,
    outcome,
    shared_budget_history,
    source_totals,
    spend_increase_blocked,
    status_value,
    subject_baseline,
    subject_campaigns,
    subject_objective_groups,
    subject_totals,
    under_utilized,
    window_totals,
)
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.analysis.window import AnalysisWindow, DaySource, SourceDayInfo
from ads_engine.simulation.scenarios import SCENARIOS, Scenario, thresholds_with

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
"""The run instant: the default window ends on 2026-10-01 in New York and in UTC."""
NY = "America/New_York"
CADENCE = timedelta(hours=3)
"""The snapshot cadence of the entity sync."""

GOOGLE_DATASET = DatasetRef(1, "google", "simulated")
LINKEDIN_DATASET = DatasetRef(2, "linkedin", "simulated")
GOOGLE_ACCOUNT = AccountInfo(1, "google", "1234567890", "Google Ads", "USD", NY)
LINKEDIN_ACCOUNT = AccountInfo(2, "linkedin", "500000001", "LinkedIn Ads", "USD")

WEEK = (date(2026, 9, 25), date(2026, 10, 1))
"""A 7-day window: the minimum known-budget days every history gate needs."""
TEN_DAYS = (date(2026, 9, 22), date(2026, 10, 1))


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    return Thresholds.load(CONFIG_DIR)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _window(
    thresholds: Thresholds,
    span: tuple[date, date],
    *,
    google_tz: str | None = NY,
    sources: tuple[DaySource, ...] = ("google", "linkedin"),
) -> AnalysisWindow:
    return AnalysisWindow.custom(
        span[0], span[1], config=thresholds, sources=SourceDayInfo(google_tz, sources), now=NOW
    )


def _context(thresholds: Thresholds, window: AnalysisWindow, **fields: Any) -> AnalysisContext:
    return AnalysisContext(window=window, thresholds=thresholds, now=NOW, **fields)


def _instant(day: date, hour: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _series(start: datetime, end: datetime, **fields: Any) -> tuple[Observation, ...]:
    """One observation every :data:`CADENCE` from ``start`` through ``end``."""
    observations: list[Observation] = []
    instant = start
    while instant <= end:
        observations.append(Observation(captured_at=instant, **fields))
        instant += CADENCE
    return tuple(observations)


def _dates(span: tuple[date, date]) -> list[date]:
    return [span[0] + timedelta(days=offset) for offset in range((span[1] - span[0]).days + 1)]


def _iso(days: Iterable[date]) -> tuple[str, ...]:
    return tuple(day.isoformat() for day in days)


def _li_campaign(
    cid: int,
    *,
    daily: str | None = "100.00",
    objective: str = "LEAD_GENERATION",
    status: CampaignStatus = "active",
    account_id: int = LINKEDIN_ACCOUNT.id,
) -> CampaignInfo:
    return CampaignInfo(
        id=cid,
        source="linkedin",
        account_id=account_id,
        external_id=str(700_000 + cid),
        name=f"LinkedIn {cid}",
        status=status,
        raw_status="ACTIVE",
        objective_or_channel=objective,
        objective_group=objective_group("linkedin", objective),
        campaign_group_id=None,
        budget_id=None,
        daily_budget=None if daily is None else Decimal(daily),
        total_budget=Decimal("5000.00") if daily is None else None,
        budget_period=None,
        currency="USD",
    )


def _google_campaign(
    cid: int,
    budget_id: int | None,
    *,
    period: BudgetPeriod | None = "DAILY",
    account_id: int = GOOGLE_ACCOUNT.id,
    currency: str = "USD",
    status: CampaignStatus = "active",
) -> CampaignInfo:
    return CampaignInfo(
        id=cid,
        source="google",
        account_id=account_id,
        external_id=str(900_000 + cid),
        name=f"Google {cid}",
        status=status,
        raw_status="ENABLED",
        objective_or_channel="SEARCH",
        objective_group="google",
        campaign_group_id=None,
        budget_id=budget_id,
        daily_budget=Decimal("100.00") if period == "DAILY" else None,
        total_budget=Decimal("5000.00") if period == "CUSTOM_PERIOD" else None,
        budget_period=period,
        currency=currency,
    )


def _budget(
    bid: int, amount: str, *, shared: bool, period: BudgetPeriod = "DAILY", currency: str = "USD"
) -> BudgetInfo:
    value = Decimal(amount)
    return BudgetInfo(
        id=bid,
        external_id=str(800_000 + bid),
        name="",
        amount=value,
        amount_micros=int(value * 1_000_000),
        period=period,
        explicitly_shared=shared,
        currency=currency,
    )


def _li_state(daily: str | None = "100.00", status: str = "active") -> dict[str, Any]:
    """Observation fields of a LinkedIn campaign snapshot."""
    return {
        "status": status,
        "budget_daily": None if daily is None else Decimal(daily),
        "budget_total": Decimal("5000.00") if daily is None else None,
    }


def _google_state(
    budget_id: int, amount: str, *, shared: bool, status: str = "active"
) -> dict[str, Any]:
    """Observation fields of a Google campaign snapshot drawing from ``budget_id``."""
    value = Decimal(amount)
    return {
        "status": status,
        "budget_daily": value,
        "budget_amount_micros": int(value * 1_000_000),
        "budget_id": budget_id,
        "payload": {"explicitly_shared": shared, "budget_period": "DAILY"},
    }


def _budget_state(amount: str, *, period: BudgetPeriod = "DAILY") -> dict[str, Any]:
    """Observation fields of a Google budget snapshot."""
    value = Decimal(amount)
    return {
        "budget_daily": value if period == "DAILY" else None,
        "budget_total": value if period == "CUSTOM_PERIOD" else None,
        "budget_amount_micros": int(value * 1_000_000),
        "payload": {"period": period, "explicitly_shared": True},
    }


def _li_day(day: date, cost: str, leads: int | None = 2, **extra: Any) -> DayFact:
    return DayFact(date=day, cost=Decimal(cost), impressions=1_000, clicks=10, leads=leads, **extra)


def _google_day(
    day: date, cost: str, conversions: str | None = "1", lost_is: str | None = None
) -> DayFact:
    return DayFact(
        date=day,
        cost=Decimal(cost),
        impressions=1_000,
        clicks=50,
        conversions=None if conversions is None else Decimal(conversions),
        budget_lost_is=None if lost_is is None else Decimal(lost_is),
    )


def _coverage(
    ref: DatasetRef, name: str, start: date | None, end: date | None
) -> dict[tuple[int, str], Freshness]:
    return {
        (ref.dataset_id, name): Freshness(ref.dataset_id, name, NOW, NOW, start, end, "complete")
    }


def _li_coverage(start: date | None, end: date | None) -> dict[tuple[int, str], Freshness]:
    return _coverage(LINKEDIN_DATASET, "linkedin.analytics_daily", start, end)


def _google_coverage(start: date | None, end: date | None) -> dict[tuple[int, str], Freshness]:
    return _coverage(GOOGLE_DATASET, "google.metrics_daily", start, end)


# ---------------------------------------------------------------------------
# Outcome by source, window totals, efficiency and baselines (AAP 0.5.4.1)
# ---------------------------------------------------------------------------


def test_outcome_reads_each_objective_groups_own_metric() -> None:
    day = DayFact(
        date=date(2026, 9, 30),
        cost=Decimal("10"),
        impressions=100,
        clicks=5,
        conversions=Decimal("2.5"),
        leads=3,
        website_conversions=4,
    )
    assert outcome(day, "google") == Decimal("2.5")
    assert outcome(day, "linkedin_lead_gen") == Decimal(3)
    assert outcome(day, "linkedin_other") == Decimal(4)
    unreported = DayFact(date=date(2026, 9, 30), cost=Decimal("10"), impressions=100, clicks=5)
    assert [outcome(unreported, group) for group in ("google", "linkedin_lead_gen")] == [None, None]
    with pytest.raises(KeyError):
        outcome(day, "hubspot")


def _totals_context(thresholds: Thresholds, **fields: Any) -> AnalysisContext:
    """LinkedIn campaigns 1-6 over the ten-day window (UTC days)."""
    eu_account = AccountInfo(3, "linkedin", "500000003", "LinkedIn EMEA", "EUR")
    before, first, second, third = (date(2026, 9, 21), *_dates(TEN_DAYS)[:3])
    campaigns = {
        1: _li_campaign(1),
        2: _li_campaign(2),
        3: _li_campaign(3),
        4: _li_campaign(4),
        5: _li_campaign(5, objective="WEBSITE_CONVERSION"),
        6: _li_campaign(6, account_id=eu_account.id),
    }
    days = {
        1: (
            _li_day(before, "500", 50),
            _li_day(first, "97"),
            _li_day(second, "0", 0),
            _li_day(third, "40", None),
        ),
        2: (_li_day(first, "3", 1),),
        3: (_li_day(first, "10", None),),
        5: (
            DayFact(date=first, cost=Decimal("20"), impressions=1, clicks=1, website_conversions=0),
        ),
        6: (_li_day(first, "5", 1),),
    }
    return _context(
        thresholds,
        _window(thresholds, TEN_DAYS, google_tz=None, sources=("linkedin",)),
        accounts={2: LINKEDIN_ACCOUNT, 3: eu_account},
        campaigns=campaigns,
        campaign_days=days,
        **fields,
    )


def test_window_totals_sum_only_window_days_and_ignore_repeats(thresholds: Thresholds) -> None:
    ctx = _totals_context(thresholds)
    assert window_totals(ctx, (1, 1)) == Totals(
        spend=Decimal("137"),
        outcomes=Decimal(2),
        impressions=3_000,
        clicks=30,
        spend_days=2,
        days=3,
    )
    both = window_totals(ctx, (2, 1))
    assert (both.spend, both.outcomes, both.spend_days, both.days) == (
        Decimal("140"),
        Decimal(3),
        2,
        3,
    )
    first, second = _dates(TEN_DAYS)[:2]
    member_days = window_totals(ctx, (1,), dates={first, second})
    assert (member_days.spend, member_days.outcomes, member_days.spend_days, member_days.days) == (
        Decimal("97"),
        Decimal(2),
        1,
        2,
    )


def test_window_totals_keep_unreported_outcomes_unavailable(thresholds: Thresholds) -> None:
    ctx = _totals_context(thresholds)
    assert window_totals(ctx, (3,)).outcomes is None
    assert window_totals(ctx, (4,)) == Totals(Decimal(0), None, 0, 0, 0, 0)
    assert window_totals(ctx, (5,)).outcomes == Decimal(0)


def test_window_totals_never_cross_groups_currencies_or_unknown_campaigns(
    thresholds: Thresholds,
) -> None:
    ctx = _totals_context(thresholds)
    with pytest.raises(ValueError, match="one source, objective group and currency"):
        window_totals(ctx, (1, 5))
    with pytest.raises(ValueError, match="currencies"):
        window_totals(ctx, (1, 6))
    with pytest.raises(KeyError, match="campaign 99"):
        window_totals(ctx, (99,))


@pytest.mark.parametrize(
    ("spend", "outcomes", "expected"),
    [
        ("100", None, None),
        ("100", "0", None),
        ("100", "4", Decimal(25)),
        ("0", "4", Decimal(0)),
    ],
)
def test_cost_per_outcome_is_unavailable_without_outcomes(
    spend: str, outcomes: str | None, expected: Decimal | None
) -> None:
    value = None if outcomes is None else Decimal(outcomes)
    assert cost_per_outcome(Decimal(spend), value) == expected


@pytest.mark.parametrize(
    ("baseline", "expected"),
    [(None, None), ("0", None), ("-1", None), ("25", Decimal(4))],
)
def test_expected_outcomes_need_a_positive_baseline(
    baseline: str | None, expected: Decimal | None
) -> None:
    value = None if baseline is None else Decimal(baseline)
    assert expected_outcomes(Decimal(100), value) == expected


def test_baseline_cpo_is_the_account_group_average_and_memoised(thresholds: Thresholds) -> None:
    ctx = _totals_context(thresholds)
    # Campaigns 1-4 of account 2: spend 137 + 3 + 10 = 150 over 3 leads. Campaign 6 is in
    # another account and campaign 5 in another objective group.
    assert baseline_cpo(ctx, LINKEDIN_ACCOUNT.id, "linkedin_lead_gen") == Decimal(50)
    assert ctx.cache[f"metrics.baseline_cpo:{LINKEDIN_ACCOUNT.id}:linkedin_lead_gen"] == 50
    ctx.cache[f"metrics.baseline_cpo:{LINKEDIN_ACCOUNT.id}:linkedin_lead_gen"] = Decimal(7)
    assert baseline_cpo(ctx, LINKEDIN_ACCOUNT.id, "linkedin_lead_gen") == Decimal(7)
    assert baseline_cpo(ctx, LINKEDIN_ACCOUNT.id, "linkedin_other") is None
    assert baseline_cpo(ctx, 999, "google") is None


def test_source_totals_list_sources_with_a_dataset_or_campaigns(thresholds: Thresholds) -> None:
    ctx = _totals_context(
        thresholds, datasets={"google": GOOGLE_DATASET, "linkedin": LINKEDIN_DATASET}
    )
    # Window spend 137 + 3 + 10 + 20 + 5; outcomes 2 + 1 + 0 + 1 (unreported values add nothing).
    assert source_totals(ctx) == {
        "google": (Decimal(0), Decimal(0)),
        "linkedin": (Decimal(175), Decimal(4)),
    }
    assert set(source_totals(_totals_context(thresholds))) == {"linkedin"}


# ---------------------------------------------------------------------------
# Budget-day classification from observations (AAP 0.5.4.1 "Budget history")
# ---------------------------------------------------------------------------

DAY = date(2026, 9, 10)


def _classify(
    observations: Iterable[Observation], tz: str = "UTC", day: date = DAY, gap: int = 6
) -> tuple[str, object]:
    result = classify_days(tuple(observations), [day], tz, value=status_value, max_gap_hours=gap)
    assert list(result) == [day]
    return result[day]


def _at(hours: Iterable[int], status: str = "active", day: date = DAY) -> list[Observation]:
    """Observations at ``hours`` after ``day`` 00:00 UTC (negative: the day before)."""
    return [Observation(_instant(day) + timedelta(hours=hour), status=status) for hour in hours]


def test_whole_day_known_needs_brackets_agreeing_at_most_the_gap_apart() -> None:
    assert _classify(_at(range(-6, 31, 6))) == ("known", "active")
    # Observations exactly on the day start and end bracket it.
    assert _classify(_at((0, 6, 12, 18, 24))) == ("known", "active")
    # Input order does not matter: observations are sorted by instant.
    assert _classify(reversed(_at(range(-6, 31, 6)))) == ("known", "active")


def test_disagreeing_brackets_make_the_day_changed_intraday() -> None:
    raised_at_noon = _at((-3, 0, 3, 6, 9)) + _at((12, 15, 18, 21, 24), status="paused")
    assert _classify(raised_at_noon) == ("changed_intraday", None)


@pytest.mark.parametrize(
    "hours",
    [
        pytest.param((), id="no-observation"),
        pytest.param((12, 18, 24, 30), id="first-snapshot-at-noon"),
        pytest.param((-3, 0, 6, 12, 18, 21), id="nothing-at-or-after-the-day-end"),
        pytest.param((0, 6, 13, 18, 24), id="seven-hour-gap"),
    ],
)
def test_missing_brackets_or_long_gaps_leave_the_day_unobserved(hours: tuple[int, ...]) -> None:
    assert _classify(_at(hours)) == ("unobserved", None)


def test_the_gap_limit_is_the_configured_value() -> None:
    seven_hours = _at((0, 6, 13, 18, 24))
    assert _classify(seven_hours, gap=7) == ("known", "active")


def test_days_are_native_days_of_the_given_zone() -> None:
    utc_day = _at(range(0, 25, 3))
    assert _classify(utc_day, "UTC") == ("known", "active")
    # In New York the day runs 04:00 to 04:00 UTC, so nothing brackets its end.
    assert _classify(utc_day, NY) == ("unobserved", None)
    # 2026-11-01 in New York has 25 hours, [04:00 UTC, 05:00 UTC next day), so an
    # observation at 04:30 UTC is still inside it and only one at 05:00 brackets its end.
    dst_end = date(2026, 11, 1)
    to_four_thirty = [
        *_at(range(-6, 25, 6), day=dst_end),
        Observation(datetime(2026, 11, 2, 4, 30, tzinfo=UTC), status="active"),
    ]
    assert _classify(to_four_thirty, NY, dst_end) == ("unobserved", None)
    to_five = [*to_four_thirty, Observation(datetime(2026, 11, 2, 5, tzinfo=UTC), status="active")]
    assert _classify(to_five, NY, dst_end) == ("known", "active")


@pytest.mark.parametrize("gap", [0, -1, True, 6.0, "6"])
def test_classify_days_rejects_a_gap_that_is_not_a_positive_integer(gap: Any) -> None:
    with pytest.raises(ValueError, match="max_gap_hours must be a positive integer"):
        classify_days(_at((0, 24)), [DAY], "UTC", value=status_value, max_gap_hours=gap)


def test_value_helpers_read_native_precision_amounts_and_periods() -> None:
    linkedin = Observation(NOW, status="active", budget_daily=Decimal("83.33"))
    assert campaign_state_value(linkedin) == ("active", Decimal("83.33"))
    google = Observation(
        NOW,
        status="active",
        budget_daily=Decimal("83.33"),
        budget_amount_micros=83_333_333,
        budget_id=11,
        payload={"explicitly_shared": False, "budget_period": "DAILY"},
    )
    assert campaign_state_value(google) == ("active", Decimal("83.333333"))
    shared = Observation(
        NOW, status="paused", budget_amount_micros=1, payload={"explicitly_shared": True}
    )
    assert campaign_state_value(shared) == ("paused", None)
    lifetime = Observation(
        NOW,
        status="active",
        budget_total=Decimal("5000"),
        budget_amount_micros=5_000_000_000,
        payload={"budget_period": "CUSTOM_PERIOD"},
    )
    assert campaign_state_value(lifetime) == ("active", None)
    assert campaign_state_value(Observation(NOW, status="active", budget_total=Decimal(9))) == (
        "active",
        None,
    )
    daily_budget = Observation(NOW, budget_amount_micros=1_000_000_000, payload={"period": "DAILY"})
    assert budget_amount_value(daily_budget) == Decimal(1000)
    custom = Observation(NOW, budget_amount_micros=7, payload={"period": "CUSTOM_PERIOD"})
    assert budget_amount_value(custom) is None
    # A period that is not text is no stated period: budget_daily decides.
    assert budget_amount_value(Observation(NOW, payload={"period": 5})) is None
    stated_daily = Observation(NOW, budget_daily=Decimal("12.5"), payload={"period": 5})
    assert budget_amount_value(stated_daily) == Decimal("12.5")
    assert membership_value(google) == 11
    assert membership_value(linkedin) is None
    assert status_value(shared) == "paused"


def test_gate_config_reads_every_budget_key_from_the_configuration(thresholds: Thresholds) -> None:
    assert GateConfig.from_thresholds(thresholds) == GateConfig(
        max_gap_hours=6,
        capped_day_ratio=Decimal("0.95"),
        capped_lost_is_share=Decimal("0.10"),
        high_utilization=Decimal("0.90"),
        under_utilization=Decimal("0.50"),
        min_known_days=7,
        min_spending_days=5,
        min_capped_days=5,
        sustained_share=Decimal("0.70"),
    )
    assert list(thresholds.subset(*BUDGET_THRESHOLD_KEYS)) == list(BUDGET_THRESHOLD_KEYS)
    assert thresholds.subset(*B2_THRESHOLD_KEYS) == {
        "b2.poor_efficiency_ratio": 1.5,
        "b2.min_expected_outcomes": 3,
    }


# ---------------------------------------------------------------------------
# Own-budget histories (AAP 0.5.4.1)
# ---------------------------------------------------------------------------

LI_START = _instant(date(2026, 9, 21))
_SPEND = ("97", "97", "97", "97", "97", "40", "40", "0")
"""Spend on 2026-09-22 to 09-29; 09-30 and 10-01 have no fact row (zero while covered)."""


def _li_days(spend: Iterable[str] = _SPEND) -> tuple[DayFact, ...]:
    outside = _li_day(date(2026, 9, 21), "500", 50)
    window = [
        _li_day(day, cost, 0 if cost == "0" else 2)
        for day, cost in zip(_dates(TEN_DAYS), spend, strict=False)
    ]
    return (outside, *window)


def _li_history_context(
    thresholds: Thresholds,
    *,
    freshness: dict[tuple[int, str], Freshness] | None = None,
    campaigns: dict[int, CampaignInfo] | None = None,
    observations: dict[tuple[str, int], tuple[Observation, ...]] | None = None,
    days: Mapping[int, tuple[DayFact, ...]] | None = None,
) -> AnalysisContext:
    """LinkedIn campaign 1 (100.00 a day) over the ten-day window, observed every 3 h."""
    return _context(
        thresholds,
        _window(thresholds, TEN_DAYS, google_tz=None, sources=("linkedin",)),
        datasets={"linkedin": LINKEDIN_DATASET},
        freshness=_li_coverage(date(2026, 9, 1), TEN_DAYS[1]) if freshness is None else freshness,
        accounts={LINKEDIN_ACCOUNT.id: LINKEDIN_ACCOUNT},
        campaigns={1: _li_campaign(1)} if campaigns is None else campaigns,
        observations=(
            {("campaign", 1): _series(LI_START, NOW, **_li_state())}
            if observations is None
            else observations
        ),
        campaign_days={1: _li_days()} if days is None else days,
    )


def test_campaign_history_counts_spending_idle_and_predicate_days(
    thresholds: Thresholds,
) -> None:
    ctx = _li_history_context(thresholds)
    history = campaign_budget_history(ctx, 1)
    assert (history.subject, history.shared, history.utilization_applicable) == (
        ("campaign", 1),
        False,
        True,
    )
    assert history.counts() == {
        "known_active": 10,
        "spending": 7,
        "idle": 3,
        "changed_intraday": 0,
        "unobserved": 0,
        "capped": 5,
        "high": 5,
        "under": 5,
    }
    # Window utilization is Σ spend ÷ Σ daily budget over known-budget active days.
    assert history.window_utilization == Decimal("0.565")
    assert history.window_spend == Decimal(565)
    assert history.latest_daily_budget == Decimal("100.00")
    assert history.known_label == "known-budget days"
    assert history.members == {}
    # Every observation from 2026-09-22 00:00 through 2026-10-02 00:00 UTC, 3 h apart.
    assert history.observation_count == 81
    assert history.assumption == "unchanged across 81 observations, at most 6 h apart"
    assert history.config == GateConfig.from_thresholds(thresholds)
    capped, under, idle = history.days[0], history.days[5], history.days[8]
    assert (capped.date, capped.klass, capped.status, capped.daily_budget, capped.spend) == (
        date(2026, 9, 22),
        "known",
        "active",
        Decimal("100.00"),
        Decimal(97),
    )
    assert (capped.u_d, capped.capped, capped.high, capped.under) == (
        Decimal("0.97"),
        True,
        True,
        False,
    )
    assert (capped.spending, capped.idle, capped.known_active) == (True, False, True)
    assert (under.u_d, under.capped, under.high, under.under) == (
        Decimal("0.4"),
        False,
        False,
        True,
    )
    # A covered date without a fact row is a known idle day: u_d = 0.
    assert (idle.date, idle.spend, idle.u_d, idle.idle, idle.under, idle.spending) == (
        date(2026, 9, 30),
        Decimal(0),
        Decimal(0),
        True,
        True,
        False,
    )
    assert campaign_budget_history(ctx, 1) is history
    assert isinstance(ctx.cache["metrics.gate_config"], GateConfig)


@pytest.mark.parametrize(
    ("freshness", "unobserved"),
    [
        pytest.param({}, ("2026-09-30", "2026-10-01"), id="no-freshness-row"),
        pytest.param(
            _li_coverage(date(2026, 10, 1), date(2026, 10, 1)),
            ("2026-09-30",),
            id="covered-from-after-the-day",
        ),
        pytest.param(
            _li_coverage(None, date(2026, 9, 30)), ("2026-10-01",), id="covered-through-before"
        ),
        pytest.param(
            _li_coverage(date(2026, 9, 1), None),
            ("2026-09-30", "2026-10-01"),
            id="no-coverage-end",
        ),
    ],
)
def test_a_missing_fact_row_outside_metrics_coverage_is_unobserved_never_idle(
    thresholds: Thresholds,
    freshness: dict[tuple[int, str], Freshness],
    unobserved: tuple[str, ...],
) -> None:
    history = campaign_budget_history(_li_history_context(thresholds, freshness=freshness), 1)
    assert _iso(day.date for day in history.days if day.klass == "unobserved") == unobserved
    assert history.unobserved == len(unobserved)
    assert history.idle == 3 - len(unobserved)
    assert history.known_active == 10 - len(unobserved)
    assert all(not day.known_active for day in history.days if day.klass == "unobserved")


def test_paused_zero_and_lifetime_budget_days_are_not_known_budget_active(
    thresholds: Thresholds,
) -> None:
    campaigns = {
        2: _li_campaign(2),
        3: _li_campaign(3, daily="0.00"),
        4: _li_campaign(4, daily=None),
    }
    ctx = _li_history_context(
        thresholds,
        campaigns=campaigns,
        observations={
            ("campaign", 2): _series(LI_START, NOW, **_li_state(status="paused")),
            ("campaign", 3): _series(LI_START, NOW, **_li_state("0.00")),
            ("campaign", 4): _series(LI_START, NOW, **_li_state(None)),
        },
        days={cid: _li_days() for cid in campaigns},
    )
    paused, zero, lifetime = (campaign_budget_history(ctx, cid) for cid in (2, 3, 4))
    for history in (paused, zero, lifetime):
        assert (history.known_active, history.spending, history.idle) == (0, 0, 0)
        assert all(day.klass == "known" and day.u_d is None for day in history.days)
        assert history.window_utilization is None
        assert cap_constrained(history).shortfalls[0] == "known-budget days 0 of 7 required"
    assert {day.status for day in paused.days} == {"paused"}
    assert paused.latest_daily_budget == Decimal("100.00")
    assert {day.daily_budget for day in zero.days} == {Decimal("0.00")}
    assert (lifetime.utilization_applicable, lifetime.latest_daily_budget) == (False, None)
    assert {day.daily_budget for day in lifetime.days} == {None}
    assert budget_subjects(ctx) == [("campaign", 2), ("campaign", 3)]


def test_a_budget_raised_at_noon_makes_that_day_changed_intraday(thresholds: Thresholds) -> None:
    raised = _instant(date(2026, 9, 25), 12)
    observations = (
        *_series(LI_START, raised - CADENCE, **_li_state()),
        *_series(raised, NOW, **_li_state("120.00")),
    )
    ctx = _li_history_context(thresholds, observations={("campaign", 1): observations})
    history = campaign_budget_history(ctx, 1)
    changed = [day for day in history.days if day.klass == "changed_intraday"]
    assert _iso(day.date for day in changed) == ("2026-09-25",)
    assert (changed[0].status, changed[0].daily_budget, changed[0].u_d) == (None, None, None)
    assert changed[0].spend == Decimal(97)
    assert (history.changed_intraday, history.known_active, history.capped) == (1, 9, 3)
    assert history.days[4].daily_budget == Decimal("120.00")
    assert history.days[4].u_d == Decimal(97) / Decimal(120)
    assert history.latest_daily_budget == Decimal("120.00")
    # The changed day's own observations (09-25 03:00 to 21:00 UTC) are not counted.
    assert history.observation_count == 81 - 7


def test_latest_daily_budget_falls_back_to_the_latest_observation(thresholds: Thresholds) -> None:
    campaigns = {6: _li_campaign(6), 7: _li_campaign(7)}
    ctx = _li_history_context(
        thresholds,
        campaigns=campaigns,
        observations={("campaign", 6): _series(_instant(TEN_DAYS[1], 12), NOW, **_li_state())},
        days={6: _li_days(), 7: _li_days()},
    )
    noon_first = campaign_budget_history(ctx, 6)
    assert (noon_first.unobserved, noon_first.known_active) == (10, 0)
    assert noon_first.latest_daily_budget == Decimal("100.00")
    assert noon_first.observation_count == 0
    never = campaign_budget_history(ctx, 7)
    assert (never.unobserved, never.latest_daily_budget) == (10, None)
    assert cap_constrained(never).shortfalls[0] == "known-budget days 0 of 7 required"


def test_campaign_history_rejects_unknown_and_shared_budget_campaigns(
    thresholds: Thresholds,
) -> None:
    ctx = _shared_context(thresholds)
    with pytest.raises(KeyError, match="campaign 99 is not in the analysis context"):
        campaign_budget_history(ctx, 99)
    with pytest.raises(ValueError, match="draws from explicitly shared budget 10"):
        campaign_budget_history(ctx, 1)


def _google_own_context(
    thresholds: Thresholds,
    span: tuple[date, date],
    days: Mapping[int, tuple[DayFact, ...]],
    *,
    window: AnalysisWindow | None = None,
    account: AccountInfo = GOOGLE_ACCOUNT,
    start: datetime = LI_START,
    end: datetime = NOW,
) -> AnalysisContext:
    """Google own-budget campaigns: 1 on budget 11 (100.00), 2 on budget 12 (1,000.00)."""
    return _context(
        thresholds,
        _window(thresholds, span) if window is None else window,
        datasets={"google": GOOGLE_DATASET},
        freshness=_google_coverage(date(2026, 9, 1), span[1]),
        accounts={account.id: account},
        budgets={11: _budget(11, "100.00", shared=False), 12: _budget(12, "1000.00", shared=False)},
        campaigns={1: _google_campaign(1, 11), 2: _google_campaign(2, 12)},
        observations={
            ("campaign", 1): _series(start, end, **_google_state(11, "100.00", shared=False)),
            ("campaign", 2): _series(start, end, **_google_state(12, "1000.00", shared=False)),
        },
        campaign_days=days,
    )


def test_google_budget_lost_impression_share_also_marks_a_capped_day(
    thresholds: Thresholds,
) -> None:
    span = (date(2026, 9, 28), date(2026, 10, 1))
    first, second, third, fourth = _dates(span)
    days = {
        1: (
            _google_day(first, "50", lost_is="0.12"),
            _google_day(second, "50", lost_is="0.05"),
            _google_day(third, "96"),
            _google_day(fourth, "50", lost_is="0.10"),
        )
    }
    history = campaign_budget_history(_google_own_context(thresholds, span, days), 1)
    assert [day.capped for day in history.days] == [True, False, True, True]
    assert [day.high for day in history.days] == [False, False, True, False]
    assert [day.under for day in history.days] == [True, True, False, True]
    assert history.latest_daily_budget == Decimal(100)
    # LinkedIn reports no lost impression share: only u_d decides there.
    li_day = _li_day(TEN_DAYS[0], "50", budget_lost_is=Decimal("0.5"))
    linkedin = _li_history_context(thresholds, days={1: (li_day,)})
    assert campaign_budget_history(linkedin, 1).days[0].capped is False


@pytest.mark.parametrize(
    ("timezone", "klass"),
    [pytest.param(NY, "unobserved", id="account-zone"), pytest.param(None, "known", id="utc")],
)
def test_google_days_fall_back_to_the_account_zone_then_utc(
    thresholds: Thresholds, timezone: str | None, klass: str
) -> None:
    day = date(2026, 9, 30)
    account = AccountInfo(1, "google", "1234567890", "Google Ads", "USD", timezone)
    # The window carries no Google boundary, and the observations cover exactly the UTC day.
    window = _window(thresholds, (day, day), google_tz=None, sources=("linkedin",))
    ctx = _google_own_context(
        thresholds,
        (day, day),
        {1: (_google_day(day, "50"),)},
        window=window,
        account=account,
        start=_instant(day),
        end=_instant(day + timedelta(days=1)),
    )
    assert campaign_budget_history(ctx, 1).days[0].klass == klass


# ---------------------------------------------------------------------------
# Shared budgets: dated membership and budget-active days (AAP 0.5.4.5)
# ---------------------------------------------------------------------------

POOL = 10
"""A Google explicitly shared DAILY budget of 1,000.00."""
OWN = 11
"""Campaign 3's own budget before it joins the pool."""
OBS_START = _instant(date(2026, 9, 23))
JOINED = _instant(date(2026, 9, 28), 15)
"""Campaign 3's first observation in the pool; it moved between 08:00 and 11:00 New York."""
_MEMBER_SPEND: dict[int, tuple[str, str]] = {1: ("600", "30"), 2: ("370", "10"), 3: ("100", "5")}
"""Daily ``(spend, conversions)`` of campaigns 1-3 on every window day."""


def _pool_state(status: str = "active") -> dict[str, Any]:
    return _google_state(POOL, "1000.00", shared=True, status=status)


def _member_days(
    spend: dict[int, tuple[str, str]] = _MEMBER_SPEND,
) -> dict[int, tuple[DayFact, ...]]:
    return {
        cid: tuple(_google_day(day, cost, conversions) for day in _dates(WEEK))
        for cid, (cost, conversions) in spend.items()
    }


def _without(observations: tuple[Observation, ...], *instants: datetime) -> tuple[Observation, ...]:
    return tuple(obs for obs in observations if obs.captured_at not in instants)


def _shared_context(
    thresholds: Thresholds,
    *,
    campaigns: dict[int, CampaignInfo] | None = None,
    observations: dict[tuple[str, int], tuple[Observation, ...]] | None = None,
    days: Mapping[int, tuple[DayFact, ...]] | None = None,
    budgets: dict[int, BudgetInfo] | None = None,
    accounts: dict[int, AccountInfo] | None = None,
    covered: bool = True,
) -> AnalysisContext:
    """Campaigns 1 and 2 draw from the pool all week; campaign 3 joins it on 2026-09-28."""
    default_observations = {
        ("budget", POOL): _series(OBS_START, NOW, **_budget_state("1000.00")),
        ("campaign", 1): _series(OBS_START, NOW, **_pool_state()),
        ("campaign", 2): _series(OBS_START, NOW, **_pool_state()),
        ("campaign", 3): (
            *_series(OBS_START, JOINED - CADENCE, **_google_state(OWN, "150.00", shared=False)),
            *_series(JOINED, NOW, **_pool_state()),
        ),
    }
    return _context(
        thresholds,
        _window(thresholds, WEEK),
        datasets={"google": GOOGLE_DATASET},
        freshness=_google_coverage(date(2026, 9, 1), WEEK[1]) if covered else {},
        accounts={GOOGLE_ACCOUNT.id: GOOGLE_ACCOUNT} if accounts is None else accounts,
        budgets=(
            {POOL: _budget(POOL, "1000.00", shared=True), OWN: _budget(OWN, "150.00", shared=False)}
            if budgets is None
            else budgets
        ),
        campaigns=(
            {cid: _google_campaign(cid, POOL) for cid in (1, 2, 3)}
            if campaigns is None
            else campaigns
        ),
        observations=default_observations if observations is None else observations,
        campaign_days=_member_days() if days is None else days,
    )


def test_shared_history_sums_member_spend_on_member_days_only(thresholds: Thresholds) -> None:
    ctx = _shared_context(thresholds)
    history = shared_budget_history(ctx, POOL)
    assert (history.subject, history.shared, history.utilization_applicable) == (
        ("budget", POOL),
        True,
        True,
    )
    assert history.known_label == "budget-active days"
    # Campaign 3 changed budgets during 2026-09-28 (New York), so that day is unknown.
    assert [day.klass for day in history.days] == [
        "known",
        "known",
        "known",
        "changed_intraday",
        "known",
        "known",
        "known",
    ]
    assert [day.spend for day in history.days] == [Decimal(970)] * 4 + [Decimal(1070)] * 3
    assert [day.u_d for day in history.days] == (
        [Decimal("0.97")] * 3 + [None] + [Decimal("1.07")] * 3
    )
    assert history.counts() == {
        "known_active": 6,
        "spending": 6,
        "idle": 0,
        "changed_intraday": 1,
        "unobserved": 0,
        "capped": 6,
        "high": 6,
        "under": 0,
    }
    assert dict(history.members) == {
        1: tuple(_dates(WEEK)),
        2: tuple(_dates(WEEK)),
        3: (date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)),
    }
    assert history.window_utilization == Decimal("1.02")
    assert history.window_spend == Decimal(7090)
    assert history.latest_daily_budget == Decimal(1000)
    # The budget's own observations from 09-25 03:00 through 10-02 06:00 UTC.
    assert history.observation_count == 58
    assert cap_constrained(history).shortfalls == ("budget-active days 6 of 7 required",)
    assert shared_budget_history(ctx, POOL) is history
    assert budget_history(ctx, ("budget", POOL)) is history


def test_shared_subject_totals_campaigns_and_baseline_follow_member_days(
    thresholds: Thresholds,
) -> None:
    ctx = _shared_context(thresholds)
    pool: SubjectKey = ("budget", POOL)
    assert subject_campaigns(ctx, pool) == (1, 2, 3)
    assert subject_objective_groups(ctx, pool) == frozenset({"google"})
    # Campaign 3 counts 3 member days: 4,200 + 2,590 + 300 spend, 210 + 70 + 15 outcomes.
    assert subject_totals(ctx, pool) == Totals(
        spend=Decimal(7090),
        outcomes=Decimal(295),
        impressions=17_000,
        clicks=850,
        spend_days=7,
        days=7,
    )
    # The baseline is the account's Google average over the whole window: 7,490 / 315.
    assert subject_baseline(ctx, pool) == Decimal(7490) / Decimal(315)
    assert subject_baseline(ctx, pool) == baseline_cpo(ctx, GOOGLE_ACCOUNT.id, "google")
    assert budget_subjects(ctx) == [pool]
    assert subject_campaigns(ctx, ("campaign", 3)) == (3,)
    assert subject_objective_groups(ctx, ("campaign", 3)) == frozenset({"google"})
    with pytest.raises(KeyError):
        subject_campaigns(ctx, ("campaign", 99))


def _pool_days_variant(
    thresholds: Thresholds, *, budget: tuple[Observation, ...] | None = None, **campaign_obs: Any
) -> list[str]:
    """Day classes of the pool when campaigns 1 and 2 are its only members."""
    observations: dict[tuple[str, int], tuple[Observation, ...]] = {
        ("budget", POOL): budget or _series(OBS_START, NOW, **_budget_state("1000.00")),
        ("campaign", 1): campaign_obs.get("one", _series(OBS_START, NOW, **_pool_state())),
        ("campaign", 2): campaign_obs.get("two", _series(OBS_START, NOW, **_pool_state())),
    }
    ctx = _shared_context(
        thresholds,
        campaigns={cid: _google_campaign(cid, POOL) for cid in (1, 2)},
        observations=observations,
        days={cid: facts for cid, facts in _member_days().items() if cid != 3},
    )
    return [day.klass for day in shared_budget_history(ctx, POOL).days]


def test_an_unknown_amount_makes_the_day_unknown(thresholds: Thresholds) -> None:
    amounts = _series(OBS_START, NOW, **_budget_state("1000.00"))
    gap = _without(amounts, _instant(date(2026, 9, 26), 9), _instant(date(2026, 9, 26), 12))
    assert _pool_days_variant(thresholds, budget=gap) == [
        "known",
        "unobserved",
        *["known"] * 5,
    ]
    raised = _instant(date(2026, 9, 26), 15)
    changed = (
        *_series(OBS_START, raised - CADENCE, **_budget_state("1000.00")),
        *_series(raised, NOW, **_budget_state("1200.00")),
    )
    assert _pool_days_variant(thresholds, budget=changed) == [
        "known",
        "changed_intraday",
        *["known"] * 5,
    ]


def test_an_unknown_membership_of_a_spending_member_makes_the_day_unobserved(
    thresholds: Thresholds,
) -> None:
    member = _series(OBS_START, NOW, **_pool_state())
    gap = _without(member, _instant(date(2026, 9, 27), 9), _instant(date(2026, 9, 27), 12))
    assert _pool_days_variant(thresholds, two=gap) == [
        "known",
        "known",
        "unobserved",
        *["known"] * 4,
    ]


@pytest.mark.parametrize(
    ("spend", "klass"),
    [pytest.param("0", "known", id="idle"), pytest.param("50", "unobserved", id="spent")],
)
def test_a_former_member_unsettles_a_day_only_when_it_spent(
    thresholds: Thresholds, spend: str, klass: str
) -> None:
    ctx = _shared_context(
        thresholds,
        campaigns={
            **{cid: _google_campaign(cid, POOL) for cid in (1, 2)},
            5: _google_campaign(5, None, period=None, status="ended"),
        },
        observations={
            ("budget", POOL): _series(OBS_START, NOW, **_budget_state("1000.00")),
            ("campaign", 1): _series(OBS_START, NOW, **_pool_state()),
            ("campaign", 2): _series(OBS_START, NOW, **_pool_state()),
            # Last seen in the pool on 2026-09-20, more than 6 h before any window day.
            ("campaign", 5): _series(
                _instant(date(2026, 9, 15)), _instant(date(2026, 9, 20)), **_pool_state()
            ),
        },
        days={
            **{cid: facts for cid, facts in _member_days().items() if cid != 3},
            5: (_google_day(date(2026, 9, 27), spend),),
        },
    )
    history = shared_budget_history(ctx, POOL)
    assert history.days[2].klass == klass
    assert 5 not in history.members
    assert subject_campaigns(ctx, ("budget", POOL)) == (1, 2)


@pytest.mark.parametrize(
    ("one", "two", "label"),
    [("paused", "paused", "paused"), ("ended", "paused", "paused"), ("ended", "ended", "ended")],
)
def test_a_day_without_an_active_member_is_not_budget_active(
    thresholds: Thresholds, one: str, two: str, label: str
) -> None:
    ctx = _shared_context(
        thresholds,
        campaigns={cid: _google_campaign(cid, POOL) for cid in (1, 2)},
        observations={
            ("budget", POOL): _series(OBS_START, NOW, **_budget_state("1000.00")),
            ("campaign", 1): _series(OBS_START, NOW, **_pool_state(one)),
            ("campaign", 2): _series(OBS_START, NOW, **_pool_state(two)),
        },
        days={cid: facts for cid, facts in _member_days().items() if cid != 3},
    )
    history = shared_budget_history(ctx, POOL)
    assert {(day.klass, day.status, day.u_d) for day in history.days} == {("known", label, None)}
    assert {day.daily_budget for day in history.days} == {Decimal(1000)}
    assert history.known_active == 0


def test_a_member_status_change_with_no_other_active_member_is_changed_intraday(
    thresholds: Thresholds,
) -> None:
    paused_at = _instant(date(2026, 9, 26), 15)
    two = (
        *_series(OBS_START, paused_at - CADENCE, **_pool_state()),
        *_series(paused_at, NOW, **_pool_state("paused")),
    )
    one = _series(OBS_START, NOW, **_pool_state("paused"))
    assert _pool_days_variant(thresholds, one=one, two=two) == [
        "known",
        "changed_intraday",
        *["known"] * 5,
    ]


def test_a_budget_no_campaign_has_shown_has_no_member_and_no_baseline(
    thresholds: Thresholds,
) -> None:
    ctx = _shared_context(
        thresholds,
        campaigns={6: _google_campaign(6, POOL)},
        observations={("budget", POOL): _series(OBS_START, NOW, **_budget_state("1000.00"))},
        days={},
    )
    pool: SubjectKey = ("budget", POOL)
    history = shared_budget_history(ctx, POOL)
    assert {(day.klass, day.status) for day in history.days} == {("known", None)}
    assert (history.known_active, dict(history.members)) == (0, {})
    # Campaign 6 draws from the pool now, so an increase would land on it.
    assert subject_campaigns(ctx, pool) == (6,)
    assert subject_baseline(ctx, pool) is None
    assert subject_totals(ctx, pool) == Totals(Decimal(0), None, 0, 0, 0, 0)
    assert member_poor_efficiency(ctx, POOL, 6) is False


@pytest.mark.parametrize(
    ("period", "amount", "applicable", "daily"),
    [
        pytest.param("CUSTOM_PERIOD", "1000.00", False, None, id="custom-period"),
        pytest.param("DAILY", "0.00", True, Decimal(0), id="zero-amount"),
    ],
)
def test_a_shared_budget_without_a_positive_daily_amount_has_no_budget_active_day(
    thresholds: Thresholds,
    period: BudgetPeriod,
    amount: str,
    applicable: bool,
    daily: Decimal | None,
) -> None:
    ctx = _shared_context(
        thresholds,
        budgets={POOL: _budget(POOL, amount, shared=True, period=period)},
        campaigns={cid: _google_campaign(cid, POOL) for cid in (1, 2)},
        observations={
            ("budget", POOL): _series(OBS_START, NOW, **_budget_state(amount, period=period)),
            ("campaign", 1): _series(OBS_START, NOW, **_pool_state()),
            ("campaign", 2): _series(OBS_START, NOW, **_pool_state()),
        },
        days={cid: facts for cid, facts in _member_days().items() if cid != 3},
    )
    history = shared_budget_history(ctx, POOL)
    assert history.utilization_applicable is applicable
    assert {(day.klass, day.status, day.daily_budget, day.u_d) for day in history.days} == {
        ("known", "active", daily, None)
    }
    assert (history.known_active, history.window_utilization) == (0, None)
    assert budget_subjects(ctx) == ([("budget", POOL)] if applicable else [])


def test_a_member_without_a_fact_row_counts_zero_only_while_covered(
    thresholds: Thresholds,
) -> None:
    days = {cid: facts for cid, facts in _member_days().items() if cid != 3}
    days[2] = tuple(fact for fact in days[2] if fact.date != date(2026, 9, 27))
    campaigns = {cid: _google_campaign(cid, POOL) for cid in (1, 2)}
    observations = {
        ("budget", POOL): _series(OBS_START, NOW, **_budget_state("1000.00")),
        ("campaign", 1): _series(OBS_START, NOW, **_pool_state()),
        ("campaign", 2): _series(OBS_START, NOW, **_pool_state()),
    }
    covered = shared_budget_history(
        _shared_context(thresholds, campaigns=campaigns, observations=observations, days=days),
        POOL,
    )
    assert (covered.days[2].klass, covered.days[2].u_d) == ("known", Decimal("0.6"))
    uncovered = shared_budget_history(
        _shared_context(
            thresholds, campaigns=campaigns, observations=observations, days=days, covered=False
        ),
        POOL,
    )
    assert (uncovered.days[2].klass, uncovered.days[2].spend) == ("unobserved", Decimal(600))
    assert uncovered.unobserved == 1


def test_shared_lost_impression_share_is_spend_weighted_over_spending_members(
    thresholds: Thresholds,
) -> None:
    first, second, *rest = _dates(WEEK)
    one = (
        _google_day(first, "300", lost_is="0.20"),
        _google_day(second, "300", lost_is="0.05"),
        *(_google_day(day, "600") for day in rest),
    )
    two = (
        _google_day(first, "100"),
        _google_day(second, "0", lost_is="0.90"),
        *(_google_day(day, "370") for day in rest),
    )
    ctx = _shared_context(
        thresholds,
        campaigns={cid: _google_campaign(cid, POOL) for cid in (1, 2)},
        observations={
            ("budget", POOL): _series(OBS_START, NOW, **_budget_state("1000.00")),
            ("campaign", 1): _series(OBS_START, NOW, **_pool_state()),
            ("campaign", 2): _series(OBS_START, NOW, **_pool_state()),
        },
        days={1: one, 2: two},
    )
    history = shared_budget_history(ctx, POOL)
    # Day 1: (0.20 x 300) / 300 = 0.20, capped at u_d 0.4. Day 2: the zero-spend member's
    # share is not weighted in, so 0.05 stays below 0.10.
    assert [(day.u_d, day.capped) for day in history.days[:2]] == [
        (Decimal("0.4"), True),
        (Decimal("0.3"), False),
    ]


def test_shared_history_rejects_unknown_and_unshared_budgets(thresholds: Thresholds) -> None:
    ctx = _shared_context(thresholds)
    with pytest.raises(KeyError, match="budget 99 is not in the analysis context"):
        shared_budget_history(ctx, 99)
    with pytest.raises(ValueError, match="budget 11 is not explicitly shared"):
        shared_budget_history(ctx, OWN)
    with pytest.raises(ValueError, match="subject type must be 'campaign' or 'budget'"):
        budget_history(ctx, _any(("keyword", 1)))


def _any(value: object) -> Any:
    """Pass a deliberately invalid value past the type checker."""
    return value


def test_budget_subjects_are_daily_own_budget_campaigns_then_shared_daily_budgets(
    thresholds: Thresholds,
) -> None:
    ctx = _context(
        thresholds,
        _window(thresholds, WEEK),
        accounts={GOOGLE_ACCOUNT.id: GOOGLE_ACCOUNT, LINKEDIN_ACCOUNT.id: LINKEDIN_ACCOUNT},
        budgets={
            POOL: _budget(POOL, "1000.00", shared=True),
            OWN: _budget(OWN, "100.00", shared=False),
            12: _budget(12, "5000.00", shared=False, period="CUSTOM_PERIOD"),
            13: _budget(13, "9000.00", shared=True, period="CUSTOM_PERIOD"),
        },
        campaigns={
            1: _li_campaign(1),
            2: _li_campaign(2, daily=None),
            3: _google_campaign(3, OWN),
            4: _google_campaign(4, 12, period="CUSTOM_PERIOD"),
            5: _google_campaign(5, POOL),
            6: _google_campaign(6, 13, period="CUSTOM_PERIOD"),
        },
    )
    assert budget_subjects(ctx) == [("campaign", 1), ("campaign", 3), ("budget", POOL)]


def test_mixed_currencies_leave_shared_efficiency_unavailable(thresholds: Thresholds) -> None:
    eu_account = AccountInfo(3, "google", "2223334444", "Google EMEA", "EUR", NY)
    ctx = _shared_context(
        thresholds,
        accounts={GOOGLE_ACCOUNT.id: GOOGLE_ACCOUNT, eu_account.id: eu_account},
        campaigns={
            1: _google_campaign(1, POOL),
            2: _google_campaign(2, POOL, account_id=eu_account.id, currency="EUR"),
        },
        observations={
            ("budget", POOL): _series(OBS_START, NOW, **_budget_state("1000.00")),
            ("campaign", 1): _series(OBS_START, NOW, **_pool_state()),
            ("campaign", 2): _series(OBS_START, NOW, **_pool_state()),
        },
        days={cid: facts for cid, facts in _member_days().items() if cid != 3},
    )
    pool: SubjectKey = ("budget", POOL)
    assert subject_totals(ctx, pool) is None
    assert subject_baseline(ctx, pool) is None
    assessment = assess_b2(ctx, pool)
    assert assessment.gate.passed is True
    assert (assessment.meets, assessment.branch) == (False, None)
    assert (assessment.spend, assessment.outcomes, assessment.cpo) == (Decimal(0), None, None)
    assert (assessment.baseline, assessment.expected) == (None, None)
    assert spend_increase_blocked(ctx) == frozenset()


# ---------------------------------------------------------------------------
# History gates (AAP 0.5.4.1 table)
# ---------------------------------------------------------------------------


def _history(
    thresholds: Thresholds,
    *,
    known: int,
    spending: int,
    capped: int = 0,
    high: int = 0,
    under: int = 0,
    shared: bool = False,
) -> BudgetHistory:
    """A history with the given day counts; gates read only the counts and the config."""
    return BudgetHistory(
        subject=("budget", POOL) if shared else ("campaign", 1),
        shared=shared,
        utilization_applicable=True,
        days=(),
        known_active=known,
        spending=spending,
        idle=known - spending,
        changed_intraday=0,
        unobserved=0,
        capped=capped,
        high=high,
        under=under,
        window_utilization=None,
        latest_daily_budget=None,
        members={},
        observation_count=0,
        config=GateConfig.from_thresholds(thresholds),
    )


def test_cap_constrained_needs_known_spending_and_capped_days(thresholds: Thresholds) -> None:
    eligible = cap_constrained(_history(thresholds, known=7, spending=5, capped=5, high=5))
    assert (eligible.passed, eligible.shortfalls, eligible.describe()) == (True, (), "")
    assert dict(eligible.counts) == {
        "known_budget_days": (7, 7),
        "spending_days": (5, 5),
        "capped_days": (5, 5),
        "capped_share": (5, 5),
    }
    # One capped day followed by six idle days: neither cap-constrained nor high-utilization.
    one_then_idle = _history(thresholds, known=7, spending=1, capped=1, high=1, under=6)
    assert cap_constrained(one_then_idle).shortfalls == (
        "spending days 1 of 5 required",
        "capped days 1 of 5 required",
        "capped days 1 of 5 required (70% of 7 known-budget days)",
    )
    assert high_utilization(one_then_idle).passed is False
    share = cap_constrained(_history(thresholds, known=10, spending=10, capped=6))
    assert share.shortfalls == ("capped days 6 of 7 required (70% of 10 known-budget days)",)
    short = cap_constrained(_history(thresholds, known=4, spending=4, capped=4))
    assert short.describe().startswith("known-budget days 4 of 7 required")


def test_high_utilization_needs_seventy_percent_spending_and_high_days(
    thresholds: Thresholds,
) -> None:
    four = high_utilization(_history(thresholds, known=7, spending=7, high=4))
    assert four.shortfalls == (
        "high-utilization days 4 of 5 required (70% of 7 known-budget days)",
    )
    assert high_utilization(_history(thresholds, known=7, spending=7, high=5)).passed is True
    sparse = high_utilization(_history(thresholds, known=10, spending=6, high=6))
    assert sparse.shortfalls == (
        "spending days 6 of 7 required (70% of 10 known-budget days)",
        "high-utilization days 6 of 7 required (70% of 10 known-budget days)",
    )
    assert dict(sparse.counts)["spending_share"] == (6, 7)


def test_under_utilized_needs_spending_days_so_an_idle_budget_is_no_donor(
    thresholds: Thresholds,
) -> None:
    donor = under_utilized(_history(thresholds, known=10, spending=6, under=8))
    assert donor.passed is True
    assert dict(donor.counts) == {
        "known_budget_days": (10, 7),
        "spending_days": (6, 5),
        "under_utilized_days": (8, 7),
    }
    idle = under_utilized(_history(thresholds, known=10, spending=0, under=10))
    assert idle.shortfalls == ("spending days 0 of 5 required",)
    # The module example: the shortfall is reported, the met check stays in the counts.
    example = under_utilized(_history(thresholds, known=9, spending=3, under=9))
    assert example.describe() == "spending days 3 of 5 required"
    assert example.counts["known_budget_days"] == (9, 7)


def test_shared_gates_count_budget_active_days(thresholds: Thresholds) -> None:
    gate = cap_constrained(_history(thresholds, known=4, spending=4, capped=4, shared=True))
    assert gate.shortfalls == (
        "budget-active days 4 of 7 required",
        "spending days 4 of 5 required",
        "capped days 4 of 5 required",
    )


def test_gates_apply_the_given_configuration(thresholds: Thresholds) -> None:
    tuned = thresholds_with({"budget.min_known_days": 10, "budget.sustained_share": 0.725})
    history = _history(thresholds, known=10, spending=10, capped=7, high=7, under=7)
    assert cap_constrained(history, tuned).shortfalls == (
        "capped days 7 of 8 required (72.5% of 10 known-budget days)",
    )
    assert cap_constrained(history).passed is True
    week = _history(thresholds, known=7, spending=7, high=7, under=7)
    assert high_utilization(week, tuned).shortfalls[0] == "known-budget days 7 of 10 required"
    assert under_utilized(week, tuned).shortfalls[0] == "known-budget days 7 of 10 required"
    assert history.config.min_known_days == 7


# ---------------------------------------------------------------------------
# B2, the G1 member predicate and the G1 set (AAP 0.5.4.2, 0.5.4.5)
# ---------------------------------------------------------------------------


def _b2_context(
    thresholds: Thresholds, hot_leads: int | None, control_leads: tuple[int, ...]
) -> AnalysisContext:
    """LinkedIn: ``hot`` (1) spends 95.00 of 100.00 daily; ``control`` (2) 300.00 of 500.00."""
    hot = tuple(_li_day(day, "95", hot_leads) for day in _dates(WEEK))
    control = tuple(
        _li_day(day, "300", leads) for day, leads in zip(_dates(WEEK), control_leads, strict=True)
    )
    return _context(
        thresholds,
        _window(thresholds, WEEK, google_tz=None, sources=("linkedin",)),
        datasets={"linkedin": LINKEDIN_DATASET},
        freshness=_li_coverage(date(2026, 9, 1), WEEK[1]),
        accounts={LINKEDIN_ACCOUNT.id: LINKEDIN_ACCOUNT},
        campaigns={1: _li_campaign(1), 2: _li_campaign(2, daily="500.00")},
        observations={
            ("campaign", 1): _series(OBS_START, NOW, **_li_state()),
            ("campaign", 2): _series(OBS_START, NOW, **_li_state("500.00")),
        },
        campaign_days={1: hot, 2: control},
    )


def test_b2_zero_outcome_branch_needs_expected_outcomes_at_the_minimum(
    thresholds: Thresholds,
) -> None:
    ctx = _b2_context(thresholds, 0, (30,) * 7)
    hot: SubjectKey = ("campaign", 1)
    assessment = assess_b2(ctx, hot)
    baseline = Decimal(2765) / Decimal(210)
    assert (assessment.subject, assessment.meets, assessment.branch) == (hot, True, "zero_outcomes")
    assert assessment.gate.passed is True
    assert (assessment.spend, assessment.outcomes, assessment.cpo) == (
        Decimal(665),
        Decimal(0),
        None,
    )
    assert (assessment.baseline, assessment.expected) == (baseline, Decimal(665) / baseline)
    assert assess_b2(ctx, hot) is assessment
    # A LinkedIn campaign has no budget row: G1 blocks the campaign alone.
    assert spend_increase_blocked(ctx) == frozenset({hot})
    rare = assess_b2(_b2_context(thresholds, 0, (1, 0, 0, 0, 0, 0, 0)), hot)
    assert rare.expected is not None
    assert rare.expected < 3
    assert (rare.meets, rare.branch) == (False, None)


@pytest.mark.parametrize(
    ("hot_leads", "control_leads"),
    [
        pytest.param(None, (30,) * 7, id="outcomes-unavailable"),
        pytest.param(0, (0,) * 7, id="baseline-unavailable"),
    ],
)
def test_b2_needs_available_outcomes_and_baseline(
    thresholds: Thresholds, hot_leads: int | None, control_leads: tuple[int, ...]
) -> None:
    assessment = assess_b2(_b2_context(thresholds, hot_leads, control_leads), ("campaign", 1))
    assert assessment.gate.passed is True
    assert (assessment.meets, assessment.branch) == (False, None)


def test_b2_poor_efficiency_branch_and_the_own_google_budget_row(thresholds: Thresholds) -> None:
    linkedin = assess_b2(_b2_context(thresholds, 1, (30,) * 7), ("campaign", 1))
    assert (linkedin.meets, linkedin.branch, linkedin.cpo) == (True, "poor_efficiency", Decimal(95))
    days = {
        1: tuple(_google_day(day, "95", "1") for day in _dates(WEEK)),
        2: tuple(_google_day(day, "500", "50") for day in _dates(WEEK)),
    }
    ctx = _google_own_context(thresholds, WEEK, days)
    blocked = spend_increase_blocked(ctx)
    assert blocked == frozenset({("campaign", 1), ("budget", 11)})
    assert assess_b2(ctx, ("campaign", 2)).meets is False
    assert spend_increase_blocked(ctx) is blocked
    assert ctx.cache["metrics.spend_increase_blocked"] is blocked


def _two_member_context(thresholds: Thresholds, member_two_conversions: str) -> AnalysisContext:
    """Campaigns 1 (600.00, 30 a day) and 2 (370.00) share the pool all week: u_d 0.97."""
    return _shared_context(
        thresholds,
        campaigns={cid: _google_campaign(cid, POOL) for cid in (1, 2)},
        observations={
            ("budget", POOL): _series(OBS_START, NOW, **_budget_state("1000.00")),
            ("campaign", 1): _series(OBS_START, NOW, **_pool_state()),
            ("campaign", 2): _series(OBS_START, NOW, **_pool_state()),
        },
        days=_member_days({1: ("600", "30"), 2: ("370", member_two_conversions)}),
    )


@pytest.mark.parametrize(
    ("conversions", "poor"),
    [
        # Baseline 6,790 / 280 = 24.25; 1.5 x 24.25 = 36.375 <= 370 / 10 = 37.
        pytest.param("10", True, id="poor-efficiency"),
        # Baseline 6,790 / 294 = 23.10; 1.5 x 23.10 = 34.64 > 370 / 12 = 30.83.
        pytest.param("12", False, id="good-efficiency"),
        # No outcomes, with 2,590 / 20 = 129.5 expected.
        pytest.param("0", True, id="zero-outcomes"),
    ],
)
def test_a_poor_member_in_a_high_utilization_shared_budget_blocks_every_increase(
    thresholds: Thresholds, conversions: str, poor: bool
) -> None:
    ctx = _two_member_context(thresholds, conversions)
    pool: SubjectKey = ("budget", POOL)
    assessment = assess_b2(ctx, pool)
    assert assessment.gate.passed is True
    assert assessment.meets is False
    assert member_poor_efficiency(ctx, POOL, 1) is False
    assert member_poor_efficiency(ctx, POOL, 2) is poor
    expected = {pool, ("campaign", 1), ("campaign", 2)} if poor else set()
    assert spend_increase_blocked(ctx) == frozenset(expected)


def test_member_predicate_rejects_unknown_campaigns_and_unshared_budgets(
    thresholds: Thresholds,
) -> None:
    ctx = _two_member_context(thresholds, "10")
    with pytest.raises(KeyError, match="campaign 99"):
        member_poor_efficiency(ctx, POOL, 99)
    with pytest.raises(ValueError, match="not explicitly shared"):
        member_poor_efficiency(ctx, OWN, 1)


def test_a_member_poor_shared_budget_is_not_blocked_while_its_gate_fails(
    thresholds: Thresholds,
) -> None:
    # The joiner week has 6 budget-active days, short of the 7 the high-utilization gate
    # requires, so a poor member does not trigger G1.
    ctx = _shared_context(thresholds, days=_member_days({**_MEMBER_SPEND, 2: ("370", "0")}))
    assert member_poor_efficiency(ctx, POOL, 2) is True
    assert assess_b2(ctx, ("budget", POOL)).gate.passed is False
    assert spend_increase_blocked(ctx) == frozenset()


# ---------------------------------------------------------------------------
# The Google counting contract (AAP 0.5.4.5 B4)
# ---------------------------------------------------------------------------

DATA_DRIVEN = "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"


def _action(
    start: datetime = OBS_START,
    end: datetime = NOW,
    *,
    include: bool = True,
    model: str = LAST_CLICK_MODEL,
    counting: str = "ONE_PER_CLICK",
) -> tuple[Observation, ...]:
    payload = {
        "include_in_conversions_metric": include,
        "attribution_model": model,
        "counting_type": counting,
    }
    return _series(start, end, status="ENABLED", payload=payload)


def _contract(
    thresholds: Thresholds,
    actions: dict[int, tuple[Observation, ...]],
    *,
    registered: Iterable[int] = (),
) -> list[str]:
    ctx = _context(
        thresholds,
        _window(thresholds, WEEK),
        accounts={GOOGLE_ACCOUNT.id: GOOGLE_ACCOUNT},
        conversion_actions={
            aid: ConversionActionInfo(aid, str(990_400_000 + aid), f"Action {aid}", "ENABLED", True)
            for aid in registered
        },
        observations={("conversion_action", aid): obs for aid, obs in actions.items()},
    )
    contract = counting_contract(ctx)
    assert list(contract) == _dates(WEEK)
    return list(contract.values())


def test_without_any_conversion_action_the_contract_is_unknown(thresholds: Thresholds) -> None:
    assert _contract(thresholds, {}) == ["unknown"] * 7
    assert _contract(thresholds, {}, registered=(1,)) == ["unknown"] * 7


@pytest.mark.parametrize(
    "actions",
    [
        pytest.param({1: _action()}, id="last-click"),
        pytest.param({1: _action(counting="MANY_PER_CLICK")}, id="many-per-click"),
        pytest.param(
            {1: _action(), 2: _action(include=False, model=DATA_DRIVEN)}, id="excluded-action"
        ),
    ],
)
def test_included_last_click_actions_make_last_click_days(
    thresholds: Thresholds, actions: dict[int, tuple[Observation, ...]]
) -> None:
    assert _contract(thresholds, actions, registered=actions) == ["last_click"] * 7


@pytest.mark.parametrize(
    "actions",
    [
        pytest.param({1: _action(model=DATA_DRIVEN)}, id="data-driven"),
        pytest.param({1: _action(), 2: _action(model="EXTERNAL")}, id="external"),
    ],
)
def test_an_included_weighted_action_makes_weighted_days(
    thresholds: Thresholds, actions: dict[int, tuple[Observation, ...]]
) -> None:
    assert _contract(thresholds, actions) == ["weighted"] * 7


def test_settings_not_whole_day_known_leave_the_contract_unknown(thresholds: Thresholds) -> None:
    noon = _action(start=_instant(date(2026, 9, 28), 12))
    assert _contract(thresholds, {1: noon}) == ["unknown"] * 4 + ["last_click"] * 3
    gap = _without(_action(), _instant(date(2026, 9, 29), 9), _instant(date(2026, 9, 29), 12))
    assert _contract(thresholds, {1: gap, 2: _action()}) == [
        *["last_click"] * 4,
        "unknown",
        *["last_click"] * 2,
    ]
    changed = _instant(date(2026, 9, 27), 15)
    counting = (
        *_action(end=changed - CADENCE, counting="MANY_PER_CLICK"),
        *_action(start=changed),
    )
    assert _contract(thresholds, {1: counting}) == [
        "last_click",
        "last_click",
        "unknown",
        *["last_click"] * 4,
    ]


def test_counting_contract_is_memoised_and_returns_a_fresh_dict(thresholds: Thresholds) -> None:
    ctx = _context(
        thresholds,
        _window(thresholds, WEEK),
        accounts={GOOGLE_ACCOUNT.id: GOOGLE_ACCOUNT},
        observations={("conversion_action", 1): _action()},
    )
    first = counting_contract(ctx)
    first.clear()
    assert list(counting_contract(ctx).values()) == ["last_click"] * 7
    assert "metrics.counting_contract" in ctx.cache


def test_an_included_action_observed_weighted_during_a_day_makes_it_weighted(
    thresholds: Thresholds,
) -> None:
    # Data-driven exactly on 2026-09-27 in New York: observed from 06:00 UTC that day through
    # 03:00 UTC the next. The 3-hourly UTC snapshots never bracket that native day with
    # agreeing values, yet the observations inside it establish weighted counting that day
    # ("data-driven ... on any day"). The neighbouring days are only not whole-day known.
    switched, reverted = _instant(date(2026, 9, 27), 6), _instant(date(2026, 9, 28), 6)
    one_day = (
        *_action(end=switched - CADENCE),
        *_action(start=switched, end=reverted - CADENCE, model=DATA_DRIVEN),
        *_action(start=reverted),
    )
    assert _contract(thresholds, {1: one_day}) == [
        "last_click",
        "unknown",
        "weighted",
        "unknown",
        *["last_click"] * 3,
    ]
    # An action excluded from metrics.conversions never weights a day.
    excluded = tuple(
        Observation(
            obs.captured_at,
            status="ENABLED",
            payload={**obs.payload, "include_in_conversions_metric": False},
        )
        for obs in one_day
    )
    assert "weighted" not in _contract(thresholds, {1: excluded, 2: _action()})


# ---------------------------------------------------------------------------
# Agreement with the scenario catalogue (simulation/scenarios.py expectations)
# ---------------------------------------------------------------------------


def _built(name: str) -> tuple[Scenario, AnalysisContext]:
    scenario = SCENARIOS[name]
    return scenario, scenario.build_context()


def _subject(scenario: Scenario, source: str, entity: str, key: str) -> SubjectKey:
    subject: SubjectKey = (
        ("budget", scenario.ids.id(source, "budget", key))
        if entity == "budget"
        else ("campaign", scenario.ids.id(source, "campaign", key))
    )
    return subject


def _dates_of(history: BudgetHistory, *classes: str) -> tuple[str, ...]:
    return _iso(day.date for day in history.days if day.klass in classes)


@pytest.mark.parametrize("name", ["c2.cap_evidence.fires", "c2.cap_evidence.does_not_fire"])
def test_scenario_cap_evidence_counts_and_gate(name: str) -> None:
    scenario, ctx = _built(name)
    history = budget_history(ctx, _subject(scenario, "linkedin", "campaign", "li-cap"))
    values = scenario.expect.values
    assert (history.known_active, history.spending, history.capped, history.idle) == (
        values["known_days"],
        values["spending_days"],
        values["capped_days"],
        values["idle_days"],
    )
    assert cap_constrained(history).passed is scenario.expect.fires
    if not scenario.expect.fires:
        # One capped day followed by six idle days is not high-utilization either.
        assert high_utilization(history).passed is False


def test_scenario_cap_evidence_short_is_withheld_with_observed_and_required() -> None:
    scenario, ctx = _built("c2.cap_evidence_short.case")
    history = budget_history(ctx, _subject(scenario, "linkedin", "campaign", "li-cap"))
    values = scenario.expect.values
    assert (history.known_active, history.unobserved) == (
        values["known_days"],
        values["unobserved_days"],
    )
    assert history.config.min_known_days == values["required"]
    gate = cap_constrained(history)
    assert gate.passed is False
    assert gate.shortfalls[0] == scenario.expect.withheld_reason
    assert gate.counts["known_budget_days"] == (values["known_days"], values["required"])


@pytest.mark.parametrize(
    ("name", "key", "classes"),
    [
        ("budget_days.intraday.case", "changed_intraday_days", ("changed_intraday",)),
        ("budget_days.first_snapshot_noon.case", "unobserved_days", ("unobserved",)),
        ("budget_days.gap_7h.fires", "unobserved_days", ("unobserved",)),
        ("budget_days.gap_7h.does_not_fire", "unobserved_days", ("unobserved",)),
    ],
)
def test_scenario_budget_day_classification(name: str, key: str, classes: tuple[str, ...]) -> None:
    scenario, ctx = _built(name)
    history = budget_history(ctx, _subject(scenario, "linkedin", "campaign", "li-cap"))
    values = scenario.expect.values
    assert _dates_of(history, *classes) == values[key]
    assert history.known_active == values["known_days"]
    # Unknown days never carry an observed value or a daily predicate.
    for day in history.days:
        if day.klass != "known":
            assert (day.status, day.daily_budget, day.u_d, day.capped, day.idle) == (
                None,
                None,
                None,
                False,
                False,
            )
    assert cap_constrained(history).passed is scenario.expect.fires


def test_scenario_days_before_the_first_snapshot_are_unknown() -> None:
    scenario, ctx = _built("budget_days.before_first_snapshot.case")
    history = budget_history(ctx, _subject(scenario, "linkedin", "campaign", "li-cap"))
    values = scenario.expect.values
    assert (history.unobserved, history.known_active) == (
        values["unobserved_days"],
        values["known_days"],
    )
    assert _dates_of(history, "unobserved") == _iso(_dates((date(2026, 9, 4), date(2026, 9, 21))))
    assert cap_constrained(history).passed is scenario.expect.fires


@pytest.mark.parametrize(
    ("name", "source", "entity", "key"),
    [
        ("c2.outcome_scale.case", "linkedin", "campaign", "li-pipeline"),
        ("c2.outcome_hold.case", "linkedin", "campaign", "li-pipeline"),
        ("c2.qlstage_unresolved.case", "linkedin", "campaign", "li-pipeline"),
        ("b1.step.case", "linkedin", "campaign", "li-cap"),
        ("g1.b2_target.does_not_fire", "linkedin", "campaign", "hot"),
        ("b3.allocation.case", "linkedin", "campaign", "receiver"),
        ("shared.poor_member.does_not_fire", "google", "budget", "pool"),
        ("shared.membership_mid_window.case", "google", "budget", "pool"),
        ("shared.member_changed_intraday.case", "google", "budget", "pool"),
        ("shared.mixed_objectives.case", "google", "budget", "pool"),
    ],
)
def test_scenario_scale_and_increase_targets_are_cap_constrained_and_unblocked(
    name: str, source: str, entity: str, key: str
) -> None:
    # C2 Scale/Hold and B1 need the cap-constrained gate and a target G1 leaves unblocked.
    scenario, ctx = _built(name)
    subject = _subject(scenario, source, entity, key)
    gate = cap_constrained(budget_history(ctx, subject))
    assert (gate.passed, gate.shortfalls) == (True, ())
    assert subject not in spend_increase_blocked(ctx)


def test_scenario_b1_step_reads_the_latest_effective_budget() -> None:
    scenario, ctx = _built("b1.step.case")
    history = budget_history(ctx, _subject(scenario, "linkedin", "campaign", "li-cap"))
    assert history.latest_daily_budget == Decimal(str(scenario.expect.values["current"]))


def test_scenario_g1_blocks_the_b2_target() -> None:
    scenario, ctx = _built("g1.b2_target.fires")
    hot = _subject(scenario, "linkedin", "campaign", "hot")
    assessment = assess_b2(ctx, hot)
    assert (assessment.meets, assessment.branch) == (True, "poor_efficiency")
    assert assessment.meets is scenario.expect.values["b2"]
    assert spend_increase_blocked(ctx) == frozenset({hot})
    _twin, twin_ctx = _built("g1.b2_target.does_not_fire")
    assert spend_increase_blocked(twin_ctx) == frozenset()


def test_scenario_four_spending_days_withhold_b2_and_b3() -> None:
    scenario, ctx = _built("b2.spending_days.case")
    hot = _subject(scenario, "linkedin", "campaign", "hot")
    history = budget_history(ctx, hot)
    values = scenario.expect.values
    assert (history.known_active, history.spending) == (
        values["known_days"],
        values["spending_days"],
    )
    assert history.config.min_spending_days == values["required"]
    reason = scenario.expect.withheld_reason
    assert high_utilization(history).shortfalls[0] == reason
    assert assess_b2(ctx, hot).meets is False
    assert values["b3_withheld"] is True
    assert under_utilized(history).shortfalls[0] == reason


@pytest.mark.parametrize("name", ["b2.share.fires", "b2.share.does_not_fire"])
def test_scenario_b2_high_utilization_share(name: str) -> None:
    scenario, ctx = _built(name)
    hot = _subject(scenario, "linkedin", "campaign", "hot")
    history = budget_history(ctx, hot)
    values = scenario.expect.values
    assert (history.high, history.known_active) == (
        values["high_utilization_days"],
        values["known_days"],
    )
    assert high_utilization(history).passed is scenario.expect.fires
    assert assess_b2(ctx, hot).meets is scenario.expect.fires


@pytest.mark.parametrize("name", ["b3.idle_known.fires", "b3.idle_known.does_not_fire"])
def test_scenario_b3_needs_spending_days_beside_known_idle_days(name: str) -> None:
    scenario, ctx = _built(name)
    history = budget_history(ctx, _subject(scenario, "linkedin", "campaign", "slack"))
    values = scenario.expect.values
    assert (history.known_active, history.spending, history.idle) == (
        values["known_days"],
        values["spending_days"],
        values["idle_days"],
    )
    if "under_utilized_days" in values:
        assert history.under == values["under_utilized_days"]
    assert under_utilized(history).passed is scenario.expect.fires


def test_scenario_unknown_days_are_never_counted_idle() -> None:
    scenario, ctx = _built("budget.unknown_not_idle.case")
    history = budget_history(ctx, _subject(scenario, "linkedin", "campaign", "slack"))
    values = scenario.expect.values
    assert (history.known_active, history.idle, history.unobserved) == (
        values["known_days"],
        values["idle_days"],
        values["unobserved_days"],
    )
    gate = under_utilized(history)
    assert (gate.passed, gate.shortfalls[0]) == (False, scenario.expect.withheld_reason)


@pytest.mark.parametrize("name", ["b3.allocation.case", "b3.no_receiver.case"])
def test_scenario_b3_donors_are_under_utilized(name: str) -> None:
    scenario, ctx = _built(name)
    for donor in ("donor-a", "donor-b"):
        history = budget_history(ctx, _subject(scenario, "linkedin", "campaign", donor))
        assert under_utilized(history).passed is True


def test_scenario_lifetime_budgets_are_excluded_from_cap_rules() -> None:
    scenario, ctx = _built("budget.lifetime.case")
    assert budget_subjects(ctx) == []
    history = campaign_budget_history(ctx, scenario.ids.id("linkedin", "campaign", "li-cap"))
    assert (history.utilization_applicable, history.window_utilization) == (False, None)
    assert history.known_active == 0
    assert cap_constrained(history).passed is False


def test_scenario_a_poor_member_blocks_the_shared_budget() -> None:
    scenario, ctx = _built("shared.poor_member.fires")
    pool = _subject(scenario, "google", "budget", str(scenario.expect.values["blocked_target"]))
    poor = scenario.ids.id("google", "campaign", str(scenario.expect.values["poor_member"]))
    good = scenario.ids.id("google", "campaign", "m1")
    assert member_poor_efficiency(ctx, pool[1], poor) is True
    assert member_poor_efficiency(ctx, pool[1], good) is False
    assessment = assess_b2(ctx, pool)
    assert (assessment.gate.passed, assessment.meets) == (True, False)
    assert spend_increase_blocked(ctx) == frozenset({pool, ("campaign", poor), ("campaign", good)})
    _twin, twin_ctx = _built("shared.poor_member.does_not_fire")
    assert spend_increase_blocked(twin_ctx) == frozenset()


def test_scenario_an_aggregate_b2_blocks_the_shared_budget() -> None:
    scenario, ctx = _built("shared.aggregate_b2.case")
    pool = _subject(scenario, "google", "budget", "pool")
    assert scenario.expect.values["subject_type"] == pool[0]
    assessment = assess_b2(ctx, pool)
    assert (assessment.meets, assessment.branch) == (True, "poor_efficiency")
    assert pool in spend_increase_blocked(ctx)


def test_scenario_a_mid_window_joiner_counts_only_on_its_member_days() -> None:
    scenario, ctx = _built("shared.membership_mid_window.case")
    values = scenario.expect.values
    pool = _subject(scenario, "google", "budget", "pool")
    joiner = scenario.ids.id("google", "campaign", str(values["member"]))
    history = budget_history(ctx, pool)
    assert len(history.members[joiner]) == values["member_days"]
    assert _dates_of(history, "changed_intraday") == (values["changed_day"],)
    assert history.members[joiner][0] == date.fromisoformat(str(values["changed_day"])) + timedelta(
        days=1
    )


def test_scenario_a_member_changing_budgets_makes_the_day_unknown() -> None:
    scenario, ctx = _built("shared.member_changed_intraday.case")
    history = budget_history(ctx, _subject(scenario, "google", "budget", "pool"))
    assert (
        _dates_of(history, "changed_intraday", "unobserved")
        == (scenario.expect.values["unknown_days"])
    )


def test_scenario_google_channels_share_one_objective_group() -> None:
    scenario, ctx = _built("shared.mixed_objectives.case")
    pool = _subject(scenario, "google", "budget", "pool")
    groups = scenario.expect.values["objective_groups"]
    assert isinstance(groups, tuple)
    assert subject_objective_groups(ctx, pool) == frozenset(groups)
    assert subject_totals(ctx, pool) is not None
    assert subject_baseline(ctx, pool) is not None


def test_scenario_b4_counting_contract_agrees_with_each_withheld_reason() -> None:
    scenario, ctx = _built("b4.google_last_click.fires")
    contract = counting_contract(ctx)
    assert set(contract.values()) == {"last_click"}
    assert scenario.expect.values["contract"] == (
        f"all included actions last-click on all {len(contract)} days"
    )
    for name in ("b4.fractional.case", "b4.no_exposure.case"):
        # The contract holds; the integrality and exposure checks are B4's own.
        assert set(counting_contract(_built(name)[1]).values()) == {"last_click"}
    weighted = counting_contract(_built("b4.google_last_click.does_not_fire")[1])
    assert "weighted" in weighted.values()
    unknown = counting_contract(_built("b4.contract_unknown.case")[1])
    assert "unknown" in unknown.values()
    assert "weighted" not in unknown.values()
