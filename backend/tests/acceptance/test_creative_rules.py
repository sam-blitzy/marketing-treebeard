"""Acceptance tests for Copy / creative changes: rules CR1, CR2 and CR3 on LinkedIn creatives.

AAP 0.11.2 row ``acceptance/test_creative_rules.py``, rules AAP 0.5.4.6 and FR-7, with P6
(sample-size withholding) and P7 ("no premature winners", AAP 0.5.4.2). The user asked three
things of the copy section: which ad variants are statistically distinguishable yet (not just
"currently ahead" on a small sample), which measurable funnel stage underperforms (open rate vs
completion rate vs click rate), and which single thing the next variant should change so the
read stays clean.

Every guarantee runs on a scenario of the catalogue (``ads_engine.simulation.scenarios``):
``fires`` / ``does_not_fire`` twins that move one input across its threshold, or single cases.
The catalogue guard checks each creative scenario's ``Expectation`` against
``creative.evaluate``; the tests after it restate every guarantee from the item's evidence and
recompute its statistics with ``analysis.stats`` from the context's own window totals, so no
label can drift from the numbers it cites.

What ``Expectation.fires`` means for each rule:

* CR1: the pair is distinguishable after the Holm correction. For the variant-admission topic
  ``cr.variants`` it means the copy campaign is analysed at all.
* CR2: a stage is weak, its Wilson upper bound below the account baseline. CR2 has no item of
  its own: its sentence is the reason of the campaign's CR3 item.
* CR3: the next test's per-variant sample is estimable.

Thresholds are read from ``ctx.thresholds`` (``cr.*``). The literal numbers asserted are the
AAP's own: 999 versus 1,000 impressions, 2 variants, and a 20% lift at alpha 0.05 and power 0.8.

Ranking campaign blocks against each other by spend is ``analysis/precedence.py``'s step; this
module checks what the rule hands it: one ``campaign:{id}`` group per analysed campaign, its
frozen campaign spend, and the order of the items inside the group.

Pure: no database and no network. Contexts come from the root ``make_context`` fixture.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Final

import pytest
from tests.support import evidence_metric

from ads_engine.analysis import creative, stats
from ads_engine.analysis.context import (
    CR1,
    CR2,
    CR3,
    CREATIVE,
    FLAG_NOT_DISTINGUISHABLE,
    OBJ_LINKEDIN_LEAD_GEN,
    AnalysisContext,
    Candidate,
    CreativeTotals,
    DecisionSubject,
    RuleResult,
    Withheld,
    creative_group,
)
from ads_engine.analysis.creative import (
    CR1_THRESHOLD_KEYS,
    CR3_THRESHOLD_KEYS,
    EXPLORATORY_TEXT,
    NEXT_TEST_VARIANTS,
    NO_WEAK_STAGE_TEXT,
    NOT_ESTIMABLE,
    NOT_ESTIMABLE_BASELINE_UNAVAILABLE,
    NOT_ESTIMABLE_NO_DIFFERENCE,
    NOT_ESTIMABLE_TARGET_ABOVE_100,
    NOT_ESTIMABLE_ZERO_BASELINE,
    NOT_ESTIMABLE_ZERO_EXPOSURE,
    NOT_ESTIMABLE_ZERO_IMPRESSIONS,
    REQUIRED_DATASETS,
    STAGE_VARIABLE,
    STAGE_VARIABLE_KEY,
    STAGES,
    TEST_ELEMENTS,
    CreativeConfig,
    plan_next_test,
)
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.simulation.scenarios import (
    FIXED_NOW,
    MODULE_CREATIVE,
    SCENARIOS,
    Expectation,
    Scenario,
    for_module,
    input_differences,
)

type ContextFactory = Callable[..., AnalysisContext]
"""The root ``make_context`` fixture: ``make_context(scenario, *, thresholds=None, now=None)``."""

type ThresholdsFactory = Callable[[Mapping[str, Any]], Thresholds]
"""The root ``make_thresholds`` fixture: an overridden copy of ``config/``, loaded."""

# ---------------------------------------------------------------------------------------------
# Catalogue names and AAP definitions
# ---------------------------------------------------------------------------------------------

CREATIVE_SCENARIOS: Final[tuple[Scenario, ...]] = for_module(MODULE_CREATIVE)
"""Every catalogue scenario of this module, in name order."""

COPY_CAMPAIGN: Final = "li-copy"
"""Spec key of the creative worlds' campaign whose variants are under test."""

BASELINE_CAMPAIGN: Final = "li-base"
"""Spec key of the creative worlds' one-creative campaign that sets the account baseline."""

VARIANT_TOPICS: Final = frozenset({"cr.variants"})
"""CR1 topics whose guarantee is variant admission: ``fires`` means the campaign is analysed."""

DEFAULT: Final = "default"
"""The end-to-end world: several analysed lead-gen campaigns with distinct spends."""

P7_CASE: Final = "p7.no_winner_wording.case"
"""The precedence catalogue's P7 case: a pair ahead on a small difference."""

AAP_STAGE_COUNTS: Final[Mapping[str, tuple[str, str]]] = {
    "click_rate": ("clicks", "impressions"),
    "open_rate": ("form_opens", "impressions"),
    "completion_rate": ("leads", "form_opens"),
}
"""AAP 0.5.4.6 stage rates: click = clicks / impressions, open = lead form opens / impressions,
completion = leads / form opens (``CreativeTotals`` attribute names)."""

AAP_STAGE_VARIABLE: Final[Mapping[str, str]] = {
    "click_rate": "headline",
    "open_rate": "call-to-action label",
    "completion_rate": "number of form questions",
}
"""AAP 0.5.4.6 CR3: the one variable a test of each stage changes."""

AAP_NEXT_TEST_TEXT: Final = "to detect a 20% lift (\u03b1 0.05, power 0.8)"
"""The CR3 sizing clause at the AAP's 20% lift, alpha (Greek letter) 0.05 and power 0.8."""

PROJECTION_BASIS: Final = "projected through the observed open rate"
"""How a completion-rate sample is projected to impressions (AAP 0.5.4.6)."""

WINNER_WORDING: Final = re.compile(r"\b(winners?|wins|better|best|beats)\b", re.IGNORECASE)
"""P7: words that call a variant a winner; forbidden until CR1 marks the pair distinguishable."""

WEAK_STAGE_REASON: Final = re.compile(
    r"(?P<stage>[A-Z][a-z]+ rate) is below account baseline: (?P<rate>[0-9.]+)% "
    r"\[(?P<low>[0-9.]+)%\u2013(?P<high>[0-9.]+)%\] vs (?P<baseline>[0-9.]+)%"
)
"""CR2: "{Stage} is below account baseline: {rate} [CI] vs {baseline}" (an en dash in the CI)."""

RATE_PLACES: Final = Decimal("0.000001")
"""Rates, shortfalls and baselines are stored at 6 places."""

P_PLACES: Final = Decimal("0.0001")
"""Labels render p-values at 4 places, or "< 0.0001" below that."""


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _topic(scenario: Scenario) -> str:
    """The scenario's ``<topic>.<case>`` part: its name without the variant."""
    return scenario.name.rsplit(".", 1)[0]


@dataclass(frozen=True, slots=True)
class _Cr:
    """The ``cr.*`` thresholds of a context, read straight from ``ctx.thresholds``."""

    min_impressions: int
    min_variants: int
    alpha: Decimal
    power: Decimal
    target_lift: Decimal
    fisher_min_expected: Decimal
    ci_level: Decimal

    @classmethod
    def of(cls, ctx: AnalysisContext) -> _Cr:
        loaded = ctx.thresholds
        return cls(
            min_impressions=loaded.int("cr.min_impressions"),
            min_variants=loaded.int("cr.min_variants"),
            alpha=loaded.decimal("cr.alpha"),
            power=loaded.decimal("cr.power"),
            target_lift=loaded.decimal("cr.target_lift"),
            fisher_min_expected=loaded.decimal("cr.fisher_min_expected"),
            ci_level=loaded.decimal("cr.ci_level"),
        )


