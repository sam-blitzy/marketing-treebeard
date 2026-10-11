"""Acceptance tests of the Keywords to adjust section: rules K1, K2 and K3 (AAP 0.5.4.3, FR-4).

AAP 0.11.2 row ``acceptance/test_keywords_rules.py``. Every guarantee runs on the scenario
catalogue (``ads_engine.simulation.scenarios``): a ``fires`` scenario and its ``does_not_fire``
twin differ in exactly one input moved across the guarantee's threshold, and each test reads
both. Contexts come from the root ``make_context`` fixture, so no world or context is built by
hand here and no test touches the database or the network.

Threshold values are read from each context's configuration (``ctx.thresholds``), never copied
from ``config/thresholds.yaml``. The only literals are the figures the AAP fixes for these
guarantees: n* = 300 at r = 1%, 3,000 at r = 0.1%, 64 at r = 0.047 and the 10-click floor at
r = 1 and r = 1.4, the 9-conversion baseline, the 0.3 fractional conversion, 3 versus 2
conversions, a one-point versus a half-point quality-score drop, 5 quality-score days and the
80% coverage line.

Coverage of the guarantees:

* Catalogue guard: every keyword scenario matches its ``Expectation``; every ``fires``
  scenario has its single-input ``does_not_fire`` twin.
* K1 gate, low and non-binary baselines, no probability claim, unusable baselines, fractional
  conversions, the spend floor, skipped statuses and ``ADDED`` terms.
* K2 conversions and efficiency gates, adjacent terms (cap, Jaccard, exclusions, order, label).
* K3 quality-score drop, quality-score days, snapshot fallback, and the CPC rise with
  cost/conversion rising or with conversions falling to 0.
* Search-term coverage, the ``no_crm_link`` flag without the verified Google-to-CRM link, the
  output fields every item shows, ranking, thresholds read from configuration, and an empty
  section without Google.

Ranking needs several items of one rule, which only the catalogue's ``default`` world has, so
the ranking checks read it at the fixed catalogue instant.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from fractions import Fraction
from functools import cached_property
from typing import Any

import pytest
from tests.support import (
    GOOGLE_LINK_UNVERIFIED_REASON,
    GoogleLinkState,
    evidence_metric,
    with_google_link,
)

from ads_engine.analysis import keywords
from ads_engine.analysis.context import (
    ENTITY_ACCOUNT,
    ENTITY_KEYWORD,
    FINDING_K1_NO_BASELINE,
    FINDING_NO_CRM_LINK,
    FINDING_SEARCH_TERM_COVERAGE,
    FLAG_NO_CRM_LINK,
    FLAG_SEARCH_TERM_COVERAGE,
    GROUP_BLOCK,
    GROUP_EXPAND,
    GROUP_REVIEW,
    K1,
    K2,
    K3,
    KEYWORDS,
    AnalysisContext,
    Candidate,
    Finding,
    Flag,
    KeywordDay,
    KeywordInfo,
    RuleResult,
    SearchTermRow,
    Withheld,
)
from ads_engine.analysis.keywords import (
    ADJACENT_TERMS_LABEL,
    COVERAGE_THRESHOLD_KEY,
    K1_ACTION,
    K1_THRESHOLD_KEYS,
    K2_ACTION_TEMPLATE,
    K2_THRESHOLD_KEYS,
    K3_ACTION,
    K3_THRESHOLD_KEYS,
    MIN_CLICKS_LABEL,
    k1_min_clicks,
)
from ads_engine.analysis.metrics import window_totals
from ads_engine.analysis.text import jaccard, tokenize
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.simulation.scenarios import (
    FIXED_NOW,
    MODULE_KEYWORDS,
    SCENARIOS,
    Scenario,
    for_module,
    input_differences,
    pairs,
)

type ContextFactory = Callable[..., AnalysisContext]
"""The root ``make_context`` fixture: ``make_context(scenario, *, thresholds=None, now=None)``."""

type ThresholdsFactory = Callable[..., Thresholds]
"""The root ``make_thresholds`` fixture: ``make_thresholds(overrides) -> Thresholds``."""

_MODULE_SCENARIOS: tuple[Scenario, ...] = for_module(MODULE_KEYWORDS)
"""Every catalogue scenario this module runs, in name order."""

_KEYWORD_RULES: tuple[str, ...] = (K1, K2, K3)
"""The rules of the Keywords to adjust section."""

_GROUPS: dict[str, str] = {K1: GROUP_BLOCK, K2: GROUP_EXPAND, K3: GROUP_REVIEW}
"""The group each keyword rule files its items under (AAP 0.5.5: Block, Expand, Review)."""

_THRESHOLD_KEYS: dict[str, tuple[str, ...]] = {
    K1: K1_THRESHOLD_KEYS,
    K2: K2_THRESHOLD_KEYS,
    K3: K3_THRESHOLD_KEYS,
}
"""The threshold keys each rule copies into ``evidence.thresholds``."""

_OUTPUT_FIELDS: tuple[str, ...] = ("term", "spend", "conversions", "cost_per_conversion")
"""The fields every keyword item shows first, in the user's order (AAP 0.5.4.3)."""

_PROBABILITY_WORDS: tuple[str, ...] = ("probability", "chance", "confidence", "likely", "95%", "5%")
"""Wording that would claim a probability for K1's click gate, which is a heuristic (AAP 0.2.4)."""

_EXCLUDED_STATUSES: frozenset[str] = frozenset({"EXCLUDED", "ADDED_EXCLUDED"})
"""Search-term statuses the advertiser has already blocked."""

_DEFAULT_SCENARIO = "default"
"""The end-to-end world, the only catalogue world with several items per keyword rule."""


# ---------------------------------------------------------------------------------------------
# Running a scenario
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Run:
    """One scenario's context and the Keywords section evaluated on it."""

    scenario: Scenario
    ctx: AnalysisContext
    result: RuleResult

    def items(self, rule_id: str) -> tuple[Candidate, ...]:
        """The candidates of ``rule_id``, in the order the rule emitted them."""
        return tuple(item for item in self.result.candidates if item.rule_id == rule_id)

    def withheld(self, rule_id: str) -> tuple[Withheld, ...]:
        """The withheld lines of ``rule_id``."""
        return tuple(line for line in self.result.withheld if line.rule_id == rule_id)

    def findings(self, code: str) -> tuple[Finding, ...]:
        """The findings with ``code``."""
        return tuple(finding for finding in self.result.findings if finding.code == code)

    def keyword_items(self) -> tuple[Candidate, ...]:
        """Every K1, K2 and K3 item."""
        return tuple(item for item in self.result.candidates if item.rule_id in _KEYWORD_RULES)


def _run(
    make_context: ContextFactory,
    scenario: str | Scenario,
    *,
    thresholds: Thresholds | None = None,
) -> _Run:
    """Build ``scenario``'s context through the root fixture and evaluate the section on it.

    The ``default`` scenario runs on the real clock unless given one, so it is read at
    :data:`FIXED_NOW`, the instant every other scenario is anchored on.
    """
    entry = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    now = None if entry.fixed_time else FIXED_NOW
    ctx = make_context(entry, thresholds=thresholds, now=now)
    return _Run(entry, ctx, keywords.evaluate(ctx))


def _baseline(item: Candidate, key: str) -> Any:
    """The value of an item's evidence baseline ``key``."""
    for baseline in item.evidence.baselines:
        if baseline.key == key:
            return baseline.value
    raise AssertionError(f"{item.rule_id} {item.entity_ref.name!r} has no baseline {key!r}")


def _exact(value: object) -> Fraction:
    """An evidence or input number as an exact fraction (``Decimal``, ``int`` or ``Fraction``)."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int | Fraction):
        raise AssertionError(f"expected a number, found {value!r}")
    return Fraction(value)


def _n_star(ctx: AnalysisContext) -> int:
    """K1's click gate for a context: n* from its own baseline rate and configuration."""
    baseline = ctx.k1_baseline
    assert baseline is not None, "the scenario has no K1 baseline"
    assert baseline.clicks > 0, "the scenario's K1 baseline has no clicks"
    return k1_min_clicks(
        Fraction(baseline.conversions) / baseline.clicks,
        floor=ctx.thresholds.int("k1.min_clicks_floor"),
        rule_of_three=ctx.thresholds.decimal("k1.rule_of_three"),
    )


def _term_row(ctx: AnalysisContext, term: str) -> SearchTermRow:
    """The single search-term row of ``term`` in the context."""
    rows = [row for row in ctx.search_terms if row.search_term == term]
    assert len(rows) == 1, f"expected one search-term row for {term!r}, found {len(rows)}"
    return rows[0]


def _keyword(ctx: AnalysisContext, text: str) -> KeywordInfo:
    """The single keyword with ``text`` in the context."""
    found = [keyword for keyword in ctx.keywords.values() if keyword.text == text]
    assert len(found) == 1, f"expected one keyword {text!r}, found {len(found)}"
    return found[0]


def _account_cost_per_conversion(ctx: AnalysisContext) -> Fraction:
    """Window spend ÷ window conversions over the context's Google campaigns, exactly."""
    google = [campaign.id for campaign in ctx.campaigns.values() if campaign.source == "google"]
    totals = window_totals(ctx, google)
    assert totals.outcomes is not None
    assert totals.outcomes > 0
    return Fraction(totals.spend) / Fraction(totals.outcomes)


def _campaign_cost_per_conversion(ctx: AnalysisContext, campaign_id: int) -> Fraction:
    """One campaign's window spend ÷ window conversions, exactly."""
    totals = window_totals(ctx, (campaign_id,))
    assert totals.outcomes is not None
    assert totals.outcomes > 0
    return Fraction(totals.spend) / Fraction(totals.outcomes)


def _cents(value: Fraction) -> Decimal:
    """A money value at the cent, as evidence shows it: the exact value rounded half up."""
    cents = abs(value) * 100
    whole = math.floor(cents + Fraction(1, 2))
    return Decimal(whole if value >= 0 else -whole).scaleb(-2)


def _window_days(ctx: AnalysisContext, keyword: KeywordInfo) -> list[KeywordDay]:
    """A keyword's daily facts inside the run window."""
    window = ctx.window
    return [
        day
        for day in ctx.keyword_days.get(keyword.id, ())
        if window.start <= day.date <= window.end
    ]


def _quality_score_means(ctx: AnalysisContext, keyword: KeywordInfo) -> tuple[Fraction, Fraction]:
    """Mean daily quality score over the first and last ``k3.qs_span_days`` of the window."""
    span = ctx.thresholds.int("k3.qs_span_days")
    days = _window_days(ctx, keyword)
    means: list[Fraction] = []
    for start, end in (ctx.window.first_n(span), ctx.window.last_n(span)):
        scores = [
            day.historical_quality_score
            for day in days
            if start <= day.date <= end and day.historical_quality_score is not None
        ]
        assert scores, f"no daily quality score for {keyword.text!r} from {start} to {end}"
        means.append(Fraction(sum(scores), len(scores)))
    return means[0], means[1]


def _snapshot_scores(ctx: AnalysisContext, keyword: KeywordInfo) -> tuple[int, int]:
    """The keyword's Q-G7 quality-score snapshots at window start and end, as loaded."""
    observed = sorted(
        (
            item
            for item in ctx.observations.get((ENTITY_KEYWORD, keyword.id), ())
            if item.quality_score is not None
        ),
        key=lambda item: item.captured_at,
    )
    assert len(observed) >= 2, f"{keyword.text!r} has fewer than two quality-score snapshots"
    first, last = observed[0].quality_score, observed[-1].quality_score
    assert first is not None
    assert last is not None
    return first, last


