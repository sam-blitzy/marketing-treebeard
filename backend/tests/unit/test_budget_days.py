"""Unit tests of budget-day classification in ``ads_engine.analysis.metrics`` (AAP 0.5.4.1).

Snapshots are observations, never effective times. A native day is **whole-day known** only
when an observation exists at or before the day start and at or after the day end, every
observation of that span agrees, and no gap between consecutive observations exceeds
``budget.max_observation_gap_hours``. Disagreeing observations make the day **changed
intraday**; a missing bracket or a longer gap makes it **unobserved**. These tests pin the
classifier itself and the day counts and gate shapes built on it, at unit level, including the
AAP 0.11.2 "Intraday budget uncertainty" guarantees:

* a budget raised at noon makes that day ``changed_intraday`` and excludes it from utilization
  and cap counts;
* a first snapshot at noon makes that day unobserved, and days before the first snapshot are
  unknown;
* a 7-hour observation gap makes the spanned day unobserved;
* a day bracketed by unchanged observations at most 6 h apart is known;
* unknown days are never counted as idle.

The rule outcomes built on these histories (C2, B1, B2, B3, G1) are proven by the acceptance
suite; here only the pure half of ``metrics.py`` runs, on contexts built in memory from the
real ``analysis.context`` types. No database and no network.

Native days (AAP 0.5.4.1): LinkedIn days are UTC days; Google days are local days in the
account's time zone, here ``America/New_York``, so a Google day is ``[local 00:00, next local
00:00)``.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ads_engine.analysis.context import (
    AccountInfo,
    AnalysisContext,
    BudgetInfo,
    BudgetPeriod,
    CampaignInfo,
    Completeness,
    DatasetRef,
    DayFact,
    Freshness,
    Observation,
    objective_group,
)
from ads_engine.analysis.metrics import (
    BudgetDay,
    BudgetHistory,
    DayClass,
    budget_amount_value,
    budget_subjects,
    campaign_budget_history,
    campaign_state_value,
    cap_constrained,
    classify_days,
    high_utilization,
    membership_value,
    shared_budget_history,
    status_value,
    under_utilized,
)
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.analysis.window import AnalysisWindow, SourceDayInfo

ThresholdsFactory = Callable[[Mapping[str, Any]], Thresholds]
"""The shape of the root conftest's ``make_thresholds`` fixture (dotted-key overrides)."""

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
"""The run instant: the default window end is 2026-10-01 in New York and in UTC."""

NY = "America/New_York"
"""The Google account's time zone: Google native days are New York local days."""

UTC_DAYS = "UTC"
"""LinkedIn's native day boundary (AAP 0.3.3, "LinkedIn days are UTC")."""

CADENCE = timedelta(hours=3)
"""The entity-sync snapshot cadence (AAP 0.3.2: every 3 h)."""

D = date(2026, 9, 30)
"""The day most single-day cases classify (EDT in New York, so a 24-hour local day)."""

ONE_DAY = timedelta(days=1)
ONE_SECOND = timedelta(seconds=1)
LIFETIME = Decimal("5000")
"""The total budget of a lifetime (``totalBudget``-only) LinkedIn campaign."""

GOOGLE_DATASET = DatasetRef(1, "google", "simulated")
LINKEDIN_DATASET = DatasetRef(2, "linkedin", "simulated")
GOOGLE_ACCOUNT = AccountInfo(1, "google", "1234567890", "Google Ads", "USD", NY)
LINKEDIN_ACCOUNT = AccountInfo(2, "linkedin", "500000001", "LinkedIn Ads", "USD")

LI_CAMPAIGN = 7
"""The LinkedIn campaign of the own-budget cases."""
G_CAMPAIGN = 8
"""The Google campaign of the own-budget cases."""
OWN_BUDGET = 40
"""The Google campaign's own (not explicitly shared) ``campaign_budget``."""
SHARED = 50
"""The Google explicitly shared budget ``S`` of the shared-budget cases."""
OTHER_BUDGET = 60
"""Campaign B's own budget before it moved to ``S``."""
CAMPAIGN_A = 1
CAMPAIGN_B = 2


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def at(day: date, hour: int = 0, tz: str = UTC_DAYS) -> datetime:
    """The instant of ``hour``:00 wall-clock time on ``day`` in ``tz``, as a UTC datetime."""
    return datetime.combine(day, time(hour), tzinfo=ZoneInfo(tz)).astimezone(UTC)


def obs(
    captured_at: datetime,
    budget: str | None = "100",
    status: str = "active",
    budget_id: int | None = None,
    *,
    shared: bool = False,
    period: BudgetPeriod = "DAILY",
) -> Observation:
    """A campaign snapshot as the entity sync writes it.

    Without ``budget_id`` it is a LinkedIn campaign: ``budget`` is its ``dailyBudget``, and
    ``None`` means a lifetime campaign with only a ``totalBudget``. With ``budget_id`` it is a
    Google campaign drawing from that ``campaign_budget``: the amount is stored at native
    precision in micros, with the budget's period and ``explicitly_shared`` in the payload.

    Raises:
        ValueError: For a Google snapshot without an amount; Google always reports one.
    """
    if budget_id is None:
        return Observation(
            captured_at=captured_at,
            status=status,
            budget_daily=None if budget is None else Decimal(budget),
            budget_total=LIFETIME if budget is None else None,
        )
    if budget is None:
        raise ValueError("a Google campaign snapshot always shows its budget amount")
    amount = Decimal(budget)
    return Observation(
        captured_at=captured_at,
        status=status,
        budget_daily=amount if period == "DAILY" else None,
        budget_total=amount if period == "CUSTOM_PERIOD" else None,
        budget_amount_micros=int(amount * 1_000_000),
        budget_id=budget_id,
        payload={"explicitly_shared": shared, "budget_period": period},
    )


def budget_obs(
    captured_at: datetime, amount: str = "200", *, period: BudgetPeriod = "DAILY"
) -> Observation:
    """A Google explicitly shared ``campaign_budget`` snapshot (no status: budgets have none)."""
    value = Decimal(amount)
    return Observation(
        captured_at=captured_at,
        budget_daily=value if period == "DAILY" else None,
        budget_total=value if period == "CUSTOM_PERIOD" else None,
        budget_amount_micros=int(value * 1_000_000),
        payload={"period": period, "explicitly_shared": True},
    )


def every(
    start: datetime,
    end: datetime,
    make: Callable[[datetime], Observation],
    *,
    step: timedelta = CADENCE,
) -> list[Observation]:
    """One observation every ``step`` from ``start`` through ``end``, both included."""
    observations: list[Observation] = []
    instant = start
    while instant <= end:
        observations.append(make(instant))
        instant += step
    return observations


def span(start: date, count: int) -> list[date]:
    """``count`` consecutive native dates from ``start``."""
    return [start + timedelta(days=offset) for offset in range(count)]


def window(config: Thresholds, start: date, end: date, *, google_tz: str = NY) -> AnalysisWindow:
    """A custom window ``[start, end]``: Google days in ``google_tz``, LinkedIn days in UTC."""
    return AnalysisWindow.custom(
        start, end, config=config, sources=SourceDayInfo(google_tz), now=NOW
    )


def li_campaign(cid: int = LI_CAMPAIGN, *, daily: str | None = "100") -> CampaignInfo:
    """A LinkedIn lead-gen campaign; ``daily=None`` makes it a lifetime-budget campaign."""
    return CampaignInfo(
        id=cid,
        source="linkedin",
        account_id=LINKEDIN_ACCOUNT.id,
        external_id=str(700_000 + cid),
        name=f"LinkedIn {cid}",
        status="active",
        raw_status="ACTIVE",
        objective_or_channel="LEAD_GENERATION",
        objective_group=objective_group("linkedin", "LEAD_GENERATION"),
        campaign_group_id=None,
        budget_id=None,
        daily_budget=None if daily is None else Decimal(daily),
        total_budget=LIFETIME if daily is None else None,
        budget_period=None,
        currency="USD",
    )