@dataclass(frozen=True, slots=True)
class _Run:
    """One scenario's context and ``creative.evaluate`` result, addressed by spec keys."""

    scenario: Scenario
    ctx: AnalysisContext
    result: RuleResult

    @property
    def cr(self) -> _Cr:
        return _Cr.of(self.ctx)

    def campaign_id(self, campaign: str = COPY_CAMPAIGN) -> int:
        return self.scenario.ids.id("linkedin", "campaign", campaign)

    def urn(self, creative_key: str) -> str:
        return self.scenario.ids.external_id("linkedin", "creative", creative_key)

    def name(self, creative_key: str) -> str:
        return self.scenario.ids.name("linkedin", "creative", creative_key)

    def items(self, rule_id: str | None = None, campaign: str = COPY_CAMPAIGN) -> list[Candidate]:
        group = creative_group(self.campaign_id(campaign))
        return [
            item
            for item in self.result.candidates
            if item.group == group and (rule_id is None or item.rule_id == rule_id)
        ]

    def cr3(self, campaign: str = COPY_CAMPAIGN) -> Candidate:
        found = self.items(CR3, campaign)
        assert len(found) == 1, f"{self.scenario.name}: {len(found)} CR3 items for {campaign}"
        return found[0]

    def pair(self, first: str, second: str) -> Candidate:
        wanted = {self.urn(first), self.urn(second)}
        found = [item for item in self.items(CR1) if set(item.evidence.extra["pair"]) == wanted]
        assert len(found) == 1, f"{self.scenario.name}: no single CR1 item for {first}/{second}"
        return found[0]

    def single_pair(self) -> Candidate:
        found = self.items(CR1)
        assert len(found) == 1, f"{self.scenario.name}: {len(found)} CR1 items, expected 1"
        return found[0]

    def withheld(self, campaign: str) -> list[Withheld]:
        campaign_id = self.campaign_id(campaign)
        return [
            line
            for line in self.result.withheld
            if line.entity is not None and line.entity.id == campaign_id
        ]

    def totals(self, creative_key: str) -> CreativeTotals:
        urn = self.urn(creative_key)
        (info,) = (info for info in self.ctx.creatives.values() if info.urn == urn)
        return self.ctx.creative_totals[info.id]

    def variant_totals(self, campaign: str = COPY_CAMPAIGN) -> list[CreativeTotals]:
        """The campaign's creatives with at least ``cr.min_impressions`` window impressions."""
        campaign_id, minimum = self.campaign_id(campaign), self.cr.min_impressions
        return sorted(
            (
                totals
                for totals in self.ctx.creative_totals.values()
                if totals.campaign_id == campaign_id and totals.impressions >= minimum
            ),
            key=lambda totals: totals.creative_id,
        )

    def baseline_totals(self, campaign: str = COPY_CAMPAIGN) -> list[CreativeTotals]:
        """Every LinkedIn creative of the campaign's ad account and objective group."""
        info = self.ctx.campaigns[self.campaign_id(campaign)]
        return [
            totals
            for totals in self.ctx.creative_totals.values()
            if (other := self.ctx.campaigns.get(totals.campaign_id)) is not None
            and other.source == "linkedin"
            and other.account_id == info.account_id
            and other.objective_group == info.objective_group
        ]


def _evaluate(make_context: ContextFactory, scenario: str | Scenario, **options: Any) -> _Run:
    """Build the scenario's context (``make_context`` options pass through) and evaluate CR."""
    entry = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    if entry.name == DEFAULT:
        options.setdefault("now", FIXED_NOW)
    ctx = make_context(entry, **options)
    return _Run(entry, ctx, creative.evaluate(ctx))


def _pooled(totals: Sequence[CreativeTotals], numerator: str, denominator: str) -> tuple[int, int]:
    """Pooled ``(successes, trials)`` of one stage over ``totals``."""
    successes = sum(int(getattr(row, numerator)) for row in totals)
    trials = sum(int(getattr(row, denominator)) for row in totals)
    return successes, trials


def _rate(successes: int, trials: int) -> Decimal:
    """``successes / trials`` at the stored 6 places."""
    return (Decimal(successes) / Decimal(trials)).quantize(RATE_PLACES)


def _at_places(actual: object, expected: object) -> bool:
    """Whether ``actual`` rounds to the expectation string at the string's own places."""
    if not isinstance(actual, Decimal):
        return False
    target = Decimal(str(expected))
    return actual.quantize(target) == target


def _shows(percent: str, rate: Decimal) -> bool:
    """Whether a rendered percentage (``"0.50"``) is ``rate`` at the places it shows."""
    shown = Decimal(percent)
    return (rate * 100).quantize(shown) == shown


def _rendered_p(value: Decimal) -> str:
    """A p-value as labels render it: 4 places, or "< 0.0001" below that."""
    return "< 0.0001" if value < P_PLACES else f"{value.quantize(P_PLACES):f}"


def _daily_delivery(variants: Sequence[CreativeTotals]) -> Decimal:
    """Current per-variant delivery: the mean of impressions per delivery day, at 6 places."""
    rates = [Decimal(row.impressions) / Decimal(row.delivery_days) for row in variants]
    return (sum(rates, Decimal(0)) / Decimal(len(rates))).quantize(RATE_PLACES)


def _stage_rows(item: Candidate) -> dict[str, Mapping[str, Any]]:
    """``evidence.extra.stages`` of a CR3 item, keyed by stage."""
    return {row["stage"]: row for row in item.evidence.extra["stages"]}


def _title(item: Candidate) -> str:
    """A CR1 action's title: the variant ahead first (variant order when level)."""
    extra = item.evidence.extra
    names = dict(zip(extra["pair"], extra["names"], strict=True))
    first, second = extra["names"]
    if extra["ahead"] is not None and names[extra["ahead"]] == second:
        first, second = second, first
    return f"{first} vs {second}"


def _without_names(text: str, item: Candidate) -> str:
    """``text`` with the variant, entity and campaign names removed (P7 checks wording only)."""
    extra = item.evidence.extra
    names = {*extra["names"], extra["campaign"]["name"], item.entity_ref.name}
    for name in sorted(names, key=len, reverse=True):
        text = text.replace(name, "")
    return text


# ---------------------------------------------------------------------------------------------
# Phase 1: catalogue guard
# ---------------------------------------------------------------------------------------------


def _check_flags_and_label(item: Candidate, expect: Expectation, *, label: str) -> None:
    """The expectation's flags are on the item, and its exact label, when it names one."""
    assert set(expect.flags) <= set(item.flag_codes), (expect.flags, item.flag_codes)
    if expect.label is not None:
        assert label == expect.label


def _keys(expected: object) -> tuple[str, ...]:
    """An expectation value naming creatives by spec key: a tuple of strings."""
    assert isinstance(expected, tuple), expected
    assert all(isinstance(key, str) for key in expected), expected
    return tuple(str(key) for key in expected)


def _no_check(rule_id: str, run: _Run, key: str) -> None:
    """Fail on an expectation value the guard cannot check, so no value is ever skipped."""
    pytest.fail(f"{run.scenario.name}: the catalogue guard has no {rule_id} check for {key!r}")


def _check_cr1(run: _Run) -> None:
    """CR1: ``fires`` is "the pair is distinguishable" (variant topics: "analysed")."""
    expect = run.scenario.expect
    values = expect.values
    analysed = bool(run.items(CR1))
    item: Candidate | None = None
    if analysed:
        pair = values.get("pair")
        item = run.single_pair() if pair is None else run.pair(*_keys(pair))
    if _topic(run.scenario) in VARIANT_TOPICS:
        fired = analysed
    else:
        assert item is not None, f"{run.scenario.name}: the copy campaign was not analysed"
        fired = item.evidence.extra["distinguishable"] is True
        if fired:
            assert not item.has_flag(FLAG_NOT_DISTINGUISHABLE)
    assert fired is expect.fires, f"{run.scenario.name}: CR1 fired={fired}"

    for key, expected in values.items():
        if key == "variants":
            if item is None:
                (line,) = run.withheld(COPY_CAMPAIGN)
                assert (line.rule_id, line.observed, line.unit) == (CR1, expected, "variants")
            else:
                assert {c.evidence.extra["variant_count"] for c in run.items()} == {expected}
            continue
        assert item is not None, f"{run.scenario.name}: CR1 value {key!r} needs an item"
        extra, test = item.evidence.extra, item.evidence.test
        match key:
            case "pair":
                assert set(extra["pair"]) == {run.urn(spec_key) for spec_key in _keys(expected)}
            case "distinguishable":
                assert extra["distinguishable"] is expected
            case "test":
                assert test is not None
                assert test.name == expected
            case "ahead":
                assert extra["ahead"] == run.urn(str(expected))
            case "p_value":
                assert test is not None
                assert _at_places(test.p_value, expected)
            case "p_adjusted":
                assert test is not None
                assert _at_places(test.p_adjusted, expected)
            case "sample_needed":
                assert extra["sample_needed"] == expected
            case "reason":
                assert extra["not_estimable_reason"] == expected
            case "excluded":
                excluded = [row["urn"] for row in extra["excluded"]]
                assert excluded == [run.urn(spec_key) for spec_key in _keys(expected)]
            case _:
                _no_check(CR1, run, key)
    if item is not None:
        _check_flags_and_label(item, expect, label=item.evidence.extra["label"])
    else:
        assert not expect.flags
        assert expect.label is None