@dataclass(frozen=True, slots=True)
class _Halves:
    """CPC and cost/conversion of a keyword in each half of the window, exactly."""

    cpc_ratio: Fraction
    cpa_ratio: Fraction | None
    first_conversions: Fraction
    second_conversions: Fraction


def _halves(ctx: AnalysisContext, keyword: KeywordInfo) -> _Halves:
    """Sum a keyword's window days by half and compare CPC and cost/conversion."""
    days = _window_days(ctx, keyword)
    sums: list[tuple[Fraction, int, Fraction]] = []
    for start, end in ctx.window.halves():
        inside = [day for day in days if start <= day.date <= end]
        reported = [day.conversions for day in inside if day.conversions is not None]
        assert len(reported) == len(inside), "every day of the half reports conversions"
        sums.append(
            (
                sum((Fraction(day.cost) for day in inside), Fraction(0)),
                sum(day.clicks for day in inside),
                sum((Fraction(value) for value in reported), Fraction(0)),
            )
        )
    (cost_1, clicks_1, conv_1), (cost_2, clicks_2, conv_2) = sums
    assert clicks_1 > 0
    assert clicks_2 > 0
    cpa_ratio = None
    if conv_1 > 0 and conv_2 > 0:
        cpa_ratio = (cost_2 / conv_2) / (cost_1 / conv_1)
    return _Halves((cost_2 / clicks_2) / (cost_1 / clicks_1), cpa_ratio, conv_1, conv_2)


def _conversions_fell(ctx: AnalysisContext, halves: _Halves) -> bool:
    """Whether a keyword's conversions fell to 0, K3's alternative to a cost/conversion rise.

    Read from the inputs: second-half conversions 0 with the first half's at least
    ``k3.min_first_half_conversions``.
    """
    minimum = Fraction(ctx.thresholds.decimal("k3.min_first_half_conversions"))
    return halves.second_conversions == 0 and halves.first_conversions >= minimum


def _texts(item: Candidate | Withheld) -> tuple[str, ...]:
    """Every rendered text of an item or withheld line, lowercased for wording checks."""
    if isinstance(item, Withheld):
        return (item.reason.casefold(),)
    evidence = json.dumps(item.evidence.to_json(), ensure_ascii=False, sort_keys=True)
    return (item.reason.casefold(), item.action.casefold(), evidence.casefold())


def _claims_probability(run: _Run) -> bool:
    """Whether any K1 item or withheld line words its click gate as a probability."""
    lines: tuple[Candidate | Withheld, ...] = (*run.items(K1), *run.withheld(K1))
    texts = [text for line in lines for text in _texts(line)]
    return any(word in text for text in texts for word in _PROBABILITY_WORDS)


# ---------------------------------------------------------------------------------------------
# Resolving an Expectation's values against a run
# ---------------------------------------------------------------------------------------------


class _Probe:
    """What a scenario's expected values are read from: its output, else its inputs.

    A value is read from the rule's item when the rule emitted one, else from its withheld line
    or finding. A ``does_not_fire`` scenario's values describe the input that was moved across
    the threshold (the term's 0.3 conversions, its 15.00 spend, the keyword's 6.5 mean quality
    score); those are read from the context, for the subject the twin's item names.
    """

    def __init__(self, run: _Run, make_context: ContextFactory) -> None:
        self.run = run
        self._make_context = make_context

    @property
    def ctx(self) -> AnalysisContext:
        """The scenario's context."""
        return self.run.ctx

    @property
    def rule(self) -> str:
        """The rule the scenario's expectation is about."""
        rule_id = self.run.scenario.expect.rule_id
        assert rule_id in _KEYWORD_RULES, f"{self.run.scenario.name} expects rule {rule_id!r}"
        return rule_id

    @cached_property
    def item(self) -> Candidate | None:
        """The rule's single item, or ``None`` when it emitted none."""
        items = self.run.items(self.rule)
        assert len(items) <= 1, f"{self.run.scenario.name}: {len(items)} {self.rule} items"
        return items[0] if items else None

    @cached_property
    def line(self) -> Withheld | None:
        """The rule's single withheld line, or ``None`` when it withheld nothing."""
        lines = self.run.withheld(self.rule)
        assert len(lines) <= 1, f"{self.run.scenario.name}: {len(lines)} {self.rule} lines"
        return lines[0] if lines else None

    def finding(self, code: str) -> Finding:
        """The run's single finding with ``code``."""
        found = self.run.findings(code)
        assert len(found) == 1, f"{self.run.scenario.name}: {len(found)} {code} findings"
        return found[0]

    @cached_property
    def subject(self) -> str:
        """The term or keyword the guarantee is about: named by this run's or its twin's output."""

        def runs() -> Iterator[_Run]:
            # The twin is built only when this run's own output names no subject.
            yield self.run
            if self.run.scenario.pair is not None:
                yield _run(self._make_context, self.run.scenario.pair)

        for candidate in runs():
            items = candidate.items(self.rule)
            if items:
                return items[0].entity_ref.name
            named = [line for line in candidate.withheld(self.rule) if line.entity is not None]
            if named:
                entity = named[0].entity
                assert entity is not None
                return entity.name
        raise AssertionError(f"{self.run.scenario.name}: no output names the rule's subject")

    def row(self) -> SearchTermRow:
        """The subject's search-term row (K1, K2)."""
        return _term_row(self.ctx, self.subject)

    def keyword(self) -> KeywordInfo:
        """The subject keyword (K3)."""
        return _keyword(self.ctx, self.subject)

    def withheld_value(self, field: str, unit: str | None = None) -> Any:
        """``observed`` or ``required`` of the rule's withheld line, optionally of one unit."""
        line = self.line
        assert line is not None, f"{self.run.scenario.name}: no {self.rule} withheld line"
        if unit is not None:
            assert line.unit == unit, f"withheld line unit {line.unit!r}, expected {unit!r}"
        return getattr(line, field)


def _item_metric_or(probe: _Probe, key: str, fallback: Callable[[], Any]) -> Any:
    """The item's metric ``key`` when the rule emitted an item, else ``fallback()``."""
    return evidence_metric(probe.item, key) if probe.item is not None else fallback()


def _resolve_conversions(probe: _Probe) -> Any:
    """Item conversions, else the withheld line's observed conversions, else the term's."""
    if probe.item is not None:
        return evidence_metric(probe.item, "conversions")
    if probe.line is not None and probe.line.unit == "conversions":
        return probe.line.observed
    return probe.row().conversions


def _resolve_cost_per_conversion(probe: _Probe) -> Any:
    """Item cost/conversion, else the term's window cost ÷ conversions at the cent."""
    if probe.item is not None:
        return evidence_metric(probe.item, "cost_per_conversion")
    row = probe.row()
    return _cents(Fraction(row.cost) / Fraction(row.conversions))


def _resolve_k1_baseline(key: str) -> Callable[[_Probe, Any], Any]:
    """An item's K1 baseline ``key``, else the ``k1_no_baseline`` finding's evidence value."""

    def resolve(probe: _Probe, _expected: Any) -> Any:
        if probe.item is not None:
            return _baseline(probe.item, key)
        return probe.finding(FINDING_K1_NO_BASELINE).evidence[key]

    return resolve


def _resolve_qs(metric: str, index: int) -> Callable[[_Probe, Any], Any]:
    """An item's quality-score metric, else the keyword's daily mean for that span."""

    def resolve(probe: _Probe, _expected: Any) -> Any:
        return _item_metric_or(
            probe, metric, lambda: _quality_score_means(probe.ctx, probe.keyword())[index]
        )

    return resolve


def _resolve_snapshot(metric: str, index: int) -> Callable[[_Probe, Any], Any]:
    """An item's snapshot quality score, else the keyword's loaded snapshot at that end."""

    def resolve(probe: _Probe, _expected: Any) -> Any:
        return _item_metric_or(
            probe, metric, lambda: _snapshot_scores(probe.ctx, probe.keyword())[index]
        )

    return resolve


def _resolve_coverage(key: str) -> Callable[[_Probe, Any], Any]:
    """A value of the account's ``search_term_coverage`` finding."""

    def resolve(probe: _Probe, _expected: Any) -> Any:
        return probe.finding(FINDING_SEARCH_TERM_COVERAGE).evidence[key]

    return resolve


def _adjacent_terms(item: Candidate | None) -> tuple[str, ...]:
    """A K2 item's adjacent terms, in the order shown."""
    assert item is not None, "a K2 item is required to read adjacent terms"
    return tuple(str(entry["term"]) for entry in item.evidence.extra["adjacent_terms"])


_RESOLVERS: dict[str, Callable[[_Probe, Any], Any]] = {
    "n_star": lambda p, _e: _item_metric_or(
        p, "min_clicks", lambda: p.withheld_value("required", "clicks")
    ),
    "clicks": lambda p, _e: _item_metric_or(p, "clicks", lambda: p.row().clicks),
    "wasted_spend": lambda p, _e: _item_metric_or(p, "wasted_spend", lambda: None),
    "conversions": lambda p, _e: _resolve_conversions(p),
    "observed": lambda p, _e: p.withheld_value("observed"),
    "required": lambda p, _e: p.withheld_value("required"),
    "baseline_rate": _resolve_k1_baseline("baseline_rate"),
    "baseline_conversions": _resolve_k1_baseline("baseline_conversions"),
    "claims_probability": lambda p, _e: _claims_probability(p.run),
    "spend": lambda p, _e: _item_metric_or(p, "spend", lambda: p.row().cost),
    "account_cpa": lambda p, _e: (
        _baseline(p.item, "account_cost_per_conversion")
        if p.item is not None
        else _cents(_account_cost_per_conversion(p.ctx))
    ),
    "term_status": lambda p, _e: (
        p.item.evidence.extra["term_status"] if p.item is not None else p.row().term_status
    ),
    "action": lambda p, _e: None if p.item is None else p.item.action,
    "matched_criterion": lambda p, _e: (
        None if p.item is None else p.item.evidence.extra["matched_criterion_id"]
    ),
    "matched_keyword": lambda p, _e: (
        None if p.item is None else p.item.evidence.extra["matched_keyword_text"]
    ),
    "pauses_keyword": lambda p, _e: any(
        "pause" in item.action.casefold() for item in p.run.result.candidates
    ),
    "cost_per_conversion": lambda p, _e: _resolve_cost_per_conversion(p),
    "adjacent_terms": lambda p, _e: _adjacent_terms(p.item),
    # Passes exactly when none of the expected terms is shown as adjacent.
    "excluded": lambda p, expected: tuple(
        term for term in expected if term not in _adjacent_terms(p.item)
    ),
    "qs_first": _resolve_qs("qs_first", 0),
    "qs_last": _resolve_qs("qs_last", 1),
    "qs_days_last": lambda p, _e: (
        p.item.evidence.extra["qs_days_last"]
        if p.item is not None
        else p.withheld_value("observed", "days")
    ),
    "qs_start": _resolve_snapshot("qs_first", 0),
    "qs_end": _resolve_snapshot("qs_last", 1),
    "basis": lambda p, _e: None if p.item is None else p.item.evidence.extra["qs_basis"],
    "cpc_ratio": lambda p, _e: _item_metric_or(
        p, "cpc_ratio", lambda: _halves(p.ctx, p.keyword()).cpc_ratio
    ),
    "cpa_ratio": lambda p, _e: _item_metric_or(
        p, "cpa_ratio", lambda: _halves(p.ctx, p.keyword()).cpa_ratio
    ),
    "conversions_first_half": lambda p, _e: _item_metric_or(
        p, "conversions_first_half", lambda: _halves(p.ctx, p.keyword()).first_conversions
    ),
    "conversions_second_half": lambda p, _e: _item_metric_or(
        p, "conversions_second_half", lambda: _halves(p.ctx, p.keyword()).second_conversions
    ),
    "conversions_fell": lambda p, _e: (
        p.item.evidence.extra["conversions_fell"]
        if p.item is not None
        else _conversions_fell(p.ctx, _halves(p.ctx, p.keyword()))
    ),
    "search_term_spend": _resolve_coverage("search_term_spend"),
    "keyword_spend": _resolve_coverage("keyword_spend"),
}
"""How each ``Expectation.values`` key used by this module's scenarios is observed."""