def google_campaign(
    cid: int = G_CAMPAIGN, budget_id: int = OWN_BUDGET, *, period: BudgetPeriod = "DAILY"
) -> CampaignInfo:
    """A Google Search campaign drawing from ``budget_id`` with budget period ``period``."""
    return CampaignInfo(
        id=cid,
        source="google",
        account_id=GOOGLE_ACCOUNT.id,
        external_id=str(900_000 + cid),
        name=f"Google {cid}",
        status="active",
        raw_status="ENABLED",
        objective_or_channel="SEARCH",
        objective_group=objective_group("google", "SEARCH"),
        campaign_group_id=None,
        budget_id=budget_id,
        daily_budget=Decimal("100") if period == "DAILY" else None,
        total_budget=Decimal("100") if period == "CUSTOM_PERIOD" else None,
        budget_period=period,
        currency="USD",
    )


def budget_info(
    bid: int, amount: str, *, shared: bool, period: BudgetPeriod = "DAILY"
) -> BudgetInfo:
    """A Google ``campaign_budget`` row."""
    value = Decimal(amount)
    return BudgetInfo(
        id=bid,
        external_id=str(800_000 + bid),
        name="",
        amount=value,
        amount_micros=int(value * 1_000_000),
        period=period,
        explicitly_shared=shared,
        currency="USD",
    )


def li_fact(day: date, cost: str, *, lost_is: str | None = None) -> DayFact:
    """A LinkedIn campaign day; ``lost_is`` proves LinkedIn never uses the Google clause."""
    return DayFact(
        date=day,
        cost=Decimal(cost),
        impressions=1_000,
        clicks=10,
        leads=1,
        budget_lost_is=None if lost_is is None else Decimal(lost_is),
    )


def google_fact(day: date, cost: str, *, lost_is: str | None = None) -> DayFact:
    """A Google campaign day with its ``search_budget_lost_impression_share``."""
    return DayFact(
        date=day,
        cost=Decimal(cost),
        impressions=1_000,
        clicks=50,
        conversions=Decimal(1),
        budget_lost_is=None if lost_is is None else Decimal(lost_is),
    )


def facts(
    make: Callable[..., DayFact], days: Sequence[date], costs: Sequence[str]
) -> tuple[DayFact, ...]:
    """One fact row per day, pairing ``days`` with ``costs`` in order (equal lengths)."""
    return tuple(make(day, cost) for day, cost in zip(days, costs, strict=True))


def metrics_coverage(
    ref: DatasetRef, name: str, start: date, end: date, completeness: Completeness
) -> dict[tuple[int, str], Freshness]:
    """A ``dataset_freshness`` row of a daily metrics dataset covering ``[start, end]``."""
    return {
        (ref.dataset_id, name): Freshness(ref.dataset_id, name, NOW, NOW, start, end, completeness)
    }


def context(
    config: Thresholds,
    win: AnalysisWindow,
    *,
    campaigns: Sequence[CampaignInfo],
    observations: Mapping[tuple[str, int], Sequence[Observation]],
    days: Mapping[int, Sequence[DayFact]] | None = None,
    budgets: Sequence[BudgetInfo] = (),
    freshness: Mapping[tuple[int, str], Freshness] | None = None,
) -> AnalysisContext:
    """An in-memory analysis context holding only what budget histories read."""
    return AnalysisContext(
        window=win,
        thresholds=config,
        now=NOW,
        datasets={"google": GOOGLE_DATASET, "linkedin": LINKEDIN_DATASET},
        freshness=dict(freshness or {}),
        accounts={account.id: account for account in (GOOGLE_ACCOUNT, LINKEDIN_ACCOUNT)},
        campaigns={campaign.id: campaign for campaign in campaigns},
        budgets={budget.id: budget for budget in budgets},
        campaign_days={cid: tuple(rows) for cid, rows in (days or {}).items()},
        observations={key: tuple(rows) for key, rows in observations.items()},
    )


def li_history(
    config: Thresholds,
    start: date,
    end: date,
    observations: Sequence[Observation],
    costs: Sequence[str],
    *,
    campaign: CampaignInfo | None = None,
) -> BudgetHistory:
    """The own-budget history of the LinkedIn campaign over ``[start, end]`` (UTC days)."""
    win = window(config, start, end)
    ctx = context(
        config,
        win,
        campaigns=[campaign or li_campaign()],
        observations={("campaign", LI_CAMPAIGN): observations},
        days={LI_CAMPAIGN: facts(li_fact, win.dates(), costs)},
    )
    return campaign_budget_history(ctx, LI_CAMPAIGN)


def klasses(history: BudgetHistory) -> list[DayClass]:
    """Each window day's class, in date order."""
    return [day.klass for day in history.days]


NO_PREDICATE: Mapping[str, bool] = {
    "capped": False,
    "high": False,
    "under": False,
    "spending": False,
    "idle": False,
}
"""The daily predicates of a day that is not a known-budget active day: none holds."""


def predicates(day: BudgetDay) -> dict[str, bool]:
    """A day's daily predicates and spending/idle flags, keyed by name."""
    return {
        "capped": day.capped,
        "high": day.high,
        "under": day.under,
        "spending": day.spending,
        "idle": day.idle,
    }


def li_known_day(
    daily: str | None = "100", status: str = "active"
) -> Callable[[datetime], Observation]:
    """A LinkedIn snapshot maker for :func:`every` showing one fixed state (``None``: lifetime)."""
    return lambda instant: obs(instant, daily, status)


def gap_hours(config: Thresholds) -> int:
    """``budget.max_observation_gap_hours`` of ``config``."""
    return config.int("budget.max_observation_gap_hours")


# ---------------------------------------------------------------------------
# Phase 1: whole-day known (AAP 0.5.4.1; AAP 0.11.2 "a day bracketed by unchanged
# observations at most 6 h apart is known")
# ---------------------------------------------------------------------------


def test_three_hourly_unchanged_observations_make_a_utc_day_whole_day_known(
    thresholds: Thresholds,
) -> None:
    """AAP 0.11.2: a LinkedIn UTC day bracketed by unchanged observations 3 h apart is known.

    Snapshots every 3 h from the previous day 21:00 through the next day 03:00, all showing a
    daily budget of 100 and status active: the day is whole-day known with that value, and
    the history counts it as a known-budget active day.
    """
    series = every(at(D - ONE_DAY, 21), at(D + ONE_DAY, 3), li_known_day())

    classified = classify_days(
        series, [D], UTC_DAYS, value=campaign_state_value, max_gap_hours=gap_hours(thresholds)
    )
    assert classified == {D: ("known", ("active", Decimal("100")))}

    history = li_history(thresholds, D, D, series, ["40"])
    (day,) = history.days
    assert day == BudgetDay(
        date=D,
        klass="known",
        status="active",
        daily_budget=Decimal("100"),
        spend=Decimal("40"),
        u_d=Decimal("0.4"),
        capped=False,
        high=False,
        under=True,
        spending=True,
        idle=False,
    )
    assert day.known_active
    assert (history.known_active, history.changed_intraday, history.unobserved) == (1, 0, 0)
    assert history.utilization_applicable
    assert history.subject == ("campaign", LI_CAMPAIGN)


def test_google_native_day_is_the_account_time_zone_day(thresholds: Thresholds) -> None:
    """AAP 0.5.4.1 native days: a Google day is ``[NY 00:00, next NY 00:00)``, never a UTC day.

    The same snapshots that bracket the New York day (previous local 21:00 through next local
    03:00) do not bracket the UTC day of the same date: the first one falls after UTC
    midnight. A Google own-budget history therefore classifies the day in New York time.
    """
    assert AnalysisWindow.day_bounds(D, NY) == (
        datetime(2026, 9, 30, 4, tzinfo=UTC),
        datetime(2026, 10, 1, 4, tzinfo=UTC),
    )
    series = every(
        at(D - ONE_DAY, 21, NY),
        at(D + ONE_DAY, 3, NY),
        lambda instant: obs(instant, "100", budget_id=OWN_BUDGET),
    )
    max_gap = gap_hours(thresholds)

    in_new_york = classify_days(series, [D], NY, value=campaign_state_value, max_gap_hours=max_gap)
    in_utc = classify_days(series, [D], UTC_DAYS, value=campaign_state_value, max_gap_hours=max_gap)
    assert in_new_york == {D: ("known", ("active", Decimal("100")))}
    assert in_utc == {D: ("unobserved", None)}

    win = window(thresholds, D, D)
    ctx = context(
        thresholds,
        win,
        campaigns=[google_campaign()],
        budgets=[budget_info(OWN_BUDGET, "100", shared=False)],
        observations={("campaign", G_CAMPAIGN): series},
        days={G_CAMPAIGN: (google_fact(D, "97"),)},
    )
    history = campaign_budget_history(ctx, G_CAMPAIGN)
    (day,) = history.days
    assert (day.klass, day.daily_budget, day.u_d, day.capped) == (
        "known",
        Decimal("100"),
        Decimal("0.97"),
        True,
    )
    assert budget_subjects(ctx) == [("campaign", G_CAMPAIGN)]