def _check_cr2(run: _Run) -> None:
    """CR2: ``fires`` is "a stage is weak"; the CR2 sentence is the CR3 item's reason."""
    expect = run.scenario.expect
    item = run.cr3()
    extra = item.evidence.extra
    weak = extra["weak_stage"]
    fired = weak["weak"] is True and weak["exploratory"] is False
    assert fired is expect.fires, f"{run.scenario.name}: CR2 fired={fired}"
    for key, expected in expect.values.items():
        match key:
            case "stage":
                assert weak["stage"] == expected
            case "rate" | "baseline":
                assert _at_places(weak[key], expected), (key, weak[key], expected)
            case "stages":
                assert tuple(row["stage"] for row in extra["stages"]) == expected
            case _:
                _no_check(CR2, run, key)
    _check_flags_and_label(item, expect, label=item.reason)


def _check_cr3(run: _Run) -> None:
    """CR3: ``fires`` is "the next test's per-variant sample is estimable"."""
    expect = run.scenario.expect
    item = run.cr3()
    extra = item.evidence.extra
    next_test = extra["next_test"]
    assert next_test["stage"] == extra["weak_stage"]["stage"]
    fired = isinstance(next_test["sample_needed"], int)
    assert fired is expect.fires, f"{run.scenario.name}: CR3 fired={fired}"
    for key, expected in expect.values.items():
        match key:
            case "stage" | "variable" | "unit" | "projected_unit" | "sample_needed":
                assert next_test[key] == expected, (key, next_test[key], expected)
            case "reason":
                assert next_test["not_estimable_reason"] == expected
            case "observed_open_rate":
                assert _at_places(next_test["observed_open_rate"], expected)
            case _:
                _no_check(CR3, run, key)
    _check_flags_and_label(item, expect, label=item.action)


_CHECKS: Final[Mapping[str, Callable[[_Run], None]]] = {
    CR1: _check_cr1,
    CR2: _check_cr2,
    CR3: _check_cr3,
}
"""How each rule's expectation is read against ``creative.evaluate``."""


def _check_item_shape(run: _Run) -> None:
    """Every creative item: the copy section, one campaign block, read-only (Mark done) item."""
    for item in run.result.candidates:
        assert item.rule_id in (CR1, CR3)
        assert item.section == CREATIVE
        campaign_id = item.evidence.extra["campaign"]["id"]
        assert item.group == creative_group(campaign_id)
        assert item.required_datasets == REQUIRED_DATASETS
        assert item.decision_subject is None
        assert item.increases_spend_on == ()
        assert item.depends_on == (DecisionSubject("campaign", campaign_id),)
    assert run.result.findings == ()
    assert dict(run.result.suppressed) == {}


@pytest.mark.parametrize("scenario", CREATIVE_SCENARIOS, ids=lambda scenario: scenario.name)
def test_catalogue_guard_evaluate_matches_every_creative_expectation(
    scenario: Scenario, make_context: ContextFactory
) -> None:
    run = _evaluate(make_context, scenario)
    expect = scenario.expect
    assert expect.rule_id in _CHECKS, f"{scenario.name}: {expect.rule_id} is not a copy rule"
    _CHECKS[expect.rule_id](run)
    if expect.withheld_reason is not None:
        assert any(line.reason.startswith(expect.withheld_reason) for line in run.result.withheld)
    if expect.finding is not None:
        assert expect.finding in {finding.code for finding in run.result.findings}
    _check_item_shape(run)


def test_catalogue_offers_one_input_twins_for_cr1_and_cr2() -> None:
    twins = [scenario for scenario in CREATIVE_SCENARIOS if scenario.variant == "fires"]
    assert {scenario.expect.rule_id for scenario in twins} >= {CR1, CR2}
    for fires in twins:
        assert fires.pair is not None
        twin = SCENARIOS[fires.pair]
        assert (twin.variant, twin.pair) == ("does_not_fire", fires.name)
        assert twin.module == MODULE_CREATIVE
        assert fires.expect.fires is True
        assert twin.expect.fires is False
        assert len(input_differences(fires, twin)) == 1, input_differences(fires, twin)


# ---------------------------------------------------------------------------------------------
# Phase 2: CR1 distinguishability (leads / impressions; clicks / impressions outside lead gen)
# ---------------------------------------------------------------------------------------------


def _end_to_end(run: _Run, item: Candidate) -> tuple[int, int, int, int]:
    """``(x_a, n_a, x_b, n_b)`` of a CR1 pair, read from the context's window totals.

    The item's own metrics must cite the same counts, in variant order.
    """
    noun = item.evidence.extra["outcome"]
    by_urn = {info.urn: info.id for info in run.ctx.creatives.values()}
    counts: list[int] = []
    for urn in item.evidence.extra["pair"]:
        totals = run.ctx.creative_totals[by_urn[urn]]
        counts.extend((int(getattr(totals, noun)), totals.impressions))
    x_a, n_a, x_b, n_b = counts
    assert (evidence_metric(item, f"{noun}_a"), evidence_metric(item, "impressions_a")) == (
        x_a,
        n_a,
    )
    assert (evidence_metric(item, f"{noun}_b"), evidence_metric(item, "impressions_b")) == (
        x_b,
        n_b,
    )
    return x_a, n_a, x_b, n_b


def test_cr1_distinguishable_pair_is_labelled_with_its_adjusted_p_and_test(
    make_context: ContextFactory,
) -> None:
    run = _evaluate(make_context, "cr1.distinguishable.fires")
    cr = run.cr
    item = run.pair("cr-a", "cr-b")
    extra, test = item.evidence.extra, item.evidence.test
    assert test is not None
    assert extra["outcome"] == "leads"
    recomputed = stats.compare_proportions(
        *_end_to_end(run, item), fisher_min_expected=cr.fisher_min_expected
    )
    assert recomputed is not None
    assert (test.name, test.p_value) == (recomputed.method, recomputed.p_value)
    assert test.name == "two_proportion_z"
    assert test.p_adjusted == stats.holm([test.p_value], alpha=cr.alpha)[0]
    assert test.alpha == cr.alpha
    assert test.p_adjusted < cr.alpha

    label = f"Distinguishable (adjusted p = {_rendered_p(test.p_adjusted)})"
    assert extra["label"] == label
    assert item.action == f"{_title(item)}: {label}"
    assert extra["distinguishable"] is True
    assert extra["sample_needed"] is None
    assert not item.has_flag(FLAG_NOT_DISTINGUISHABLE)
    assert item.reason.endswith("the two rates are statistically distinguishable.")
    assert dict(item.evidence.thresholds) == run.ctx.thresholds.subset(*CR1_THRESHOLD_KEYS)


