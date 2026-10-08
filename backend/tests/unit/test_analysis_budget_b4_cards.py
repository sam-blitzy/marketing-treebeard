"""Unit tests for ``ads_engine.analysis.budget``: the B4 weekday / weekend card and its texts.

They hold rule B4 to AAP 0.5.4.5 (the B4 row and the fields every budget card shows side by
side), 0.5.4.1 (budget-history evidence, shared and lifetime budgets), 0.5.4.8 (the
thresholds applied) and 0.5.3 (the ``Withheld`` shape): every B4 scenario of
``acceptance/test_budget_rules.py`` is evaluated against its expectation; the four withheld
reasons are the AAP's exact words with their counts in the sample; every B4 card shows the
budget history of its budget subject (the shared budget for a member campaign, "n/a"
utilization for a lifetime or custom-period budget) with the keys and labels B1-B3 use, and
records every ``budget.*`` key; known zero spend on either or both sides is withheld "no
exposure" while absent facts say nothing; and a p-value is never rendered on the wrong side
of alpha.

Contexts come from the scenario catalogue (``SCENARIOS[name].build_context()``); a few cases
copy a built context with :func:`dataclasses.replace` (always with a fresh ``cache``, so no
memoised table of the original leaks in) or tune its thresholds with ``thresholds_with``.
Pure: no database and no network.
"""

import re
from dataclasses import replace
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Final

import pytest

from ads_engine.analysis import budget
from ads_engine.analysis.budget import (
    B4_TEST_NAME,
    B4_THRESHOLD_KEYS,
    CONTRACT_UNKNOWN_REASON,
    FRACTIONAL_REASON,
    NO_EXPOSURE_REASON,
    WEIGHTED_REASON,
)
from ads_engine.analysis.campaigns import VETO_THRESHOLD_KEYS
from ads_engine.analysis.context import (
    B1,
    B2,
    B3,
    B4,
    BUDGET,
    GROUP_CHANGES,
    AnalysisContext,
    Candidate,
    DayFact,
    Metric,
    Observation,
    RuleResult,
    Sample,
    Withheld,
)
from ads_engine.analysis.metrics import (
    BUDGET_THRESHOLD_KEYS,
    BudgetHistory,
    budget_history,
    counting_contract,
    under_utilized,
)
from ads_engine.analysis.stats import poisson_rate_test
from ads_engine.simulation.scenarios import (
    MODULE_BUDGET,
    SCENARIOS,
    Scenario,
    for_module,
    thresholds_with,
)

B4_SCENARIOS: Final = tuple(
    scenario for scenario in for_module(MODULE_BUDGET) if scenario.expect.rule_id == B4
)

GOOGLE_FIRES: Final = "b4.google_last_click.fires"
LINKEDIN_FIRES: Final = "b4.linkedin.fires"
LIFETIME: Final = "budget.lifetime.case"

HISTORY_KEYS: Final = (
    "window_utilization",
    "known_days",
    "spending_days",
    "spending_day_share",
    "idle_days",
    "changed_intraday_days",
    "unobserved_days",
)
"""The utilization metrics every budget card shows, in card order (no rule predicate in B4)."""

HISTORY_EXTRA_KEYS: Final = (
    "day_counts",
    "observation_assumption",
    "idle_dates",
    "changed_intraday_dates",
    "unobserved_dates",
    "unknown_dates",
)

B4_OWN_METRICS: Final = (
    "spend",
    "window_days",
    "outcomes",
    "cost_per_outcome",
    "weekend_days",
    "weekdays",
    "weekend_spend",
    "weekday_spend",
    "weekend_outcomes",
    "weekday_outcomes",
    "weekend_cpo",
    "weekday_cpo",
    "weekend_rate",
    "weekday_rate",
    "daily_budget",
)

AAP_REASONS: Final = {
    "weighted": "weighted attribution: rate test not valid",
    "unknown": "conversion counting contract unknown",
    "fractional": "fractional conversions",
    "exposure": "no exposure",
}
"""AAP 0.5.4.5 B4 row, word for word."""