def _assert_matches(what: str, expected: object, observed: object) -> None:
    """Compare an expected value with what the run shows.

    Numbers compare exactly as fractions, whichever of ``str``, ``int``, ``Decimal`` or
    ``Fraction`` each side uses (the catalogue writes ``"61.00"``, evidence holds
    ``Decimal("61.00")``). Booleans, ``None``, texts and tuples compare as they are.
    """
    if isinstance(expected, bool) or expected is None:
        assert observed is expected, f"{what}: expected {expected!r}, observed {observed!r}"
        return
    if isinstance(expected, tuple):
        assert observed == expected, f"{what}: expected {expected}, observed {observed}"
        return
    numeric = isinstance(observed, Decimal | int | Fraction) and not isinstance(observed, bool)
    if numeric and isinstance(expected, str | int):
        assert _exact(observed) == Fraction(Decimal(str(expected))), (
            f"{what}: expected {expected!r}, observed {observed!r}"
        )
        return
    assert observed == expected, f"{what}: expected {expected!r}, observed {observed!r}"


# ---------------------------------------------------------------------------------------------
# Catalogue guard: every keyword scenario, fires and does-not-fire
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", _MODULE_SCENARIOS, ids=lambda scenario: scenario.name)
def test_every_keywords_scenario_matches_its_expectation(
    scenario: Scenario, make_context: ContextFactory
) -> None:
    """Catalogue guard: each keyword scenario's rule, withheld line, finding, flags and values."""
    run = _run(make_context, scenario)
    expect = scenario.expect
    probe = _Probe(run, make_context)
    rule = probe.rule

    items = run.items(rule)
    assert bool(items) is expect.fires, (
        f"{scenario.name}: {rule} {'did not fire' if expect.fires else 'fired'} "
        f"({scenario.guarantee})"
    )
    for item in items:
        assert item.section == KEYWORDS
        assert item.group == _GROUPS[rule]
    if expect.withheld_reason is not None:
        reasons = [line.reason for line in run.withheld(rule)]
        assert any(reason.startswith(expect.withheld_reason) for reason in reasons), (
            f"{scenario.name}: no {rule} withheld line reads {expect.withheld_reason!r}: {reasons}"
        )
    if expect.finding is not None:
        assert run.findings(expect.finding), f"{scenario.name}: no {expect.finding} finding"
    for code in expect.flags:
        assert items, f"{scenario.name}: flag {code} expected but {rule} emitted no item"
        assert all(item.has_flag(code) for item in items), f"{scenario.name}: {code} missing"
    for key, expected in expect.values.items():
        resolver = _RESOLVERS.get(key)
        assert resolver is not None, f"{scenario.name}: no resolver for expectation key {key!r}"
        _assert_matches(f"{scenario.name} {key}", expected, resolver(probe, expected))


def test_every_keywords_fires_scenario_has_a_single_input_does_not_fire_twin() -> None:
    """Catalogue pairs: each fires scenario has a does_not_fire twin moving one input."""
    names = {scenario.name for scenario in _MODULE_SCENARIOS}
    fires = [scenario for scenario in _MODULE_SCENARIOS if scenario.variant == "fires"]
    twins = [scenario for scenario in _MODULE_SCENARIOS if scenario.variant == "does_not_fire"]
    assert fires
    assert len(fires) == len(twins)
    paired = {
        first.name: second.name for first, second in pairs() if first.module == MODULE_KEYWORDS
    }
    assert set(paired) == {scenario.name for scenario in fires}
    for scenario in fires:
        assert scenario.pair is not None
        assert scenario.pair in names
        twin = SCENARIOS[scenario.pair]
        assert twin.variant == "does_not_fire"
        assert twin.pair == scenario.name
        assert twin.module == MODULE_KEYWORDS
        assert paired[scenario.name] == twin.name
        assert scenario.name.removesuffix(".fires") == twin.name.removesuffix(".does_not_fire")
        differences = input_differences(scenario, twin)
        assert len(differences) == 1, f"{scenario.name} and its twin differ in {differences}"
    for scenario in _MODULE_SCENARIOS:
        if scenario.variant == "case":
            assert scenario.pair is None
    assert {scenario.expect.rule_id for scenario in _MODULE_SCENARIOS} == set(_KEYWORD_RULES)


# ---------------------------------------------------------------------------------------------
# K1 gate and baselines (AAP 0.5.4.3 K1, 0.2.4 "Spent with no conversions")
# ---------------------------------------------------------------------------------------------


def test_k1_gate_fires_at_n_star_clicks_and_is_withheld_one_click_below(
    make_context: ContextFactory,
) -> None:
    """K1 gate: fires at n* clicks / withheld at n*-1 with clicks observed and required."""
    fires = _run(make_context, "k1.gate.fires")
    held = _run(make_context, "k1.gate.does_not_fire")
    n_star = _n_star(fires.ctx)
    assert n_star == _n_star(held.ctx) == SCENARIOS["k1.gate.fires"].expect.values["n_star"]

    (item,) = fires.items(K1)
    assert (item.section, item.group, item.action) == (KEYWORDS, GROUP_BLOCK, K1_ACTION)
    assert K1_ACTION == "Add as exact-match negative"
    sample = item.evidence.sample
    assert sample is not None
    assert (sample.observed, sample.required, sample.unit) == (n_star, n_star, "clicks")
    assert evidence_metric(item, "clicks") == evidence_metric(item, "min_clicks") == n_star
    assert fires.withheld(K1) == ()

    assert held.items(K1) == ()
    (line,) = held.withheld(K1)
    assert (line.observed, line.required, line.unit) == (n_star - 1, n_star, "clicks")
    assert line.reason == f"clicks {n_star - 1} of {n_star} required"
    assert line.entity is not None
    assert line.entity.key == item.entity_ref.key


@pytest.mark.parametrize(
    ("rate", "n_star"),
    [
        ("0.01", 300),
        ("0.001", 3_000),
        ("0.0001", 30_000),
        ("0.00001", 300_000),
        ("0.047", 64),
        ("1.4", 10),
        ("1", 10),
        ("0.3", 10),
        ("2.5", 10),
    ],
)
def test_k1_min_clicks_is_the_rule_of_three_with_a_floor_and_no_upper_cap(
    rate: str, n_star: int
) -> None:
    """K1 low and non-binary baselines: 300 at 1%, 3,000 at 0.1%, 64 at 0.047, 10 at r >= 0.3."""
    assert k1_min_clicks(Decimal(rate), floor=10, rule_of_three=3) == n_star
    assert k1_min_clicks(Fraction(Decimal(rate)), floor=10, rule_of_three=Decimal(3)) == n_star


def test_k1_min_clicks_takes_the_exact_ceiling() -> None:
    """K1 gate arithmetic is exact: 3 conversions over 47 clicks need 47 clicks, never 48."""
    # Binary floating point overshoots the integer quotient: 3 / (3 / 47) is 47.00000000000001.
    assert math.ceil(3 / (3 / 47)) == 48
    assert k1_min_clicks(Fraction(3, 47), floor=10, rule_of_three=3) == 47
    # A quotient just above an integer still rounds up, and r = 0.3 lands exactly on the floor.
    assert k1_min_clicks(Decimal("0.299"), floor=10, rule_of_three=3) == 11
    assert k1_min_clicks(Decimal("0.3"), floor=10, rule_of_three=3) == 10


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"r": Decimal(0)}, ValueError),
        ({"r": Decimal("-0.01")}, ValueError),
        ({"r": Decimal("NaN")}, ValueError),
        ({"r": Decimal("Infinity")}, ValueError),
        ({"r": 0.01}, TypeError),
        ({"r": True}, TypeError),
        ({"r": "0.01"}, TypeError),
        ({"floor": -1}, ValueError),
        ({"floor": 10.0}, TypeError),
        ({"floor": True}, TypeError),
        ({"rule_of_three": 0}, ValueError),
        ({"rule_of_three": 3.0}, TypeError),
    ],
)
def test_k1_min_clicks_rejects_an_unusable_rate_or_configuration(
    kwargs: dict[str, Any], error: type[Exception]
) -> None:
    """K1 unusable baseline: n* is never computed from a zero, negative or non-exact rate."""
    arguments: dict[str, Any] = {"r": Decimal("0.01"), "floor": 10, "rule_of_three": 3} | kwargs
    rate = arguments.pop("r")
    with pytest.raises(error):
        k1_min_clicks(rate, **arguments)


@pytest.mark.parametrize(
    ("case", "rate", "n_star"),
    [("k1.low_rate_1pct", "0.01", 300), ("k1.low_rate_01pct", "0.001", 3_000)],
)
def test_k1_low_baseline_rates_raise_n_star_without_a_cap(
    case: str, rate: str, n_star: int, make_context: ContextFactory
) -> None:
    """K1 low baseline rates: n* = 300 at r = 1% and 3,000 at r = 0.1%, with no cap."""
    fires = _run(make_context, f"{case}.fires")
    held = _run(make_context, f"{case}.does_not_fire")
    baseline = fires.ctx.k1_baseline
    assert baseline is not None
    assert baseline.rate == Decimal(rate)
    assert _n_star(fires.ctx) == n_star == math.ceil(Fraction(3) / Fraction(Decimal(rate)))

    (item,) = fires.items(K1)
    sample = item.evidence.sample
    assert sample is not None
    assert (sample.required, sample.unit) == (n_star, "clicks")
    assert evidence_metric(item, "min_clicks") == n_star
    assert _baseline(item, "baseline_rate") == Decimal(rate)

    assert held.items(K1) == ()
    (line,) = held.withheld(K1)
    assert (line.observed, line.required) == (n_star - 1, n_star)


def test_k1_fractional_data_driven_baseline_gives_n_star_64(make_context: ContextFactory) -> None:
    """K1 non-binary baselines: a fractional data-driven baseline (r = 0.047) gives n* = 64."""
    fires = _run(make_context, "k1.fractional_baseline.fires")
    held = _run(make_context, "k1.fractional_baseline.does_not_fire")
    ctx = fires.ctx
    baseline = ctx.k1_baseline
    assert baseline is not None
    assert baseline.rate == Decimal("0.047")
    models = {action.attribution_model for action in ctx.conversion_actions.values()}
    assert models == {"GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"}
    # 3 / 0.047 = 63.83: the quotient is fractional, so the gate rounds up to 64.
    assert Fraction(3) / Fraction(baseline.rate) == Fraction(3000, 47)
    assert _n_star(ctx) == 64
    (item,) = fires.items(K1)
    assert evidence_metric(item, "min_clicks") == 64
    assert evidence_metric(item, "clicks") == 64
    (line,) = held.withheld(K1)
    assert (line.observed, line.required, line.unit) == (63, 64, "clicks")