def test_cr1_ahead_but_not_distinguishable_states_the_impressions_still_needed(
    make_context: ContextFactory,
) -> None:
    run = _evaluate(make_context, "cr1.distinguishable.does_not_fire")
    cr = run.cr
    item = run.pair("cr-a", "cr-b")
    extra, test = item.evidence.extra, item.evidence.test
    assert test is not None
    assert test.p_adjusted is not None
    assert test.p_adjusted >= cr.alpha
    needed = stats.additional_n_for_difference(
        *_end_to_end(run, item), alpha=cr.alpha, power=cr.power
    )
    assert isinstance(needed, int)
    assert needed > 0

    leader, other = run.name("cr-a"), run.name("cr-b")
    label = (
        f"{leader} ahead, not yet distinguishable (adjusted p = {_rendered_p(test.p_adjusted)}); "
        f"about {needed:,} more impressions per variant needed at the observed difference"
    )
    assert extra["ahead"] == run.urn("cr-a")
    assert extra["label"] == label
    assert item.action == f"{leader} vs {other}: {label}"
    assert extra["sample_needed"] == needed
    assert evidence_metric(item, "additional_impressions_per_variant") == needed
    assert item.flag_codes == (FLAG_NOT_DISTINGUISHABLE,)
    assert item.reason.endswith("the gap is not yet distinguishable from noise.")
    # The twin moves one input across the threshold: cr-b's leads.
    fires = SCENARIOS["cr1.distinguishable.fires"]
    assert len(input_differences(fires, run.scenario)) == 1


def test_cr1_holm_adjustment_flips_a_raw_significant_pair_to_not_distinguishable(
    make_context: ContextFactory,
) -> None:
    run = _evaluate(make_context, "cr1.holm_flip.case")
    cr = run.cr
    assert len(run.variant_totals()) == 3
    items = run.items(CR1)
    assert len(items) == 3
    tests = [item.evidence.test for item in items]
    assert all(test is not None for test in tests)
    raw = [test.p_value for test in tests if test is not None and test.p_value is not None]
    adjusted = [test.p_adjusted for test in tests if test is not None]
    assert stats.holm(raw, alpha=cr.alpha) == adjusted

    flip = run.pair("cr-a", "cr-b")
    test = flip.evidence.test
    assert test is not None
    assert test.p_value is not None
    assert test.p_adjusted is not None
    assert test.p_value < cr.alpha <= test.p_adjusted
    recomputed = stats.compare_proportions(
        *_end_to_end(run, flip), fisher_min_expected=cr.fisher_min_expected
    )
    assert recomputed is not None
    assert recomputed.p_value == test.p_value
    assert flip.evidence.extra["distinguishable"] is False
    assert flip.has_flag(FLAG_NOT_DISTINGUISHABLE)
    assert "ahead, not yet distinguishable (adjusted p = " in flip.action
    assert "Holm-adjusted across 3 pairs" in flip.reason
    assert {item.evidence.extra["comparisons"] for item in items} == {3}


