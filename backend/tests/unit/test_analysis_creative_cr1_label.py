"""Unit tests for the CR1 pair label of ``ads_engine.analysis.creative``.

AAP 0.5.4.6 CR1 output: a pair is labelled "Distinguishable (adjusted p = …)" or "{A} ahead,
not yet distinguishable (adjusted p = …); about {n} more impressions per variant needed at
the observed difference", with the figure replaced by "not estimable" for identical rates
("no observed difference") or a variant without impressions. The template holds for every
estimated n, 0 included: when the current samples already reach ``cr.power`` at the observed
difference, the label still reads "about 0 more …" and the evidence records the power reached
(``evidence.extra.power`` and ``power_reached_at_observed_difference``). The figure is the
p-value at 4 places ("adjusted p = 0.0865"); below 0.0001 it is the bound itself ("adjusted
p < 0.0001"), never "adjusted p = < 0.0001", in the label, the ``not_distinguishable`` flag and
the reason.

Contexts come from the scenario catalogue (``SCENARIOS[name].build_context()``). The n = 0
pair reuses the ``cr1.fisher`` world with 5 vs 0 leads on 1,000 impressions per variant
(``cr.min_impressions``): an expected cell of 2.5 selects Fisher's exact test, p ≈ 0.062 stays
at or above ``cr.alpha``, and the observed difference already has ``cr.power``. The bounded
pair reuses the ``cr1.distinguishable`` world with 100 vs 50 leads on 10,000 impressions per
variant: the z-test's p ≈ 0.00004 is below 0.0001, distinguishable at the shipped ``cr.alpha``
and not yet distinguishable at a ``cr.alpha`` override below it. Thresholds are read from
``ctx.thresholds``. Pure: no database and no network.
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

BOUNDED_IMPRESSIONS: Final = 10_000
"""Impressions per variant of the bounded pair."""

BOUNDED_LEADS: Final = (100, 50)
"""Leads of the bounded pair: a two-proportion z-test p ≈ 0.00004, below :data:`P_PLACES`."""

STRICT_ALPHA: Final = Decimal("0.00001")
"""A ``cr.alpha`` override below the bounded pair's p: the pair is not yet distinguishable."""

AHEAD_TEXT: Final = "ahead, not yet distinguishable (adjusted p "
NEEDED_TEXT: Final = "more impressions per variant needed at the observed difference"
SAMPLE_METRIC: Final = "additional_impressions_per_variant"
P_PLACES: Final = Decimal("0.0001")
"""The 4 places an adjusted p-value is rendered at, and the bound shown below them."""

BOUND_TEXT: Final = "< 0.0001"
"""How a p-value below :data:`P_PLACES` renders after "p": the bound, with no "=" before it."""


def _rendered_p(value: Decimal) -> str:
    """A p-value with its relation as items render it: "= 0.0123", or "< 0.0001" below that."""
    return BOUND_TEXT if value < P_PLACES else f"= {value.quantize(P_PLACES):f}"


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


def _bounded_context(alpha: Decimal | None = None) -> AnalysisContext:
    """The ``cr1.distinguishable`` world with a pair whose p is below 0.0001.

    ``alpha`` overrides ``cr.alpha``; ``None`` keeps the scenario's configuration.
    """
    leads_a, leads_b = BOUNDED_LEADS
    spec = scenarios._creative_world(
        "cr1.distinguishable",
        (
            ("cr-a", scenarios._funnel_days(BOUNDED_IMPRESSIONS, 100, 200, leads_a)),
            ("cr-b", scenarios._funnel_days(BOUNDED_IMPRESSIONS, 100, 200, leads_b)),
        ),
    )
    base = SCENARIOS[DISTINGUISHABLE]
    context = base.context
    if alpha is not None:
        overrides = {**context.threshold_overrides, "cr.alpha": alpha}
        context = replace(context, threshold_overrides=overrides)
    return replace(base, spec=spec, context=context).build_context()


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

    shown_p = _rendered_p(test.p_adjusted)
    assert shown_p == f"= {test.p_adjusted.quantize(P_PLACES):f}"
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
    test = item.evidence.test
    assert test is not None
    assert test.p_adjusted is not None
    assert (
        f"Level, not yet distinguishable (adjusted p {_rendered_p(test.p_adjusted)}); "
        in item.action
    )
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
    test = item.evidence.test
    assert test is not None
    assert test.p_adjusted is not None
    assert extra["label"] == f"Distinguishable (adjusted p {_rendered_p(test.p_adjusted)})"
    assert NEEDED_TEXT not in item.action
    assert extra["sample_needed"] is None
    assert extra["power"] == config.power
    assert extra["power_reached_at_observed_difference"] is None
    assert SAMPLE_METRIC not in {m.key for m in item.evidence.metrics}
    assert item.flags == ()