@pytest.mark.parametrize(
    ("case", "rate", "counting_type"),
    [("k1.many_per_click", "1.4", "MANY_PER_CLICK"), ("k1.rate_one", "1", "MANY_PER_CLICK")],
)
def test_k1_rates_at_or_above_one_give_the_ten_click_floor(
    case: str, rate: str, counting_type: str, make_context: ContextFactory
) -> None:
    """K1 non-binary baselines: r = 1.4 (more conversions than clicks) and r = 1 give 10 clicks."""
    fires = _run(make_context, f"{case}.fires")
    held = _run(make_context, f"{case}.does_not_fire")
    ctx = fires.ctx
    baseline = ctx.k1_baseline
    assert baseline is not None
    assert baseline.rate == Decimal(rate)
    assert baseline.conversions >= baseline.clicks
    if rate == "1.4":
        assert baseline.conversions > baseline.clicks
    assert {action.counting_type for action in ctx.conversion_actions.values()} == {counting_type}

    floor = ctx.thresholds.int("k1.min_clicks_floor")
    assert _n_star(ctx) == floor == 10
    (item,) = fires.items(K1)
    assert evidence_metric(item, "min_clicks") == floor
    assert _baseline(item, "baseline_rate") == Decimal(rate)
    (line,) = held.withheld(K1)
    assert (line.observed, line.required, line.unit) == (floor - 1, floor, "clicks")


def _k1_scenarios() -> tuple[str, ...]:
    """Every K1 scenario of the module, plus the default world with its several K1 items."""
    names = [scenario.name for scenario in _MODULE_SCENARIOS if scenario.expect.rule_id == K1]
    return (*names, _DEFAULT_SCENARIO)


def test_k1_never_words_its_click_gate_as_a_probability(make_context: ContextFactory) -> None:
    """K1 non-binary baselines: no evidence or reason text claims a probability."""
    checked = {"items": 0, "withheld": 0}
    for name in _k1_scenarios():
        run = _run(make_context, name)
        lines: tuple[Candidate | Withheld, ...] = (*run.items(K1), *run.withheld(K1))
        for line in lines:
            checked["withheld" if isinstance(line, Withheld) else "items"] += 1
            for text in _texts(line):
                found = [word for word in _PROBABILITY_WORDS if word in text]
                assert not found, f"{name}: {line.rule_id} text claims a probability ({found})"
        for item in run.items(K1):
            assert item.evidence.metric("min_clicks").label == MIN_CLICKS_LABEL
            assert item.evidence.extra["min_clicks_label"] == MIN_CLICKS_LABEL
            assert "for the account's conversion rate" in item.reason
    assert checked["items"] > 0, checked
    assert checked["withheld"] > 0, checked
    assert MIN_CLICKS_LABEL.casefold() == "minimum clicks for the account's conversion rate"


@pytest.mark.parametrize(
    ("scenario", "rate"),
    [
        ("k1.no_baseline.does_not_fire", Decimal("0.3")),
        ("k1.zero_rate.case", Decimal(0)),
        ("k1.rate_unavailable.case", None),
    ],
)
def test_k1_unusable_baseline_withholds_k1_account_wide(
    scenario: str, rate: Decimal | None, make_context: ContextFactory
) -> None:
    """K1 unusable baseline: 9 conversions, r = 0 or r unavailable withhold K1 account-wide."""
    run = _run(make_context, scenario)
    ctx = run.ctx
    baseline = ctx.k1_baseline
    minimum = ctx.thresholds.decimal("k1.min_baseline_conversions")
    assert baseline is not None
    assert baseline.rate == rate
    if scenario == "k1.no_baseline.does_not_fire":
        assert baseline.conversions == 9 < minimum
    if rate is None:
        assert baseline.clicks == 0

    assert run.items(K1) == ()
    (line,) = run.withheld(K1)
    assert line.entity is None, "the baseline withholds K1 for the whole account"
    assert (line.observed, line.required, line.unit) == (
        baseline.conversions,
        minimum,
        "conversions",
    )
    (finding,) = run.findings(FINDING_K1_NO_BASELINE)
    assert finding.section == KEYWORDS
    assert finding.entity is None
    assert finding.evidence["baseline_conversions"] == baseline.conversions
    assert finding.evidence["baseline_rate"] == rate
    assert finding.evidence["required"] == minimum
    assert dict(finding.evidence["thresholds"]) == ctx.thresholds.subset(*K1_THRESHOLD_KEYS)


def test_k1_ten_baseline_conversions_evaluate_k1_normally(make_context: ContextFactory) -> None:
    """K1 unusable baseline: the 10-conversion partner of the 9-conversion case evaluates K1."""
    run = _run(make_context, "k1.no_baseline.fires")
    baseline = run.ctx.k1_baseline
    assert baseline is not None
    assert baseline.conversions == run.ctx.thresholds.decimal("k1.min_baseline_conversions") == 10
    (item,) = run.items(K1)
    assert _baseline(item, "baseline_conversions") == 10
    assert run.findings(FINDING_K1_NO_BASELINE) == ()
    assert all(line.entity is not None for line in run.withheld(K1))


def test_k1_no_baseline_finding_names_one_conversion_in_the_singular(
    make_context: ContextFactory,
) -> None:
    """K1 unusable baseline: 1 baseline conversion reads "1 conversion", never "1 conversions"."""
    run = _run(make_context, "k3.conversions_fell.fires")
    baseline = run.ctx.k1_baseline
    assert baseline is not None
    assert baseline.conversions == 1
    assert baseline.clicks > 1
    (finding,) = run.findings(FINDING_K1_NO_BASELINE)
    assert f"has 1 conversion over {baseline.clicks:,} clicks, below the " in finding.message
    assert "1 conversions" not in finding.message
    minimum = run.ctx.thresholds.decimal("k1.min_baseline_conversions")
    assert minimum > 1
    assert f"needs at least {minimum:f} baseline conversions and a rate above 0" in (
        finding.message
    )


# ---------------------------------------------------------------------------------------------
# K1 non-firing cases and ADDED terms
# ---------------------------------------------------------------------------------------------


def _twin_runs(make_context: ContextFactory, case: str) -> tuple[_Run, _Run]:
    """The ``fires`` and ``does_not_fire`` runs of ``case``."""
    return _run(make_context, f"{case}.fires"), _run(make_context, f"{case}.does_not_fire")


def _k1_target(run: _Run) -> str:
    """The K1 target term: the term the fires run's single K1 item is about."""
    (item,) = run.items(K1)
    return item.entity_ref.name


def test_k1_a_fractional_conversion_counts_as_converting(make_context: ContextFactory) -> None:
    """K1 non-firing: a 0.3 fractional conversion counts as converting; 0 conversions fire."""
    fires, held = _twin_runs(make_context, "k1.fractional_conversion")
    term = _k1_target(fires)
    assert _term_row(fires.ctx, term).conversions == 0
    assert evidence_metric(fires.items(K1)[0], "conversions") == 0

    row = _term_row(held.ctx, term)
    assert row.conversions == Decimal("0.3")
    assert row.clicks >= _n_star(held.ctx)
    assert held.items(K1) == ()
    assert held.withheld(K1) == (), "a converting term is not a withheld K1 pattern"


def test_k1_spend_below_one_account_cost_per_conversion_does_not_fire(
    make_context: ContextFactory,
) -> None:
    """K1 non-firing: spend below 1 x the account cost per conversion; at or above fires."""
    fires, held = _twin_runs(make_context, "k1.spend_below_cpa")
    term = _k1_target(fires)
    multiple = Fraction(fires.ctx.thresholds.ratio("k1.min_spend_cpa_multiple"))

    fired_row = _term_row(fires.ctx, term)
    assert Fraction(fired_row.cost) >= multiple * _account_cost_per_conversion(fires.ctx)
    (item,) = fires.items(K1)
    assert _baseline(item, "account_cost_per_conversion") == _cents(
        _account_cost_per_conversion(fires.ctx)
    )

    held_row = _term_row(held.ctx, term)
    assert Fraction(held_row.cost) < multiple * _account_cost_per_conversion(held.ctx)
    assert held_row.clicks >= _n_star(held.ctx)
    assert held_row.conversions == 0
    assert held.items(K1) == ()
    assert held.withheld(K1) == ()


@pytest.mark.parametrize("first_day", [Decimal(0), None])
def test_k1_spend_floor_needs_a_known_account_cost_per_conversion(
    make_context: ContextFactory, first_day: Decimal | None
) -> None:
    """K1 and unknown outcomes: the spend floor reads a known account cost/conversion only.

    ``k1.gate.fires`` blocks its term, which spends more than one account cost/conversion,
    taken from the account campaign's daily conversions. With the window's first day at a
    known 0 conversions instead of its 10, that cost/conversion rises but stays below the
    term's spend, so K1 still fires. With that day unreported (``None``) the account's window
    conversions are unknown, never the 270 the other days add up to: no K1 item, and one
    account-wide withheld line naming the unreported days against the days with metrics.

    A search term's own conversions are never unknown: ``SearchTermRow.conversions`` is a
    number by contract, because the Google parser reads an omitted additive metric as 0 and
    rejects an explicit ``null`` as a defect. The unknown input K1 can meet is therefore the
    account's daily conversions, which a campaign day may leave unreported.
    """
    fires = _run(make_context, "k1.gate.fires")
    term = _k1_target(fires)
    window = fires.ctx.window
    (campaign_id,) = [c.id for c in fires.ctx.campaigns.values() if c.source == "google"]
    known = fires.ctx.campaign_days[campaign_id]
    assert all(day.conversions is not None and day.conversions > 0 for day in known)
    days = tuple(
        replace(day, conversions=first_day) if day.date == window.start else day for day in known
    )
    ctx = replace(fires.ctx, campaign_days={**fires.ctx.campaign_days, campaign_id: days}, cache={})
    run = _Run(fires.scenario, ctx, keywords.evaluate(ctx))
    row = _term_row(ctx, term)
    assert (row.term_status, row.conversions) == ("NONE", 0)
    assert row.clicks >= _n_star(ctx)
    if first_day is not None:
        multiple = Fraction(ctx.thresholds.ratio("k1.min_spend_cpa_multiple"))
        assert Fraction(row.cost) >= multiple * _account_cost_per_conversion(ctx)
        (item,) = run.items(K1)
        assert item.entity_ref.name == term
        assert _baseline(item, "account_cost_per_conversion") == _cents(
            _account_cost_per_conversion(ctx)
        )
        assert run.withheld(K1) == ()
        return
    totals = window_totals(ctx, (campaign_id,))
    assert (totals.outcomes, totals.unreported_days, totals.days) == (None, 1, window.days)
    assert run.items(K1) == ()
    (line,) = run.withheld(K1)
    assert line.reason == (
        f"account cost/conversion unavailable: conversions unavailable on 1 of {window.days} "
        f"days in the {window.days}-day window"
    )
    assert line.reason == (
        "account cost/conversion unavailable: conversions unavailable on 1 of 28 days in the "
        "28-day window"
    )
    assert (line.observed, line.required, line.unit) == (
        window.days - 1,
        window.days,
        "days with conversions reported",
    )
    assert line.entity is not None
    assert line.entity.type == ENTITY_ACCOUNT
    assert run.findings(FINDING_K1_NO_BASELINE) == ()