def test_cr1_significance_level_is_read_from_the_configuration(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    loosened = make_thresholds({"cr.alpha": Decimal("0.2")})
    run = _evaluate(make_context, "cr1.holm_flip.case", thresholds=loosened)
    item = run.pair("cr-a", "cr-b")
    test = item.evidence.test
    assert test is not None
    assert test.p_adjusted is not None
    assert test.alpha == run.cr.alpha == Decimal("0.2")
    assert test.p_adjusted < test.alpha
    assert item.evidence.extra["distinguishable"] is True
    assert item.evidence.extra["label"].startswith("Distinguishable (adjusted p = ")
    assert not item.has_flag(FLAG_NOT_DISTINGUISHABLE)
    assert item.evidence.thresholds["cr.alpha"] == 0.2


@pytest.mark.parametrize(
    ("name", "method", "label", "below_minimum"),
    [
        ("cr1.fisher.case", "fisher_exact", "Fisher's exact test", True),
        ("cr1.distinguishable.fires", "two_proportion_z", "two-proportion z-test", False),
    ],
)
def test_cr1_uses_fisher_below_five_expected_and_the_z_test_otherwise(
    make_context: ContextFactory, name: str, method: str, label: str, below_minimum: bool
) -> None:
    run = _evaluate(make_context, name)
    cr = run.cr
    item = run.single_pair()
    test = item.evidence.test
    assert test is not None
    expected_min = evidence_metric(item, "expected_min_cell")
    assert isinstance(expected_min, Decimal)
    assert (expected_min < cr.fisher_min_expected) is below_minimum
    recomputed = stats.compare_proportions(
        *_end_to_end(run, item), fisher_min_expected=cr.fisher_min_expected
    )
    assert recomputed is not None
    assert (recomputed.method, recomputed.expected_min) == (method, expected_min)
    assert test.name == item.evidence.extra["test"]["method"] == method
    assert item.evidence.extra["test"]["label"] == label
    assert f"{label} p = {_rendered_p(recomputed.p_value)}" in item.reason


def test_cr1_identical_rates_leave_the_impressions_needed_not_estimable(
    make_context: ContextFactory,
) -> None:
    run = _evaluate(make_context, "cr1.identical_rates.case")
    cr = run.cr
    item = run.single_pair()
    extra = item.evidence.extra
    counts = _end_to_end(run, item)
    assert stats.additional_n_for_difference(*counts, alpha=cr.alpha, power=cr.power) is None
    assert extra["ahead"] is None
    assert extra["label"].endswith(
        f"more impressions per variant needed: {NOT_ESTIMABLE} ({NOT_ESTIMABLE_NO_DIFFERENCE})"
    )
    assert "about" not in extra["label"]
    assert extra["sample_needed"] == NOT_ESTIMABLE
    assert extra["not_estimable_reason"] == NOT_ESTIMABLE_NO_DIFFERENCE
    assert evidence_metric(item, "additional_impressions_per_variant") == NOT_ESTIMABLE
    assert item.has_flag(FLAG_NOT_DISTINGUISHABLE)


def test_cr1_zero_impression_creative_is_excluded_and_its_comparison_not_estimable(
    make_context: ContextFactory,
) -> None:
    run = _evaluate(make_context, "cr1.zero_impressions.case")
    cr = run.cr
    item = run.single_pair()
    extra = item.evidence.extra
    assert set(extra["pair"]) == {run.urn("cr-a"), run.urn("cr-b")}
    assert extra["variant_count"] == 2
    excluded = {row["urn"]: row["impressions"] for row in extra["excluded"]}
    assert excluded == {run.urn("cr-c"): 0}

    # cr.min_impressions >= 1 keeps a zero-impression creative from ever being a variant, so
    # its "not estimable" comparison is reached only on the module's own pair path.
    assert cr.min_impressions >= 1
    assert stats.compare_proportions(10, 1_000, 0, 0) is None
    assert (
        stats.additional_n_for_difference(10, 1_000, 0, 0, alpha=cr.alpha, power=cr.power) is None
    )
    config = CreativeConfig.from_thresholds(run.ctx.thresholds)
    (campaign,) = (
        c for c in creative._campaigns(run.ctx, config) if c.info.id == run.campaign_id()
    )
    (info,) = (info for info in run.ctx.creatives.values() if info.urn == run.urn("cr-c"))
    silent = CreativeTotals(info.id, info.campaign_id, Decimal(0), 0, 0, 0, 0, 0, 0, 0)
    zero = creative._Creative(info.id, info.urn, run.name("cr-c"), info, silent)
    widened = replace(campaign, variants=(*campaign.variants, zero))
    pairs = creative._pairs(widened, config)
    untestable = [pair for pair in pairs if pair.test is None]
    assert len(untestable) == 2
    assert {pair.unavailable for pair in untestable} == {NOT_ESTIMABLE_ZERO_IMPRESSIONS}
    compared = sum(1 for pair in pairs if pair.test is not None)
    candidate = creative._pair_candidate(run.ctx, widened, untestable[0], compared, config)
    detail = candidate.evidence.extra
    assert candidate.evidence.test is None
    assert detail["label"] == (
        f"Not yet distinguishable; comparison {NOT_ESTIMABLE} ({NOT_ESTIMABLE_ZERO_IMPRESSIONS})"
    )
    assert detail["sample_needed"] == NOT_ESTIMABLE
    assert detail["not_estimable_reason"] == NOT_ESTIMABLE_ZERO_IMPRESSIONS
    assert candidate.flag_codes == (FLAG_NOT_DISTINGUISHABLE,)
    assert candidate.reason.endswith(f"could not be tested ({NOT_ESTIMABLE_ZERO_IMPRESSIONS}).")


def test_p7_no_cr1_item_calls_a_variant_a_winner_before_it_is_distinguishable(
    make_context: ContextFactory,
) -> None:
    names = [scenario.name for scenario in CREATIVE_SCENARIOS] + [P7_CASE, DEFAULT]
    checked: dict[str, int] = dict.fromkeys(names, 0)
    for name in names:
        run = _evaluate(make_context, name)
        for item in run.result.candidates:
            if item.rule_id != CR1 or item.evidence.extra["distinguishable"]:
                continue
            assert item.has_flag(FLAG_NOT_DISTINGUISHABLE), (name, item.action)
            for text in (item.action, item.reason, item.evidence.extra["label"]):
                found = WINNER_WORDING.search(_without_names(text, item))
                assert found is None, (name, text)
            checked[name] += 1
    # The P7 case's pair is ahead on a small difference, and the default world has such pairs.
    assert checked[P7_CASE] == 1
    assert checked[DEFAULT] >= 1
    assert sum(checked.values()) > len(names) // 2


# ---------------------------------------------------------------------------------------------
# Phase 3: CR2 weak stage
# ---------------------------------------------------------------------------------------------


def test_cr2_stage_definitions_match_the_aap_funnel() -> None:
    assert [stage.key for stage in STAGES] == list(AAP_STAGE_COUNTS)
    for stage in STAGES:
        assert (stage.numerator, stage.denominator) == AAP_STAGE_COUNTS[stage.key]
    units = {stage.key: stage.trial_unit for stage in STAGES}
    assert units == {
        "click_rate": "impressions",
        "open_rate": "impressions",
        "completion_rate": "form opens",
    }


@pytest.mark.parametrize(
    "name", ["cr2.weak_stage.fires", "cr3.completion.case", "cr1.holm_flip.case"]
)
def test_cr2_stage_rates_use_each_stage_own_denominator_against_the_account_baseline(
    make_context: ContextFactory, name: str
) -> None:
    run = _evaluate(make_context, name)
    item = run.cr3()
    rows = _stage_rows(item)
    assert list(rows) == list(AAP_STAGE_COUNTS)
    variants, account = run.variant_totals(), run.baseline_totals()
    # The baseline pools every creative of the account and objective, beyond the campaign's.
    assert len(account) > len(variants)
    for stage, (numerator, denominator) in AAP_STAGE_COUNTS.items():
        row = rows[stage]
        successes, trials = _pooled(variants, numerator, denominator)
        assert (row["successes"], row["trials"]) == (successes, trials)
        assert row["rate"] == _rate(successes, trials)
        assert row["baseline"] == _rate(*_pooled(account, numerator, denominator))
        assert row["unit"] == ("form opens" if denominator == "form_opens" else "impressions")
        assert evidence_metric(item, stage) == row["rate"]
    baselines = {entry.key: entry.value for entry in item.evidence.baselines}
    assert baselines == {f"{stage}_baseline": rows[stage]["baseline"] for stage in rows}


def test_cr2_non_lead_gen_campaign_uses_the_click_rate_only(
    make_context: ContextFactory,
) -> None:
    run = _evaluate(make_context, "cr2.click_only.case")
    campaign = run.ctx.campaigns[run.campaign_id()]
    assert campaign.objective_group != OBJ_LINKEDIN_LEAD_GEN
    item = run.cr3()
    rows = _stage_rows(item)
    assert list(rows) == ["click_rate"]
    clicks, impressions = _pooled(run.variant_totals(), "clicks", "impressions")
    assert rows["click_rate"]["rate"] == _rate(clicks, impressions)
    assert item.evidence.extra["next_test"]["stage"] == "click_rate"
    for row in item.evidence.extra["variants"]:
        assert row["click_rate"] == _rate(row["clicks"], row["impressions"])
        assert row["open_rate"] is None
        assert row["completion_rate"] is None

    # CR1's end-to-end rate is clicks / impressions: the variants have no leads at all, so a
    # lead-rate comparison would be level, yet their click rates are distinguishable.
    pair = run.single_pair()
    assert all(totals.leads == 0 for totals in run.variant_totals())
    assert pair.evidence.extra["outcome"] == "clicks"
    assert pair.reason.startswith("Click rate (clicks ÷ impressions): ")
    cr = run.cr
    recomputed = stats.compare_proportions(
        *_end_to_end(run, pair), fisher_min_expected=cr.fisher_min_expected
    )
    assert recomputed is not None
    assert pair.evidence.test is not None
    assert pair.evidence.test.p_value == recomputed.p_value
    assert pair.evidence.extra["distinguishable"] is True


def test_cr2_weak_stage_has_its_wilson_upper_bound_below_the_account_baseline(
    make_context: ContextFactory,
) -> None:
    run = _evaluate(make_context, "cr2.weak_stage.fires")
    cr = run.cr
    item = run.cr3()
    weak = item.evidence.extra["weak_stage"]
    row = _stage_rows(item)[weak["stage"]]
    interval = stats.wilson(row["successes"], row["trials"], confidence=cr.ci_level)
    assert interval is not None
    assert (interval.point, interval.low, interval.high) == (
        row["rate"],
        row["ci_low"],
        row["ci_high"],
    )
    assert (weak["rate"], tuple(weak["interval"])) == (
        interval.point,
        (interval.low, interval.high),
    )
    assert interval.high < row["baseline"] == weak["baseline"]
    assert row["weak"] is weak["weak"] is True
    assert weak["exploratory"] is False
    assert evidence_metric(item, f"{weak['stage']}_ci_low") == interval.low
    assert evidence_metric(item, f"{weak['stage']}_ci_high") == interval.high

    shown = WEAK_STAGE_REASON.fullmatch(item.reason)
    assert shown is not None, item.reason
    assert shown["stage"] == weak["label"] == "Click rate"
    assert _shows(shown["rate"], interval.point)
    assert _shows(shown["low"], interval.low)
    assert _shows(shown["high"], interval.high)
    assert _shows(shown["baseline"], weak["baseline"])
    assert dict(item.evidence.thresholds) == run.ctx.thresholds.subset(*CR3_THRESHOLD_KEYS)


def test_cr2_twin_with_no_stage_below_its_baseline_reports_none(
    make_context: ContextFactory,
) -> None:
    run = _evaluate(make_context, "cr2.weak_stage.does_not_fire")
    item = run.cr3()
    assert item.reason == NO_WEAK_STAGE_TEXT == "No stage measurably below baseline"
    for row in item.evidence.extra["stages"]:
        assert row["weak"] is False
        assert row["ci_high"] >= row["baseline"]
    assert item.evidence.extra["weak_stage"]["exploratory"] is True
    # The twin moves one input: the baseline campaign's click rate.
    fires = SCENARIOS["cr2.weak_stage.fires"]
    assert len(input_differences(fires, run.scenario)) == 1


def test_cr2_reports_the_largest_relative_shortfall_among_weak_stages(
    make_context: ContextFactory,
) -> None:
    order = list(AAP_STAGE_COUNTS)
    several_weak = 0
    not_first_weak = 0
    for name in [DEFAULT, *(scenario.name for scenario in CREATIVE_SCENARIOS)]:
        run = _evaluate(make_context, name)
        for item in run.result.candidates:
            if item.rule_id != CR3:
                continue
            weak_rows = [row for row in item.evidence.extra["stages"] if row["weak"]]
            reported = item.evidence.extra["weak_stage"]
            if not weak_rows:
                assert reported["exploratory"] is True
                continue
            for row in weak_rows:
                expected = ((row["baseline"] - row["rate"]) / row["baseline"]).quantize(RATE_PLACES)
                assert row["shortfall"] == expected
            largest = max(weak_rows, key=lambda row: (row["shortfall"], -order.index(row["stage"])))
            assert reported["stage"] == largest["stage"], (name, item.entity_ref.name)
            assert reported["exploratory"] is False
            if len(weak_rows) > 1:
                several_weak += 1
                not_first_weak += reported["stage"] != weak_rows[0]["stage"]
    # The default world has campaigns with two and three weak stages, and in at least one the
    # largest shortfall is not the first weak stage of the funnel.
    assert several_weak >= 2
    assert not_first_weak >= 1


# ---------------------------------------------------------------------------------------------
# Phase 4: CR3 next test
# ---------------------------------------------------------------------------------------------


def _aap_levels(cr: _Cr) -> None:
    """The run is configured at the AAP's 20% lift, alpha 0.05 and power 0.8, as texts say."""
    assert (cr.target_lift, cr.alpha, cr.power) == (Decimal("0.2"), Decimal("0.05"), Decimal("0.8"))


def _required(cr: _Cr, baseline: Decimal) -> int | None:
    """``stats.required_n`` for a ``cr.target_lift`` relative lift of ``baseline``."""
    target = baseline * (Decimal(1) + cr.target_lift)
    return stats.required_n(baseline, target, alpha=cr.alpha, power=cr.power)


def test_cr3_stage_variables_are_the_aap_mapping() -> None:
    assert dict(STAGE_VARIABLE) == AAP_STAGE_VARIABLE
    labels = dict(TEST_ELEMENTS)
    assert {key: labels[key] for key in STAGE_VARIABLE_KEY.values()} == {
        STAGE_VARIABLE_KEY[stage]: label for stage, label in AAP_STAGE_VARIABLE.items()
    }
    assert {"intro text", "offer"} <= set(labels.values())
    assert NEXT_TEST_VARIANTS == 2


@pytest.mark.parametrize(
    ("name", "stage"),
    [
        ("cr3.click.case", "click_rate"),
        ("cr3.open.case", "open_rate"),
        ("cr3.completion.case", "completion_rate"),
    ],
)
def test_cr3_next_test_changes_exactly_one_variable_for_its_stage(
    make_context: ContextFactory, name: str, stage: str
) -> None:
    run = _evaluate(make_context, name)
    item = run.cr3()
    next_test = item.evidence.extra["next_test"]
    assert next_test["stage"] == item.evidence.extra["weak_stage"]["stage"] == stage
    variable = AAP_STAGE_VARIABLE[stage]
    assert (next_test["variable"], next_test["variable_label"]) == (
        STAGE_VARIABLE_KEY[stage],
        variable,
    )
    held = [label for key, label in TEST_ELEMENTS if key != next_test["variable"]]
    assert list(next_test["keep_unchanged"]) == held
    assert {"intro text", "offer"} <= set(held)
    assert next_test["variants"] == NEXT_TEST_VARIANTS
    assert item.action.startswith(
        f"Next test: change only {variable}; keep everything else identical; 2 variants; "
    )
    # No second variable is named anywhere in the action.
    for label in held:
        assert re.search(rf"\b{re.escape(label)}\b", item.action) is None, (label, item.action)
    assert item.rank_key == (0,)


@pytest.mark.parametrize("name", ["cr2.weak_stage.does_not_fire", "cr1.fisher.case"])
def test_cr3_without_a_weak_stage_tests_the_lowest_stage_relative_to_baseline_as_exploratory(
    make_context: ContextFactory, name: str
) -> None:
    run = _evaluate(make_context, name)
    item = run.cr3()
    rows = item.evidence.extra["stages"]
    assert not any(row["weak"] for row in rows)
    order = list(AAP_STAGE_COUNTS)
    relative = {
        row["stage"]: (row["rate"] / row["baseline"]).quantize(RATE_PLACES)
        for row in rows
        if row["rate"] is not None and row["baseline"]
    }
    lowest = min(relative, key=lambda stage: (relative[stage], order.index(stage)))
    next_test = item.evidence.extra["next_test"]
    assert next_test["stage"] == lowest
    assert next_test["exploratory"] is True
    assert item.evidence.extra["weak_stage"]["exploratory"] is True
    assert item.reason == NO_WEAK_STAGE_TEXT
    assert item.action.endswith(f"; {EXPLORATORY_TEXT}")
    assert "exploratory" in item.action


@pytest.mark.parametrize("name", ["cr3.click.case", "cr3.open.case"])
def test_cr3_click_and_open_rate_tests_are_sized_in_impressions_per_variant(
    make_context: ContextFactory, name: str
) -> None:
    run = _evaluate(make_context, name)
    cr = run.cr
    _aap_levels(cr)
    item = run.cr3()
    next_test = item.evidence.extra["next_test"]
    stage = next_test["stage"]
    baseline = next_test["baseline"]
    assert baseline == _stage_rows(item)[stage]["baseline"]
    needed = _required(cr, baseline)
    assert isinstance(needed, int)
    assert next_test["sample_needed"] == needed
    assert next_test["unit"] == "impressions"
    assert next_test["target"] == (baseline * (Decimal(1) + cr.target_lift)).quantize(RATE_PLACES)
    assert next_test["projected"] is None
    days = math.ceil(Decimal(needed) / _daily_delivery(run.variant_totals()))
    assert next_test["days"] == days
    assert f"; {needed:,} impressions per variant {AAP_NEXT_TEST_TEXT}" in item.action
    assert item.action.endswith(f"; ≈ {days:,} days at current per-variant delivery (projection)")
    sample = item.evidence.metric("sample_per_variant")
    assert (sample.value, sample.unit) == (needed, "impressions")
    assert evidence_metric(item, "projected_days") == days


def test_cr3_completion_rate_test_is_sized_in_form_opens_and_projected_to_impressions(
    make_context: ContextFactory,
) -> None:
    run = _evaluate(make_context, "cr3.completion.case")
    cr = run.cr
    _aap_levels(cr)
    item = run.cr3()
    next_test = item.evidence.extra["next_test"]
    assert next_test["stage"] == "completion_rate"
    needed = _required(cr, next_test["baseline"])
    assert isinstance(needed, int)
    assert (next_test["sample_needed"], next_test["unit"]) == (needed, "form opens")
    variants = run.variant_totals()
    opens, impressions = _pooled(variants, "form_opens", "impressions")
    assert next_test["observed_open_rate"] == _rate(opens, impressions)
    projected = -(-needed * impressions // opens)
    assert projected == math.ceil(Decimal(needed) / (Decimal(opens) / Decimal(impressions)))
    # Priority decision (creative.py): the projection travels in extra.next_test and the
    # metrics; evidence.sample stays the variant gate, never an unmet P6 minimum.
    assert dict(next_test["projected"]) == {
        "value": projected,
        "unit": "impressions",
        "basis": PROJECTION_BASIS,
    }
    assert next_test["projected_unit"] == "impressions"
    days = math.ceil(Decimal(projected) / _daily_delivery(variants))
    assert next_test["days"] == days

    pieces = [
        f"; {needed:,} form opens per variant {AAP_NEXT_TEST_TEXT}",
        f"; ≈ {projected:,} impressions per variant ({PROJECTION_BASIS})",
        f"; ≈ {days:,} days at current per-variant delivery (projection)",
    ]
    positions = [item.action.find(piece) for piece in pieces]
    assert -1 not in positions, item.action
    assert positions == sorted(positions)
    sample = item.evidence.metric("sample_per_variant")
    assert (sample.value, sample.unit) == (needed, "form opens")
    projection = item.evidence.metric("projected_impressions_per_variant")
    assert (projection.value, projection.unit) == (projected, "impressions")
    gate = item.evidence.sample
    assert gate is not None
    assert (gate.observed, gate.required, gate.unit) == (
        min(row.impressions for row in variants),
        cr.min_impressions,
        "impressions",
    )
    assert gate.projected is None


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("cr3.not_estimable_zero_baseline.case", NOT_ESTIMABLE_ZERO_BASELINE),
        ("cr3.not_estimable_target_over_100.case", NOT_ESTIMABLE_TARGET_ABOVE_100),
    ],
)
def test_cr3_sample_is_not_estimable_with_its_reason(
    make_context: ContextFactory, name: str, reason: str
) -> None:
    run = _evaluate(make_context, name)
    cr = run.cr
    item = run.cr3()
    next_test = item.evidence.extra["next_test"]
    baseline = next_test["baseline"]
    if reason == NOT_ESTIMABLE_ZERO_BASELINE:
        assert baseline == 0
    else:
        assert baseline * (Decimal(1) + cr.target_lift) > 1
    assert _required(cr, baseline) is None
    assert next_test["sample_needed"] == NOT_ESTIMABLE
    assert next_test["not_estimable_reason"] == reason
    assert next_test["projected"] is None
    assert next_test["days"] is None
    assert f"; sample per variant {NOT_ESTIMABLE} ({reason})" in item.action
    assert evidence_metric(item, "sample_per_variant") == NOT_ESTIMABLE
    assert "projected_days" not in {metric.key for metric in item.evidence.metrics}