def test_bounded_pair_preconditions_hold_at_the_configured_thresholds() -> None:
    config = CreativeConfig.from_thresholds(_bounded_context().thresholds)
    leads_a, leads_b = BOUNDED_LEADS
    test = compare_proportions(
        leads_a,
        BOUNDED_IMPRESSIONS,
        leads_b,
        BOUNDED_IMPRESSIONS,
        fisher_min_expected=config.fisher_min_expected,
    )
    assert test is not None
    assert test.method == "two_proportion_z"
    assert STRICT_ALPHA <= test.p_value < P_PLACES <= config.alpha
    assert _rendered_p(test.p_value) == BOUND_TEXT


def test_distinguishable_p_below_the_bound_reads_without_an_equals_sign() -> None:
    ctx = _bounded_context()
    config = CreativeConfig.from_thresholds(ctx.thresholds)
    item = _cr1_item(ctx)
    extra = item.evidence.extra
    test = item.evidence.test
    assert test is not None
    assert test.p_value is not None
    assert test.p_adjusted is not None
    assert test.p_value < P_PLACES
    assert test.p_adjusted < P_PLACES
    assert extra["distinguishable"] is True

    label = f"Distinguishable (adjusted p {BOUND_TEXT})"
    assert extra["label"] == label
    assert item.action == f"{_title(item)}: {label}"
    campaign = extra["campaign"]["name"]
    assert (
        f"; {extra['test']['label']} p {BOUND_TEXT}, Holm-adjusted across 1 pair in {campaign} "
        f"{BOUND_TEXT}, against \u03b1 {config.alpha.normalize():f}; "
    ) in item.reason
    assert item.reason.endswith("the two rates are statistically distinguishable.")
    assert item.flags == ()
    for text in (item.action, item.reason):
        assert "= <" not in text


def test_not_distinguishable_p_below_the_bound_reads_without_an_equals_sign() -> None:
    ctx = _bounded_context(STRICT_ALPHA)
    config = CreativeConfig.from_thresholds(ctx.thresholds)
    assert config.alpha == STRICT_ALPHA
    item = _cr1_item(ctx)
    extra = item.evidence.extra
    test = item.evidence.test
    assert test is not None
    assert test.p_value is not None
    assert test.p_adjusted is not None
    assert STRICT_ALPHA <= test.p_adjusted < P_PLACES
    assert test.p_value < P_PLACES
    assert extra["distinguishable"] is False
    needed = extra["sample_needed"]
    assert isinstance(needed, int)

    label = f"{_leader_name(item)} {AHEAD_TEXT}{BOUND_TEXT}); about {needed:,} {NEEDED_TEXT}"
    assert extra["label"] == label
    assert item.action == f"{_title(item)}: {label}"
    assert [(f.code, f.message) for f in item.flags] == [
        (FLAG_NOT_DISTINGUISHABLE, f"Not yet distinguishable (adjusted p {BOUND_TEXT})")
    ]
    campaign = extra["campaign"]["name"]
    assert (
        f"; {extra['test']['label']} p {BOUND_TEXT}, Holm-adjusted across 1 pair in {campaign} "
        f"{BOUND_TEXT}, against \u03b1 {config.alpha.normalize():f}; "
    ) in item.reason
    assert item.reason.endswith("the gap is not yet distinguishable from noise.")
    for text in (item.action, item.reason, *(f.message for f in item.flags)):
        assert "= <" not in text
