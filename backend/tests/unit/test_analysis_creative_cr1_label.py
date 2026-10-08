"""Unit tests for the CR1 pair label of ``ads_engine.analysis.creative``.

AAP 0.5.4.6 CR1 output: a pair is labelled "Distinguishable (adjusted p = …)" or "{A} ahead,
not yet distinguishable (adjusted p = …); about {n} more impressions per variant needed at
the observed difference", with the figure replaced by "not estimable" for identical rates
("no observed difference") or a variant without impressions. The template holds for every
estimated n, 0 included: when the current samples already reach ``cr.power`` at the observed
difference, the label still reads "about 0 more …" and the evidence records the power reached
(``evidence.extra.power`` and ``power_reached_at_observed_difference``).

Contexts come from the scenario catalogue (``SCENARIOS[name].build_context()``). The n = 0
pair reuses the ``cr1.fisher`` world with 5 vs 0 leads on 1,000 impressions per variant
(``cr.min_impressions``): an expected cell of 2.5 selects Fisher's exact test, p ≈ 0.062 stays
at or above ``cr.alpha``, and the observed difference already has ``cr.power``. Thresholds are
read from ``ctx.thresholds``. Pure: no database and no network.
"""

from dataclasses import replace
from decimal import Decimal
from typing import Final

from ads_engine.analysis import creative
from ads_engine.analysis.context import (
    CR1,
    FLAG_NOT_DISTINGUISHABLE,
    AnalysisContext,
    Candidate,
)
from ads_engine.analysis.creative import NOT_ESTIMABLE, NOT_ESTIMABLE_NO_DIFFERENCE, CreativeConfig
from ads_engine.analysis.stats import additional_n_for_difference, compare_proportions
from ads_engine.simulation import scenarios
from ads_engine.simulation.scenarios import SCENARIOS

FISHER: Final = "cr1.fisher.case"
"""1,000 impressions per variant, 6 vs 1 leads: Fisher's exact test, 862 more needed."""

IDENTICAL: Final = "cr1.identical_rates.case"
"""Identical rates: the figure is not estimable ("no observed difference")."""

DISTINGUISHABLE: Final = "cr1.distinguishable.fires"
"""A pair distinguishable after the Holm correction."""

VARIANT_IMPRESSIONS: Final = 1_000
"""Impressions per variant of the n = 0 pair, equal to the shipped ``cr.min_impressions``."""

POWERED_LEADS: Final = (5, 0)
"""Leads of the n = 0 pair: Fisher's p ≈ 0.062 (not distinguishable) at power above 0.8."""

AHEAD_TEXT: Final = "ahead, not yet distinguishable (adjusted p = "
NEEDED_TEXT: Final = "more impressions per variant needed at the observed difference"
SAMPLE_METRIC: Final = "additional_impressions_per_variant"
P_PLACES: Final = Decimal("0.0001")
"""The 4 places an adjusted p-value is rendered at."""


def _powered_context() -> AnalysisContext:
    """The ``cr1.fisher`` world with 5 vs 0 leads: not distinguishable yet already powered."""
    leads_a, leads_b = POWERED_LEADS
    spec = scenarios._creative_world(
        "cr1.fisher",
        (
            ("cr-a", scenarios._funnel_days(VARIANT_IMPRESSIONS, 20, 30, leads_a)),
            ("cr-b", scenarios._funnel_days(VARIANT_IMPRESSIONS, 20, 30, leads_b)),
        ),
    )
    return replace(SCENARIOS[FISHER], spec=spec).build_context()


def _cr1_item(ctx: AnalysisContext) -> Candidate:
    """The single CR1 item of the context's analysed campaign."""
    items = [c for c in creative.evaluate(ctx).candidates if c.rule_id == CR1]
    assert len(items) == 1
    return items[0]


def _metric(item: Candidate, key: str) -> object:
    """The value of one evidence metric of ``item``."""
    values = {m.key: m.value for m in item.evidence.metrics}
    return values[key]


def _leader_name(item: Candidate) -> str:
    """The display name of the variant the item says is ahead."""
    extra = item.evidence.extra
    names = dict(zip(extra["pair"], extra["names"], strict=True))
    return str(names[extra["ahead"]])


def _title(item: Candidate) -> str:
    """The action's title: the variant ahead first, then the other variant."""
    leader = _leader_name(item)
    (other,) = (name for name in item.evidence.extra["names"] if name != leader)
    return f"{leader} vs {other}"