def test_cr3_stage_with_zero_exposure_is_never_tested(make_context: ContextFactory) -> None:
    run = _evaluate(make_context, "cr3.zero_exposure.case")
    item = run.cr3()
    rows = _stage_rows(item)
    completion = rows["completion_rate"]
    assert (completion["trials"], completion["rate"]) == (0, None)
    assert completion["unmeasured"] == "no form opens"
    # With no form opens the 0% open rate is the weak stage: change the CTA label.
    assert rows["open_rate"]["rate"] == 0
    assert item.evidence.extra["next_test"]["stage"] == "open_rate"
    assert item.evidence.extra["next_test"]["variable"] == "cta_label"

    # The module's sizing helper reports a test of the unexposed stage as not estimable.
    config = CreativeConfig.from_thresholds(run.ctx.thresholds)
    impressions = sum(row.impressions for row in run.variant_totals())
    plan = plan_next_test(
        "completion_rate",
        baseline=completion["baseline"],
        exposure=0,
        daily_impressions_per_variant=_daily_delivery(run.variant_totals()),
        config=config,
        open_counts=(0, impressions),
    )
    assert plan.not_estimable_reason == NOT_ESTIMABLE_ZERO_EXPOSURE
    assert (plan.per_variant, plan.projected_impressions, plan.days) == (None, None, None)
    assert plan.action(config) == (
        "Next test: change only number of form questions; keep everything else identical; "
        f"2 variants; sample per variant {NOT_ESTIMABLE} ({NOT_ESTIMABLE_ZERO_EXPOSURE})"
    )