@pytest.mark.parametrize(
    ("case", "status"),
    [("k1.excluded_term", "EXCLUDED"), ("k1.added_excluded_term", "ADDED_EXCLUDED")],
)
def test_k1_skips_terms_that_are_already_excluded(
    case: str, status: str, make_context: ContextFactory
) -> None:
    """K1 non-firing: an EXCLUDED or ADDED_EXCLUDED term is skipped; the same term as NONE fires."""
    fires, held = _twin_runs(make_context, case)
    term = _k1_target(fires)
    assert _term_row(fires.ctx, term).term_status == "NONE"
    assert fires.items(K1)[0].evidence.extra["term_status"] == "NONE"

    assert _term_row(held.ctx, term).term_status == status
    assert held.items(K1) == ()
    assert held.withheld(K1) == (), "an excluded term is neither an item nor a withheld line"


def test_k1_added_term_names_the_matched_criterion_and_never_pauses(
    make_context: ContextFactory,
) -> None:
    """K1 ADDED term: the negative-term candidate names the matched criterion, never a pause."""
    run = _run(make_context, "k1.added_term.case")
    (item,) = run.items(K1)
    extra = item.evidence.extra
    row = _term_row(run.ctx, item.entity_ref.name)
    assert row.term_status == "ADDED" == extra["term_status"]
    assert item.action == K1_ACTION
    assert extra["negative_match_type"] == "EXACT"

    criterion = extra["matched_criterion_id"]
    assert criterion == row.matched_criterion_id
    keyword = _keyword(run.ctx, extra["matched_keyword_text"])
    assert keyword.criterion_id == criterion, "the criterion is the matched Q-G7 keyword"
    assert f"criterion {criterion}" in item.reason
    assert extra["matched_keyword_text"] in item.reason
    assert "An exact-match negative also blocks that keyword's exact-match traffic." in item.reason

    paused = [c for c in run.result.candidates if "pause" in c.action.casefold()]
    assert paused == [], "K1 never recommends pausing a term or its keyword"
    assert all(c.entity_ref.type != ENTITY_KEYWORD for c in run.items(K1))


def test_k1_evidence_shows_wasted_spend_clicks_matched_keyword_and_n_star(
    make_context: ContextFactory,
) -> None:
    """K1 output: wasted spend (= spend), clicks, the matched keyword and n* on every item."""
    checked = 0
    for name in _k1_scenarios():
        run = _run(make_context, name)
        for item in run.items(K1):
            checked += 1
            row = _term_row(run.ctx, item.entity_ref.name)
            sample = item.evidence.sample
            assert sample is not None
            assert evidence_metric(item, "wasted_spend") == evidence_metric(item, "spend")
            # AAP 0.1.2: K1's headline number is the wasted spend.
            assert item.evidence.extra["headline_metric"] == "wasted_spend"
            assert _exact(evidence_metric(item, "spend")) == Fraction(_cents(Fraction(row.cost)))
            assert evidence_metric(item, "clicks") == row.clicks == sample.observed
            assert evidence_metric(item, "min_clicks") == sample.required == _n_star(run.ctx)
            assert sample.unit == "clicks"
            assert evidence_metric(item, "conversions") == 0
            assert evidence_metric(item, "cost_per_conversion") == "—"
            assert row.matched_keyword_text is not None
            assert str(evidence_metric(item, "matched_keyword")).startswith(
                row.matched_keyword_text
            )
            assert item.rank_key == (-row.cost,)
    firing = [s for s in _MODULE_SCENARIOS if s.expect.rule_id == K1 and s.expect.fires]
    assert checked > len(firing), "every firing K1 scenario and the default world were checked"


# ---------------------------------------------------------------------------------------------
# K2 expansion candidates (AAP 0.5.4.3 K2)
# ---------------------------------------------------------------------------------------------


def _k2_target(run: _Run) -> tuple[Candidate, SearchTermRow]:
    """The fires run's single K2 item and its search-term row."""
    (item,) = run.items(K2)
    return item, _term_row(run.ctx, item.entity_ref.name)


def _assert_k2_item(run: _Run, item: Candidate, row: SearchTermRow) -> None:
    """A K2 item: a converting NONE term, phrase-match action in its ad group, efficiency met."""
    ctx = run.ctx
    assert (item.section, item.group) == (KEYWORDS, GROUP_EXPAND)
    assert row.term_status == "NONE"
    ad_group = ctx.ad_groups[row.ad_group_id].name
    assert item.action == K2_ACTION_TEMPLATE.format(ad_group=ad_group)
    assert item.action.startswith("Add as phrase-match keyword in ")
    assert item.evidence.extra["ad_group"] == ad_group
    assert item.evidence.extra["suggested_match_type"] == "PHRASE"
    # The expansion card leads with the term's conversions.
    assert item.evidence.extra["headline_metric"] == "conversions"
    assert Fraction(row.conversions) >= Fraction(ctx.thresholds.decimal("k2.min_conversions"))
    term_cpa = Fraction(row.cost) / Fraction(row.conversions)
    campaign_cpa = _campaign_cost_per_conversion(ctx, row.campaign_id)
    assert term_cpa <= Fraction(ctx.thresholds.ratio("k2.max_cpa_ratio")) * campaign_cpa
    assert evidence_metric(item, "cost_per_conversion") == _cents(term_cpa)
    assert _baseline(item, "campaign_cost_per_conversion") == _cents(campaign_cpa)


def test_k2_fires_at_three_conversions_and_is_withheld_at_two(make_context: ContextFactory) -> None:
    """K2: 3 conversions fire / 2 do not (withheld with conversions observed and required)."""
    fires, held = _twin_runs(make_context, "k2.min_conversions")
    item, row = _k2_target(fires)
    _assert_k2_item(fires, item, row)
    assert row.conversions == 3 == evidence_metric(item, "conversions")
    sample = item.evidence.sample
    assert sample is not None
    assert (sample.observed, sample.unit) == (3, "conversions")

    minimum = held.ctx.thresholds.decimal("k2.min_conversions")
    assert _term_row(held.ctx, item.entity_ref.name).conversions == 2
    assert held.items(K2) == ()
    (line,) = held.withheld(K2)
    assert (line.observed, line.required, line.unit) == (2, minimum, "conversions")
    assert line.reason == "conversions 2 of 3 required"
    assert line.entity is not None
    assert line.entity.key == item.entity_ref.key


def test_k2_cost_per_conversion_above_the_campaign_does_not_fire(
    make_context: ContextFactory,
) -> None:
    """K2: cost/conversion at or below the campaign's fires / above it does not."""
    fires, held = _twin_runs(make_context, "k2.cpa_above_campaign")
    item, row = _k2_target(fires)
    _assert_k2_item(fires, item, row)

    held_row = _term_row(held.ctx, row.search_term)
    ratio = Fraction(held.ctx.thresholds.ratio("k2.max_cpa_ratio"))
    term_cpa = Fraction(held_row.cost) / Fraction(held_row.conversions)
    assert held_row.conversions == row.conversions
    assert term_cpa > ratio * _campaign_cost_per_conversion(held.ctx, held_row.campaign_id)
    assert held.items(K2) == ()
    assert held.withheld(K2) == (), "a term less efficient than its campaign is not a K2 pattern"


def test_k2_adjacent_terms_are_capped_similar_new_and_ordered(make_context: ContextFactory) -> None:
    """K2 adjacent terms: capped at 5 and excluding existing keywords and excluded terms."""
    run = _run(make_context, "k2.adjacent_terms.case")
    ctx = run.ctx
    item, row = _k2_target(run)
    _assert_k2_item(run, item, row)
    max_terms = ctx.thresholds.int("k2.adjacent_max_terms")
    similarity = ctx.thresholds.ratio("k2.adjacent_jaccard")
    min_clicks = ctx.thresholds.int("k2.adjacent_min_clicks")

    totals: dict[str, list[Any]] = {}
    for term_row in ctx.search_terms:
        entry = totals.setdefault(term_row.search_term, [Decimal(0), 0, Decimal(0)])
        entry[0] += term_row.cost
        entry[1] += term_row.clicks
        entry[2] += term_row.conversions
    keyword_tokens = {tokenize(keyword.text) for keyword in ctx.keywords.values()}
    excluded = {r.search_term for r in ctx.search_terms if r.term_status in _EXCLUDED_STATUSES}
    source = tokenize(row.search_term)

    def similar(term: str) -> bool:
        return term != row.search_term and jaccard(source, tokenize(term)) >= similarity

    qualifying = sorted(
        (
            term
            for term, (_spend, clicks, _conversions) in totals.items()
            if similar(term)
            and tokenize(term) not in keyword_tokens
            and term not in excluded
            and clicks >= min_clicks
        ),
        key=lambda term: (-totals[term][2], -totals[term][1], term),
    )
    assert len(qualifying) > max_terms, "the scenario has more qualifying neighbours than the cap"

    shown = item.evidence.extra["adjacent_terms"]
    terms = [str(entry["term"]) for entry in shown]
    assert len(terms) == max_terms
    assert terms == qualifying[:max_terms]
    for entry in shown:
        term = str(entry["term"])
        assert jaccard(source, tokenize(term)) >= similarity
        assert tokenize(term) not in keyword_tokens
        assert term not in excluded
        assert entry["clicks"] == totals[term][1] >= min_clicks
        assert entry["conversions"] == totals[term][2]
    order = [(-entry["conversions"], -entry["clicks"]) for entry in shown]
    assert order == sorted(order), "adjacent terms are ordered by conversions, then clicks"

    keyword_neighbours = [t for t in totals if similar(t) and tokenize(t) in keyword_tokens]
    excluded_neighbours = [t for t in totals if similar(t) and t in excluded]
    assert keyword_neighbours
    assert excluded_neighbours
    assert not set(terms) & {*keyword_neighbours, *excluded_neighbours}

    assert item.evidence.extra["adjacent_terms_label"] == ADJACENT_TERMS_LABEL
    assert ADJACENT_TERMS_LABEL == "from your search-term report"
    assert f"Adjacent terms to try, {ADJACENT_TERMS_LABEL}: {', '.join(terms)}." in item.reason


def test_k2_without_qualifying_neighbours_says_so(make_context: ContextFactory) -> None:
    """K2 adjacent terms: an item with no qualifying neighbour states that none qualified."""
    run = _run(make_context, "k2.min_conversions.fires")
    item, _row = _k2_target(run)
    assert item.evidence.extra["adjacent_terms"] == ()
    assert f"No adjacent terms {ADJACENT_TERMS_LABEL} qualified." in item.reason


# ---------------------------------------------------------------------------------------------
# K3 review (AAP 0.5.4.3 K3)
# ---------------------------------------------------------------------------------------------


def _k3_target(run: _Run) -> tuple[Candidate, KeywordInfo]:
    """The fires run's single K3 item and its keyword."""
    (item,) = run.items(K3)
    return item, _keyword(run.ctx, item.entity_ref.name)


def _assert_k3_gates(run: _Run, item: Candidate) -> None:
    """A K3 item: review action and group, window and per-half click minimums met."""
    thresholds = run.ctx.thresholds
    assert (item.section, item.group, item.action) == (KEYWORDS, GROUP_REVIEW, K3_ACTION)
    assert item.action.startswith("Review keyword")
    assert item.entity_ref.type == ENTITY_KEYWORD
    assert run.ctx.window.days >= thresholds.int("k3.min_window_days")
    minimum = thresholds.int("k3.min_clicks_per_half")
    assert evidence_metric(item, "clicks_first_half") >= minimum
    assert evidence_metric(item, "clicks_second_half") >= minimum
    sample = item.evidence.sample
    assert sample is not None
    assert (sample.required, sample.unit) == (minimum, "clicks per half")