def test_a_dst_day_is_classified_over_its_true_25_hours(thresholds: Thresholds) -> None:
    """AAP 0.5.4.1: a New York fall-back day ends at 05:00 UTC, 25 hours after it starts.

    An observation at 04:00 UTC the next morning would close a 24-hour day but falls inside
    the 25-hour one, so it does not bracket the day; one at 05:00 UTC does.
    """
    fall_back = date(2026, 11, 1)
    lower, upper = AnalysisWindow.day_bounds(fall_back, NY)
    assert upper - lower == timedelta(hours=25)
    series = every(
        datetime(2026, 11, 1, 1, tzinfo=UTC),
        datetime(2026, 11, 2, 4, tzinfo=UTC),
        li_known_day(),
    )
    max_gap = gap_hours(thresholds)
    short = classify_days(
        series, [fall_back], NY, value=campaign_state_value, max_gap_hours=max_gap
    )
    assert short == {fall_back: ("unobserved", None)}

    closed = [*series, obs(datetime(2026, 11, 2, 5, tzinfo=UTC))]
    full = classify_days(closed, [fall_back], NY, value=campaign_state_value, max_gap_hours=max_gap)
    assert full == {fall_back: ("known", ("active", Decimal("100")))}


@pytest.mark.parametrize(
    ("first_shift", "last_shift", "expected"),
    [
        pytest.param(timedelta(0), timedelta(0), "known", id="exactly-at-start-and-end"),
        pytest.param(ONE_SECOND, timedelta(0), "unobserved", id="first-just-after-start"),
        pytest.param(timedelta(0), -ONE_SECOND, "unobserved", id="last-just-before-end"),
    ],
)
def test_observations_exactly_at_day_start_and_end_bracket_the_day(
    thresholds: Thresholds, first_shift: timedelta, last_shift: timedelta, expected: DayClass
) -> None:
    """AAP 0.5.4.1: "at or before the day start" and "at or after the day end" include equality.

    Snapshots exactly at 00:00, 06:00, 12:00, 18:00 and next 00:00 bracket the day with gaps of
    exactly the 6-hour limit, which is not exceeded. Moving the first one a second later, or
    the last one a second earlier, leaves the day without a bracket.
    """
    start, end = at(D) + first_shift, at(D + ONE_DAY) + last_shift
    series = [obs(instant) for instant in (start, at(D, 6), at(D, 12), at(D, 18), end)]

    classified = classify_days(
        series, [D], UTC_DAYS, value=campaign_state_value, max_gap_hours=gap_hours(thresholds)
    )
    value = ("active", Decimal("100")) if expected == "known" else None
    assert classified == {D: (expected, value)}


def _gapped_series(gap: timedelta) -> list[Observation]:
    """3-hourly snapshots around ``D`` with one ``gap`` after ``D`` 03:00 inside the bracket."""
    before = every(at(D - ONE_DAY, 21), at(D, 3), li_known_day())
    after = every(at(D, 3) + gap, at(D + ONE_DAY, 6), li_known_day())
    return [*before, *after]


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        pytest.param(timedelta(hours=6), "known", id="6h-gap-known"),
        pytest.param(timedelta(hours=7), "unobserved", id="7h-gap-unobserved"),
    ],
)
def test_observation_gap_inside_the_bracket_decides_known_or_unobserved(
    thresholds: Thresholds, gap: timedelta, expected: DayClass
) -> None:
    """AAP 0.11.2: "a 7-hour observation gap makes the spanned day unobserved"; 6 h stays known.

    The budget is unchanged throughout, so only the gap between two consecutive snapshots of
    the bracketing span decides: at most ``budget.max_observation_gap_hours`` (6) apart is
    known, a longer gap leaves the day unobserved and excluded from utilization.
    """
    series = _gapped_series(gap)
    classified = classify_days(
        series, [D], UTC_DAYS, value=campaign_state_value, max_gap_hours=gap_hours(thresholds)
    )
    assert classified[D][0] == expected

    history = li_history(thresholds, D, D, series, ["100"])
    (day,) = history.days
    assert day.klass == expected
    if expected == "known":
        assert (history.known_active, history.unobserved) == (1, 0)
        assert day.u_d == Decimal(1)
    else:
        assert (history.known_active, history.unobserved) == (0, 1)
        assert (day.u_d, day.daily_budget, day.capped) == (None, None, False)
        assert history.window_utilization is None


def test_a_gap_outside_the_bracketing_span_does_not_unsettle_the_day(
    thresholds: Thresholds,
) -> None:
    """AAP 0.5.4.1: only gaps between the bracketing observations count.

    A 9-hour gap the previous afternoon (12:00 to 21:00) ends before the last observation at
    or before the day start (00:00), so the day is still whole-day known.
    """
    series = [
        obs(at(D - ONE_DAY, 12)),
        *every(at(D - ONE_DAY, 21), at(D + ONE_DAY, 3), li_known_day()),
    ]
    assert series[1].captured_at - series[0].captured_at == timedelta(hours=9)
    classified = classify_days(
        series, [D], UTC_DAYS, value=campaign_state_value, max_gap_hours=gap_hours(thresholds)
    )
    assert classified == {D: ("known", ("active", Decimal("100")))}


def test_the_gap_limit_is_read_from_configuration(make_thresholds: ThresholdsFactory) -> None:
    """AAP 0.5.4.1: ``budget.max_observation_gap_hours`` is configuration, never a literal.

    With the limit raised to 8 hours, the 7-hour gap that leaves a day unobserved at the
    default 6 hours is tolerated and the day is known; a 9-hour gap still is not. The history
    records the limit it was classified with and states it in its assumption.
    """
    loaded = make_thresholds({"budget.max_observation_gap_hours": 8})
    assert gap_hours(loaded) == 8

    seven = _gapped_series(timedelta(hours=7))
    nine = _gapped_series(timedelta(hours=9))
    for series, expected in ((seven, "known"), (nine, "unobserved")):
        classified = classify_days(
            series, [D], UTC_DAYS, value=campaign_state_value, max_gap_hours=gap_hours(loaded)
        )
        assert classified[D][0] == expected

    history = li_history(loaded, D, D, seven, ["50"])
    assert klasses(history) == ["known"]
    assert history.known_active == 1
    assert history.config.max_gap_hours == 8
    assert history.assumption.endswith("at most 8 h apart")


def test_history_states_the_observation_assumption_it_relied_on(thresholds: Thresholds) -> None:
    """AAP 0.5.4.1 evidence: "unchanged across N observations, at most 6 h apart".

    N counts the distinct observations the known days were read from: two consecutive known
    UTC days bracketed by 3-hourly snapshots use the 17 from the first day's 00:00 through
    the second day's end, the shared midnight once. A changed-intraday day contributes none.
    """
    first = D - ONE_DAY
    series = every(at(first - ONE_DAY, 21), at(D + ONE_DAY, 3), li_known_day())
    history = li_history(thresholds, first, D, series, ["10", "20"])
    assert klasses(history) == ["known", "known"]
    assert history.observation_count == 17
    assert history.assumption == "unchanged across 17 observations, at most 6 h apart"
    assert history.known_label == "known-budget days"

    raised = [
        *every(at(first - ONE_DAY, 21), at(D, 9), li_known_day("100")),
        *every(at(D, 12), at(D + ONE_DAY, 3), li_known_day("150")),
    ]
    changed = li_history(thresholds, first, D, raised, ["10", "20"])
    assert klasses(changed) == ["known", "changed_intraday"]
    assert changed.observation_count == 9
    assert changed.assumption == "unchanged across 9 observations, at most 6 h apart"