_P_TEXT: Final = re.compile(r"p (=|<) (\d+\.\d+)")


# ---------------------------------------------------------------------------
# Builders and lookups
# ---------------------------------------------------------------------------


def _built(name: str) -> tuple[Scenario, AnalysisContext]:
    scenario = SCENARIOS[name]
    return scenario, scenario.build_context()


def _fresh(ctx: AnalysisContext, **changes: Any) -> AnalysisContext:
    """Copy ``ctx`` with ``changes`` and an empty per-run cache, so no memo carries over."""
    return replace(ctx, cache={}, **changes)


def _tuned(
    scenario: Scenario, ctx: AnalysisContext, overrides: dict[str, object]
) -> AnalysisContext:
    """Copy ``ctx`` with the scenario's thresholds plus ``overrides``."""
    tuned = thresholds_with(
        {**scenario.context.threshold_overrides, **overrides}, scenario.context.content_assets
    )
    return _fresh(ctx, thresholds=tuned)


def _days(ctx: AnalysisContext, campaign_id: int, **changes: Any) -> AnalysisContext:
    """Copy ``ctx`` with every window day of one campaign changed by ``changes``."""
    days = tuple(replace(day, **changes) for day in ctx.campaign_days[campaign_id])
    return _fresh(ctx, campaign_days={**ctx.campaign_days, campaign_id: days})


def _weekend_days(ctx: AnalysisContext, campaign_id: int, **changes: Any) -> AnalysisContext:
    """Copy ``ctx`` with only the campaign's Saturdays and Sundays changed by ``changes``."""
    days = tuple(
        replace(day, **changes) if day.date.weekday() >= 5 else day
        for day in ctx.campaign_days[campaign_id]
    )
    return _fresh(ctx, campaign_days={**ctx.campaign_days, campaign_id: days})


def _b4_items(result: RuleResult) -> list[Candidate]:
    return [item for item in result.candidates if item.rule_id == B4]


def _b4_lines(result: RuleResult) -> list[Withheld]:
    return [line for line in result.withheld if line.rule_id == B4]


def _only_item(ctx: AnalysisContext) -> Candidate:
    result = budget.evaluate(ctx)
    items = _b4_items(result)
    assert len(items) == 1, [line.reason for line in _b4_lines(result)]
    return items[0]


def _only_line(ctx: AnalysisContext) -> Withheld:
    result = budget.evaluate(ctx)
    assert _b4_items(result) == []
    lines = _b4_lines(result)
    assert len(lines) == 1, [line.reason for line in lines]
    return lines[0]


def _metrics(item: Candidate) -> dict[str, Metric]:
    return {metric.key: metric for metric in item.evidence.metrics}