def test_k3_a_one_point_quality_score_drop_fires_and_half_a_point_does_not(
    make_context: ContextFactory,
) -> None:
    """K3: a one-point quality-score drop fires / a 0.5-point drop does not."""
    fires, held = _twin_runs(make_context, "k3.qs_drop")
    item, keyword = _k3_target(fires)
    _assert_k3_gates(fires, item)
    drop = Fraction(fires.ctx.thresholds.decimal("k3.qs_drop"))
    extra = item.evidence.extra
    assert extra["qs_basis"] == "daily"
    assert "quality_score" in extra["triggers"]
    # The quality-score branch fired, so the card leads with the latest quality score.
    assert extra["headline_metric"] == "qs_last"
    first, last = _quality_score_means(fires.ctx, keyword)
    assert (first, last) == (Fraction(7), Fraction(6))
    assert first - last == 1 >= drop
    assert (evidence_metric(item, "qs_first"), evidence_metric(item, "qs_last")) == (7, 6)
    minimum = fires.ctx.thresholds.int("k3.min_qs_days_per_span")
    assert extra["qs_days_first"] >= minimum
    assert extra["qs_days_last"] >= minimum

    held_keyword = _keyword(held.ctx, keyword.text)
    first, last = _quality_score_means(held.ctx, held_keyword)
    assert (first, last) == (Fraction(7), Fraction(13, 2))
    assert first - last < drop
    assert held.items(K3) == ()
    assert held.withheld(K3) == ()


def test_k3_needs_quality_scores_on_five_days_of_each_span(make_context: ContextFactory) -> None:
    """K3: quality scores on 5 days of the last span fire / fewer than 5 QS days do not."""
    fires, held = _twin_runs(make_context, "k3.qs_days")
    item, keyword = _k3_target(fires)
    _assert_k3_gates(fires, item)
    minimum = fires.ctx.thresholds.int("k3.min_qs_days_per_span")
    assert item.evidence.extra["qs_days_last"] == minimum == 5

    assert held.items(K3) == ()
    (line,) = held.withheld(K3)
    span = held.ctx.thresholds.int("k3.qs_span_days")
    assert (line.observed, line.required, line.unit) == (4, minimum, "days")
    assert line.reason == f"quality score on 4 of {minimum} required days in the last {span} days"
    assert line.entity is not None
    assert line.entity.key == item.entity_ref.key
    assert line.entity.name == keyword.text


def test_k3_compares_quality_score_snapshots_when_daily_scores_are_absent(
    make_context: ContextFactory,
) -> None:
    """K3 snapshot fallback: no daily scores, Q-G7 snapshots at window start and end compared."""
    fires, held = _twin_runs(make_context, "k3.snapshot_fallback")
    item, keyword = _k3_target(fires)
    _assert_k3_gates(fires, item)
    days = _window_days(fires.ctx, keyword)
    assert days
    assert all(day.historical_quality_score is None for day in days)
    start, end = _snapshot_scores(fires.ctx, keyword)
    assert (start, end) == (7, 6)
    assert start - end >= fires.ctx.thresholds.decimal("k3.qs_drop")
    extra = item.evidence.extra
    assert extra["qs_basis"] == "snapshots"
    assert "quality_score" in extra["triggers"]
    assert (evidence_metric(item, "qs_first"), evidence_metric(item, "qs_last")) == (7, 6)
    snapshots = extra["qs_snapshots"]
    lower, _ = fires.ctx.window.day_bounds(
        fires.ctx.window.start, fires.ctx.window.boundary("google")
    )
    _, upper = fires.ctx.window.day_bounds(
        fires.ctx.window.end, fires.ctx.window.boundary("google")
    )
    assert lower <= snapshots["start_at"] < snapshots["end_at"] <= upper
    assert "between the keyword snapshots at window start and end" in item.reason

    held_keyword = _keyword(held.ctx, keyword.text)
    assert all(day.historical_quality_score is None for day in _window_days(held.ctx, held_keyword))
    assert _snapshot_scores(held.ctx, held_keyword) == (7, 7)
    assert held.items(K3) == ()
    assert held.withheld(K3) == ()


def test_k3_cpc_rise_without_a_matching_conversion_rise_fires(make_context: ContextFactory) -> None:
    """K3: CPC up 20% with flat conversions fires / CPC and conversions rising together do not."""
    fires, held = _twin_runs(make_context, "k3.cpc_rise")
    item, keyword = _k3_target(fires)
    _assert_k3_gates(fires, item)
    rise = Fraction(fires.ctx.thresholds.ratio("k3.rise_ratio"))
    halves = _halves(fires.ctx, keyword)
    assert halves.cpc_ratio == Fraction(6, 5) >= rise
    assert halves.cpa_ratio is not None
    assert halves.cpa_ratio >= rise
    assert halves.first_conversions == halves.second_conversions, "conversions stayed flat"
    extra = item.evidence.extra
    assert "cpc" in extra["triggers"]
    # Only the CPC branch fired, so the card leads with the CPC rise.
    assert list(extra["triggers"]) == ["cpc"]
    assert extra["headline_metric"] == "cpc_ratio"
    assert extra["conversions_fell"] is False
    assert _exact(evidence_metric(item, "cpc_ratio")) == halves.cpc_ratio
    assert _exact(evidence_metric(item, "cpa_ratio")) == halves.cpa_ratio
    assert "CPC rose from" in item.reason
    assert "while cost/conversion rose from" in item.reason

    held_halves = _halves(held.ctx, _keyword(held.ctx, keyword.text))
    assert held_halves.cpc_ratio == Fraction(6, 5) >= rise
    assert held_halves.cpa_ratio == 1 < rise
    assert held_halves.second_conversions > held_halves.first_conversions
    assert held.items(K3) == ()
    assert held.withheld(K3) == ()


def test_k3_cpc_rise_with_conversions_falling_to_zero_fires(make_context: ContextFactory) -> None:
    """K3: CPC up 20% with second-half conversions 0 and first-half conversions >= 1 fires.

    No first-half conversion does not: the twin moves the first half's single conversion below
    ``k3.min_first_half_conversions``, so conversions did not fall. Both halves then have a
    known 0 conversions, so cost/conversion shows "—" in each and there is no cost/conversion
    ratio.
    """
    fires, held = _twin_runs(make_context, "k3.conversions_fell")
    item, keyword = _k3_target(fires)
    _assert_k3_gates(fires, item)
    thresholds = fires.ctx.thresholds
    rise = Fraction(thresholds.ratio("k3.rise_ratio"))
    minimum = Fraction(thresholds.decimal("k3.min_first_half_conversions"))
    halves = _halves(fires.ctx, keyword)
    assert halves.cpc_ratio == Fraction(6, 5) >= rise
    assert halves.first_conversions == minimum, "the first half sits on the threshold"
    assert halves.second_conversions == 0
    assert halves.cpa_ratio is None, "no second-half cost/conversion to compare"
    assert _conversions_fell(fires.ctx, halves) is True
    extra = item.evidence.extra
    assert "cpc" in extra["triggers"]
    assert "quality_score" not in extra["triggers"]
    assert extra["conversions_fell"] is True
    assert _exact(evidence_metric(item, "cpc_ratio")) == halves.cpc_ratio
    assert _exact(evidence_metric(item, "conversions_first_half")) == halves.first_conversions
    assert _exact(evidence_metric(item, "conversions_second_half")) == 0
    (first_start, first_end), _ = fires.ctx.window.halves()
    first_cost = sum(
        (
            Fraction(day.cost)
            for day in _window_days(fires.ctx, keyword)
            if first_start <= day.date <= first_end
        ),
        Fraction(0),
    )
    assert evidence_metric(item, "cost_per_conversion_first_half") == _cents(
        first_cost / halves.first_conversions
    )
    second_cpa = item.evidence.metric("cost_per_conversion_second_half")
    assert (second_cpa.value, second_cpa.unit) == ("—", "currency")
    assert "unavailable" not in second_cpa.to_json(), "a known 0 shows '—', never unavailable"
    assert evidence_metric(item, "cpa_ratio") is None
    assert "CPC rose from" in item.reason
    fell = f"while conversions fell from {evidence_metric(item, 'conversions_first_half')} to 0."
    assert item.reason.endswith(fell)
    assert "while cost/conversion rose" not in item.reason

    held_halves = _halves(held.ctx, _keyword(held.ctx, keyword.text))
    assert held_halves.cpc_ratio == Fraction(6, 5) >= rise
    assert held_halves.first_conversions == held_halves.second_conversions == 0
    assert held_halves.first_conversions < minimum
    assert held_halves.cpa_ratio is None
    assert _conversions_fell(held.ctx, held_halves) is False
    assert held.items(K3) == ()
    assert held.withheld(K3) == ()


def _keyword_conversions(
    run: _Run, keyword: KeywordInfo, when: date, conversions: Decimal | None
) -> _Run:
    """``run`` evaluated again with ``keyword``'s conversions on ``when`` set to ``conversions``.

    Moves exactly one input; ``None`` leaves that day's conversions unreported. The context is
    copied with an empty cache, so nothing memoised on the original carries over.
    """
    days = tuple(
        replace(day, conversions=conversions) if day.date == when else day
        for day in run.ctx.keyword_days[keyword.id]
    )
    assert days != run.ctx.keyword_days[keyword.id], f"{keyword.text!r} has no day {when}"
    ctx = replace(run.ctx, keyword_days={**run.ctx.keyword_days, keyword.id: days}, cache={})
    return _Run(run.scenario, ctx, keywords.evaluate(ctx))


def test_k3_unreported_second_half_conversions_never_read_as_a_fall_to_zero(
    make_context: ContextFactory,
) -> None:
    """K3 and unknown outcomes: conversions fell to 0 only when every second-half day says so.

    ``k3.conversions_fell.fires`` reviews its keyword: CPC up 20% while conversions fell from 1
    to a known 0. With the window's last day reporting no conversions (``None``) instead of its
    known 0, the second half's conversions are unavailable rather than 0: they did not fall,
    and there is no second-half cost/conversion to compare, so K3 neither fires nor withholds.
    """
    fires = _run(make_context, "k3.conversions_fell.fires")
    item, keyword = _k3_target(fires)
    assert item.evidence.extra["conversions_fell"] is True
    last = fires.ctx.window.end
    assert [day.conversions for day in _window_days(fires.ctx, keyword) if day.date == last] == [0]

    unknown = _keyword_conversions(fires, keyword, last, None)
    _, (second_start, second_end) = unknown.ctx.window.halves()
    second = [
        day for day in _window_days(unknown.ctx, keyword) if second_start <= day.date <= second_end
    ]
    assert [day.conversions for day in second].count(None) == 1
    assert all(day.conversions == 0 for day in second if day.conversions is not None)
    assert unknown.items(K3) == ()
    assert unknown.withheld(K3) == ()