@pytest.mark.parametrize("bad", [0, -1, True, 6.5, "6"])
def test_classify_days_rejects_a_gap_limit_that_is_not_a_positive_integer(bad: Any) -> None:
    """``budget.max_observation_gap_hours`` must be a positive ``int`` (``bool`` rejected)."""
    series = every(at(D - ONE_DAY, 21), at(D + ONE_DAY, 3), li_known_day())
    with pytest.raises(ValueError, match="max_gap_hours must be a positive integer"):
        classify_days(series, [D], UTC_DAYS, value=campaign_state_value, max_gap_hours=bad)


# ---------------------------------------------------------------------------
# Phase 2: changed intraday and unobserved (AAP 0.5.4.1; AAP 0.11.2 "Intraday budget
# uncertainty")
# ---------------------------------------------------------------------------


def test_a_budget_raised_at_noon_makes_the_day_changed_intraday(thresholds: Thresholds) -> None:
    """AAP 0.11.2: "a budget raised at noon makes that day changed_intraday and excludes it
    from utilization and cap counts".

    The snapshots disagree inside the day (100 through 09:00, 150 from 12:00), so the
    effective time of the raise is unknown: the day carries no budget, no ``u_d`` and no
    predicate, although its spend of 200 would read as capped against either amount. The
    days either side stay known with their own amounts.
    """
    first, last = D - ONE_DAY, D + ONE_DAY
    series = [
        *every(at(first - ONE_DAY, 21), at(D, 9), li_known_day("100")),
        *every(at(D, 12), at(last + ONE_DAY, 3), li_known_day("150")),
    ]
    classified = classify_days(
        series,
        [first, D, last],
        UTC_DAYS,
        value=campaign_state_value,
        max_gap_hours=gap_hours(thresholds),
    )
    assert classified == {
        first: ("known", ("active", Decimal("100"))),
        D: ("changed_intraday", None),
        last: ("known", ("active", Decimal("150"))),
    }

    history = li_history(thresholds, first, last, series, ["50", "200", "150"])
    assert klasses(history) == ["known", "changed_intraday", "known"]
    raised = history.days[1]
    assert raised.spend == Decimal("200")
    assert (raised.status, raised.daily_budget, raised.u_d) == (None, None, None)
    assert predicates(raised) == NO_PREDICATE
    assert not raised.known_active
    assert history.changed_intraday == 1
    assert history.known_active == 2
    assert history.capped == 1
    assert history.window_utilization == Decimal("0.8")


def test_a_status_change_during_the_day_is_not_whole_day_known(thresholds: Thresholds) -> None:
    """AAP 0.5.4.1: a day's status, like its budget, must be unchanged across its bracket.

    A pause between the 09:00 and 12:00 snapshots makes the day changed intraday whether
    status alone or the campaign state is compared; the next day is known paused, which is
    whole-day known but not a known-budget active day.
    """
    first, last = D - ONE_DAY, D + ONE_DAY
    series = [
        *every(at(first - ONE_DAY, 21), at(D, 9), li_known_day("100", "active")),
        *every(at(D, 12), at(last + ONE_DAY, 3), li_known_day("100", "paused")),
    ]
    max_gap = gap_hours(thresholds)
    by_status = classify_days(series, [D], UTC_DAYS, value=status_value, max_gap_hours=max_gap)
    assert by_status == {D: ("changed_intraday", None)}

    history = li_history(thresholds, first, last, series, ["60", "30", "0"])
    assert klasses(history) == ["known", "changed_intraday", "known"]
    paused = history.days[2]
    assert (paused.status, paused.daily_budget, paused.u_d) == ("paused", Decimal("100"), None)
    assert (history.known_active, history.changed_intraday, history.idle) == (1, 1, 0)


def test_days_up_to_a_first_snapshot_at_noon_are_unobserved(thresholds: Thresholds) -> None:
    """AAP 0.11.2: "a first snapshot at noon makes that day unobserved; days before the first
    snapshot are unknown".

    Backfilled spend before the first snapshot is never measured against today's budget: the
    two earlier days and the day of the first snapshot keep their spend for display but have
    no budget and no ``u_d``, and only the two later, bracketed days count.
    """
    start = D - timedelta(days=3)
    first_snapshot = at(D - ONE_DAY, 12)
    series = every(first_snapshot, at(D + timedelta(days=2), 3), li_known_day())
    history = li_history(thresholds, start, D + ONE_DAY, series, ["100"] * 5)

    assert klasses(history) == ["unobserved", "unobserved", "unobserved", "known", "known"]
    for day in history.days[:3]:
        assert (day.spend, day.daily_budget, day.u_d) == (Decimal("100"), None, None)
        assert predicates(day) == NO_PREDICATE
    assert (history.unobserved, history.known_active, history.capped) == (3, 2, 2)
    assert history.window_utilization == Decimal(1)


def test_a_day_without_an_observation_at_or_after_its_end_is_unobserved(
    thresholds: Thresholds,
) -> None:
    """AAP 0.5.4.1: the bracket needs an observation at or after the day end.

    The last snapshot ever is at 21:00 on the window's last day, so that day is unobserved
    while the day before, closed by that day's own snapshots, is known.
    """
    first = D - ONE_DAY
    series = every(at(first - ONE_DAY, 21), at(D, 21), li_known_day())
    history = li_history(thresholds, first, D, series, ["80", "80"])
    assert klasses(history) == ["known", "unobserved"]
    assert (history.known_active, history.unobserved) == (1, 1)
    assert history.days[1].u_d is None


# ---------------------------------------------------------------------------
# Phase 3: active, spending, idle and unknown days; utilization (AAP 0.5.4.1)
# ---------------------------------------------------------------------------


def test_a_whole_day_known_paused_day_is_not_known_budget_active(thresholds: Thresholds) -> None:
    """AAP 0.5.4.1: a known-budget active day is whole-day known *with status active*.

    A day known paused keeps its status and budget for evidence but has no ``u_d``, so it is
    neither idle nor spending and no gate counts it, even with zero spend.
    """
    series = every(at(D - ONE_DAY, 21), at(D + ONE_DAY, 3), li_known_day("100", "paused"))
    history = li_history(thresholds, D, D, series, ["0"])
    (day,) = history.days
    assert (day.klass, day.status, day.daily_budget, day.u_d) == (
        "known",
        "paused",
        Decimal("100"),
        None,
    )
    assert not day.known_active
    assert predicates(day) == NO_PREDICATE
    assert (history.known_active, history.idle, history.spending) == (0, 0, 0)
    assert history.window_utilization is None


def test_a_known_active_day_is_idle_at_zero_spend_and_spending_above_it(
    thresholds: Thresholds,
) -> None:
    """AAP 0.5.4.1: a known idle day is a genuine observation with ``u_d`` = 0.

    The idle day counts as under-utilized (``u_d ≤ budget.under_utilization``) but not as
    spending; the day with spend is a spending day.
    """
    first = D - ONE_DAY
    series = every(at(first - ONE_DAY, 21), at(D + ONE_DAY, 3), li_known_day())
    history = li_history(thresholds, first, D, series, ["0", "30"])
    idle, spending = history.days
    assert (idle.u_d, idle.idle, idle.spending, idle.under) == (Decimal(0), True, False, True)
    assert (spending.u_d, spending.idle, spending.spending) == (Decimal("0.3"), False, True)
    assert (history.known_active, history.idle, history.spending, history.under) == (2, 1, 1, 2)