def test_cr3_sizing_helper_names_every_missing_input(thresholds: Thresholds) -> None:
    config = CreativeConfig.from_thresholds(thresholds)
    baseline = Decimal("0.2")
    needed = stats.required_n(
        baseline,
        baseline * (Decimal(1) + config.target_lift),
        alpha=config.alpha,
        power=config.power,
    )
    assert isinstance(needed, int)

    unavailable = plan_next_test(
        "click_rate",
        baseline=None,
        exposure=40_000,
        daily_impressions_per_variant=Decimal(700),
        config=config,
        exploratory=True,
    )
    assert unavailable.not_estimable_reason == NOT_ESTIMABLE_BASELINE_UNAVAILABLE
    assert unavailable.action(config).endswith(
        f"sample per variant {NOT_ESTIMABLE} ({NOT_ESTIMABLE_BASELINE_UNAVAILABLE}); "
        f"{EXPLORATORY_TEXT}"
    )

    no_open_rate = plan_next_test(
        "completion_rate",
        baseline=baseline,
        exposure=800,
        daily_impressions_per_variant=Decimal(700),
        config=config,
    )
    assert (no_open_rate.per_variant, no_open_rate.unit) == (needed, "form opens")
    assert no_open_rate.projected_impressions is None
    assert no_open_rate.projection_reason == NOT_ESTIMABLE_ZERO_EXPOSURE
    assert no_open_rate.action(config).endswith(
        f"; delivery projection {NOT_ESTIMABLE} ({NOT_ESTIMABLE_ZERO_EXPOSURE})"
    )

    no_delivery = plan_next_test(
        "click_rate",
        baseline=baseline,
        exposure=40_000,
        daily_impressions_per_variant=None,
        config=config,
    )
    assert (no_delivery.per_variant, no_delivery.days) == (needed, None)
    assert no_delivery.projection_reason == NOT_ESTIMABLE_ZERO_EXPOSURE
    assert no_delivery.to_json()["sample_needed"] == needed

    with pytest.raises(KeyError):
        plan_next_test(
            "form_rate",  # type: ignore[arg-type]
            baseline=baseline,
            exposure=1,
            daily_impressions_per_variant=Decimal(1),
            config=config,
        )


# ---------------------------------------------------------------------------------------------
# Phase 5: variants and ordering
# ---------------------------------------------------------------------------------------------


def test_variants_minimum_admits_1000_impressions_and_excludes_999(
    make_context: ContextFactory,
) -> None:
    admitted = _evaluate(make_context, "cr.variants.fires")
    excluded = _evaluate(make_context, "cr.variants.does_not_fire")
    cr = admitted.cr
    assert cr.min_impressions == 1_000
    assert admitted.totals("cr-b").impressions == cr.min_impressions
    assert excluded.totals("cr-b").impressions == cr.min_impressions - 1

    variant_urns = [row["urn"] for row in admitted.cr3().evidence.extra["variants"]]
    assert variant_urns == [admitted.urn("cr-a"), admitted.urn("cr-b")]
    assert set(admitted.single_pair().evidence.extra["pair"]) == set(variant_urns)

    assert excluded.items() == []
    (line,) = excluded.withheld(COPY_CAMPAIGN)
    assert (line.rule_id, line.section, line.unit) == (CR1, CREATIVE, "variants")
    assert (line.observed, line.required) == (1, cr.min_variants)
    assert line.reason.startswith(f"variants 1 of {cr.min_variants} required")
    assert len(input_differences(admitted.scenario, excluded.scenario)) == 1