def test_powered_pair_preconditions_hold_at_the_configured_thresholds() -> None:
    ctx = _powered_context()
    config = CreativeConfig.from_thresholds(ctx.thresholds)
    leads_a, leads_b = POWERED_LEADS
    assert config.min_impressions == VARIANT_IMPRESSIONS
    test = compare_proportions(
        leads_a,
        VARIANT_IMPRESSIONS,
        leads_b,
        VARIANT_IMPRESSIONS,
        fisher_min_expected=config.fisher_min_expected,
    )
    assert test is not None
    assert test.method == "fisher_exact"
    assert test.expected_min < config.fisher_min_expected
    assert test.p_value >= config.alpha
    needed = additional_n_for_difference(
        leads_a,
        VARIANT_IMPRESSIONS,
        leads_b,
        VARIANT_IMPRESSIONS,
        alpha=config.alpha,
        power=config.power,
    )
    assert needed == 0


def test_zero_more_impressions_keeps_the_prescribed_template() -> None:
    ctx = _powered_context()
    config = CreativeConfig.from_thresholds(ctx.thresholds)
    item = _cr1_item(ctx)
    extra = item.evidence.extra
    label = extra["label"]
    test = item.evidence.test
    assert test is not None
    assert test.name == "fisher_exact"
    assert test.p_adjusted is not None
    assert test.p_adjusted >= config.alpha

    shown_p = f"{test.p_adjusted.quantize(P_PLACES):f}"
    expected = f"{_leader_name(item)} {AHEAD_TEXT}{shown_p}); about 0 {NEEDED_TEXT}"
    assert label == expected
    assert item.action.endswith(f"about 0 {NEEDED_TEXT}")
    assert AHEAD_TEXT in item.action
    assert "no more impressions" not in item.action
    assert "already reach" not in item.action
    assert item.action == f"{_title(item)}: {label}"

    assert extra["distinguishable"] is False
    assert extra["sample_needed"] == 0
    assert extra["not_estimable_reason"] is None
    assert extra["power"] == config.power
    assert extra["power_reached_at_observed_difference"] is True
    assert _metric(item, SAMPLE_METRIC) == 0
    assert [f.code for f in item.flags] == [FLAG_NOT_DISTINGUISHABLE]

    sample = item.evidence.sample
    assert sample is not None
    assert (sample.observed, sample.required, sample.unit) == (
        VARIANT_IMPRESSIONS,
        config.min_impressions,
        "impressions",
    )
    assert item.reason.endswith("the gap is not yet distinguishable from noise.")


def test_positive_figure_keeps_the_prescribed_template() -> None:
    ctx = SCENARIOS[FISHER].build_context()
    config = CreativeConfig.from_thresholds(ctx.thresholds)
    item = _cr1_item(ctx)
    extra = item.evidence.extra
    needed = extra["sample_needed"]
    assert isinstance(needed, int)
    assert needed > 0
    assert extra["label"].startswith(f"{_leader_name(item)} {AHEAD_TEXT}")
    assert item.action.endswith(f"; about {needed:,} {NEEDED_TEXT}")
    assert item.action == f"{_title(item)}: {extra['label']}"
    assert extra["power"] == config.power
    assert extra["power_reached_at_observed_difference"] is False
    assert _metric(item, SAMPLE_METRIC) == needed
    assert [f.code for f in item.flags] == [FLAG_NOT_DISTINGUISHABLE]


def test_not_estimable_figure_keeps_its_wording() -> None:
    ctx = SCENARIOS[IDENTICAL].build_context()
    config = CreativeConfig.from_thresholds(ctx.thresholds)
    item = _cr1_item(ctx)
    extra = item.evidence.extra
    assert item.action.endswith(
        f"more impressions per variant needed: {NOT_ESTIMABLE} ({NOT_ESTIMABLE_NO_DIFFERENCE})"
    )
    assert "Level, not yet distinguishable (adjusted p = " in item.action
    assert "about" not in item.action
    assert extra["sample_needed"] == NOT_ESTIMABLE
    assert extra["not_estimable_reason"] == NOT_ESTIMABLE_NO_DIFFERENCE
    assert extra["power"] == config.power
    assert extra["power_reached_at_observed_difference"] is None
    assert _metric(item, SAMPLE_METRIC) == NOT_ESTIMABLE


def test_distinguishable_label_is_unchanged() -> None:
    ctx = SCENARIOS[DISTINGUISHABLE].build_context()
    config = CreativeConfig.from_thresholds(ctx.thresholds)
    item = _cr1_item(ctx)
    extra = item.evidence.extra
    assert extra["distinguishable"] is True
    assert extra["label"].startswith("Distinguishable (adjusted p = ")
    assert NEEDED_TEXT not in item.action
    assert extra["sample_needed"] is None
    assert extra["power"] == config.power
    assert extra["power_reached_at_observed_difference"] is None
    assert SAMPLE_METRIC not in {m.key for m in item.evidence.metrics}
    assert item.flags == ()