@pytest.mark.parametrize(
    ("completeness", "expected"),
    [
        pytest.param("complete", "known", id="complete-coverage-means-zero-spend"),
        pytest.param("incomplete", "unobserved", id="unverified-coverage-means-unknown-spend"),
    ],
)
def test_a_missing_fact_row_is_idle_only_under_verified_complete_coverage(
    thresholds: Thresholds, completeness: Completeness, expected: DayClass
) -> None:
    """AAP 0.5.4.1: unknown days are never assigned an observed value, spend included.

    A known active day without a fact row reads as zero spend (idle) only when
    ``linkedin.analytics_daily`` is ``complete`` over a range holding the date; otherwise its
    spend is unknown and the day is unobserved, never idle.
    """
    series = every(at(D - ONE_DAY, 21), at(D + ONE_DAY, 3), li_known_day())
    win = window(thresholds, D, D)
    ctx = context(
        thresholds,
        win,
        campaigns=[li_campaign()],
        observations={("campaign", LI_CAMPAIGN): series},
        freshness=metrics_coverage(
            LINKEDIN_DATASET, "linkedin.analytics_daily", D - ONE_DAY, D, completeness
        ),
    )
    history = campaign_budget_history(ctx, LI_CAMPAIGN)
    assert campaign_budget_history(ctx, LI_CAMPAIGN) is history
    (day,) = history.days
    assert day.klass == expected
    assert day.spend == Decimal(0)
    assert (history.idle, history.unobserved) == ((1, 0) if expected == "known" else (0, 1))


def test_unknown_days_with_zero_spend_are_never_idle(thresholds: Thresholds) -> None:
    """AAP 0.11.2: "unknown days are never counted as idle".

    Three zero-spend days: one changed intraday (the budget moved from 100 to 120 at noon),
    one unobserved (a 7-hour gap), one known. Only the known one is idle and only it enters
    utilization, whose window value is therefore 0 ÷ 120.
    """
    first, last = D - ONE_DAY, D + ONE_DAY
    series = [
        *every(at(first - ONE_DAY, 21), at(first, 9), li_known_day("100")),
        *every(at(first, 12), at(D, 3), li_known_day("120")),
        *every(at(D, 10), at(last + ONE_DAY, 3), li_known_day("120")),
    ]
    history = li_history(thresholds, first, last, series, ["0", "0", "0"])
    assert klasses(history) == ["changed_intraday", "unobserved", "known"]
    assert [day.idle for day in history.days] == [False, False, True]
    assert [day.u_d for day in history.days] == [None, None, Decimal(0)]
    assert (history.idle, history.changed_intraday, history.unobserved) == (1, 1, 1)
    assert history.window_utilization == Decimal(0)


def test_budget_history_counts_every_kind_of_day_over_ten_days(thresholds: Thresholds) -> None:
    """AAP 0.5.4.1 evidence: every day count, each matching a constructed 10-day series.

    UTC days 2026-09-22 to 2026-10-01 of a LinkedIn campaign, snapshots every 3 h from
    09-23 12:00:

    ========  ==========================================  =========================
    date      observations                                 class (spend)
    ========  ==========================================  =========================
    09-22     before the first snapshot                    unobserved (40)
    09-23     first snapshot at 12:00                      unobserved (40)
    09-24     budget 100, active                           known, capped (100)
    09-25     budget 100, active                           known, idle (0)
    09-26     budget 100, active                           known, under (50)
    09-27     raised to 200 between 09:00 and 12:00        changed intraday (180)
    09-28     budget 200, active                           known, capped (190)
    09-29     7-hour gap from 06:00 to 13:00               unobserved (60)
    09-30     paused between 10:00 and 13:00               changed intraday (30)
    10-01     budget 200, paused                           known, not active (0)
    ========  ==========================================  =========================
    """
    start = date(2026, 9, 22)
    end = date(2026, 10, 1)
    series = [
        *every(datetime(2026, 9, 23, 12, tzinfo=UTC), at(date(2026, 9, 27), 9), li_known_day()),
        *every(at(date(2026, 9, 27), 12), at(date(2026, 9, 29), 6), li_known_day("200")),
        *every(at(date(2026, 9, 29), 13), at(D, 10), li_known_day("200")),
        *every(at(D, 13), at(date(2026, 10, 2), 4), li_known_day("200", "paused")),
    ]
    costs = ["40", "40", "100", "0", "50", "180", "190", "60", "30", "0"]
    history = li_history(thresholds, start, end, series, costs)

    assert klasses(history) == [
        "unobserved",
        "unobserved",
        "known",
        "known",
        "known",
        "changed_intraday",
        "known",
        "unobserved",
        "changed_intraday",
        "known",
    ]
    assert [day.date for day in history.days] == span(start, 10)
    assert history.counts() == {
        "known_active": 4,
        "spending": 3,
        "idle": 1,
        "changed_intraday": 2,
        "unobserved": 3,
        "capped": 2,
        "high": 2,
        "under": 2,
    }
    assert [day.date for day in history.days if day.known_active] == [
        date(2026, 9, 24),
        date(2026, 9, 25),
        date(2026, 9, 26),
        date(2026, 9, 28),
    ]
    paused = history.days[-1]
    assert (paused.klass, paused.status, paused.u_d) == ("known", "paused", None)
    assert history.window_utilization == Decimal("0.68")
    assert history.window_spend == Decimal("690")
    assert history.latest_daily_budget == Decimal("200")


def test_window_utilization_is_spend_over_budget_on_known_active_days(
    thresholds: Thresholds,
) -> None:
    """AAP 0.5.4.1: window utilization = Σ spend ÷ Σ daily budget over known-budget active days.

    Spends of 50 and 100 against a daily budget of 100 give 150 ÷ 200 = 0.75, a ``Decimal``;
    the 500 spent on the following unobserved day is shown but never enters the ratio.
    """
    first, last = D - ONE_DAY, D + ONE_DAY
    series = every(at(first - ONE_DAY, 21), at(last, 21), li_known_day())
    history = li_history(thresholds, first, last, series, ["50", "100", "500"])
    assert klasses(history) == ["known", "known", "unobserved"]
    assert isinstance(history.window_utilization, Decimal)
    assert history.window_utilization == Decimal("0.75")
    assert history.window_spend == Decimal("650")


def _lifetime_linkedin(config: Thresholds) -> tuple[AnalysisContext, int]:
    """A LinkedIn campaign with a lifetime ``totalBudget`` only, fully spent every day."""
    win = window(config, D - ONE_DAY, D)
    series = every(at(D - timedelta(days=2), 21), at(D + ONE_DAY, 3), li_known_day(None))
    ctx = context(
        config,
        win,
        campaigns=[li_campaign(daily=None)],
        observations={("campaign", LI_CAMPAIGN): series},
        days={LI_CAMPAIGN: facts(li_fact, win.dates(), ["100", "100"])},
    )
    return ctx, LI_CAMPAIGN


def _custom_period_google(config: Thresholds) -> tuple[AnalysisContext, int]:
    """A Google campaign whose own budget is ``CUSTOM_PERIOD``, fully spent every day."""
    win = window(config, D - ONE_DAY, D)
    series = every(
        at(D - timedelta(days=2), 21, NY),
        at(D + ONE_DAY, 3, NY),
        lambda instant: obs(instant, "100", budget_id=OWN_BUDGET, period="CUSTOM_PERIOD"),
    )
    ctx = context(
        config,
        win,
        campaigns=[google_campaign(period="CUSTOM_PERIOD")],
        budgets=[budget_info(OWN_BUDGET, "100", shared=False, period="CUSTOM_PERIOD")],
        observations={("campaign", G_CAMPAIGN): series},
        days={G_CAMPAIGN: facts(google_fact, win.dates(), ["100", "100"])},
    )
    return ctx, G_CAMPAIGN


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_lifetime_linkedin, id="linkedin-lifetime"),
        pytest.param(_custom_period_google, id="google-custom-period"),
    ],
)
def test_lifetime_and_custom_period_budgets_have_no_utilization(
    thresholds: Thresholds, build: Callable[[Thresholds], tuple[AnalysisContext, int]]
) -> None:
    """AAP 0.5.4.1: lifetime or custom-period budgets show utilization as "n/a" (``None``).

    Their days are whole-day known but never known-budget active, so no ``u_d``, no capped
    day and no gate count exists, and they are not budget subjects at all.
    """
    ctx, campaign_id = build(thresholds)
    history = campaign_budget_history(ctx, campaign_id)
    assert not history.utilization_applicable
    assert history.window_utilization is None
    assert klasses(history) == ["known", "known"]
    assert all(day.u_d is None and not day.capped for day in history.days)
    assert (history.known_active, history.capped, history.high) == (0, 0, 0)
    assert history.latest_daily_budget is None
    gate = cap_constrained(history)
    assert not gate.passed
    assert gate.counts["known_budget_days"] == (0, thresholds.int("budget.min_known_days"))
    assert budget_subjects(ctx) == []