def test_variants_minimum_is_read_from_the_configuration(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    lowered = make_thresholds({"cr.min_impressions": 999})
    run = _evaluate(make_context, "cr.variants.does_not_fire", thresholds=lowered)
    assert run.cr.min_impressions == 999
    item = run.cr3()
    assert item.evidence.extra["variant_count"] == 2
    assert item.evidence.thresholds["cr.min_impressions"] == 999
    assert run.withheld(COPY_CAMPAIGN) == []


@pytest.mark.parametrize("scenario", CREATIVE_SCENARIOS, ids=lambda scenario: scenario.name)
def test_campaign_with_one_variant_has_no_group_and_is_withheld(
    scenario: Scenario, make_context: ContextFactory
) -> None:
    run = _evaluate(make_context, scenario)
    assert run.items(campaign=BASELINE_CAMPAIGN) == []
    (line,) = run.withheld(BASELINE_CAMPAIGN)
    assert (line.rule_id, line.observed, line.required, line.unit) == (
        CR1,
        1,
        run.cr.min_variants,
        "variants",
    )


@pytest.mark.parametrize("name", [DEFAULT, "cr1.holm_flip.case"])
def test_campaign_block_holds_its_cr3_item_first_then_every_variant_pair(
    make_context: ContextFactory, name: str
) -> None:
    run = _evaluate(make_context, name)
    groups: dict[str, list[Candidate]] = {}
    for item in run.result.candidates:
        groups.setdefault(item.group, []).append(item)
    spends: dict[str, Decimal] = {}
    for group, items in groups.items():
        (test_item,) = (item for item in items if item.rule_id == CR3)
        campaign_id = test_item.entity_ref.id
        assert campaign_id is not None
        assert group == creative_group(campaign_id)
        ordered = sorted(items, key=lambda item: item.sort_key)
        assert ordered[0] is test_item
        pairs = ordered[1:]
        assert all(item.rule_id == CR1 for item in pairs)
        ranks = [item.rank_key[1] for item in pairs]
        assert ranks == sorted(ranks)
        variants = test_item.evidence.extra["variant_count"]
        assert len(pairs) == variants * (variants - 1) // 2
        keys = {frozenset(item.evidence.extra["pair"]) for item in pairs}
        assert len(keys) == len(pairs)
        spend = sum(
            (
                row.cost
                for row in run.ctx.creative_totals.values()
                if row.campaign_id == campaign_id
            ),
            Decimal(0),
        )
        assert {item.evidence.extra["campaign_spend"] for item in items} == {spend}
        spends[group] = spend
    if name == DEFAULT:
        # Several campaign blocks with distinct spends: ranking them by spend is a total order.
        assert len(spends) >= 2
        assert len(set(spends.values())) == len(spends)


@pytest.mark.parametrize("name", ["cr3.completion.case", "cr2.click_only.case"])
def test_variants_table_lists_impressions_stage_rates_and_leads_per_variant(
    make_context: ContextFactory, name: str
) -> None:
    run = _evaluate(make_context, name)
    item = run.cr3()
    lead_gen = run.ctx.campaigns[run.campaign_id()].objective_group == OBJ_LINKEDIN_LEAD_GEN
    rows = item.evidence.extra["variants"]
    assert [row["urn"] for row in rows] == [run.urn("cr-a"), run.urn("cr-b")]
    for key, row in zip(("cr-a", "cr-b"), rows, strict=True):
        totals = run.totals(key)
        assert row["name"] == run.name(key)
        assert (row["impressions"], row["clicks"], row["form_opens"], row["leads"]) == (
            totals.impressions,
            totals.clicks,
            totals.form_opens,
            totals.leads,
        )
        assert totals.form_opens is not None
        assert totals.leads is not None
        assert row["click_rate"] == _rate(totals.clicks, totals.impressions)
        if lead_gen:
            assert row["open_rate"] == _rate(totals.form_opens, totals.impressions)
            assert row["completion_rate"] == _rate(totals.leads, totals.form_opens)
        else:
            assert (row["open_rate"], row["completion_rate"]) == (None, None)


# ---------------------------------------------------------------------------------------------
# Unavailable and inconsistent platform counts (AAP 0.11.1: absent values are "unavailable")
# ---------------------------------------------------------------------------------------------


def _with_totals(run: _Run, changes: Mapping[str, Mapping[str, Any]]) -> _Run:
    """The run re-evaluated with some creatives' window totals as ``load_context`` reads them.

    ``CreativeTotals`` holds ``None`` for a count a window day did not report; the simulator
    always reports every count, so these states are set on the scenario's own context.
    """
    totals = dict(run.ctx.creative_totals)
    for creative_key, fields in changes.items():
        row = run.totals(creative_key)
        totals[row.creative_id] = replace(row, **fields)
    ctx = replace(run.ctx, creative_totals=totals)
    return _Run(run.scenario, ctx, creative.evaluate(ctx))


def test_unreported_form_opens_leave_their_stages_unmeasured_never_zero(
    make_context: ContextFactory,
) -> None:
    run = _with_totals(
        _evaluate(make_context, "cr3.completion.case"), {"cr-a": {"form_opens": None}}
    )
    item = run.cr3()
    rows = _stage_rows(item)
    assert rows["open_rate"]["unmeasured"] == (
        "form opens or impressions not reported for every variant"
    )
    assert rows["completion_rate"]["unmeasured"] == (
        "leads or form opens not reported for every variant"
    )
    for stage in ("open_rate", "completion_rate"):
        assert (rows[stage]["rate"], rows[stage]["ci_high"], rows[stage]["weak"]) == (
            None,
            None,
            False,
        )
        assert evidence_metric(item, stage) is None
    # The account baseline pools only the creatives that reported the stage's counts.
    reported = [row for row in run.baseline_totals() if row.form_opens is not None]
    assert rows["open_rate"]["baseline"] == _rate(*_pooled(reported, "form_opens", "impressions"))
    # Only the click rate is measured, so the next test is the click-rate test.
    assert item.evidence.extra["next_test"]["stage"] == "click_rate"
    variant = next(row for row in item.evidence.extra["variants"] if row["urn"] == run.urn("cr-a"))
    assert (variant["form_opens"], variant["open_rate"], variant["completion_rate"]) == (
        None,
        None,
        None,
    )


def test_unreported_leads_make_the_pair_untestable_and_say_why(
    make_context: ContextFactory,
) -> None:
    run = _with_totals(
        _evaluate(make_context, "cr1.distinguishable.fires"), {"cr-b": {"leads": None}}
    )
    item = run.single_pair()
    extra = item.evidence.extra
    assert item.evidence.test is None
    assert extra["distinguishable"] is False
    assert (
        extra["label"]
        == f"Not yet distinguishable; comparison {NOT_ESTIMABLE} (leads not reported)"
    )
    assert extra["sample_needed"] == NOT_ESTIMABLE
    assert f"{run.name('cr-b')} unavailable" in item.reason
    assert item.reason.endswith("the pair could not be tested (leads not reported).")
    assert item.flag_codes == (FLAG_NOT_DISTINGUISHABLE,)
    assert evidence_metric(item, "rate_b") is None


def test_inconsistent_counts_are_never_compared(make_context: ContextFactory) -> None:
    base = _evaluate(make_context, "cr1.distinguishable.fires")
    impressions = base.totals("cr-a").impressions
    run = _with_totals(base, {"cr-a": {"leads": impressions + 1}})
    pair = run.single_pair()
    reason = "inconsistent counts: leads exceed impressions"
    assert pair.evidence.test is None
    assert pair.evidence.extra["not_estimable_reason"] == reason
    assert pair.evidence.extra["label"].endswith(f"comparison {NOT_ESTIMABLE} ({reason})")
    completion = _stage_rows(run.cr3())["completion_rate"]
    assert completion["rate"] is None
    assert str(completion["unmeasured"]).startswith("inconsistent counts: ")
    assert str(completion["unmeasured"]).endswith(" form opens")


def test_next_test_is_withheld_when_no_stage_can_be_compared_with_a_baseline(
    make_context: ContextFactory,
) -> None:
    base = _evaluate(make_context, "cr1.distinguishable.fires")
    pooled_impressions = sum(row.impressions for row in base.variant_totals())
    run = _with_totals(base, {"cr-a": {"clicks": pooled_impressions + 1, "form_opens": None}})
    assert run.items(CR3) == []
    assert len(run.items(CR1)) == 1
    lines = [line for line in run.withheld(COPY_CAMPAIGN) if line.rule_id == CR3]
    assert len(lines) == 1
    line = lines[0]
    assert line.reason == "no funnel stage could be measured against an account baseline"
    assert (line.observed, line.required, line.unit, line.section) == (0, 1, "stages", CREATIVE)


def test_delivery_projection_is_not_estimable_without_delivery_days(
    make_context: ContextFactory,
) -> None:
    run = _with_totals(
        _evaluate(make_context, "cr3.click.case"),
        {"cr-a": {"delivery_days": 0}, "cr-b": {"delivery_days": 0}},
    )
    item = run.cr3()
    next_test = item.evidence.extra["next_test"]
    assert isinstance(next_test["sample_needed"], int)
    assert next_test["days"] is None
    assert next_test["projection_reason"] == NOT_ESTIMABLE_ZERO_EXPOSURE
    assert item.action.endswith(
        f"; delivery projection {NOT_ESTIMABLE} ({NOT_ESTIMABLE_ZERO_EXPOSURE})"
    )
    assert evidence_metric(item, "projected_days") == NOT_ESTIMABLE


def test_cr3_sample_is_not_estimable_when_no_sample_reaches_the_power(
    thresholds: Thresholds,
) -> None:
    config = CreativeConfig.from_thresholds(thresholds)
    reachable = plan_next_test(
        "click_rate",
        baseline=Decimal("0.012"),
        exposure=40_000,
        daily_impressions_per_variant=Decimal(700),
        config=config,
    )
    assert reachable.estimable is True
    # A rate this small gives no effect a test of any reachable size can detect.
    unreachable = plan_next_test(
        "click_rate",
        baseline=Decimal("1E-200"),
        exposure=40_000,
        daily_impressions_per_variant=Decimal(700),
        config=config,
    )
    assert unreachable.estimable is False
    assert unreachable.not_estimable_reason == creative.NOT_ESTIMABLE_POWER_UNREACHABLE
    assert unreachable.to_json()["sample_needed"] == NOT_ESTIMABLE


_VALID_CONFIG: Final[Mapping[str, Any]] = {
    "min_impressions": 1_000,
    "min_variants": 2,
    "alpha": Decimal("0.05"),
    "power": Decimal("0.8"),
    "target_lift": Decimal("0.20"),
    "fisher_min_expected": Decimal(5),
    "ci_level": Decimal("0.95"),
}
"""``CreativeConfig`` fields at the shipped values."""


def test_creative_config_reads_the_shipped_cr_thresholds(thresholds: Thresholds) -> None:
    assert CreativeConfig.from_thresholds(thresholds) == CreativeConfig(**_VALID_CONFIG)


@pytest.mark.parametrize(
    ("field", "value", "key"),
    [
        ("min_impressions", 0, "cr.min_impressions"),
        ("min_variants", 1, "cr.min_variants"),
        ("alpha", Decimal(0), "cr.alpha"),
        ("ci_level", Decimal(1), "cr.ci_level"),
        ("power", Decimal("0.05"), "cr.power"),
        ("target_lift", Decimal(0), "cr.target_lift"),
        ("fisher_min_expected", Decimal(-1), "cr.fisher_min_expected"),
    ],
)
def test_creative_config_rejects_values_the_statistics_cannot_use(
    field: str, value: object, key: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(key)):
        CreativeConfig(**{**_VALID_CONFIG, field: value})