def test_k3_item_with_unreported_conversions_shows_its_cost_per_conversion_unavailable(
    make_context: ContextFactory,
) -> None:
    """K3 output and unknown outcomes: unreported conversions are unavailable, never "—" or 0.

    ``k3.qs_drop.fires`` reviews its keyword on the quality-score branch, which reads no
    conversions. With the window's first day reporting no conversions (``None``) the item still
    fires, but the window's and the first half's conversions are unavailable, and so are their
    cost/conversion and the cost/conversion ratio, while the fully reported second half keeps
    its figures. With that day at a known 0 instead, every figure is a number.
    """
    fires = _run(make_context, "k3.qs_drop.fires")
    _, keyword = _k3_target(fires)
    start = fires.ctx.window.start
    count_keys = ("conversions", "conversions_first_half")
    cost_keys = ("cost_per_conversion", "cost_per_conversion_first_half")
    for conversions in (Decimal(0), None):
        run = _keyword_conversions(fires, keyword, start, conversions)
        item, _ = _k3_target(run)
        _assert_k3_gates(run, item)
        assert tuple(item.evidence.extra["triggers"]) == ("quality_score",)
        _, (second_start, second_end) = run.ctx.window.halves()
        second = [
            day for day in _window_days(run.ctx, keyword) if second_start <= day.date <= second_end
        ]
        reported = [day.conversions for day in second if day.conversions is not None]
        assert len(reported) == len(second), "the second half reports every day"
        assert evidence_metric(item, "conversions_second_half") == sum(reported, Decimal(0))
        assert evidence_metric(item, "cost_per_conversion_second_half") is not None
        shown = [evidence_metric(item, key) for key in (*count_keys, *cost_keys, "cpa_ratio")]
        if conversions is not None:
            assert all(value is not None for value in shown), shown
            continue
        assert shown == [None] * len(shown)
        for key in (*count_keys, *cost_keys):
            assert item.evidence.metric(key).to_json()["unavailable"] is True, key


def test_k3_evidence_shows_quality_score_cpc_and_cost_per_conversion_by_half(
    make_context: ContextFactory,
) -> None:
    """K3 output: QS first vs last, CPC by half and cost/conversion by half on every item."""
    names = [s.name for s in _MODULE_SCENARIOS if s.expect.rule_id == K3 and s.expect.fires]
    checked = 0
    for name in (*names, _DEFAULT_SCENARIO):
        run = _run(make_context, name)
        for item in run.items(K3):
            checked += 1
            keyword = _keyword(run.ctx, item.entity_ref.name)
            halves = _halves(run.ctx, keyword)
            # The ratio is shown to four places: within half a unit of the fourth place.
            shown = _exact(evidence_metric(item, "cpc_ratio"))
            assert abs(shown - halves.cpc_ratio) <= Fraction(1, 20_000)
            for key in ("cpc_first_half", "cpc_second_half"):
                assert item.evidence.metric(key).value is not None, f"{name}: {key} unavailable"
            for key in ("qs_first", "qs_last"):
                # Present on every item; known whenever the quality-score branch fired.
                value = item.evidence.metric(key).value
                assert value is not None or "quality_score" not in item.evidence.extra["triggers"]
            # The headline is the fired branch's number: the latest quality score when it
            # dropped (whether or not CPC rose too), else the CPC rise.
            triggers = item.evidence.extra["triggers"]
            assert item.evidence.extra["headline_metric"] == (
                "qs_last" if "quality_score" in triggers else "cpc_ratio"
            ), name
            for key, conversions_key in (
                ("cost_per_conversion_first_half", "conversions_first_half"),
                ("cost_per_conversion_second_half", "conversions_second_half"),
            ):
                # Shown on every item, never dropped: "—" at a known 0 conversions, and
                # unavailable only when the half's conversions are unreported.
                cost = item.evidence.metric(key)
                assert cost.unit == "currency"
                half_conversions = evidence_metric(item, conversions_key)
                if half_conversions == 0:
                    assert cost.value == "—", f"{name}: {key}"
                    assert "unavailable" not in cost.to_json()
                elif half_conversions is None:
                    assert cost.to_json()["unavailable"] is True, f"{name}: {key}"
            assert evidence_metric(item, "clicks_first_half") + evidence_metric(
                item, "clicks_second_half"
            ) == evidence_metric(item, "clicks")
            first, second = run.ctx.window.halves()
            assert item.evidence.extra["halves"] == {
                "first": {"start": first[0], "end": first[1]},
                "second": {"start": second[0], "end": second[1]},
            }
    assert checked > len(names)


# ---------------------------------------------------------------------------------------------
# Search-term coverage, output fields and ranking (AAP 0.5.4.3, 0.5.4.8, 0.5.5)
# ---------------------------------------------------------------------------------------------


def _coverage_spends(ctx: AnalysisContext) -> tuple[Decimal, Decimal]:
    """Window search-term spend and keyword spend over the context's Google campaigns."""
    window = ctx.window
    terms = sum((row.cost for row in ctx.search_terms), Decimal(0))
    keyword_spend = sum(
        (
            day.cost
            for days in ctx.keyword_days.values()
            for day in days
            if window.start <= day.date <= window.end
        ),
        Decimal(0),
    )
    return terms, keyword_spend


def test_search_term_coverage_below_80_percent_flags_every_keyword_item(
    make_context: ContextFactory,
) -> None:
    """Coverage: search-term spend below 80% of keyword spend flags items / at 80% or above not."""
    fires, held = _twin_runs(make_context, "keywords.coverage")
    for run, flagged in ((fires, True), (held, False)):
        ctx = run.ctx
        minimum = Fraction(ctx.thresholds.ratio(COVERAGE_THRESHOLD_KEY))
        assert minimum == Fraction(80, 100)
        terms, keyword_spend = _coverage_spends(ctx)
        share = Fraction(terms) / Fraction(keyword_spend)
        assert (share < minimum) is flagged

        (finding,) = run.findings(FINDING_SEARCH_TERM_COVERAGE)
        assert finding.section == KEYWORDS
        evidence = finding.evidence
        assert evidence["search_term_spend"] == _cents(Fraction(terms))
        assert evidence["keyword_spend"] == _cents(Fraction(keyword_spend))
        assert abs(_exact(evidence["coverage_share"]) - share) <= Fraction(1, 20_000)
        assert evidence["below_minimum"] is flagged
        assert dict(evidence["thresholds"]) == ctx.thresholds.subset(COVERAGE_THRESHOLD_KEY)

        items = run.keyword_items()
        assert items, "the coverage twins keep their K1 item"
        for item in items:
            assert item.has_flag(FLAG_SEARCH_TERM_COVERAGE) is flagged
            assert item.evidence.extra["search_term_coverage"]["below_minimum"] is flagged
    fires_share = fires.findings(FINDING_SEARCH_TERM_COVERAGE)[0].evidence["coverage_percent"]
    held_share = held.findings(FINDING_SEARCH_TERM_COVERAGE)[0].evidence["coverage_percent"]
    assert fires_share < 80 <= held_share


def _relinked(run: _Run, state: GoogleLinkState) -> _Run:
    """``run`` evaluated again with its context's Google-to-CRM link in ``state``."""
    ctx = with_google_link(run.ctx, state)
    return _Run(run.scenario, ctx, keywords.evaluate(ctx))


def _flags_by_item(run: _Run) -> dict[tuple[str, str], tuple[Flag, ...]]:
    """Each keyword item's flags, keyed by its rule and entity key."""
    flags = {(item.rule_id, item.entity_ref.key): item.flags for item in run.keyword_items()}
    assert len(flags) == len(run.keyword_items()), "one item per rule and entity"
    return flags


@pytest.mark.parametrize("scenario", [_DEFAULT_SCENARIO, "keywords.coverage.fires"])
@pytest.mark.parametrize(
    ("state", "reason"),
    [("absent", "no active HubSpot dataset"), ("unverified", GOOGLE_LINK_UNVERIFIED_REASON)],
)
def test_every_keyword_item_carries_no_crm_link_without_the_verified_google_link(
    make_context: ContextFactory, scenario: str, state: GoogleLinkState, reason: str
) -> None:
    """CRM link: Google items carry no_crm_link while the Google-to-CRM link is not verified.

    AAP 0.3.3 and 0.5.4.1. Every K1, K2 and K3 item carries it last, after exactly the flags it
    carries once the link is verified, when it carries no ``no_crm_link``. The section records
    no ``no_crm_link`` finding either way: AAP 0.5.4.3 defines its section finding as
    search-term coverage, and its findings are the same in every link state.
    """
    base = _run(make_context, scenario)
    unlinked, linked = _relinked(base, state), _relinked(base, "verified")
    expected = Flag(FLAG_NO_CRM_LINK, f"No CRM link for Google campaigns: {reason}")
    with_link, without_link = _flags_by_item(linked), _flags_by_item(unlinked)
    assert without_link.keys() == with_link.keys()
    for key, flags in with_link.items():
        assert FLAG_NO_CRM_LINK not in {flag.code for flag in flags}, key
        assert without_link[key] == (*flags, expected), key
    if scenario == _DEFAULT_SCENARIO:
        assert {rule for rule, _ in without_link} == set(_KEYWORD_RULES)
    else:
        assert any(with_link.values()), "an item with flags of its own keeps them first"
    for run in (unlinked, linked):
        assert run.findings(FINDING_NO_CRM_LINK) == ()
    assert unlinked.result.findings == linked.result.findings
    assert unlinked.result.withheld == linked.result.withheld


def _all_keyword_runs(make_context: ContextFactory) -> Iterator[_Run]:
    """Every scenario of this module, the default world, then one item with unknown conversions.

    The last run is ``k3.qs_drop.fires`` with its keyword's conversions unreported on the
    window's first day: K1 and K2 items show search-term conversions, which are always a number,
    so only a K3 item, reading its keyword's daily conversions, can carry unknown conversions.
    """
    for scenario in _MODULE_SCENARIOS:
        yield _run(make_context, scenario)
    yield _run(make_context, _DEFAULT_SCENARIO)
    qs_drop = _run(make_context, "k3.qs_drop.fires")
    _, keyword = _k3_target(qs_drop)
    yield _keyword_conversions(qs_drop, keyword, qs_drop.ctx.window.start, None)


def test_every_keyword_item_shows_term_spend_conversions_cost_and_its_window(
    make_context: ContextFactory,
) -> None:
    """Output fields: term, spend, conversions, cost/conversion and reason, campaign, ad group.

    Cost/conversion shows "—" at a known 0 conversions, a number above it, and unavailable only
    when conversions are unreported; each of the three is met by at least one item.
    """
    seen: set[str] = set()
    branches: set[str] = set()
    for run in _all_keyword_runs(make_context):
        ctx = run.ctx
        google = [account for account in ctx.accounts.values() if account.source == "google"]
        assert len(google) == 1
        timezone = google[0].timezone
        assert timezone
        for item in run.keyword_items():
            seen.add(item.rule_id)
            what = f"{run.scenario.name} {item.rule_id} {item.entity_ref.name!r}"
            evidence = item.evidence
            keys = tuple(metric.key for metric in evidence.metrics[: len(_OUTPUT_FIELDS)])
            assert keys == _OUTPUT_FIELDS, f"{what}: leading metrics {keys}"
            assert evidence_metric(item, "term") == item.entity_ref.name
            assert evidence.metric("spend").unit == "currency"
            assert evidence.metric("spend").currency == google[0].currency
            assert item.reason.strip(), f"{what}: empty reason"
            conversions = evidence_metric(item, "conversions")
            cost = evidence.metric("cost_per_conversion")
            if conversions == 0:
                # A known zero denominator renders "—", never "unavailable".
                assert cost.value == "—"
                assert "unavailable" not in cost.to_json()
                branches.add("known_zero")
            elif conversions is not None:
                assert cost.value is not None
                branches.add("converting")
            else:
                # Unreported conversions alone leave the cost/conversion unavailable.
                assert cost.to_json()["unavailable"] is True
                branches.add("unreported")
            extra = evidence.extra
            assert extra["campaign"], f"{what}: campaign or ad group missing"
            assert extra["ad_group"], f"{what}: campaign or ad group missing"
            assert dict(evidence.thresholds) == ctx.thresholds.subset(
                *_THRESHOLD_KEYS[item.rule_id]
            )
            window = evidence.window
            assert (window["start"], window["end"], window["days"]) == (
                ctx.window.start.isoformat(),
                ctx.window.end.isoformat(),
                ctx.window.days,
            )
            boundaries = {day["source"]: day["boundary"] for day in window["source_days"]}
            assert boundaries["google"] == timezone, f"{what}: Google day boundary"
            assert item.decision_subject is None, "keyword items offer Mark done, not decisions"
            assert item.increases_spend_on == ()
    assert seen == set(_KEYWORD_RULES)
    assert branches == {"known_zero", "converting", "unreported"}