# ---------------------------------------------------------------------------
# Phase 4: gate result shape (AAP 0.5.4.1 history gates). Rule outcomes live in the
# acceptance suite; these pin observed-against-required counts and shortfall wording.
# ---------------------------------------------------------------------------

WEEK_START = date(2026, 9, 25)
"""A 7-day UTC window 2026-09-25 to 2026-10-01: exactly ``budget.min_known_days`` days."""


def _week_history(config: Thresholds, costs: Sequence[str]) -> BudgetHistory:
    """A LinkedIn campaign known active at a daily budget of 100 on every day of the week."""
    end = WEEK_START + timedelta(days=6)
    series = every(at(WEEK_START - ONE_DAY, 21), at(end + ONE_DAY, 3), li_known_day())
    return li_history(config, WEEK_START, end, series, costs)


def test_one_capped_day_and_six_idle_days_is_neither_capped_nor_high_utilization(
    thresholds: Thresholds,
) -> None:
    """AAP 0.5.4.1: "One capped day followed by six idle days is therefore 1 capped day of 7
    and 1 spending day of 7. It is neither cap-constrained nor high-utilization."

    Every check is reported observed against required, the share check with how its
    requirement was derived (⌈70% x 7⌉ = 5). The idle days are under-utilized, yet the
    under-utilized gate fails too: a budget that rarely serves is idle, not a B3 donor.
    """
    history = _week_history(thresholds, ["100", "0", "0", "0", "0", "0", "0"])
    assert (history.known_active, history.spending, history.idle, history.capped) == (7, 1, 6, 1)

    cap = cap_constrained(history)
    assert not cap.passed
    assert dict(cap.counts) == {
        "known_budget_days": (7, 7),
        "spending_days": (1, 5),
        "capped_days": (1, 5),
        "capped_share": (1, 5),
    }
    assert cap.shortfalls == (
        "spending days 1 of 5 required",
        "capped days 1 of 5 required",
        "capped days 1 of 5 required (70% of 7 known-budget days)",
    )

    high = high_utilization(history)
    assert not high.passed
    assert high.counts["spending_share"] == (1, 5)
    assert high.counts["high_utilization_days"] == (1, 5)

    under = under_utilized(history)
    assert under.counts["under_utilized_days"] == (6, 5)
    assert not under.passed
    assert under.shortfalls == ("spending days 1 of 5 required",)


def test_four_known_budget_days_fall_short_of_the_configured_minimum(
    thresholds: Thresholds, make_thresholds: ThresholdsFactory
) -> None:
    """AAP 0.5.4.1: every gate needs ``budget.min_known_days`` (7) known-budget active days.

    The first snapshot is taken exactly at the start of the fourth day, so only four of the
    seven days are known; each gate reports "known-budget days 4 of 7 required". Lowering the
    configured minimums to 4 lets the same history pass, so the minimum is configuration.
    """
    end = WEEK_START + timedelta(days=6)
    series = every(at(WEEK_START + timedelta(days=3)), at(end + ONE_DAY, 3), li_known_day())
    history = li_history(thresholds, WEEK_START, end, series, ["100"] * 7)
    assert klasses(history) == ["unobserved"] * 3 + ["known"] * 4
    assert (history.known_active, history.spending, history.capped) == (4, 4, 4)

    required = thresholds.int("budget.min_known_days")
    assert required == 7
    for gate in (cap_constrained, high_utilization, under_utilized):
        result = gate(history)
        assert not result.passed
        assert result.counts["known_budget_days"] == (4, required)
        assert "known-budget days 4 of 7 required" in result.shortfalls

    lowered = make_thresholds(
        {"budget.min_known_days": 4, "budget.min_spending_days": 4, "budget.min_capped_days": 4}
    )
    relaxed = cap_constrained(history, lowered)
    assert relaxed.passed
    assert relaxed.counts["known_budget_days"] == (4, 4)
    assert relaxed.counts["capped_share"] == (4, 3)


@pytest.mark.parametrize(
    ("costs", "passed", "shortfalls"),
    [
        pytest.param(["100"] * 5 + ["0"] * 2, True, (), id="5-capped-of-7"),
        pytest.param(
            ["100"] * 4 + ["50"] + ["0"] * 2,
            False,
            (
                "capped days 4 of 5 required",
                "capped days 4 of 5 required (70% of 7 known-budget days)",
            ),
            id="4-capped-of-7",
        ),
    ],
)
def test_five_capped_of_seven_known_days_with_five_spending_is_cap_constrained(
    thresholds: Thresholds, costs: list[str], passed: bool, shortfalls: tuple[str, ...]
) -> None:
    """AAP 0.11.2: "5 capped of 7 known-budget days → eligible"; one capped day fewer is not.

    Both histories have 5 spending days of 7 known-budget active days; only the capped-day
    count moves across ``budget.min_capped_days`` and the 70% share.
    """
    history = _week_history(thresholds, costs)
    assert (history.known_active, history.spending) == (7, 5)
    result = cap_constrained(history)
    assert result.passed is passed
    assert result.shortfalls == shortfalls
    assert result.describe() == "; ".join(shortfalls)
    capped = 5 if passed else 4
    assert result.counts["capped_days"] == (capped, 5)
    assert result.counts["capped_share"] == (capped, 5)


@pytest.mark.parametrize(
    ("source", "lost_is", "capped"),
    [
        pytest.param("google", "0.10", True, id="google-lost-is-at-threshold"),
        pytest.param("google", "0.09", False, id="google-lost-is-below"),
        pytest.param("google", None, False, id="google-lost-is-unreported"),
        pytest.param("linkedin", "0.50", False, id="linkedin-has-no-lost-is-clause"),
    ],
)
def test_google_budget_lost_impression_share_marks_a_capped_day(
    thresholds: Thresholds, source: str, lost_is: str | None, capped: bool
) -> None:
    """AAP 0.5.4.1: a capped day is ``u_d ≥ 0.95`` or, for Google only,
    ``search_budget_lost_impression_share ≥ budget.capped_lost_is_share`` (10%) that day.

    At ``u_d`` = 0.6 only the Google clause can make the day capped; it never applies to
    LinkedIn, and it never makes the day high-utilization.
    """
    assert thresholds.ratio("budget.capped_lost_is_share") == Decimal("0.10")
    win = window(thresholds, D, D)
    if source == "google":
        series = every(
            at(D - ONE_DAY, 21, NY),
            at(D + ONE_DAY, 3, NY),
            lambda instant: obs(instant, "100", budget_id=OWN_BUDGET),
        )
        ctx = context(
            thresholds,
            win,
            campaigns=[google_campaign()],
            budgets=[budget_info(OWN_BUDGET, "100", shared=False)],
            observations={("campaign", G_CAMPAIGN): series},
            days={G_CAMPAIGN: (google_fact(D, "60", lost_is=lost_is),)},
        )
        history = campaign_budget_history(ctx, G_CAMPAIGN)
    else:
        series = every(at(D - ONE_DAY, 21), at(D + ONE_DAY, 3), li_known_day())
        ctx = context(
            thresholds,
            win,
            campaigns=[li_campaign()],
            observations={("campaign", LI_CAMPAIGN): series},
            days={LI_CAMPAIGN: (li_fact(D, "60", lost_is=lost_is),)},
        )
        history = campaign_budget_history(ctx, LI_CAMPAIGN)
    (day,) = history.days
    assert day.u_d == Decimal("0.6")
    assert day.capped is capped
    assert not day.high
    assert history.capped == (1 if capped else 0)


# ---------------------------------------------------------------------------
# Phase 5: shared budgets (AAP 0.5.4.5: dated membership, budget-active days, aggregation)
# ---------------------------------------------------------------------------

SHARED_DAYS = span(date(2026, 9, 27), 5)
"""Google New York days 2026-09-27 to 2026-10-01 of the shared-budget scenario."""


def member(
    budget: str = "200", status: str = "active", budget_id: int = SHARED
) -> Callable[[datetime], Observation]:
    """A Google campaign snapshot maker for :func:`every`, drawing from ``budget_id``."""
    return lambda instant: obs(instant, budget, status, budget_id, shared=budget_id == SHARED)