def _normalised(value: object) -> object:
    """Compare numbers by value: ``"3.0"``, ``3`` and ``Decimal("3.000000")`` are equal."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    return value


def _share(value: Decimal | None) -> Decimal | str:
    """A history ratio as the card records it: four places half even, "n/a" when absent."""
    if value is None:
        return "n/a"
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)


def _expected_history(history: BudgetHistory) -> dict[str, object]:
    """The value each utilization metric must show for ``history``."""
    known = history.known_active
    share = None if known == 0 else _share(Decimal(history.spending) / Decimal(known))
    return {
        "window_utilization": _share(history.window_utilization),
        "known_days": known,
        "spending_days": history.spending,
        "spending_day_share": share,
        "idle_days": history.idle,
        "changed_intraday_days": history.changed_intraday,
        "unobserved_days": history.unobserved,
    }


def _shown_p(text: str) -> tuple[str, Decimal]:
    """The relation and number of the first ``p = x`` or ``p < x`` in ``text``."""
    match = _P_TEXT.search(text)
    assert match is not None, text
    return match.group(1), Decimal(match.group(2))


def _test_p(ctx: AnalysisContext, campaign_id: int) -> Decimal:
    """The B4 rate test's p-value for one campaign's window days."""
    window = ctx.window
    days = [day for day in ctx.campaign_days[campaign_id] if window.start <= day.date <= window.end]
    group = ctx.campaigns[campaign_id].objective_group

    def side(selected: list[DayFact]) -> tuple[int, Decimal]:
        outcomes = sum((day.outcome(group) or Decimal(0) for day in selected), Decimal(0))
        return int(outcomes), sum((day.cost for day in selected), Decimal(0))

    weekend = side([day for day in days if day.date.weekday() >= 5])
    weekday = side([day for day in days if day.date.weekday() < 5])
    test = poisson_rate_test(weekend[0], weekend[1], weekday[0], weekday[1])
    assert test is not None
    return test.p_value


# ---------------------------------------------------------------------------
# The catalogue guard (simulation/scenarios.py expectations)
# ---------------------------------------------------------------------------


def test_the_guard_covers_b4_firing_and_every_withheld_reason() -> None:
    names = {scenario.name for scenario in B4_SCENARIOS}
    assert {GOOGLE_FIRES, LINKEDIN_FIRES, "b4.no_exposure.case"} <= names
    assert {scenario.expect.fires for scenario in B4_SCENARIOS} == {True, False}
    reasons = {scenario.expect.withheld_reason for scenario in B4_SCENARIOS}
    assert set(AAP_REASONS.values()) <= reasons


@pytest.mark.parametrize("scenario", B4_SCENARIOS, ids=lambda scenario: scenario.name)
def test_b4_matches_the_catalogue_expectation(scenario: Scenario) -> None:
    expect = scenario.expect
    result = budget.evaluate(scenario.build_context())
    items = _b4_items(result)
    lines = _b4_lines(result)
    if expect.fires:
        assert len(items) == 1, [line.reason for line in lines]
        item = items[0]
        assert (item.section, item.group) == (BUDGET, GROUP_CHANGES)
        metrics = _metrics(item)
        for key, expected in expect.values.items():
            actual = metrics[key].value if key in metrics else item.evidence.extra[key]
            assert _normalised(actual) == _normalised(expected), (key, actual, expected)
        assert lines == []
        return
    assert items == []
    assert len(lines) == 1, [line.reason for line in lines]
    line = lines[0]
    assert expect.withheld_reason is not None
    if expect.withheld_reason in AAP_REASONS.values():
        assert line.reason == expect.withheld_reason
    else:
        assert line.reason.startswith(expect.withheld_reason)
    for key, expected in expect.values.items():
        actual = line.required if key == "required" else line.observed
        assert _normalised(actual) == _normalised(expected), (key, actual, expected)


# ---------------------------------------------------------------------------
# The four withheld reasons (AAP 0.5.4.5 B4 row, 0.5.3 Withheld)
# ---------------------------------------------------------------------------


def test_the_reason_constants_are_the_aaps_words() -> None:
    constants = {
        "weighted": WEIGHTED_REASON,
        "unknown": CONTRACT_UNKNOWN_REASON,
        "fractional": FRACTIONAL_REASON,
        "exposure": NO_EXPOSURE_REASON,
    }
    assert constants == AAP_REASONS


def test_weighted_attribution_counts_last_click_days_in_the_sample() -> None:
    _scenario, ctx = _built("b4.google_last_click.does_not_fire")
    contract = counting_contract(ctx)
    last_click = sum(1 for kind in contract.values() if kind == "last_click")
    line = _only_line(ctx)
    assert line.reason == WEIGHTED_REASON
    assert (line.observed, line.required, line.unit) == (last_click, 28, "last-click days")
    assert last_click == 25
    assert line.to_json()["sample"] == {"observed": 25, "required": 28, "unit": "last-click days"}


def test_an_unknown_contract_counts_whole_day_known_last_click_days() -> None:
    _scenario, ctx = _built("b4.contract_unknown.case")
    contract = counting_contract(ctx)
    unknown = sum(1 for kind in contract.values() if kind == "unknown")
    line = _only_line(ctx)
    assert line.reason == CONTRACT_UNKNOWN_REASON
    assert (line.observed, line.required) == (len(contract) - unknown, len(contract))
    assert (line.observed, line.required) == (20, 28)
    assert line.unit == "whole-day known last-click days"


def test_fractional_conversions_count_whole_number_days() -> None:
    _scenario, ctx = _built("b4.fractional.case")
    line = _only_line(ctx)
    assert line.reason == FRACTIONAL_REASON
    assert (line.observed, line.required) == (27, 28)
    assert line.unit == "days with whole-number conversions"


def test_zero_weekend_spend_is_no_exposure_with_the_side_named() -> None:
    _scenario, ctx = _built("b4.no_exposure.case")
    line = _only_line(ctx)
    assert line.reason == NO_EXPOSURE_REASON
    assert (line.observed, line.required, line.unit) == (0, 1, "weekend days with spend")


# ---------------------------------------------------------------------------
# Exposure before every other check; absent facts say nothing
# ---------------------------------------------------------------------------


def test_known_zero_spend_on_both_sides_is_withheld_no_exposure() -> None:
    _scenario, ctx = _built(GOOGLE_FIRES)
    line = _only_line(_days(ctx, 1, cost=Decimal(0)))
    assert line.reason == NO_EXPOSURE_REASON
    assert (line.observed, line.required) == (0, 1)
    assert line.unit == "weekend days and weekdays with spend"
    assert line.entity is not None
    assert line.entity.id == 1


def test_zero_weekday_spend_names_the_weekdays() -> None:
    _scenario, ctx = _built(GOOGLE_FIRES)
    days = tuple(
        replace(day, cost=Decimal(0)) if day.date.weekday() < 5 else day
        for day in ctx.campaign_days[1]
    )
    line = _only_line(_fresh(ctx, campaign_days={1: days}))
    assert (line.reason, line.unit) == (NO_EXPOSURE_REASON, "weekdays with spend")


def test_exposure_is_checked_before_outcomes_are_reported() -> None:
    _scenario, ctx = _built(GOOGLE_FIRES)
    line = _only_line(_days(ctx, 1, cost=Decimal(0), conversions=None))
    assert (line.reason, line.unit) == (NO_EXPOSURE_REASON, "weekend days and weekdays with spend")


def test_no_fact_row_in_the_window_says_nothing() -> None:
    _scenario, ctx = _built(GOOGLE_FIRES)
    result = budget.evaluate(_fresh(ctx, campaign_days={}))
    assert _b4_items(result) == []
    assert _b4_lines(result) == []


def test_spend_without_any_reported_outcome_says_nothing() -> None:
    _scenario, ctx = _built(GOOGLE_FIRES)
    result = budget.evaluate(_days(ctx, 1, conversions=None))
    assert _b4_items(result) == []
    assert _b4_lines(result) == []


def test_outcomes_missing_on_some_days_are_withheld_with_the_reported_days() -> None:
    _scenario, ctx = _built(LINKEDIN_FIRES)
    line = _only_line(_weekend_days(ctx, 1, leads=None))
    assert line.reason == "leads unavailable on 8 of 28 days"
    assert (line.observed, line.required, line.unit) == (20, 28, "days with leads reported")


# ---------------------------------------------------------------------------
# Budget history on every B4 card (AAP 0.5.4.5, 0.5.4.1 Evidence)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [GOOGLE_FIRES, LINKEDIN_FIRES])
def test_a_b4_card_shows_its_own_budget_history(name: str) -> None:
    _scenario, ctx = _built(name)
    item = _only_item(ctx)
    history = budget_history(ctx, ("campaign", 1))
    metrics = _metrics(item)
    keys = [metric.key for metric in item.evidence.metrics]
    assert keys[: len(B4_OWN_METRICS) + len(HISTORY_KEYS)] == [*B4_OWN_METRICS, *HISTORY_KEYS]
    for key, expected in _expected_history(history).items():
        assert metrics[key].value == expected, key
    assert metrics["known_days"].label == "Known-budget days"
    assert metrics["daily_budget"].value == history.latest_daily_budget
    assert metrics["daily_budget"].currency == "USD"
    extra = item.evidence.extra
    assert extra["budget_history"] == {
        "subject": {"type": "campaign", "id": 1},
        "name": ctx.campaigns[1].name,
        "shared": False,
        "known_label": "known-budget days",
        "minimums": {
            "known_budget_days": {
                "observed": history.known_active,
                "required": history.config.min_known_days,
            },
            "spending_days": {
                "observed": history.spending,
                "required": history.config.min_spending_days,
            },
        },
        "note": "shown beside the pattern; B4 is not gated on budget history",
    }
    assert extra["day_counts"] == history.counts()
    assert extra["observation_assumption"] == history.assumption
    assert extra["unknown_dates"] == ()
    assert "members" not in extra
    # B4's own gate, sample, test and affordance are unchanged.
    assert set(extra["gate"]) == {"weekend_days", "weekdays", "outcomes"}
    assert item.evidence.sample == Sample(216, 10, "outcomes")
    assert item.evidence.test is not None
    assert item.evidence.test.name == B4_TEST_NAME
    assert item.decision_subject is None
    assert item.increases_spend_on == ()


@pytest.mark.parametrize("name", [GOOGLE_FIRES, LINKEDIN_FIRES])
def test_b4_history_metrics_are_the_ones_b1_to_b3_show(name: str) -> None:
    _scenario, ctx = _built(name)
    item = _only_item(ctx)
    subject = ("campaign", 1)
    history = budget_history(ctx, subject)
    shared = budget.budget_evidence(
        ctx, subject, history, under_utilized(history), outcomes=None, cpo=None, baseline=None
    )
    reference = {metric.key: metric for metric in shared.metrics}
    for key in HISTORY_KEYS:
        assert _metrics(item)[key] == reference[key], key
    for key in HISTORY_EXTRA_KEYS:
        assert item.evidence.extra[key] == shared.extra[key], key


def test_a_shared_budget_member_shows_the_shared_budgets_history() -> None:
    _scenario, ctx = _built(GOOGLE_FIRES)
    shared_ctx = _fresh(ctx, budgets={1: replace(ctx.budgets[1], explicitly_shared=True)})
    item = _only_item(shared_ctx)
    history = budget_history(shared_ctx, ("budget", 1))
    assert history.shared
    metrics = _metrics(item)
    for key, expected in _expected_history(history).items():
        assert metrics[key].value == expected, key
    assert metrics["known_days"].label == "Budget-active days"
    assert metrics["spending_day_share"].label == "Share of budget-active days with spend"
    assert metrics["window_utilization"].value != "n/a"
    assert metrics["daily_budget"].value == history.latest_daily_budget
    extra = item.evidence.extra
    assert extra["budget_history"]["subject"] == {"type": "budget", "id": 1}
    assert extra["budget_history"]["name"] == shared_ctx.budgets[1].name
    assert extra["budget_history"]["shared"] is True
    assert extra["budget_history"]["known_label"] == "budget-active days"
    assert extra["members"] == (
        {
            "campaign_id": 1,
            "name": shared_ctx.campaigns[1].name,
            "member_days": len(history.members[1]),
        },
    )
    assert extra["day_counts"] == history.counts()
    assert item.decision_subject is None


def test_a_custom_period_google_budget_keeps_utilization_na() -> None:
    _scenario, ctx = _built(GOOGLE_FIRES)
    campaign = replace(
        ctx.campaigns[1],
        budget_period="CUSTOM_PERIOD",
        daily_budget=None,
        total_budget=Decimal("4000.00"),
    )
    # Every snapshot of the campaign and its budget states the custom period: the window
    # series and the newest stored snapshot alike, since the latest effective daily budget
    # is read from whichever of the two is newer.
    subjects = {("campaign", 1), ("budget", 1)}

    def custom_period(obs: Observation) -> Observation:
        return replace(obs, budget_daily=None, payload={**obs.payload, "period": "CUSTOM_PERIOD"})

    observations = {
        key: tuple(custom_period(obs) for obs in rows) if key in subjects else rows
        for key, rows in ctx.observations.items()
    }
    latest = {
        key: custom_period(obs) if key in subjects else obs
        for key, obs in ctx.latest_observations.items()
    }
    assert ("campaign", 1) in latest
    custom = _fresh(
        ctx,
        campaigns={1: campaign},
        budgets={1: replace(ctx.budgets[1], period="CUSTOM_PERIOD")},
        observations=observations,
        latest_observations=latest,
    )
    item = _only_item(custom)
    metrics = _metrics(item)
    assert metrics["window_utilization"].value == "n/a"
    assert metrics["known_days"].value == 0
    assert metrics["spending_day_share"].value is None
    assert metrics["daily_budget"].value is None
    assert item.evidence.extra["budget_history"]["subject"] == {"type": "campaign", "id": 1}


def test_a_linkedin_lifetime_budget_keeps_utilization_na() -> None:
    _scenario, ctx = _built(LIFETIME)
    campaign_id = next(iter(ctx.campaigns))
    assert ctx.campaigns[campaign_id].daily_budget is None
    pattern = _weekend_days(_days(ctx, campaign_id, leads=5), campaign_id, leads=1)
    item = _only_item(pattern)
    metrics = _metrics(item)
    assert metrics["window_utilization"].value == "n/a"
    assert metrics["known_days"].value == 0
    assert metrics["daily_budget"].value is None
    assert item.evidence.extra["budget_history"]["minimums"]["known_budget_days"] == {
        "observed": 0,
        "required": 7,
    }


# ---------------------------------------------------------------------------
# Thresholds applied (AAP 0.5.4.8)
# ---------------------------------------------------------------------------


def test_b4_threshold_keys_include_every_budget_history_key() -> None:
    assert set(BUDGET_THRESHOLD_KEYS) <= set(B4_THRESHOLD_KEYS)
    assert "budget.max_observation_gap_hours" in B4_THRESHOLD_KEYS
    assert len(set(B4_THRESHOLD_KEYS)) == len(B4_THRESHOLD_KEYS)
    _scenario, ctx = _built(GOOGLE_FIRES)
    assert set(ctx.thresholds.subset(*B4_THRESHOLD_KEYS)) == set(B4_THRESHOLD_KEYS)


def test_google_b4_evidence_records_the_observation_gap_and_history_keys() -> None:
    _scenario, ctx = _built(GOOGLE_FIRES)
    item = _only_item(ctx)
    thresholds = item.evidence.thresholds
    assert set(thresholds) == set(B4_THRESHOLD_KEYS)
    assert set(BUDGET_THRESHOLD_KEYS) <= set(thresholds)
    gap = ctx.thresholds.subset("budget.max_observation_gap_hours")
    assert thresholds["budget.max_observation_gap_hours"] == gap["budget.max_observation_gap_hours"]


def test_linkedin_b4_evidence_adds_the_veto_keys_to_the_history_keys() -> None:
    _scenario, ctx = _built(LINKEDIN_FIRES)
    item = _only_item(ctx)
    assert "ql" in _metrics(item)
    thresholds = set(item.evidence.thresholds)
    assert set(BUDGET_THRESHOLD_KEYS) <= thresholds
    assert thresholds == set(B4_THRESHOLD_KEYS) | set(VETO_THRESHOLD_KEYS)


def test_a_changed_observation_gap_is_recorded_on_the_card() -> None:
    scenario, ctx = _built(GOOGLE_FIRES)
    item = _only_item(_tuned(scenario, ctx, {"budget.max_observation_gap_hours": 5}))
    assert item.evidence.thresholds["budget.max_observation_gap_hours"] == 5


# ---------------------------------------------------------------------------
# p-values never read on the wrong side of alpha
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "alpha", "text"),
    [
        ("0.05006", "0.05005", "p = 0.0501"),
        ("0.04996", "0.05", "p = 0.04996"),
        ("0.05", "0.05", "p = 0.0500"),
        ("0.050001", "0.05", "p = 0.0500"),
        ("0.049999", "0.05", "p = 0.049999"),
        ("0.0123", "0.05", "p = 0.0123"),
        ("1", "0.05", "p = 1.0000"),
        ("0.000050", "0.05", "p < 0.0001"),
        ("0.000000", "0.05", "p < 0.0001"),
        ("0.0001", "0.05", "p = 0.0001"),
        ("0.00003", "0.00005", "p = 0.00003"),
        ("0.000004", "0.00005", "p < 0.00001"),
        ("0.00005", "0.00005", "p = 0.00005"),
    ],
)
def test_p_value_text(value: str, alpha: str, text: str) -> None:
    assert budget._p_value(Decimal(value), Decimal(alpha)) == text


@pytest.mark.parametrize("alpha", ["0.05", "0.05005", "0.01", "0.0001", "0.00005"])
def test_p_value_lies_on_the_side_of_alpha_the_value_does(alpha: str) -> None:
    level = Decimal(alpha)
    step = Decimal("0.000001")
    values = {level + step * offset for offset in range(-120, 121)} | {
        Decimal(0),
        Decimal(1),
        level,
    }
    for value in sorted(value for value in values if 0 <= value <= 1):
        relation, shown = _shown_p(budget._p_value(value, level))
        if relation == "<":
            assert shown <= level, (value, shown)
            assert value < shown, (value, shown)
            continue
        assert (shown < level) == (value < level), (value, shown)
        assert shown.as_tuple().exponent <= -4, (value, shown)


def test_a_failed_test_never_reads_below_alpha() -> None:
    scenario, ctx = _built(LINKEDIN_FIRES)
    close = _weekend_days(ctx, 1, leads=9)
    p_value = _test_p(close, 1)
    alpha = p_value - Decimal("0.000001")
    # Alpha sits just below the p-value and above the p-value floored to four places, so a
    # four-place floor would show the p-value below alpha.
    assert p_value.quantize(Decimal("0.0001"), rounding=ROUND_FLOOR) < alpha < p_value
    line = _only_line(_tuned(scenario, close, {"b4.alpha": alpha}))
    assert line.reason.startswith("weekend and weekday rates not distinguishable")
    relation, shown = _shown_p(line.reason)
    assert relation == "="
    assert shown >= alpha, (p_value, alpha, line.reason)
    assert (line.observed, line.required, line.unit) == (272, 10, "outcomes")


def test_a_significant_test_reads_below_alpha() -> None:
    scenario, ctx = _built(LINKEDIN_FIRES)
    close = _weekend_days(ctx, 1, leads=9)
    p_value = _test_p(close, 1)
    alpha = p_value + Decimal("0.000001")
    item = _only_item(_tuned(scenario, close, {"b4.alpha": alpha}))
    relation, shown = _shown_p(item.reason)
    assert relation == "="
    assert shown < alpha, (p_value, alpha, item.reason)
    assert item.evidence.test is not None
    assert item.evidence.test.p_value == p_value
    assert item.evidence.test.alpha == alpha


# ---------------------------------------------------------------------------
# B1-B3 cards keep their layout (the shared history helpers moved, nothing else)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["b3.no_receiver.case", "b3.allocation.case", "b2.share.fires", "shared.decision_subject.case"],
)
def test_b1_to_b3_cards_show_the_shared_history_block_in_place(name: str) -> None:
    _scenario, ctx = _built(name)
    items = [item for item in budget.evaluate(ctx).candidates if item.rule_id in (B1, B2, B3)]
    assert items, name
    for item in items:
        keys = [metric.key for metric in item.evidence.metrics]
        start = keys.index("window_utilization")
        before = "proposed_daily_budget" if "proposed_daily_budget" in keys else "daily_budget"
        assert keys[start - 1] == before
        end = keys.index("unobserved_days")
        assert keys[start : start + 4] == list(HISTORY_KEYS[:4])
        assert keys[end - 2 : end + 1] == list(HISTORY_KEYS[4:])
        extra_keys = list(item.evidence.extra)
        first = extra_keys.index("day_counts")
        assert extra_keys[first - 1] == "gate_shortfalls"
        assert extra_keys[first : first + len(HISTORY_EXTRA_KEYS)] == list(HISTORY_EXTRA_KEYS)
        assert extra_keys[first + len(HISTORY_EXTRA_KEYS)] == "day_boundary"
        assert item.evidence.sample is not None
        assert item.evidence.sample.unit in ("known-budget days", "budget-active days")