def _spend(item: Candidate) -> Fraction:
    return _exact(evidence_metric(item, "spend"))


def test_keyword_items_are_ranked_by_their_rule_order(make_context: ContextFactory) -> None:
    """Ranking: K1 and K3 by spend descending; K2 by conversions, then cost/conversion."""
    run = _run(make_context, _DEFAULT_SCENARIO)
    orders: dict[str, Callable[[Candidate], tuple[Any, ...]]] = {
        K1: lambda item: (-_spend(item), item.entity_ref.name),
        K2: lambda item: (
            -_exact(evidence_metric(item, "conversions")),
            Fraction(_term_row(run.ctx, item.entity_ref.name).cost)
            / Fraction(_term_row(run.ctx, item.entity_ref.name).conversions),
            item.entity_ref.name,
        ),
        K3: lambda item: (-_spend(item), item.entity_ref.name),
    }
    for rule, order in orders.items():
        items = run.items(rule)
        assert len(items) >= 2, f"the default world needs several {rule} items to rank"
        ranked = [item.entity_ref.name for item in sorted(items, key=lambda c: c.sort_key)]
        expected = [item.entity_ref.name for item in sorted(items, key=order)]
        assert ranked == expected, f"{rule} ranking"
    k2 = sorted(run.items(K2), key=lambda c: c.sort_key)
    conversions = [_exact(evidence_metric(item, "conversions")) for item in k2]
    assert len(set(conversions)) < len(conversions), "a conversions tie exercises the CPA order"


# ---------------------------------------------------------------------------------------------
# Thresholds come from configuration (AAP 0.5.4: every threshold is a named, configured key)
# ---------------------------------------------------------------------------------------------


def test_k1_click_floor_is_read_from_configuration(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """K1 gate: n* = max(k1.min_clicks_floor, ...) follows a raised configured floor."""
    configured = make_thresholds({"k1.min_clicks_floor": 11})
    run = _run(make_context, "k1.many_per_click.fires", thresholds=configured)
    assert _n_star(run.ctx) == 11
    assert run.items(K1) == ()
    (line,) = run.withheld(K1)
    assert (line.observed, line.required, line.unit) == (10, 11, "clicks")


def test_k1_spend_floor_multiple_is_read_from_configuration(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """K1 spend floor: half the account cost/conversion admits the term held below one CPA."""
    configured = make_thresholds({"k1.min_spend_cpa_multiple": 0.5})
    run = _run(make_context, "k1.spend_below_cpa.does_not_fire", thresholds=configured)
    (item,) = run.items(K1)
    assert item.evidence.thresholds["k1.min_spend_cpa_multiple"] == 0.5
    assert item.evidence.extra["min_spend_cpa_multiple"] == Decimal("0.5")
    floor = _cents(Fraction(1, 2) * _account_cost_per_conversion(run.ctx))
    assert _baseline(item, "min_spend") == floor
    assert (
        f"at or above the spend floor of USD {floor:,} (0.5x the account cost/conversion)."
        in item.reason
    )


def test_k1_reason_cites_the_spend_floor_it_compared_under_a_multiple_other_than_one(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """K1 reason (AAP 0.5.4): at 14x a 1.43 cost/conversion the reason cites the 20.00 floor.

    The account spends 200.00 for 140 conversions, so 14 x the exact cost/conversion is
    exactly the term's 20.00, while 14 x the displayed 1.43 would be 20.02, above the spend.
    """
    configured = make_thresholds({"k1.min_spend_cpa_multiple": 14})
    run = _run(make_context, "k1.many_per_click.fires", thresholds=configured)
    (item,) = run.items(K1)
    floor = _baseline(item, "min_spend")
    assert floor == Decimal("20.00")
    assert floor == _cents(14 * _account_cost_per_conversion(run.ctx))
    assert evidence_metric(item, "spend") >= floor
    assert (
        ", and at or above the spend floor of USD 20.00 (14x the account cost/conversion). "
        in item.reason
    )
    assert "the account cost/conversion of" not in item.reason
    assert "(conversions ÷ clicks)" in item.reason
    assert "Matched keyword: " in item.reason


def test_k1_reason_at_the_default_multiple_cites_one_account_cost_per_conversion(
    make_context: ContextFactory,
) -> None:
    """K1 reason: at the default multiple of 1 it cites the account cost/conversion itself."""
    run = _run(make_context, "k1.many_per_click.fires")
    assert run.ctx.thresholds.ratio("k1.min_spend_cpa_multiple") == 1
    (item,) = run.items(K1)
    cost_per_conversion = _baseline(item, "account_cost_per_conversion")
    assert _baseline(item, "min_spend") == cost_per_conversion
    assert (
        f", and at least 1x the account cost/conversion of USD {cost_per_conversion:,}."
        in item.reason
    )
    assert "spend floor" not in item.reason


def test_k2_efficiency_ratio_is_read_from_configuration(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """K2: a configured k2.max_cpa_ratio of 1.5 admits the term 1.49x the campaign's CPA.

    The reason cites the highest qualifying cost/conversion it was compared with, never 1.5x
    the displayed campaign cost/conversion (AAP 0.5.4).
    """
    configured = make_thresholds({"k2.max_cpa_ratio": 1.5})
    run = _run(make_context, "k2.cpa_above_campaign.does_not_fire", thresholds=configured)
    item, row = _k2_target(run)
    _assert_k2_item(run, item, row)
    highest = _baseline(item, "max_cost_per_conversion")
    assert highest == _cents(
        Fraction(3, 2) * _campaign_cost_per_conversion(run.ctx, row.campaign_id)
    )
    assert evidence_metric(item, "cost_per_conversion") <= highest
    assert (
        f", at or below the highest qualifying cost/conversion of USD {highest:,} "
        "(1.5x the campaign's cost/conversion)."
    ) in item.reason
    assert "x the campaign's USD" not in item.reason


def test_k3_window_minimum_is_read_from_configuration(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """K3: a window shorter than k3.min_window_days withholds K3 with days observed and required."""
    configured = make_thresholds({"k3.min_window_days": 29})
    run = _run(make_context, "k3.qs_drop.fires", thresholds=configured)
    assert run.ctx.window.days == 28
    assert run.items(K3) == ()
    (line,) = run.withheld(K3)
    assert line.entity is None
    assert (line.observed, line.required, line.unit) == (28, 29, "days")
    assert line.reason == "window 28 days of 29 required"


def test_k3_clicks_per_half_minimum_is_read_from_configuration(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """K3: fewer than k3.min_clicks_per_half clicks in a half withhold the drop with both halves."""
    configured = make_thresholds({"k3.min_clicks_per_half": 71})
    run = _run(make_context, "k3.qs_drop.fires", thresholds=configured)
    assert run.items(K3) == ()
    (line,) = run.withheld(K3)
    (first_start, first_end), (second_start, second_end) = run.ctx.window.halves()
    assert (line.observed, line.required, line.unit) == (70, 71, "clicks per half")
    assert line.reason == (
        f"clicks 70 of 71 required in the first half ({first_start} to {first_end}); "
        f"clicks 70 of 71 required in the second half ({second_start} to {second_end})"
    )


def test_search_term_coverage_minimum_is_read_from_configuration(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """Coverage: raising the configured minimum to 81% flags the 80.2% twin's items."""
    configured = make_thresholds({COVERAGE_THRESHOLD_KEY: 0.81})
    run = _run(make_context, "keywords.coverage.does_not_fire", thresholds=configured)
    items = run.keyword_items()
    assert items
    assert all(item.has_flag(FLAG_SEARCH_TERM_COVERAGE) for item in items)
    (finding,) = run.findings(FINDING_SEARCH_TERM_COVERAGE)
    assert finding.evidence["below_minimum"] is True
    assert finding.evidence["min_share"] == Decimal("0.81")


def test_search_term_coverage_text_stays_below_a_minimum_set_just_above_the_share(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """Coverage (AAP 0.5.4): a share below a finer minimum never shows at or above it.

    The minimum is set at the next four-place decimal above the default world's share, where
    one place rounds the percentage up to the minimum or past it ("cover 88.5% ... below the
    88.5% minimum"). The finding, its evidence, each item's flag and ``evidence.extra`` show
    the same percentage, with as many places as keep it below the minimum, and the minimum
    exactly as configured.
    """
    terms, keyword_spend = _coverage_spends(_run(make_context, _DEFAULT_SCENARIO).ctx)
    share = Fraction(terms) / Fraction(keyword_spend)
    limit = Decimal(math.floor(share * 10_000) + 1) / 10_000
    minimum = Fraction(limit)
    assert share < minimum
    assert round(share * 100, 1) >= minimum * 100, "one place would not reach the minimum"
    assert round(share, 4) >= minimum, "four places would not reach the minimum"

    configured = make_thresholds({COVERAGE_THRESHOLD_KEY: limit})
    run = _run(make_context, _DEFAULT_SCENARIO, thresholds=configured)
    (finding,) = run.findings(FINDING_SEARCH_TERM_COVERAGE)
    evidence = finding.evidence
    assert evidence["below_minimum"] is True
    assert evidence["min_share"] == limit
    percent = evidence["coverage_percent"]
    assert Fraction(percent) < minimum * 100
    assert abs(Fraction(percent) - share * 100) < Fraction(1, 20)
    assert Fraction(evidence["coverage_share"]) < minimum
    assert abs(Fraction(evidence["coverage_share"]) - share) < Fraction(1, 20_000)
    assert finding.message.startswith(f"Search terms cover {percent:f}% of keyword spend (")
    assert f" Coverage is below the {(limit * 100).normalize():f}% minimum, " in finding.message

    items = run.keyword_items()
    assert items
    for item in items:
        (flag,) = (flag for flag in item.flags if flag.code == FLAG_SEARCH_TERM_COVERAGE)
        assert flag.message == f"Low search-term coverage: {percent:f}% of keyword spend"
        assert item.evidence.extra["search_term_coverage"]["percent"] == percent


def test_keywords_section_is_empty_without_google(make_context: ContextFactory) -> None:
    """Section scope: with no Google dataset or campaign the section emits nothing at all."""
    # No keyword scenario lacks Google, so any catalogue world without one serves.
    scenario = next(
        entry for entry in SCENARIOS.values() if entry.spec.google is None and entry.fixed_time
    )
    run = _run(make_context, scenario)
    assert "google" not in run.ctx.datasets
    assert all(campaign.source != "google" for campaign in run.ctx.campaigns.values())
    assert run.result == RuleResult()