def shared_amount(amount: str = "200") -> Callable[[datetime], Observation]:
    """A shared-budget snapshot maker for :func:`every`."""
    return lambda instant: budget_obs(instant, amount)


def _shared_context(config: Thresholds, *, b_switches: bool = True) -> AnalysisContext:
    """Shared budget ``S`` (200 a day) with campaigns A and B, over :data:`SHARED_DAYS`.

    A shows ``S`` in every observation. B draws from its own budget until its first snapshot
    showing ``S`` at 2026-09-29 00:00 New York time, so its membership changes during
    2026-09-28 and it is a member from 2026-09-29; with ``b_switches`` false it never
    moves. Spend per day, A: 60, 70, 80, 0, 100; B: 50, 40, 90, 0, 100.
    """
    first, last = SHARED_DAYS[0], SHARED_DAYS[-1]
    open_at, close_at = at(first - ONE_DAY, 21, NY), at(last + ONE_DAY, 3, NY)
    switch_day = SHARED_DAYS[2]
    if b_switches:
        b_series = [
            *every(open_at, at(switch_day - ONE_DAY, 21, NY), member("80", budget_id=OTHER_BUDGET)),
            *every(at(switch_day, 0, NY), close_at, member()),
        ]
    else:
        b_series = every(open_at, close_at, member("80", budget_id=OTHER_BUDGET))
    win = window(config, first, last)
    return context(
        config,
        win,
        campaigns=[
            google_campaign(CAMPAIGN_A, SHARED),
            google_campaign(CAMPAIGN_B, SHARED if b_switches else OTHER_BUDGET),
        ],
        budgets=[
            budget_info(SHARED, "200", shared=True),
            budget_info(OTHER_BUDGET, "80", shared=False),
        ],
        observations={
            ("budget", SHARED): every(open_at, close_at, shared_amount()),
            ("campaign", CAMPAIGN_A): every(open_at, close_at, member()),
            ("campaign", CAMPAIGN_B): b_series,
        },
        days={
            CAMPAIGN_A: facts(google_fact, SHARED_DAYS, ["60", "70", "80", "0", "100"]),
            CAMPAIGN_B: facts(google_fact, SHARED_DAYS, ["50", "40", "90", "0", "100"]),
        },
    )


def test_shared_budget_counts_each_member_only_on_its_member_days(
    thresholds: Thresholds,
) -> None:
    """AAP 0.5.4.5: dated membership; spend over that day's members; ``u_d`` ÷ the shared amount.

    B is a member only from 2026-09-29, so on 2026-09-27 the day's spend is A's 60 alone
    (B's 50 drew from its own budget) and ``u_d`` = 60 ÷ 200. On member days the spend is
    A + B against the one shared cap. A budget-active day with zero member spend is idle,
    one with spend is a spending day. The members are budget-level only: the shared budget
    is the subject, never a member campaign.
    """
    ctx = _shared_context(thresholds)
    history = shared_budget_history(ctx, SHARED)

    assert history.subject == ("budget", SHARED)
    assert history.shared
    assert history.utilization_applicable
    assert history.known_label == "budget-active days"
    assert dict(history.members) == {
        CAMPAIGN_A: tuple(SHARED_DAYS),
        CAMPAIGN_B: tuple(SHARED_DAYS[2:]),
    }
    assert klasses(history) == ["known", "changed_intraday", "known", "known", "known"]
    assert [day.spend for day in history.days] == [
        Decimal(value) for value in ("60", "70", "170", "0", "200")
    ]
    assert [day.u_d for day in history.days] == [
        Decimal("0.3"),
        None,
        Decimal("0.85"),
        Decimal(0),
        Decimal(1),
    ]
    known = [day for day in history.days if day.known_active]
    assert all(day.daily_budget == Decimal("200") for day in known)
    assert [(day.spending, day.idle) for day in known] == [
        (True, False),
        (True, False),
        (False, True),
        (True, False),
    ]
    assert history.counts() == {
        "known_active": 4,
        "spending": 3,
        "idle": 1,
        "changed_intraday": 1,
        "unobserved": 0,
        "capped": 1,
        "high": 1,
        "under": 2,
    }
    assert history.window_utilization == Decimal("0.5375")
    assert history.window_spend == Decimal("500")
    assert history.latest_daily_budget == Decimal("200")

    assert budget_subjects(ctx) == [("budget", SHARED)]
    a_snapshot = ctx.observations[("campaign", CAMPAIGN_A)][0]
    assert campaign_state_value(a_snapshot) == ("active", None)
    with pytest.raises(ValueError, match="draws from explicitly shared budget"):
        campaign_budget_history(ctx, CAMPAIGN_A)


def test_a_day_a_member_switched_budgets_is_not_budget_active(thresholds: Thresholds) -> None:
    """AAP 0.5.4.5: budget-active needs every member's membership whole-day known.

    B's snapshots bracketing 2026-09-28 show its own budget and then ``S``, so its membership
    changed during that day and the day is unknown, however A spent. Without B's switch the
    same day is budget-active on A's spend alone.
    """
    switch_day = SHARED_DAYS[1]
    ctx = _shared_context(thresholds)
    b_membership = classify_days(
        ctx.observations[("campaign", CAMPAIGN_B)],
        SHARED_DAYS,
        NY,
        value=membership_value,
        max_gap_hours=gap_hours(thresholds),
    )
    assert b_membership == {
        SHARED_DAYS[0]: ("known", OTHER_BUDGET),
        switch_day: ("changed_intraday", None),
        SHARED_DAYS[2]: ("known", SHARED),
        SHARED_DAYS[3]: ("known", SHARED),
        SHARED_DAYS[4]: ("known", SHARED),
    }
    switched = shared_budget_history(ctx, SHARED).days[1]
    assert switched.date == switch_day
    assert (switched.klass, switched.u_d, switched.daily_budget) == (
        "changed_intraday",
        None,
        None,
    )
    assert not switched.known_active

    stayed = _shared_context(thresholds, b_switches=False)
    history = shared_budget_history(stayed, SHARED)
    assert klasses(history) == ["known"] * 5
    assert dict(history.members) == {CAMPAIGN_A: tuple(SHARED_DAYS)}
    day = history.days[1]
    assert (day.spend, day.u_d) == (Decimal("70"), Decimal("0.35"))
    assert day.known_active


@pytest.mark.parametrize(
    ("statuses", "budget_active"),
    [
        pytest.param(("paused", "paused"), False, id="every-member-paused"),
        pytest.param(("active", "paused"), True, id="one-member-active"),
    ],
)
def test_budget_active_needs_at_least_one_member_known_active(
    thresholds: Thresholds, statuses: tuple[str, str], budget_active: bool
) -> None:
    """AAP 0.5.4.5: a budget-active day has at least one member whole-day known active.

    With the shared amount and both memberships whole-day known, a day on which every member
    is paused is known but not budget-active: no ``u_d`` and no gate count. One active member
    makes every day budget-active.
    """
    days = span(date(2026, 9, 29), 3)
    open_at, close_at = at(days[0] - ONE_DAY, 21, NY), at(days[-1] + ONE_DAY, 3, NY)
    a_status, b_status = statuses
    a_cost = "50" if a_status == "active" else "0"
    ctx = context(
        thresholds,
        window(thresholds, days[0], days[-1]),
        campaigns=[google_campaign(CAMPAIGN_A, SHARED), google_campaign(CAMPAIGN_B, SHARED)],
        budgets=[budget_info(SHARED, "200", shared=True)],
        observations={
            ("budget", SHARED): every(open_at, close_at, shared_amount()),
            ("campaign", CAMPAIGN_A): every(open_at, close_at, member(status=a_status)),
            ("campaign", CAMPAIGN_B): every(open_at, close_at, member(status=b_status)),
        },
        days={
            CAMPAIGN_A: facts(google_fact, days, [a_cost] * 3),
            CAMPAIGN_B: facts(google_fact, days, ["0"] * 3),
        },
    )
    history = shared_budget_history(ctx, SHARED)
    assert klasses(history) == ["known"] * 3
    assert dict(history.members) == {CAMPAIGN_A: tuple(days), CAMPAIGN_B: tuple(days)}
    if budget_active:
        assert history.known_active == 3
        assert [day.u_d for day in history.days] == [Decimal("0.25")] * 3
        assert [day.status for day in history.days] == ["active"] * 3
    else:
        assert history.known_active == 0
        assert [day.status for day in history.days] == ["paused"] * 3
        assert all(day.u_d is None for day in history.days)
        assert all(predicates(day) == NO_PREDICATE for day in history.days)
        assert "budget-active days 0 of 7 required" in cap_constrained(history).shortfalls


def test_a_member_status_change_with_no_member_known_active_leaves_the_day_unknown(
    thresholds: Thresholds,
) -> None:
    """AAP 0.5.4.5: a member status that is itself unknown makes the day unknown.

    A is paused between the 09:00 and 12:00 New York snapshots of the second day while B is
    paused throughout: on that day no member is known active and A's status is changed
    intraday, so the day is changed intraday rather than a known inactive day. The first day
    (A active) is budget-active; the third (both paused) is known and not budget-active.
    """
    days = span(date(2026, 9, 29), 3)
    open_at, close_at = at(days[0] - ONE_DAY, 21, NY), at(days[-1] + ONE_DAY, 3, NY)
    a_series = [
        *every(open_at, at(days[1], 9, NY), member()),
        *every(at(days[1], 12, NY), close_at, member(status="paused")),
    ]
    ctx = context(
        thresholds,
        window(thresholds, days[0], days[-1]),
        campaigns=[google_campaign(CAMPAIGN_A, SHARED), google_campaign(CAMPAIGN_B, SHARED)],
        budgets=[budget_info(SHARED, "200", shared=True)],
        observations={
            ("budget", SHARED): every(open_at, close_at, shared_amount()),
            ("campaign", CAMPAIGN_A): a_series,
            ("campaign", CAMPAIGN_B): every(open_at, close_at, member(status="paused")),
        },
        days={
            CAMPAIGN_A: facts(google_fact, days, ["100", "40", "0"]),
            CAMPAIGN_B: facts(google_fact, days, ["0", "0", "0"]),
        },
    )
    history = shared_budget_history(ctx, SHARED)
    assert klasses(history) == ["known", "changed_intraday", "known"]
    assert [day.known_active for day in history.days] == [True, False, False]
    assert history.days[2].status == "paused"
    assert (history.known_active, history.changed_intraday) == (1, 1)


@pytest.mark.parametrize(
    ("completeness", "expected"),
    [
        pytest.param("complete", "known", id="complete-coverage-means-zero-spend"),
        pytest.param("incomplete", "unobserved", id="unverified-coverage-means-unknown-spend"),
    ],
)
def test_a_member_without_a_fact_row_is_idle_only_under_verified_complete_coverage(
    thresholds: Thresholds, completeness: Completeness, expected: DayClass
) -> None:
    """AAP 0.5.4.1, 0.5.4.5: a shared budget's unknown days are never idle either.

    The only member has no fact row on the day. Its zero spend is an observation only when
    ``google.metrics_daily`` is ``complete`` over a range holding the date; otherwise the
    day's member spend is unknown and the day is unobserved.
    """
    open_at, close_at = at(D - ONE_DAY, 21, NY), at(D + ONE_DAY, 3, NY)
    ctx = context(
        thresholds,
        window(thresholds, D, D),
        campaigns=[google_campaign(CAMPAIGN_A, SHARED)],
        budgets=[budget_info(SHARED, "200", shared=True)],
        observations={
            ("budget", SHARED): every(open_at, close_at, shared_amount()),
            ("campaign", CAMPAIGN_A): every(open_at, close_at, member()),
        },
        freshness=metrics_coverage(
            GOOGLE_DATASET, "google.metrics_daily", D - ONE_DAY, D, completeness
        ),
    )
    history = shared_budget_history(ctx, SHARED)
    (day,) = history.days
    assert day.klass == expected
    assert day.idle is (expected == "known")
    assert (history.idle, history.unobserved) == ((1, 0) if expected == "known" else (0, 1))


def test_budget_active_needs_a_whole_day_known_shared_amount(thresholds: Thresholds) -> None:
    """AAP 0.5.4.5: the shared amount must be whole-day known, like a campaign's own budget.

    The shared budget's first snapshot is at noon on the first day (unobserved), and its
    amount is raised from 200 to 300 at noon on the second (changed intraday), although the
    member is whole-day known active throughout; the third day is known at 300.
    """
    days = span(date(2026, 9, 29), 3)
    budget_series = [
        *every(at(days[0], 12, NY), at(days[1], 9, NY), shared_amount("200")),
        *every(at(days[1], 12, NY), at(days[2] + ONE_DAY, 3, NY), shared_amount("300")),
    ]
    amount = classify_days(
        budget_series,
        days,
        NY,
        value=budget_amount_value,
        max_gap_hours=gap_hours(thresholds),
    )
    assert amount == {
        days[0]: ("unobserved", None),
        days[1]: ("changed_intraday", None),
        days[2]: ("known", Decimal("300")),
    }
    ctx = context(
        thresholds,
        window(thresholds, days[0], days[-1]),
        campaigns=[google_campaign(CAMPAIGN_A, SHARED)],
        budgets=[budget_info(SHARED, "300", shared=True)],
        observations={
            ("budget", SHARED): budget_series,
            ("campaign", CAMPAIGN_A): every(
                at(days[0] - ONE_DAY, 21, NY), at(days[2] + ONE_DAY, 3, NY), member()
            ),
        },
        days={CAMPAIGN_A: facts(google_fact, days, ["150"] * 3)},
    )
    history = shared_budget_history(ctx, SHARED)
    assert klasses(history) == ["unobserved", "changed_intraday", "known"]
    assert [day.u_d for day in history.days] == [None, None, Decimal("0.5")]
    assert history.days[2].daily_budget == Decimal("300")
    assert (history.known_active, history.unobserved, history.changed_intraday) == (1, 1, 1)
    assert history.latest_daily_budget == Decimal("300")


def test_a_custom_period_shared_budget_has_no_utilization(thresholds: Thresholds) -> None:
    """AAP 0.5.4.1: a ``CUSTOM_PERIOD`` shared budget shows utilization "n/a".

    Its amount is a lifetime amount, never a daily cap, so no day is budget-active, and
    neither it nor its member (evaluated at budget level) is a budget subject.
    """
    days = span(date(2026, 9, 30), 2)
    open_at, close_at = at(days[0] - ONE_DAY, 21, NY), at(days[-1] + ONE_DAY, 3, NY)
    custom = every(
        open_at, close_at, lambda instant: budget_obs(instant, "5000", period="CUSTOM_PERIOD")
    )
    assert budget_amount_value(custom[0]) is None
    ctx = context(
        thresholds,
        window(thresholds, days[0], days[-1]),
        campaigns=[google_campaign(CAMPAIGN_A, SHARED, period="CUSTOM_PERIOD")],
        budgets=[budget_info(SHARED, "5000", shared=True, period="CUSTOM_PERIOD")],
        observations={
            ("budget", SHARED): custom,
            ("campaign", CAMPAIGN_A): every(
                open_at,
                close_at,
                lambda instant: obs(
                    instant, "5000", budget_id=SHARED, shared=True, period="CUSTOM_PERIOD"
                ),
            ),
        },
        days={CAMPAIGN_A: facts(google_fact, days, ["400", "400"])},
    )
    history = shared_budget_history(ctx, SHARED)
    assert not history.utilization_applicable
    assert history.window_utilization is None
    assert klasses(history) == ["known", "known"]
    assert all(day.u_d is None for day in history.days)
    assert history.known_active == 0
    assert budget_subjects(ctx) == []


def test_shared_budget_history_rejects_unknown_and_unshared_budgets(
    thresholds: Thresholds,
) -> None:
    """A shared-budget history exists only for an explicitly shared budget in the context."""
    ctx = _shared_context(thresholds)
    with pytest.raises(ValueError, match="not explicitly shared"):
        shared_budget_history(ctx, OTHER_BUDGET)
    with pytest.raises(KeyError, match="999"):
        shared_budget_history(ctx, 999)
