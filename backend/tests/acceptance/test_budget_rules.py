"""Acceptance tests for "Budget changes": B1-B4, the G1 guard, shared budgets and B3 allocation.

AAP 0.11.2 row ``acceptance/test_budget_rules.py`` (FR-6): every guarantee runs against the
scenario catalogue (``ads_engine.simulation.scenarios``), built so that it holds in one case
and, with a single input moved across its threshold, does not hold in the twin. The rules are
evaluated as a run evaluates them: the modules that produce spend increases
(``campaigns.evaluate``, ``budget.evaluate``, ``white_papers.evaluate``) feed
``precedence.apply_precedence``, so every expectation that concerns G1 or the B3 allocation is
checked after the guard, exactly where the user's prohibition "never recommend increasing
spend on high-utilization + poor-efficiency campaigns" is enforced.

Covered, in the order of the AAP row:

* **Catalogue guard**: each scenario of the module fires or stays silent as its
  ``Expectation`` states, after precedence.
* **B1 and G1**: +``b1.step`` of the latest effective daily budget at the currency's minor unit;
  no B1 under a C2 "Hold"; no increase of any kind survives on a B2 target; bypassing the G1
  filtering step trips the runner assertion.
* **Spending-day gates** (AAP 0.5.4.1 table) and the utilization-beside-efficiency card, which
  records every amount and outcome count at six places whatever the source.
* **Shared budgets** (AAP 0.5.4.5): dated membership, the member safety predicate, the
  budget subject, mixed objective groups and, against the database, a shared-budget decision
  that raises no discrepancy on any member at the next entity sync.
* **B3 allocation** (AAP 0.5.4.2): 300 and 200 against a 400 increase, the deterministic
  orders, the over-allocation assertion, the plain decrease without a receiver, and no
  funding across sources or currencies, for campaign and shared-budget receivers alike.
* **B4**: the rate test on unweighted counts only, and every withholding reason.
* **Lifetime and custom-period budgets**: utilization "n/a" and no cap rule.

Thresholds are read from ``ctx.thresholds``; only the AAP's own example numbers (300/200/400,
"4 of 5", "5 of 7", "3 weekend days of 4") appear as literals, because the catalogue builds
its worlds from exactly those numbers. Contexts derived from a catalogue context are copied
with :func:`dataclasses.replace` and a fresh ``cache``, so no memoised table leaks across.
Every test but the one persistence case is pure: no database and no network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.support import evidence_metrics, numeric

from ads_engine.analysis import budget, campaigns, precedence, white_papers
from ads_engine.analysis.context import (
    B1,
    B2,
    B3,
    B4,
    BUDGET,
    C0,
    C2,
    CAMPAIGNS,
    ENTITY_CAMPAIGN,
    ENTITY_CONTENT_ASSET,
    FLAG_SHARED_BUDGET,
    G1,
    GROUP_CHANGES,
    GROUP_SCALE,
    OBJ_LINKEDIN_LEAD_GEN,
    SUPPRESS_G1_BLOCKED,
    W1,
    WHITE_PAPERS,
    AccountInfo,
    AnalysisContext,
    Candidate,
    DecisionSubject,
    EntityRef,
    Metric,
    RuleResult,
    Source,
    SubjectKey,
    Withheld,
    cohort_group,
    content_asset_external_id,
)
from ads_engine.analysis.metrics import (
    BudgetHistory,
    Totals,
    assess_b2,
    budget_history,
    budget_subjects,
    member_poor_efficiency,
    spend_increase_blocked,
    subject_campaigns,
    window_totals,
)
from ads_engine.analysis.precedence import Allocation, PrecedenceInvariantError, PrecedenceOutput
from ads_engine.analysis.stats import percentile, poisson_rate_test
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.db.models.decisions import Decision, Discrepancy
from ads_engine.decisions import (
    DecisionPatch,
    SubjectRef,
    minor_unit_exponent,
    record_decision,
    round_to_minor_unit,
)
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.scenarios import (
    FIXED_ANCHOR,
    FIXED_NOW,
    MODULE_BUDGET,
    SCENARIOS,
    Scenario,
    for_module,
    input_differences,
)
from ads_engine.sync.jobs import JOB_ENTITIES, run_source_jobs
from ads_engine.sync.mode import SourceModeController

pytestmark = pytest.mark.anyio

type ContextFactory = Callable[..., AnalysisContext]
type WorldFactory = Callable[..., SimulatedWorld]

BUDGET_SCENARIOS: Final[tuple[Scenario, ...]] = for_module(MODULE_BUDGET)
"""Every catalogue scenario this module runs, in name order."""

GUARDED: Final[tuple[str, ...]] = (B1, B2, B3, B4, G1)
"""The rules and the guard this module proves firing and not firing."""

BUDGET_ITEM_RULES: Final[frozenset[str]] = frozenset({B1, B2, B3})
"""Budget rules whose cards carry a budget history gate (B4 shows its own pattern gate)."""

SPENDING_GATE_MINIMUM: Final = 5
"""AAP 0.11.2 "spending days 4 of 5": the configured ``budget.min_spending_days``."""

FOCUS: Final[Mapping[str, tuple[str, str, str]]] = {
    "b1.step": ("linkedin", "campaign", "li-cap"),
    "b2.share": ("linkedin", "campaign", "hot"),
    "b2.spending_days": ("linkedin", "campaign", "hot"),
    "b3.allocation": ("linkedin", "campaign", "receiver"),
    "b3.idle_known": ("linkedin", "campaign", "slack"),
    "b3.no_receiver": ("linkedin", "campaign", "donor-a"),
    "b4.contract_unknown": ("google", "campaign", "weekly"),
    "b4.fractional": ("google", "campaign", "weekly"),
    "b4.google_last_click": ("google", "campaign", "weekly"),
    "b4.linkedin": ("linkedin", "campaign", "li-cap"),
    "b4.no_exposure": ("google", "campaign", "weekly"),
    "b4.rate_test": ("linkedin", "campaign", "li-cap"),
    "budget.lifetime": ("linkedin", "campaign", "li-cap"),
    "budget.unknown_not_idle": ("linkedin", "campaign", "slack"),
    "g1.b2_target": ("linkedin", "campaign", "hot"),
    "shared.aggregate_b2": ("google", "budget", "pool"),
    "shared.decision_subject": ("google", "budget", "pool"),
    "shared.member_changed_intraday": ("google", "budget", "pool"),
    "shared.membership_mid_window": ("google", "budget", "pool"),
    "shared.mixed_objectives": ("google", "budget", "pool"),
    "shared.poor_member": ("google", "budget", "pool"),
}
"""The entity each scenario topic is about: the subject a silent case is judged on.

Keyed by ``"<topic>.<case>"``; a catalogue entry added without a focus fails the guard with a
message naming it, so no scenario of this module is ever left unchecked.
"""

_HISTORY_COUNTS: Final[Mapping[str, str]] = {
    "known_days": "known_active",
    "spending_days": "spending",
    "idle_days": "idle",
    "changed_intraday_days": "changed_intraday",
    "unobserved_days": "unobserved",
    "capped_days": "capped",
    "high_utilization_days": "high",
    "under_utilized_days": "under",
}
"""Expectation keys of a silent case that name a budget-history day count."""

_CARD_DAY_METRICS: Final[tuple[str, ...]] = (
    "known_days",
    "spending_days",
    "spending_day_share",
    "idle_days",
    "changed_intraday_days",
    "unobserved_days",
)
"""The day counts every budget card shows beside its utilization (AAP 0.5.4.1 Evidence)."""

_PREDICATE_METRICS: Final[Mapping[str, str]] = {
    B1: "capped_days",
    B2: "high_utilization_days",
    B3: "under_utilized_days",
}
"""The daily predicate each budget rule's card counts against its gate."""


# ---------------------------------------------------------------------------
# Builders and lookups
# ---------------------------------------------------------------------------


def _results(ctx: AnalysisContext) -> list[RuleResult]:
    """The rule modules that produce spend increases or budget items, as a run calls them."""
    return [campaigns.evaluate(ctx), budget.evaluate(ctx), white_papers.evaluate(ctx)]


def _run(ctx: AnalysisContext, *extra: RuleResult) -> PrecedenceOutput:
    """Evaluate the increase-producing rules (plus ``extra``) and apply precedence."""
    return precedence.apply_precedence([*_results(ctx), *extra], ctx)


def _fresh(ctx: AnalysisContext, **changes: Any) -> AnalysisContext:
    """Copy ``ctx`` with ``changes`` and an empty per-run cache, so no memo carries over."""
    return replace(ctx, cache={}, **changes)


def _items(out: PrecedenceOutput, section: str, rule_id: str) -> list[Candidate]:
    """The surviving items of one rule in one section, in rank order."""
    return [
        ranked.candidate for ranked in out.items[section] if ranked.candidate.rule_id == rule_id
    ]


def _survivors(out: PrecedenceOutput) -> list[Candidate]:
    """Every surviving item of the run, section by section."""
    return [ranked.candidate for ranked in out.all_items()]


def _only(out: PrecedenceOutput, section: str, rule_id: str) -> Candidate:
    """The one surviving item of ``rule_id`` in ``section``."""
    found = _items(out, section, rule_id)
    assert len(found) == 1, [item.entity_ref.name for item in found]
    return found[0]


def _lines(out: PrecedenceOutput, rule_id: str, entity_id: int | None = None) -> list[Withheld]:
    """The Budget section's withheld lines of ``rule_id``, optionally about one entity."""
    return [
        line
        for line in out.withheld[BUDGET]
        if line.rule_id == rule_id
        and (entity_id is None or (line.entity is not None and line.entity.id == entity_id))
    ]


def _campaign_id(scenario: Scenario, key: str, source: str = "linkedin") -> int:
    """The internal id the catalogue assigns to a campaign key."""
    return scenario.ids.id(source, "campaign", key)


def _budget_id(scenario: Scenario, key: str) -> int:
    """The internal id the catalogue assigns to a Google budget key."""
    return scenario.ids.id("google", "budget", key)


def _unknown_dates(extra: Mapping[str, Any]) -> tuple[str, ...]:
    """A budget card's unknown days: its changed-intraday and unobserved dates, ascending.

    The evidence lists the two kinds separately, as it counts them; an unknown day is one of
    either, so the union is the whole set.
    """
    return tuple(sorted({*extra["changed_intraday_dates"], *extra["unobserved_dates"]}))


def _topic(scenario: Scenario) -> str:
    """``"<topic>.<case>"`` of a scenario name (its variant dropped)."""
    return scenario.name.rsplit(".", 1)[0]


def _focus(scenario: Scenario) -> SubjectKey:
    """The subject a scenario is about, as a ``(type, id)`` key."""
    try:
        source, entity, key = FOCUS[_topic(scenario)]
    except KeyError:
        pytest.fail(
            f"{scenario.name}: no focus entity in FOCUS; add the subject this scenario is "
            "about so the catalogue guard can check its expectation",
            pytrace=False,
        )
    subject_id = scenario.ids.id(source, entity, key)
    return ("budget", subject_id) if entity == "budget" else ("campaign", subject_id)


def _on(items: Iterable[Candidate], subject: SubjectKey) -> list[Candidate]:
    """The items about ``subject`` (their entity is the campaign or the shared budget)."""
    subject_type, subject_id = subject
    entity = "budget" if subject_type == "budget" else ENTITY_CAMPAIGN
    return [
        item for item in items if (item.entity_ref.type, item.entity_ref.id) == (entity, subject_id)
    ]


def _money(amount: Decimal, currency: str = "USD") -> str:
    """Render an amount as budget cards do: ``USD 1,200.00`` (``JPY 100`` at exponent 0)."""
    places = minor_unit_exponent(currency)
    return f"{currency} {round_to_minor_unit(amount, currency):,.{places}f}"


def _percent(share: Decimal) -> str:
    """Render a configured share as reason texts do: ``0.20`` → ``20%``."""
    return f"{(share * 100).normalize():f}%"


def _increase_targets(items: Iterable[Candidate]) -> frozenset[SubjectKey]:
    """Every subject the given items would raise spend on."""
    return frozenset(target for item in items for target in item.spend_targets)


# ---------------------------------------------------------------------------
# Phase 1: the catalogue guard (simulation/scenarios.py expectations)
# ---------------------------------------------------------------------------


def _fired_value(
    scenario: Scenario, out: PrecedenceOutput, rule_id: str, items: list[Candidate], key: str
) -> object:
    """The post-precedence counterpart of one expectation value of a firing case.

    Allocation keys read the whole run (every donor, the receiver and the allocations made);
    subject and member keys resolve catalogue keys to internal ids; every other key is a
    metric of the one firing item or, failing that, an ``evidence.extra`` entry.
    """
    expected = scenario.expect.values[key]
    if key == "freed":
        return tuple(sorted((item.evidence.extra["freed"] for item in items), reverse=True))
    if key == "allocations":
        return tuple(allocation.amount for allocation in out.allocations)
    if key == "unallocated":
        return sum((item.evidence.extra["unallocated"] for item in items), Decimal(0))
    if key in {"b1_increase", "funded_by_reallocation", "from_new_budget"}:
        receiver = _only(out, BUDGET, B1)
        return receiver.evidence.extra["increase" if key == "b1_increase" else key]
    assert len(items) == 1, (rule_id, [item.entity_ref.name for item in items])
    item = items[0]
    extra = item.evidence.extra
    if key == "subject":
        assert extra["subject"] == {"type": "budget", "id": _budget_id(scenario, str(expected))}
        return expected
    if key == "member":
        member = _campaign_id(scenario, str(expected), "google")
        assert member in [entry["campaign_id"] for entry in extra["members"]], extra["members"]
        return expected
    if key == "member_days":
        member = _campaign_id(scenario, str(scenario.expect.values["member"]), "google")
        (entry,) = [entry for entry in extra["members"] if entry["campaign_id"] == member]
        return entry["member_days"]
    if key == "changed_day":
        assert expected in extra["changed_intraday_dates"], extra["changed_intraday_dates"]
        return expected
    if key == "unknown_days":
        return _unknown_dates(extra)
    metrics_by_key = evidence_metrics(item)
    if key in metrics_by_key:
        return metrics_by_key[key]
    assert key in extra, f"{scenario.name}: no counterpart for expectation key {key!r}"
    return extra[key]


def _silent_value(
    scenario: Scenario, ctx: AnalysisContext, out: PrecedenceOutput, rule_id: str, key: str
) -> object:
    """The counterpart of one expectation value of a silent case, read on its focus subject.

    ``required`` and a key naming the withheld line's unit read that line; ``b3_withheld``
    asks for a B3 line on the same subject; the day counts read the subject's budget history;
    ``utilization`` and ``budget_period`` describe its budget.
    """
    subject = _focus(scenario)
    lines = _lines(out, rule_id, subject[1])
    if key == "required":
        assert lines, f"{scenario.name}: no {rule_id} withheld line on the focus subject"
        return lines[0].required
    if key == "b3_withheld":
        return bool(_lines(out, B3, subject[1]))
    for line in lines:
        if line.unit.replace(" ", "_") == key:
            return line.observed
    history = budget_history(ctx, subject)
    if key in _HISTORY_COUNTS:
        return getattr(history, _HISTORY_COUNTS[key])
    if key == "utilization":
        return "n/a" if history.window_utilization is None else history.window_utilization
    if key == "budget_period":
        campaign = ctx.campaigns[subject[1]]
        lifetime = campaign.daily_budget is None and campaign.total_budget is not None
        return "lifetime" if lifetime else campaign.budget_period
    pytest.fail(f"{scenario.name}: no counterpart for expectation key {key!r}", pytrace=False)


def _check_rule_case(scenario: Scenario, ctx: AnalysisContext, out: PrecedenceOutput) -> None:
    """A B1-B4 expectation, judged on the Budget section after the guard."""
    expect = scenario.expect
    rule_id = expect.rule_id
    assert rule_id is not None
    items = _items(out, BUDGET, rule_id)
    if expect.fires:
        assert items, [line.reason for line in _lines(out, rule_id)]
        for item in items:
            assert (item.section, item.group) == (BUDGET, GROUP_CHANGES)
            assert set(expect.flags) <= set(item.flag_codes), item.flag_codes
        for key, expected in expect.values.items():
            actual = _fired_value(scenario, out, rule_id, items, key)
            assert numeric(actual) == numeric(expected), (key, actual, expected)
    else:
        assert _on(items, _focus(scenario)) == [], [item.action for item in items]
        for key, expected in expect.values.items():
            actual = _silent_value(scenario, ctx, out, rule_id, key)
            assert numeric(actual) == numeric(expected), (key, actual, expected)
    if expect.withheld_reason is not None:
        reasons = [line.reason for line in _lines(out, rule_id)]
        assert any(reason.startswith(expect.withheld_reason) for reason in reasons), reasons


def _check_g1_case(scenario: Scenario, ctx: AnalysisContext, out: PrecedenceOutput) -> None:
    """A G1 expectation: the focus subject is (or is not) blocked and nothing raises it."""
    expect = scenario.expect
    blocked = spend_increase_blocked(ctx)
    focus = _focus(scenario)
    survivors = _survivors(out)
    assert not _increase_targets(survivors) & blocked
    assert (focus in blocked) is expect.fires
    raising = [item for item in _results(ctx)[1].candidates if focus in item.spend_targets]
    for item in raising:
        assert set(expect.flags) <= set(item.flag_codes), item.flag_codes
    for key, expected in expect.values.items():
        actual: object
        if key == "blocked_target":
            actual = None if focus not in blocked else expected
        elif key == "b2":
            actual = bool(_on(_items(out, BUDGET, B2), focus))
        elif key == "b1":
            actual = bool(_on(_items(out, BUDGET, B1), focus))
        elif key == "surviving_increases":
            actual = sum(1 for item in survivors if focus in item.spend_targets)
        elif key == "poor_member":
            member = _campaign_id(scenario, str(expected), "google")
            assert member_poor_efficiency(ctx, focus[1], member)
            actual = expected
        else:
            pytest.fail(f"{scenario.name}: unknown G1 expectation key {key!r}", pytrace=False)
        assert numeric(actual) == numeric(expected), (key, actual, expected)


def _check_c0_case(scenario: Scenario, ctx: AnalysisContext, out: PrecedenceOutput) -> None:
    """The shared-budget decision case: one budget-subject decision and no discrepancy."""
    expect = scenario.expect
    focus = _focus(scenario)
    assert not _items(out, CAMPAIGNS, C0)
    assert bool(ctx.discrepancies) is expect.fires
    members = subject_campaigns(ctx, focus)
    for key, expected in expect.values.items():
        actual: object
        if key == "decision_subject":
            actual = ctx.decisions[focus].shape
        elif key == "budget_discrepancies":
            actual = sum(1 for item in ctx.discrepancies if item.subject_type == "budget")
        elif key == "member_discrepancies":
            actual = sum(
                1
                for item in ctx.discrepancies
                if item.subject_type == "campaign" and item.subject_id in members
            )
        else:
            pytest.fail(f"{scenario.name}: unknown C0 expectation key {key!r}", pytrace=False)
        assert numeric(actual) == numeric(expected), (key, actual, expected)


def test_every_budget_rule_and_the_g1_guard_fire_and_stay_silent_in_the_catalogue() -> None:
    """AAP 0.11.2: B1-B4 and G1 each have a catalogue case that fires and one that does not."""
    fired = {s.expect.rule_id for s in BUDGET_SCENARIOS if s.expect.fires}
    silent = {s.expect.rule_id for s in BUDGET_SCENARIOS if not s.expect.fires}
    assert set(GUARDED) <= fired
    assert set(GUARDED) <= silent
    assert {s.module for s in BUDGET_SCENARIOS} == {MODULE_BUDGET}


@pytest.mark.parametrize(
    "scenario",
    [s for s in BUDGET_SCENARIOS if s.variant == "fires"],
    ids=lambda scenario: scenario.name,
)
def test_each_twin_moves_its_input_across_the_threshold(scenario: Scenario) -> None:
    """AAP 0.11.2: a ``does_not_fire`` twin differs from its ``fires`` case in one input."""
    assert scenario.pair is not None
    twin = SCENARIOS[scenario.pair]
    assert twin.module == MODULE_BUDGET
    assert len(input_differences(scenario, twin)) == 1, input_differences(scenario, twin)


@pytest.mark.parametrize("scenario", BUDGET_SCENARIOS, ids=lambda scenario: scenario.name)
def test_scenario_matches_its_expectation_after_precedence(
    scenario: Scenario, make_context: ContextFactory
) -> None:
    """Every scenario of this module shows its ``Expectation`` once precedence has run.

    B1-B4 cases are judged on the Budget section's surviving items and withheld lines, G1 cases
    on the blocked set and every surviving increase, and the shared-budget decision case on the
    stored decisions and discrepancies the run reads.
    """
    ctx = make_context(scenario.name)
    out = _run(ctx)
    rule_id = scenario.expect.rule_id
    if rule_id == G1:
        _check_g1_case(scenario, ctx, out)
    elif rule_id == C0:
        _check_c0_case(scenario, ctx, out)
    else:
        assert rule_id in (B1, B2, B3, B4), rule_id
        _check_rule_case(scenario, ctx, out)
    assert scenario.expect.finding is None or scenario.expect.finding in [
        finding.code for finding in out.findings[BUDGET]
    ]


# ---------------------------------------------------------------------------
# Phase 2: B1 Increase and the G1 guard (AAP 0.5.4.5 B1, 0.5.4.2 G1)
# ---------------------------------------------------------------------------


def _in_currency(ctx: AnalysisContext, campaign_id: int, currency: str) -> AnalysisContext:
    """Copy ``ctx`` with one campaign and its account moved to ``currency``."""
    campaign = ctx.campaigns[campaign_id]
    account = ctx.accounts[campaign.account_id]
    return _fresh(
        ctx,
        accounts={**ctx.accounts, account.id: replace(account, currency=currency)},
        campaigns={**ctx.campaigns, campaign_id: replace(campaign, currency=currency)},
    )


def _candidate(
    ctx: AnalysisContext,
    rule_id: str,
    section: str,
    group: str,
    entity: EntityRef,
    *,
    targets: Iterable[SubjectKey] = (),
    subject: SubjectKey | None = None,
    extra: Mapping[str, Any] | None = None,
    metrics_shown: Iterable[Metric] = (),
) -> Candidate:
    """A rule-shaped candidate for the guard and allocation steps of precedence.

    Precedence judges candidates by what they declare (``increases_spend_on``, the decision
    subject, ``extra.increase`` / ``extra.freed`` and ``extra.currency``), so a candidate
    carrying those fields stands for any rule output of its kind. Like the C2 Scale, B1 and
    B3 cards, a card outside White papers leads with its window utilization, appended after
    ``metrics_shown``; a white-paper row names no headline.
    """
    datasets = ("linkedin.entities", "linkedin.analytics_daily")
    card = section != WHITE_PAPERS
    utilization = Metric(
        "window_utilization", "Window utilization (spend vs cap)", Decimal("0.95"), "ratio"
    )
    evidence = ctx.make_evidence(
        required_datasets=datasets,
        crm_scope=None,
        metrics=(*metrics_shown, *((utilization,) if card else ())),
        extra=dict(extra or {}),
        headline=utilization.key if card else None,
    )
    return Candidate(
        rule_id=rule_id,
        section=section,
        group=group,
        entity_ref=entity,
        action=f"{rule_id} on {entity.name}",
        reason=f"{rule_id} proposed on {entity.name}.",
        evidence=evidence,
        rank_key=(Decimal(0),),
        flags=(),
        required_datasets=datasets,
        increases_spend_on=tuple(DecisionSubject.of(target) for target in targets),
        decision_subject=None if subject is None else DecisionSubject.of(subject),
    )


def _campaign_ref(ctx: AnalysisContext, campaign_id: int) -> EntityRef:
    """The entity reference of a campaign of ``ctx``."""
    campaign = ctx.campaigns[campaign_id]
    return EntityRef(
        campaign.source, ENTITY_CAMPAIGN, campaign.id, campaign.external_id, campaign.name
    )


def _increases_on(ctx: AnalysisContext, target: int, donor: int) -> RuleResult:
    """Every kind of spend increase naming campaign ``target``, plus a B3 donor beside it.

    A C2 "Scale", a B1 with its increase as headroom, a W1 "Double down" naming the campaign,
    and a B3 donor in the same source and currency whose freed amount would fund the B1.
    """
    target_key: SubjectKey = ("campaign", target)
    asset = EntityRef(
        "linkedin", ENTITY_CONTENT_ASSET, None, content_asset_external_id("guide", "USD"), "Guide"
    )
    return RuleResult(
        candidates=(
            _candidate(
                ctx,
                C2,
                CAMPAIGNS,
                GROUP_SCALE,
                _campaign_ref(ctx, target),
                targets=(target_key,),
                subject=target_key,
            ),
            _candidate(
                ctx,
                B1,
                BUDGET,
                GROUP_CHANGES,
                _campaign_ref(ctx, target),
                targets=(target_key,),
                subject=target_key,
                extra={"increase": Decimal("20.00"), "currency": "USD"},
            ),
            _candidate(
                ctx,
                W1,
                WHITE_PAPERS,
                cohort_group("USD"),
                asset,
                targets=(target_key,),
            ),
            _candidate(
                ctx,
                B3,
                BUDGET,
                GROUP_CHANGES,
                _campaign_ref(ctx, donor),
                subject=("campaign", donor),
                extra={"freed": Decimal("50.00"), "currency": "USD"},
            ),
        )
    )


def test_b1_raises_the_latest_effective_daily_budget_by_the_configured_step(
    make_context: ContextFactory,
) -> None:
    """B1: exactly + ``b1.step`` of the latest effective daily budget, at the minor unit.

    83.33 x 1.2 is 99.996, which the card proposes as 100.00; the item decides the campaign,
    declares it as its only spend target and links the C2 "Scale" it shares an evaluation with.
    """
    scenario = SCENARIOS["b1.step.case"]
    ctx = make_context(scenario.name)
    out = _run(ctx)
    campaign_id = _campaign_id(scenario, "li-cap")
    step = ctx.thresholds.ratio("b1.step")
    currency = ctx.account_currency(campaign_id)
    current = budget_history(ctx, ("campaign", campaign_id)).latest_daily_budget
    assert current is not None
    proposed = round_to_minor_unit(current * (1 + step), currency)
    assert proposed != current * (1 + step)
    item = _only(out, BUDGET, B1)
    shown = evidence_metrics(item)
    assert shown["daily_budget"] == current == item.evidence.extra["current"]
    assert shown["proposed_daily_budget"] == proposed == item.evidence.extra["proposed"]
    assert shown["increase"] == proposed - current == item.evidence.extra["increase"]
    assert item.evidence.extra["step"] == step
    # The increase card leads with the capped budget's utilization.
    assert item.evidence.extra["headline_metric"] == "window_utilization"
    assert "window_utilization" in shown
    assert item.evidence.thresholds["b1.step"] == ctx.thresholds.value("b1.step")
    assert item.action == (
        f"Raise daily budget from {_money(current, currency)} to {_money(proposed, currency)}"
    )
    assert f"raise the daily budget by {_percent(step)}" in item.reason
    assert item.decision_subject == DecisionSubject("campaign", campaign_id)
    assert item.spend_targets == {("campaign", campaign_id)}
    scale = _only(out, CAMPAIGNS, C2)
    assert scale.action == campaigns.SCALE_ACTION
    assert item.evidence.extra["c2_link"] == {
        "rule_id": C2,
        "subject": {"type": "campaign", "id": campaign_id},
    }


def test_b1_step_comes_from_the_configuration(
    make_context: ContextFactory, make_thresholds: Callable[..., Thresholds]
) -> None:
    """B1: a configured ``b1.step`` of 0.25 proposes 83.33 x 1.25 = 104.1625 → 104.16."""
    scenario = SCENARIOS["b1.step.case"]
    tuned = make_thresholds({"b1.step": Decimal("0.25")})
    ctx = make_context(scenario.name, thresholds=tuned)
    item = _only(_run(ctx), BUDGET, B1)
    current = evidence_metrics(item)["daily_budget"]
    assert item.evidence.extra["step"] == Decimal("0.25")
    assert evidence_metrics(item)["proposed_daily_budget"] == round_to_minor_unit(
        current * Decimal("1.25"), "USD"
    )
    assert evidence_metrics(item)["proposed_daily_budget"] == Decimal("104.16")


def test_b1_example_amount_rounds_to_the_cent() -> None:
    """B1 rounding (AAP 0.5.4.5): 123.45 USD raised by 20% is 148.14 at the minor unit."""
    assert round_to_minor_unit(Decimal("123.45") * Decimal("1.20"), "USD") == Decimal("148.14")
    assert round_to_minor_unit(Decimal("123.456") * Decimal("1.20"), "USD") == Decimal("148.15")


@pytest.mark.parametrize(
    ("currency", "proposed"),
    [("USD", Decimal("100.00")), ("JPY", Decimal("100")), ("KWD", Decimal("99.996"))],
)
def test_b1_rounds_the_proposal_to_each_currencys_minor_unit(
    make_context: ContextFactory, currency: str, proposed: Decimal
) -> None:
    """B1: a zero-decimal currency proposes whole units; a three-decimal one keeps mils.

    The value sits at the minor unit, with no finer digit; the evidence records it at six
    places, like every budget amount, and the action states it at the minor unit.
    """
    scenario = SCENARIOS["b1.step.case"]
    campaign_id = _campaign_id(scenario, "li-cap")
    ctx = _in_currency(make_context(scenario.name), campaign_id, currency)
    item = _only(_run(ctx), BUDGET, B1)
    shown = evidence_metrics(item)
    assert shown["proposed_daily_budget"] == proposed
    minor_unit = Decimal(1).scaleb(-minor_unit_exponent(currency))
    assert shown["proposed_daily_budget"] == shown["proposed_daily_budget"].quantize(minor_unit)
    assert shown["proposed_daily_budget"].as_tuple().exponent == -6
    assert item.action.endswith(f"to {_money(proposed, currency)}")


def test_no_b1_exists_under_a_c2_hold(make_context: ContextFactory) -> None:
    """B1: under a veto "Hold" the C2 item declares no increase and no B1 is emitted."""
    out = _run(make_context("c2.outcome_hold.case"))
    hold = _only(out, CAMPAIGNS, C2)
    assert hold.action == campaigns.HOLD_ACTION
    assert hold.increases_spend_on == ()
    assert _items(out, BUDGET, B1) == []
    assert _lines(out, B1) == []


def test_g1_removes_every_spend_increase_on_a_b2_target(make_context: ContextFactory) -> None:
    """G1 on a B2 target: no C2 Scale, B1, B3 reallocation into it or W1 naming it survives.

    The target meets B2 (high utilization with cost per outcome at least
    ``b2.poor_efficiency_ratio`` x baseline). Every kind of increase naming it is presented to
    the guard; each is removed and counted ``g1_blocked`` in its own section, B2 itself stays,
    and the donor beside it keeps its whole freed amount as a plain decrease.

    The catalogue's B2 target is too inefficient for any rule to propose scaling it, so each
    kind of increase is presented as a rule-shaped candidate (:func:`_increases_on`); the
    increases rules build themselves are removed in
    :func:`test_shared_budget_increases_built_against_g1_are_removed_and_counted`.
    """
    scenario = SCENARIOS["g1.b2_target.fires"]
    ctx = make_context(scenario.name)
    hot = _campaign_id(scenario, "hot")
    efficient = _campaign_id(scenario, "efficient")
    assessment = assess_b2(ctx, ("campaign", hot))
    assert assessment.meets
    assert assessment.branch == "poor_efficiency"
    assert assessment.cpo is not None
    assert assessment.baseline is not None
    assert assessment.cpo >= ctx.thresholds.ratio("b2.poor_efficiency_ratio") * assessment.baseline
    assert spend_increase_blocked(ctx) == {("campaign", hot)}
    out = _run(ctx, _increases_on(ctx, hot, efficient))
    survivors = _survivors(out)
    assert all(("campaign", hot) not in item.spend_targets for item in survivors)
    assert [item.entity_ref.id for item in _items(out, BUDGET, B2)] == [hot]
    assert _items(out, BUDGET, B1) == []
    assert _items(out, CAMPAIGNS, C2) == []
    assert _items(out, WHITE_PAPERS, W1) == []
    assert out.allocations == ()
    (donor,) = _items(out, BUDGET, B3)
    assert donor.evidence.extra["unallocated"] == Decimal("50.00")
    assert "allocations" not in donor.evidence.extra
    for section in (CAMPAIGNS, BUDGET, WHITE_PAPERS):
        assert out.suppressed[section][SUPPRESS_G1_BLOCKED] == 1, section
    assert out.suppressed_total(SUPPRESS_G1_BLOCKED) == 3


def test_g1_twin_below_the_poor_efficiency_ratio_keeps_its_increase(
    make_context: ContextFactory,
) -> None:
    """G1 twin: the same capped campaign with efficient leads is not blocked and scales.

    High utilization alone never blocks: efficiency below ``b2.poor_efficiency_ratio`` x
    baseline leaves the C2 "Scale" and its B1 standing, and nothing is counted ``g1_blocked``.
    """
    scenario = SCENARIOS["g1.b2_target.does_not_fire"]
    ctx = make_context(scenario.name)
    hot: SubjectKey = ("campaign", _campaign_id(scenario, "hot"))
    assessment = assess_b2(ctx, hot)
    assert assessment.gate.passed
    assert assessment.cpo is not None
    assert assessment.baseline is not None
    assert assessment.cpo < ctx.thresholds.ratio("b2.poor_efficiency_ratio") * assessment.baseline
    assert not assessment.meets
    assert hot not in spend_increase_blocked(ctx)
    out = _run(ctx)
    assert _on(_items(out, BUDGET, B1), hot)
    assert _on(_items(out, CAMPAIGNS, C2), hot)
    assert _on(_items(out, BUDGET, B2), hot) == []
    assert out.suppressed_total(SUPPRESS_G1_BLOCKED) == 0


def test_shared_budget_increases_built_against_g1_are_removed_and_counted(
    make_context: ContextFactory,
) -> None:
    """G1: a blocked shared budget's own C2 "Scale" and B1 are built, then removed by the guard.

    ``campaigns.scale_outcomes`` keeps a blocked subject's Scale outcome (marked
    ``g1_blocked``) so the guard, not the rule, removes it and records the suppression.
    """
    scenario = SCENARIOS["shared.poor_member.fires"]
    ctx = make_context(scenario.name)
    pool: SubjectKey = ("budget", _budget_id(scenario, "pool"))
    built = [item for item in budget.evaluate(ctx).candidates if item.rule_id == B1]
    assert [item.evidence.extra.get("g1_blocked") for item in built] == [True]
    out = _run(ctx)
    assert pool not in _increase_targets(_survivors(out))
    assert out.suppressed[BUDGET][SUPPRESS_G1_BLOCKED] == 1
    assert out.suppressed[CAMPAIGNS][SUPPRESS_G1_BLOCKED] == 1


def test_w1_never_names_a_campaign_g1_blocks(make_context: ContextFactory) -> None:
    """G1 and W1: "Double down" names only campaigns the guard leaves unblocked."""
    ctx = make_context("w1.unblocked_only.case")
    blocked = spend_increase_blocked(ctx)
    out = _run(ctx)
    (b2,) = _items(out, BUDGET, B2)
    assert ("campaign", b2.entity_ref.id) in blocked
    double_down = _items(out, WHITE_PAPERS, W1)
    assert double_down
    assert all(item.spend_targets and not item.spend_targets & blocked for item in double_down)


@pytest.mark.parametrize("case", ["shared_budget", "b2_target"])
def test_bypassing_the_g1_filtering_step_trips_the_runner_assertion(
    make_context: ContextFactory, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """G1 runner assertion: with the filtering step passing everything through, the
    independent post-check (on the unpatched ``metrics.spend_increase_blocked``) raises."""
    if case == "shared_budget":
        scenario = SCENARIOS["shared.poor_member.fires"]
        ctx = make_context(scenario.name)
        extra: tuple[RuleResult, ...] = ()
    else:
        scenario = SCENARIOS["g1.b2_target.fires"]
        ctx = make_context(scenario.name)
        extra = (
            _increases_on(ctx, _campaign_id(scenario, "hot"), _campaign_id(scenario, "efficient")),
        )
    assert spend_increase_blocked(ctx)

    def pass_through(
        candidates: Iterable[Candidate], blocked: frozenset[SubjectKey]
    ) -> tuple[tuple[Candidate, ...], tuple[Candidate, ...]]:
        del blocked
        return tuple(candidates), ()

    monkeypatch.setattr(precedence, "apply_g1", pass_through)
    with pytest.raises(PrecedenceInvariantError, match="G1"):
        _run(ctx, *extra)


# ---------------------------------------------------------------------------
# Phase 3: spending-day gates and the budget card (AAP 0.5.4.1, 0.5.4.5)
# ---------------------------------------------------------------------------


def _history_of(scenario: Scenario, ctx: AnalysisContext, key: str) -> BudgetHistory:
    """The budget history of a LinkedIn campaign of the scenario."""
    return budget_history(ctx, ("campaign", _campaign_id(scenario, key)))


def test_four_spending_days_of_five_withhold_b2_and_b3(make_context: ContextFactory) -> None:
    """Spending-day gate: 7 known-budget days with 4 spending days withhold B2 and B3.

    Both lines lead with "spending days 4 of 5 required" and carry 4 observed against the
    configured ``budget.min_spending_days`` (5); neither rule emits an item.
    """
    scenario = SCENARIOS["b2.spending_days.case"]
    ctx = make_context(scenario.name)
    hot = _campaign_id(scenario, "hot")
    minimum = ctx.thresholds.int("budget.min_spending_days")
    assert minimum == SPENDING_GATE_MINIMUM
    history = _history_of(scenario, ctx, "hot")
    assert (history.known_active, history.spending) == (7, 4)
    assert history.known_active >= ctx.thresholds.int("budget.min_known_days")
    out = _run(ctx)
    for rule_id in (B2, B3):
        assert _on(_items(out, BUDGET, rule_id), ("campaign", hot)) == []
        (line,) = _lines(out, rule_id, hot)
        assert line.reason.startswith(f"spending days 4 of {minimum} required"), line.reason
        assert (line.observed, line.required, line.unit) == (4, minimum, "spending days")


def test_b3_fires_on_eight_under_utilized_of_ten_known_days_with_four_idle(
    make_context: ContextFactory,
) -> None:
    """Spending-day gate: 10 known days, 6 spending, 4 known idle, u_d <= 0.5 on 8 → B3.

    The proposal is the ``b3.spend_percentile`` of daily spend over the known-budget days
    (idle days included, at 0) x ``b3.headroom``, cross-checked with ``stats.percentile``.
    """
    scenario = SCENARIOS["b3.idle_known.fires"]
    ctx = make_context(scenario.name)
    history = _history_of(scenario, ctx, "slack")
    assert (history.known_active, history.spending, history.idle, history.under) == (10, 6, 4, 8)
    spends = [day.spend for day in history.days if day.known_active]
    assert spends.count(Decimal(0)) == history.idle
    quantile = ctx.thresholds.ratio("b3.spend_percentile") / 100
    observed = percentile(spends, quantile)
    assert observed is not None
    proposed = round_to_minor_unit(observed * ctx.thresholds.ratio("b3.headroom"), "USD")
    current = history.latest_daily_budget
    assert current is not None
    item = _only(_run(ctx), BUDGET, B3)
    shown = evidence_metrics(item)
    assert shown["spend_percentile"] == observed
    assert shown["proposed_daily_budget"] == proposed == Decimal("88.00")
    assert item.action == f"Lower daily budget to {_money(proposed)}"
    assert shown["freed"] == current - proposed == item.evidence.extra["freed"]
    assert (shown["idle_days"], shown["under_utilized_days"]) == (4, 8)
    # The decrease card leads with the budget's low utilization.
    assert item.evidence.extra["headline_metric"] == "window_utilization"
    assert "window_utilization" in shown
    assert item.decision_subject == DecisionSubject("campaign", _campaign_id(scenario, "slack"))
    assert item.increases_spend_on == ()


def test_a_budget_that_only_idles_is_no_b3_donor(make_context: ContextFactory) -> None:
    """Spending-day gate: 10 known idle days and no spending day are idle, not a donor.

    No B3 item and no withheld line: a budget that never serves frees nothing to reallocate.
    """
    scenario = SCENARIOS["b3.idle_known.does_not_fire"]
    ctx = make_context(scenario.name)
    history = _history_of(scenario, ctx, "slack")
    assert (history.known_active, history.spending, history.idle) == (10, 0, 10)
    out = _run(ctx)
    assert _items(out, BUDGET, B3) == []
    assert _lines(out, B3) == []
    assert out.allocations == ()


@pytest.mark.parametrize(
    ("name", "high_days", "fires"),
    [("b2.share.fires", 5, True), ("b2.share.does_not_fire", 4, False)],
)
def test_b2_needs_high_utilization_on_the_sustained_share_of_known_days(
    make_context: ContextFactory, name: str, high_days: int, fires: bool
) -> None:
    """B2 gate: high-utilization days on 5 of 7 (71%) fire with poor efficiency; 4 of 7 do not.

    The firing card reads "Do not increase. Lower daily budget by 20% or fix efficiency first"
    (the step is ``b1.step``), names the ``poor_efficiency`` branch and prefills the 20% lower
    budget; a full history short of the share yields nothing, not even a withheld line.
    """
    scenario = SCENARIOS[name]
    ctx = make_context(scenario.name)
    hot: SubjectKey = ("campaign", _campaign_id(scenario, "hot"))
    history = budget_history(ctx, hot)
    share = ctx.thresholds.ratio("budget.sustained_share")
    assert (history.high, history.known_active) == (high_days, 7)
    assert (Decimal(history.high) / Decimal(history.known_active) >= share) is fires
    out = _run(ctx)
    found = _on(_items(out, BUDGET, B2), hot)
    if not fires:
        assert found == []
        assert _lines(out, B2, hot[1]) == []
        return
    (item,) = found
    step = ctx.thresholds.ratio("b1.step")
    assert item.action == (
        f"Do not increase. Lower daily budget by {_percent(step)} or fix efficiency first"
    )
    assert item.action == "Do not increase. Lower daily budget by 20% or fix efficiency first"
    assert item.evidence.extra["branch"] == "poor_efficiency"
    shown = evidence_metrics(item)
    # The poor-efficiency branch leads with the cost per outcome that is poor.
    assert item.evidence.extra["headline_metric"] == "cost_per_outcome"
    assert shown["cost_per_outcome"] is not None
    assert shown["high_utilization_days"] == high_days
    assert shown["proposed_daily_budget"] == round_to_minor_unit(
        shown["daily_budget"] * (1 - step), "USD"
    )
    assert item.increases_spend_on == ()
    assert item.decision_subject == DecisionSubject.of(hot)
    assert hot in spend_increase_blocked(ctx)


def test_b2_zero_outcome_branch_needs_the_expected_outcome_minimum(
    make_context: ContextFactory,
) -> None:
    """B2 zero-outcome branch: 0 outcomes where at least ``b2.min_expected_outcomes`` were due.

    The B2 target's leads are removed from its days; spend and utilization are unchanged, so
    the high-utilization gate still passes and the branch is ``zero_outcomes``.
    """
    scenario = SCENARIOS["g1.b2_target.fires"]
    base = make_context(scenario.name)
    hot = _campaign_id(scenario, "hot")
    days = tuple(replace(day, leads=0) for day in base.campaign_days[hot])
    ctx = _fresh(base, campaign_days={**base.campaign_days, hot: days})
    out = _run(ctx)
    (item,) = _on(_items(out, BUDGET, B2), ("campaign", hot))
    shown = evidence_metrics(item)
    assert item.evidence.extra["branch"] == "zero_outcomes"
    # Zero outcomes have no cost per outcome, so the card leads with the outcomes themselves;
    # the cost of a known zero denominator reads "—", never unavailable (AAP 0.5.5).
    assert item.evidence.extra["headline_metric"] == "outcomes"
    assert shown["outcomes"] == 0
    assert shown["cost_per_outcome"] == "—"
    assert "unavailable" not in item.evidence.metric("cost_per_outcome").to_json()
    assert shown["expected_outcomes"] >= ctx.thresholds.ratio("b2.min_expected_outcomes")
    assert ("campaign", hot) in spend_increase_blocked(ctx)


def _zero_outcome_target(
    scenario: Scenario, base: AnalysisContext, first_day_leads: int
) -> AnalysisContext:
    """``g1.b2_target.fires`` with hot at 0 leads and an account baseline set by its peer.

    hot keeps its spend (97.00 a day of its 100.00 budget), so its budget history and
    high-utilization gate are the catalogue's, day for day. The efficient peer spends 873.00 a
    day, nine times hot's, so the account spends exactly ten times what hot does, and it
    reports 1 lead a day except ``first_day_leads`` on the window's first day: 30 account
    leads make hot's spend worth exactly 3 expected leads, 29 make it 2.9. That day's leads
    are the single input the twins move.
    """
    hot = _campaign_id(scenario, "hot")
    efficient = _campaign_id(scenario, "efficient")
    first = base.window.start
    peer = tuple(
        replace(day, cost=Decimal("873.00"), leads=first_day_leads if day.date == first else 1)
        for day in base.campaign_days[efficient]
    )
    target = tuple(replace(day, leads=0) for day in base.campaign_days[hot])
    return _fresh(base, campaign_days={**base.campaign_days, hot: target, efficient: peer})


def _account_totals(ctx: AnalysisContext, campaign_id: int) -> Totals:
    """Window totals of every campaign in ``campaign_id``'s account and objective group."""
    campaign = ctx.campaigns[campaign_id]
    return window_totals(
        ctx,
        [
            other.id
            for other in ctx.campaigns.values()
            if (other.account_id, other.objective_group)
            == (campaign.account_id, campaign.objective_group)
        ],
    )


@pytest.mark.parametrize(
    ("first_day_leads", "expected", "fires"),
    [(3, Decimal(3), True), (2, Decimal("2.9"), False)],
)
def test_b2_zero_outcome_branch_fires_at_three_expected_outcomes_and_not_at_two_point_nine(
    make_context: ContextFactory, first_day_leads: int, expected: Decimal, fires: bool
) -> None:
    """B2 and G1, zero-outcome branch: 3.0 expected outcomes fire / 2.9 do not.

    Both twins keep hot's high-utilization history and its 0 leads; only the peer's leads on
    one day move the account baseline (:func:`_zero_outcome_target`), so hot's spend is worth
    exactly ``b2.min_expected_outcomes`` (3) expected leads in one twin and 2.9 in the other.
    At 3.0 B2 fires on its ``zero_outcomes`` branch and G1 removes every increase naming hot
    (C2 Scale, B1, W1); at 2.9 hot meets neither branch: no B2 item, no withheld line, no
    block, and every increase on it stands.
    """
    scenario = SCENARIOS["g1.b2_target.fires"]
    base = make_context(scenario.name)
    hot = _campaign_id(scenario, "hot")
    efficient = _campaign_id(scenario, "efficient")
    subject: SubjectKey = ("campaign", hot)
    ctx = _zero_outcome_target(scenario, base, first_day_leads)
    assert ctx.thresholds.ratio("b2.min_expected_outcomes") == 3
    assert budget_history(ctx, subject).counts() == budget_history(base, subject).counts()
    assessment = assess_b2(ctx, subject)
    assert assessment.gate.passed
    assert assessment.outcomes == 0
    account = _account_totals(ctx, hot)
    assert account.spend == 10 * assessment.spend
    assert account.outcomes == ctx.window.days - 1 + first_day_leads
    # Expected outcomes = spend ÷ (account spend ÷ account outcomes), exactly (AAP 0.5.4.1).
    assert assessment.spend * account.outcomes / account.spend == expected
    assert assessment.expected is not None
    assert assessment.expected.quantize(Decimal("0.000001")) == expected
    out = _run(ctx, _increases_on(ctx, hot, efficient))
    found = _on(_items(out, BUDGET, B2), subject)
    assert _lines(out, B2, hot) == []
    if not fires:
        assert (assessment.meets, assessment.branch) == (False, None)
        assert found == []
        assert subject not in spend_increase_blocked(ctx)
        assert subject in _increase_targets(_survivors(out))
        assert out.suppressed_total(SUPPRESS_G1_BLOCKED) == 0
        return
    assert (assessment.meets, assessment.branch) == (True, "zero_outcomes")
    (item,) = found
    shown = evidence_metrics(item)
    assert item.evidence.extra["branch"] == "zero_outcomes"
    assert (shown["outcomes"], shown["cost_per_outcome"]) == (0, "—")
    assert shown["expected_outcomes"] == expected
    assert "with 0 leads where 3.0 leads were expected" in item.reason
    assert subject in spend_increase_blocked(ctx)
    assert subject not in _increase_targets(_survivors(out))
    assert out.suppressed_total(SUPPRESS_G1_BLOCKED) == 3


@pytest.mark.parametrize("unreported", ["known_zero", "one_day", "every_day"])
def test_b2_never_reads_unreported_outcomes_as_zero(
    make_context: ContextFactory, unreported: str
) -> None:
    """B2 and G1, unknown outcomes: unreported leads are unavailable, never 0 (AAP 0.11.1).

    hot at a known 0 leads on every day (the context of
    :func:`test_b2_zero_outcome_branch_needs_the_expected_outcome_minimum`) fires B2 on its
    ``zero_outcomes`` branch and is blocked. With its leads unreported (``None``) on one day, or
    on every day, and 0 on the rest, hot's outcomes are unavailable while its spend and
    utilization are unchanged: neither branch is judged, so there is no B2 item, no withheld
    line and no G1 block. hot belongs to its own account baseline, so that is unavailable too.
    """
    scenario = SCENARIOS["g1.b2_target.fires"]
    base = make_context(scenario.name)
    hot = _campaign_id(scenario, "hot")
    subject: SubjectKey = ("campaign", hot)
    days = sorted(base.campaign_days[hot], key=lambda day: day.date)
    count = {"known_zero": 0, "one_day": 1, "every_day": len(days)}[unreported]
    leads = tuple(replace(day, leads=None if n < count else 0) for n, day in enumerate(days))
    ctx = _fresh(base, campaign_days={**base.campaign_days, hot: leads})
    assert window_totals(ctx, (hot,)).unreported_days == count
    assessment = assess_b2(ctx, subject)
    assert assessment.gate.passed
    out = _run(ctx)
    found = _on(_items(out, BUDGET, B2), subject)
    assert _lines(out, B2, hot) == []
    if not count:
        assert (assessment.outcomes, assessment.branch) == (0, "zero_outcomes")
        assert [item.evidence.extra["branch"] for item in found] == ["zero_outcomes"]
        assert subject in spend_increase_blocked(ctx)
        return
    assert assessment.outcomes is None
    assert (assessment.meets, assessment.branch) == (False, None)
    assert (assessment.cpo, assessment.baseline, assessment.expected) == (None, None, None)
    assert found == []
    assert subject not in spend_increase_blocked(ctx)


def test_unknown_days_are_never_counted_as_idle(make_context: ContextFactory) -> None:
    """Unknown days (unobserved, changed intraday) are counted apart, never as idle days.

    Four window days before the first snapshot are unobserved: the slack campaign keeps 6
    known-budget days, 0 idle days, and B3 is withheld "known-budget days 6 of 7 required".
    """
    scenario = SCENARIOS["budget.unknown_not_idle.case"]
    ctx = make_context(scenario.name)
    slack = _campaign_id(scenario, "slack")
    history = _history_of(scenario, ctx, "slack")
    assert (history.known_active, history.idle, history.unobserved) == (6, 0, 4)
    unknown = [day for day in history.days if day.klass != "known"]
    assert len(unknown) == history.unobserved + history.changed_intraday
    assert all(not day.idle and not day.known_active for day in unknown)
    out = _run(ctx)
    (line,) = _lines(out, B3, slack)
    required = ctx.thresholds.int("budget.min_known_days")
    assert line.reason == f"known-budget days 6 of {required} required"
    assert (line.observed, line.required) == (6, required)

    shared = SCENARIOS["shared.member_changed_intraday.case"]
    shared_ctx = make_context(shared.name)
    pool_history = budget_history(shared_ctx, ("budget", _budget_id(shared, "pool")))
    assert pool_history.changed_intraday == 1
    assert pool_history.idle == 0
    item = _only(_run(shared_ctx), BUDGET, B1)
    extra = item.evidence.extra
    assert extra["changed_intraday_dates"] == ("2026-09-27",)
    assert extra["idle_dates"] == ()
    assert evidence_metrics(item)["changed_intraday_days"] == 1
    assert evidence_metrics(item)["idle_days"] == 0


def test_budget_rules_are_withheld_below_the_sustained_window_minimum(
    make_context: ContextFactory, make_thresholds: Callable[..., Thresholds]
) -> None:
    """Sustained patterns only (AAP 0.5.4.1): below ``window.sustained_min_days`` B1-B4 are
    withheld for the run with observed against required days, never shown as items.

    The 7-day window that fires B2 at the configured minimum of 7 is judged against a minimum
    of 8, so the same history yields only "window 7 days of 8 required" lines.
    """
    scenario = SCENARIOS["b2.share.fires"]
    assert _on(_items(_run(make_context(scenario.name)), BUDGET, B2), _focus(scenario))
    ctx = make_context(scenario.name, thresholds=make_thresholds({"window.sustained_min_days": 8}))
    assert ctx.window.days == 7
    assert not ctx.window.sustained_ok
    out = _run(ctx)
    for rule_id in (B1, B2, B3, B4):
        assert _items(out, BUDGET, rule_id) == [], rule_id
        lines = _lines(out, rule_id)
        assert lines, rule_id
        for line in lines:
            assert line.reason == "window 7 days of 8 required", (rule_id, line.reason)
            assert (line.observed, line.required, line.unit) == (7, 8, "days")
    assert _items(out, CAMPAIGNS, C2) == []


def _budget_items(make_context: ContextFactory) -> list[tuple[str, AnalysisContext, Candidate]]:
    """Every surviving Budget item of every scenario of this module, with its context."""
    found: list[tuple[str, AnalysisContext, Candidate]] = []
    for scenario in BUDGET_SCENARIOS:
        ctx = make_context(scenario.name)
        found.extend((scenario.name, ctx, item) for item in _items_of(_run(ctx), BUDGET))
    return found


def _items_of(out: PrecedenceOutput, section: str) -> list[Candidate]:
    """Every surviving item of one section, in rank order."""
    return [ranked.candidate for ranked in out.items[section]]


def test_every_budget_card_shows_utilization_beside_efficiency(
    make_context: ContextFactory,
) -> None:
    """Budget cards (AAP 0.5.4.5): utilization with every day count, observed against required,
    beside cost per outcome against its baseline, outcomes, the daily budget and the exact
    window with each source's day boundary."""
    seen: set[str] = set()
    for name, ctx, item in _budget_items(make_context):
        seen.add(item.rule_id)
        keys = [metric.key for metric in item.evidence.metrics]
        for key in ("window_utilization", "cost_per_outcome", "outcomes", "daily_budget"):
            assert key in keys, (name, item.rule_id, key)
        for key in _CARD_DAY_METRICS:
            assert key in keys, (name, item.rule_id, key)
        baselines = [baseline.key for baseline in item.evidence.baselines]
        assert "baseline_cost_per_outcome" in baselines, (name, item.rule_id)
        window = item.evidence.window
        assert (str(window["start"]), str(window["end"]), window["days"]) == (
            ctx.window.start.isoformat(),
            ctx.window.end.isoformat(),
            ctx.window.days,
        )
        source_days = {entry["source"]: entry["boundary"] for entry in window["source_days"]}
        assert source_days, (name, item.rule_id)
        assert all(source_days.values()), (name, source_days)
        boundary = item.evidence.extra["day_boundary"]
        assert boundary["boundary"] == source_days[boundary["source"]], (name, boundary)
        if item.rule_id in BUDGET_ITEM_RULES:
            predicate = _PREDICATE_METRICS[item.rule_id]
            assert predicate in keys, (name, item.rule_id)
            assert f"{predicate}_share" in keys, (name, item.rule_id)
            assert "proposed_daily_budget" in keys, (name, item.rule_id)
            sample = item.evidence.sample
            assert sample is not None
            assert sample.required == ctx.thresholds.int("budget.min_known_days")
            assert sample.observed == evidence_metrics(item)["known_days"]
            gate = item.evidence.extra["gate"]
            assert gate, (name, item.rule_id)
            assert all({"observed", "required"} <= set(check) for check in gate.values())
            assert "unchanged across" in item.evidence.extra["observation_assumption"]
            for key in ("budget.min_known_days", "budget.min_spending_days"):
                assert item.evidence.thresholds[key] == ctx.thresholds.value(key)
        else:
            assert item.evidence.extra["gate"]["outcomes"]["required"] == ctx.thresholds.int(
                "b4.min_outcomes"
            )
    assert seen == {B1, B2, B3, B4}


_SIX_PLACE_EXTRAS: Final[tuple[str, ...]] = (
    "current",
    "proposed",
    "increase",
    "freed",
    "allocated",
    "unallocated",
    "funded_by_reallocation",
    "from_new_budget",
)
"""The ``evidence.extra`` amounts a budget card records at six places, beside each
``allocations[].amount``."""

_FUNDING_KEYS: Final[Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    B1: (
        ("daily_budget", "proposed_daily_budget", "increase", "outcomes"),
        ("current", "proposed", "increase", "funded_by_reallocation", "from_new_budget"),
    ),
    B3: (
        ("daily_budget", "proposed_daily_budget", "freed", "outcomes"),
        ("current", "proposed", "freed", "allocated", "unallocated"),
    ),
}
"""``(metric keys, extra keys)`` a funded B1 receiver and a giving B3 donor must record."""


def _six_places(value: object, rendered: object) -> bool:
    """Whether a recorded ``Decimal`` and its stored JSON text both carry six places."""
    return (
        isinstance(value, Decimal)
        and value.as_tuple().exponent == -6
        and isinstance(rendered, str)
        and len(rendered.partition(".")[2]) == 6
    )


@pytest.mark.parametrize(
    ("name", "source"),
    [("default", "google"), ("b3.allocation.case", "linkedin"), ("b1.step.case", None)],
)
def test_budget_evidence_records_every_amount_and_outcome_count_at_six_places(
    make_context: ContextFactory, name: str, source: str | None
) -> None:
    """Budget evidence keeps one scale per metric, whatever the source: ``numeric(18,6)``.

    A Google budget read from whole micros, a LinkedIn budget, a proposal at the currency's
    minor unit, the B1 increase, the B3 freed amount and its allocation split, and the window
    outcomes (Google conversions are decimals, LinkedIn leads a whole count) are all recorded
    at six places, in the metrics, in ``extra`` and in the stored JSON, on funded and unfunded
    items alike. ``default`` and ``b3.allocation.case`` each hold a funded B1 receiver and a
    giving B3 donor of ``source``, and ``default`` adds unfunded Google and LinkedIn donors, a
    B2 and B4 cards; ``b1.step.case`` holds a B1 nothing funds (``source`` ``None``), whose
    zero reallocation keeps the same scale. Day counts and the B4 side outcome counts the rate
    test ran on stay integers.
    """
    out = _run(make_context(name))
    items = _items_of(out, BUDGET)
    funded = {
        item.rule_id: item
        for item in items
        if item.entity_ref.source == source and item.evidence.extra.get("allocations")
    }
    if source is None:
        assert out.allocations == ()
        unfunded = _only(out, BUDGET, B1).evidence.extra
        assert unfunded["funded_by_reallocation"] == 0
        assert unfunded["from_new_budget"] == unfunded["increase"]
    else:
        assert set(funded) == {B1, B3}, (name, sorted(funded))
        for rule_id, (metric_keys, extra_keys) in _FUNDING_KEYS.items():
            shown = evidence_metrics(funded[rule_id])
            assert set(metric_keys) <= set(shown), (name, rule_id)
            assert set(extra_keys) <= set(funded[rule_id].evidence.extra), (name, rule_id)
        assert out.allocations
    assert all(allocation.amount.as_tuple().exponent == -6 for allocation in out.allocations)
    for item in items:
        where = (name, item.rule_id, item.entity_ref.source, item.entity_ref.name)
        rendered = item.evidence.to_json()
        shown_json = {metric["key"]: metric["value"] for metric in rendered["metrics"]}
        for metric in item.evidence.metrics:
            if metric.key in ("weekend_outcomes", "weekday_outcomes", "known_days"):
                assert type(metric.value) is int, (*where, metric.key)
            elif (metric.unit == "currency" or metric.key == "outcomes") and not isinstance(
                metric.value, str | None
            ):
                assert _six_places(metric.value, shown_json[metric.key]), (
                    *where,
                    metric.key,
                    metric.value,
                )
        extra = item.evidence.extra
        for key in _SIX_PLACE_EXTRAS:
            if key in extra:
                assert _six_places(extra[key], rendered["extra"][key]), (*where, key, extra[key])
        for entry, entry_json in zip(
            extra.get("allocations", ()), rendered["extra"].get("allocations", []), strict=True
        ):
            assert _six_places(entry["amount"], entry_json["amount"]), (*where, entry)


# ---------------------------------------------------------------------------
# Phase 4: shared budgets (AAP 0.5.4.5)
# ---------------------------------------------------------------------------

SHARED_ITEM_SCENARIOS: Final[tuple[str, ...]] = (
    "shared.aggregate_b2.case",
    "shared.decision_subject.case",
    "shared.member_changed_intraday.case",
    "shared.membership_mid_window.case",
    "shared.mixed_objectives.case",
    "shared.poor_member.does_not_fire",
)
"""Shared-budget scenarios whose budget-level item survives precedence."""


def test_budget_active_days_follow_dated_membership(make_context: ContextFactory) -> None:
    """Shared budgets: a campaign that joined mid-window counts only on its member days.

    m3 moved onto the pool during 2026-09-21: that day's membership is not whole-day known,
    so it is no budget-active day, and m3's member days are the 10 days after it, while the
    members that never moved count every window day.
    """
    scenario = SCENARIOS["shared.membership_mid_window.case"]
    ctx = make_context(scenario.name)
    pool: SubjectKey = ("budget", _budget_id(scenario, "pool"))
    m3 = _campaign_id(scenario, "m3", "google")
    history = budget_history(ctx, pool)
    member_days = history.members[m3]
    assert len(member_days) == 10
    assert min(member_days).isoformat() > "2026-09-21"
    assert {day.date.isoformat(): day.klass for day in history.days}["2026-09-21"] != "known"
    assert history.known_active == ctx.window.days - 1
    longer = [cid for cid, dates in history.members.items() if cid != m3]
    assert longer
    assert all(len(history.members[cid]) == ctx.window.days for cid in longer)
    item = _only(_run(ctx), BUDGET, B1)
    (entry,) = [e for e in item.evidence.extra["members"] if e["campaign_id"] == m3]
    assert entry["member_days"] == 10


def test_a_member_changing_budgets_during_a_day_makes_that_day_unknown(
    make_context: ContextFactory,
) -> None:
    """Shared budgets: a member moving to another budget mid-day leaves that day unknown."""
    scenario = SCENARIOS["shared.member_changed_intraday.case"]
    ctx = make_context(scenario.name)
    pool: SubjectKey = ("budget", _budget_id(scenario, "pool"))
    history = budget_history(ctx, pool)
    unknown = [day.date.isoformat() for day in history.days if day.klass != "known"]
    assert unknown == ["2026-09-27"]
    assert history.known_active == ctx.window.days - 1
    m2 = _campaign_id(scenario, "m2", "google")
    assert all(day.isoformat() < "2026-09-27" for day in history.members[m2])
    item = _only(_run(ctx), BUDGET, B1)
    assert _unknown_dates(item.evidence.extra) == ("2026-09-27",)


def test_a_poor_member_blocks_the_budget_level_increase(make_context: ContextFactory) -> None:
    """Shared budgets: a poor-efficiency member inside a high-utilization pool blocks its B1.

    The pool's aggregate is efficient and capped, so B2 does not meet at budget level; m2's own
    cost per outcome over its member days does, which blocks the pool and every member.
    """
    scenario = SCENARIOS["shared.poor_member.fires"]
    ctx = make_context(scenario.name)
    pool_id = _budget_id(scenario, "pool")
    m1, m2 = (_campaign_id(scenario, key, "google") for key in ("m1", "m2"))
    assessment = assess_b2(ctx, ("budget", pool_id))
    assert assessment.gate.passed
    assert not assessment.meets
    assert member_poor_efficiency(ctx, pool_id, m2)
    assert not member_poor_efficiency(ctx, pool_id, m1)
    assert {("budget", pool_id), ("campaign", m1), ("campaign", m2)} <= spend_increase_blocked(ctx)
    out = _run(ctx)
    assert _items(out, BUDGET, B1) == []
    assert _items(out, CAMPAIGNS, C2) == []
    assert not _increase_targets(_survivors(out)) & spend_increase_blocked(ctx)


def test_a_good_efficiency_member_leaves_the_budget_level_increase(
    make_context: ContextFactory,
) -> None:
    """Shared budgets twin: with m2 efficient the pool's B1 stands: 1,000.00 → 1,200.00."""
    scenario = SCENARIOS["shared.poor_member.does_not_fire"]
    ctx = make_context(scenario.name)
    pool_id = _budget_id(scenario, "pool")
    members = subject_campaigns(ctx, ("budget", pool_id))
    assert not any(member_poor_efficiency(ctx, pool_id, cid) for cid in members)
    assert spend_increase_blocked(ctx) == frozenset()
    item = _only(_run(ctx), BUDGET, B1)
    step = ctx.thresholds.ratio("b1.step")
    current = evidence_metrics(item)["daily_budget"]
    assert evidence_metrics(item)["proposed_daily_budget"] == round_to_minor_unit(
        current * (1 + step), "USD"
    )
    assert evidence_metrics(item)["proposed_daily_budget"] == Decimal("1200.00")
    assert item.decision_subject == DecisionSubject("budget", pool_id)


def _zero_outcome_member(
    scenario: Scenario,
    base: AnalysisContext,
    *,
    solo_first_day: int = 1,
    m2_first_day: Decimal | None = Decimal(0),
) -> AnalysisContext:
    """``shared.poor_member.fires`` with member m2 converting nothing and a tuned baseline.

    The members keep their spend (m1 700.00 and m2 270.00 a day of the 1,000.00 pool), so the
    pool's budget-active days and high-utilization gate are the catalogue's. The own-budget
    campaign solo spends 1,730.00 a day, so the account spends exactly ten times m2's
    7,560.00. m1 and solo convert once a day on the window's first 15 days, solo's first day
    reporting ``solo_first_day`` instead: 30 account conversions make m2's spend worth exactly
    3 expected conversions, 29 make it 2.9, while m1, solo and the pool's aggregate stay
    efficient. m2 reports 0 conversions on every day but the first, which reports
    ``m2_first_day`` (``None`` leaves that day unreported).
    """
    solo, m1, m2 = (_campaign_id(scenario, key, "google") for key in ("solo", "m1", "m2"))
    first = base.window.start
    converting = frozenset(first + timedelta(days=offset) for offset in range(15))

    def once_a_day(day: date) -> Decimal:
        """One conversion on each of the window's first 15 days, none after."""
        return Decimal(1 if day in converting else 0)

    solo_days = tuple(
        replace(
            day,
            cost=Decimal("1730.00"),
            conversions=Decimal(solo_first_day) if day.date == first else once_a_day(day.date),
        )
        for day in base.campaign_days[solo]
    )
    m1_days = tuple(
        replace(day, conversions=once_a_day(day.date)) for day in base.campaign_days[m1]
    )
    m2_days = tuple(
        replace(day, conversions=m2_first_day if day.date == first else Decimal(0))
        for day in base.campaign_days[m2]
    )
    return _fresh(
        base, campaign_days={**base.campaign_days, solo: solo_days, m1: m1_days, m2: m2_days}
    )


@pytest.mark.parametrize(
    ("solo_first_day", "expected", "blocked"),
    [(1, Decimal(3), True), (0, Decimal("2.9"), False)],
)
def test_a_zero_outcome_member_blocks_the_pool_at_three_expected_outcomes_not_at_two_point_nine(
    make_context: ContextFactory, solo_first_day: int, expected: Decimal, blocked: bool
) -> None:
    """Member safety predicate, zero-outcome branch: 3.0 expected outcomes block / 2.9 do not.

    m2 converts nothing over its member days inside the high-utilization pool, and only the
    peer's conversions on one day move the account baseline (:func:`_zero_outcome_member`), so
    m2's spend is worth exactly ``b2.min_expected_outcomes`` (3) expected conversions in one
    twin and 2.9 in the other. The pool's aggregate is efficient in both, so the member
    predicate alone decides: at 3.0 G1 blocks the pool and its members and removes the pool's
    own C2 "Scale" and B1; at 2.9 nothing is blocked and the B1 raises the pool to 1,200.00.
    """
    scenario = SCENARIOS["shared.poor_member.fires"]
    base = make_context(scenario.name)
    pool_id = _budget_id(scenario, "pool")
    pool: SubjectKey = ("budget", pool_id)
    m1, m2 = (_campaign_id(scenario, key, "google") for key in ("m1", "m2"))
    ctx = _zero_outcome_member(scenario, base, solo_first_day=solo_first_day)
    assert ctx.thresholds.ratio("b2.min_expected_outcomes") == 3
    history = budget_history(ctx, pool)
    assert history.counts() == budget_history(base, pool).counts()
    aggregate = assess_b2(ctx, pool)
    assert aggregate.gate.passed
    assert not aggregate.meets
    member = window_totals(ctx, (m2,), dates=history.members[m2])
    account = _account_totals(ctx, m2)
    assert member.outcomes == 0
    assert account.spend == 10 * member.spend
    # Expected outcomes = spend ÷ (account spend ÷ account outcomes), exactly (AAP 0.5.4.1).
    assert account.outcomes is not None
    assert member.spend * account.outcomes / account.spend == expected
    assert member_poor_efficiency(ctx, pool_id, m2) is blocked
    assert member_poor_efficiency(ctx, pool_id, m1) is False
    out = _run(ctx)
    if blocked:
        assert {pool, ("campaign", m1), ("campaign", m2)} <= spend_increase_blocked(ctx)
        assert _on(_items(out, BUDGET, B1), pool) == []
        assert pool not in _increase_targets(_survivors(out))
        assert out.suppressed[BUDGET][SUPPRESS_G1_BLOCKED] == 1
        assert out.suppressed[CAMPAIGNS][SUPPRESS_G1_BLOCKED] == 1
        return
    assert spend_increase_blocked(ctx) == frozenset()
    (item,) = _on(_items(out, BUDGET, B1), pool)
    assert evidence_metrics(item)["proposed_daily_budget"] == Decimal("1200.00")
    assert out.suppressed_total(SUPPRESS_G1_BLOCKED) == 0


@pytest.mark.parametrize("m2_first_day", [Decimal(0), None])
def test_unreported_member_outcomes_never_meet_the_member_safety_predicate(
    make_context: ContextFactory, m2_first_day: Decimal | None
) -> None:
    """Member safety predicate, unknown outcomes: an unreported day is never a zero.

    The 3.0-expected-outcome pool of
    :func:`test_a_zero_outcome_member_blocks_the_pool_at_three_expected_outcomes_not_at_two_point_nine`
    with m2's first member day known at 0 conversions meets the predicate and blocks the pool.
    With that one day unreported (``None``), m2's outcomes over its member days are
    unavailable, so the predicate is not met and G1 does not block the pool; the pool's own
    outcomes are unavailable as well, so no rule proposes an increase on it either.
    """
    scenario = SCENARIOS["shared.poor_member.fires"]
    base = make_context(scenario.name)
    pool_id = _budget_id(scenario, "pool")
    pool: SubjectKey = ("budget", pool_id)
    m2 = _campaign_id(scenario, "m2", "google")
    ctx = _zero_outcome_member(scenario, base, m2_first_day=m2_first_day)
    history = budget_history(ctx, pool)
    assert history.counts() == budget_history(base, pool).counts()
    assert assess_b2(ctx, pool).gate.passed
    member = window_totals(ctx, (m2,), dates=history.members[m2])
    if m2_first_day is not None:
        assert member.outcomes == 0
        assert member_poor_efficiency(ctx, pool_id, m2)
        assert pool in spend_increase_blocked(ctx)
        return
    assert (member.outcomes, member.unreported_days) == (None, 1)
    assert not member_poor_efficiency(ctx, pool_id, m2)
    assert not assess_b2(ctx, pool).meets
    assert pool not in spend_increase_blocked(ctx)
    assert pool not in _increase_targets(_survivors(_run(ctx)))


def test_an_aggregate_b2_blocks_the_shared_budget(make_context: ContextFactory) -> None:
    """Shared budgets: an aggregate B2 is decided at budget level and blocks every member."""
    scenario = SCENARIOS["shared.aggregate_b2.case"]
    ctx = make_context(scenario.name)
    pool: SubjectKey = ("budget", _budget_id(scenario, "pool"))
    assert assess_b2(ctx, pool).meets
    blocked = spend_increase_blocked(ctx)
    assert pool in blocked
    assert {("campaign", cid) for cid in subject_campaigns(ctx, pool)} <= blocked
    out = _run(ctx)
    (item,) = _items(out, BUDGET, B2)
    assert item.decision_subject == DecisionSubject.of(pool)
    assert item.has_flag(FLAG_SHARED_BUDGET)
    assert pool not in _increase_targets(_survivors(out))


@pytest.mark.parametrize("name", SHARED_ITEM_SCENARIOS)
def test_shared_budget_items_are_decided_at_budget_level(
    make_context: ContextFactory, name: str
) -> None:
    """Shared budgets: the budget is the subject, the card names every member with its member
    days and carries ``shared_budget``; member campaigns get no increase or decrease items."""
    scenario = SCENARIOS[name]
    ctx = make_context(scenario.name)
    pool: SubjectKey = ("budget", _budget_id(scenario, "pool"))
    history = budget_history(ctx, pool)
    members = set(subject_campaigns(ctx, pool))
    out = _run(ctx)
    pool_items = [item for item in _items_of(out, BUDGET) if item.rule_id in BUDGET_ITEM_RULES]
    assert pool_items
    for item in pool_items:
        if item.decision_subject is not None and item.decision_subject.type == "campaign":
            assert item.decision_subject.id not in members, (item.rule_id, item.action)
            continue
        assert item.decision_subject == DecisionSubject.of(pool)
        assert item.has_flag(FLAG_SHARED_BUDGET)
        assert item.evidence.extra["shared"] is True
        listed = {
            entry["campaign_id"]: entry["member_days"] for entry in item.evidence.extra["members"]
        }
        assert set(listed) == members
        assert listed == {cid: len(history.members.get(cid, ())) for cid in members}
        assert evidence_metrics(item)["known_days"] == history.known_active
    assert ("budget", pool[1]) in budget_subjects(ctx)
    assert not any(("campaign", cid) in budget_subjects(ctx) for cid in members)


def _mixed_members(ctx: AnalysisContext, member: int, how: str) -> AnalysisContext:
    """Copy a shared-budget context with one member in another objective group or currency."""
    campaign = ctx.campaigns[member]
    if how == "currency":
        account = ctx.accounts[campaign.account_id]
        other = AccountInfo(account.id + 1_000, "google", "EU-1", "EU", "EUR", account.timezone)
        moved = replace(campaign, account_id=other.id, currency="EUR")
        return _fresh(
            ctx,
            accounts={**ctx.accounts, other.id: other},
            campaigns={**ctx.campaigns, member: moved},
        )
    moved = replace(
        campaign,
        source="linkedin",
        objective_or_channel="LEAD_GENERATION",
        objective_group=OBJ_LINKEDIN_LEAD_GEN,
    )
    return _fresh(ctx, campaigns={**ctx.campaigns, member: moved})


@pytest.mark.parametrize(
    ("how", "reason"),
    [
        ("objective_group", "members span 2 objective groups: efficiency n/a"),
        ("currency", "members span more than one currency: efficiency n/a"),
    ],
)
def test_members_spanning_objective_groups_withhold_b1_b2_and_b3(
    make_context: ContextFactory, how: str, reason: str
) -> None:
    """Shared budgets: members in more than one objective group make efficiency "n/a".

    B1, B2 and B3 are withheld for that budget and no budget-level item is emitted. Google
    campaigns all share one objective group, so the catalogue cannot build such a pool from
    Google members alone (its ``shared.mixed_objectives`` case pins that SEARCH and DISPLAY
    stay one group); the guarantee is proven on a copy of the pool whose member is moved into
    a second objective group, and likewise into a second currency, which also never sums.
    """
    scenario = SCENARIOS["shared.poor_member.does_not_fire"]
    pool_id = _budget_id(scenario, "pool")
    ctx = _mixed_members(make_context(scenario.name), _campaign_id(scenario, "m2", "google"), how)
    out = _run(ctx)
    for rule_id in (B1, B2, B3):
        assert _on(_items(out, BUDGET, rule_id), ("budget", pool_id)) == []
        (line,) = _lines(out, rule_id, pool_id)
        assert line.reason == reason
    assert _on(_items(out, CAMPAIGNS, C2), ("budget", pool_id)) == []

    same_group = SCENARIOS["shared.mixed_objectives.case"]
    item = _only(_run(make_context(same_group.name)), BUDGET, B1)
    assert item.evidence.extra["objective_groups"] == ("google",)
    assert evidence_metrics(item)["cost_per_outcome"] is not None


class _Clock:
    """A settable clock shared by the simulated world and the source-mode controller."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


async def _rows(
    db: async_sessionmaker[AsyncSession], model: type[Decision] | type[Discrepancy], dataset: int
) -> list[Any]:
    """Every decision or discrepancy row of a dataset, in insertion order."""
    async with db() as session:
        result = await session.scalars(
            select(model).where(model.dataset_id == dataset).order_by(model.id)
        )
        return list(result)


async def test_a_shared_budget_decision_raises_no_discrepancy_on_any_member(
    db: async_sessionmaker[AsyncSession],
    make_world: WorldFactory,
    set_env: Callable[..., None],
) -> None:
    """Shared budgets: a decision on the shared amount writes only the budget subject.

    A simulated Google dataset is built while the pool is still 1,000.00, which baselines the
    pool in the ``budget`` shape and each member in the ``shared_member`` shape (status and
    assignment, never an amount). The user then decides 1,200.00 on the pool. The next entity
    sync, before the platform applies it, raises exactly one ``budget`` discrepancy on the
    budget subject and none on any member; once the platform shows 1,200.00 the next sync
    closes it, and no member ever had a discrepancy or a new decision.
    """
    set_env(DATA_MODE="simulated", BACKFILL_DAYS="35")
    scenario = SCENARIOS["shared.decision_subject.case"]
    (change,) = [c for c in scenario.spec.platform_changes if c.entity == "budget"]
    applied_at = change.instant(FIXED_ANCHOR)
    clock = _Clock(applied_at - timedelta(hours=10))
    world = make_world(scenario.name, clock=clock)
    controller = SourceModeController(sessionmaker=db, world=world, clock=clock)

    outcome = await controller.build_dataset(
        "google", "simulated", reason="credentials not configured"
    )
    assert outcome.activated, outcome.reason
    dataset = outcome.dataset_id
    assert dataset is not None
    baseline = await _rows(db, Decision, dataset)
    (pool,) = [row for row in baseline if row.subject_type == "budget"]
    members = [row for row in baseline if row.shape == "shared_member"]
    assert pool.shape == "budget"
    assert pool.status is None
    assert pool.daily_budget == Decimal("1000.00")
    assert len(members) == 2
    assert all(row.budget_id == pool.subject_id and row.daily_budget is None for row in members)
    assert await _rows(db, Discrepancy, dataset) == []

    clock.at = applied_at - timedelta(hours=9)
    decided = await record_decision(
        SubjectRef("budget", pool.subject_id),
        DecisionPatch(daily_budget=Decimal("1200.00")),
        "user",
        now=clock.at,
    )
    assert (decided.subject_type, decided.shape) == ("budget", "budget")
    assert decided.daily_budget == Decimal("1200.00")
    assert decided.status is None

    clock.at = applied_at - timedelta(hours=8)
    report = await run_source_jobs(
        "google", trigger="manual", jobs=(JOB_ENTITIES,), controller=controller, now=clock.at
    )
    assert report.dataset_id == dataset, report.reason
    pending = [row for row in await _rows(db, Discrepancy, dataset) if row.state != "resolved"]
    assert [(row.subject_type, row.subject_id, row.kind) for row in pending] == [
        ("budget", pool.subject_id, "budget")
    ]

    clock.at = FIXED_NOW
    await run_source_jobs(
        "google", trigger="manual", jobs=(JOB_ENTITIES,), controller=controller, now=clock.at
    )
    discrepancies = await _rows(db, Discrepancy, dataset)
    assert [row.state for row in discrepancies] == ["resolved"]
    member_ids = {row.subject_id for row in members}
    assert not [
        row
        for row in discrepancies
        if row.subject_type == "campaign" and row.subject_id in member_ids
    ]
    decisions = await _rows(db, Decision, dataset)
    assert [
        row.origin
        for row in decisions
        if row.subject_type == "campaign" and row.subject_id in member_ids
    ] == ["baseline", "baseline"]


# ---------------------------------------------------------------------------
# Phase 5: B3 headroom allocation (AAP 0.5.4.2)
# ---------------------------------------------------------------------------


def test_b3_allocation_funds_the_b1_increase_and_never_more(make_context: ContextFactory) -> None:
    """B3 allocation: donors freeing 300 and 200 against one B1 increase of 400.

    Allocations are 300 and 100, summing to the increase; the unallocated 100 of the second
    donor stays a plain decrease. The B1 card reads "Raise by 400: 400 funded by reallocation
    from {donors}, 0 from new budget" and each B3 card "Move {allocation} to fund {receiver}'s
    recommended increase (B1)", so neither card reads as adding to the other.
    """
    scenario = SCENARIOS["b3.allocation.case"]
    ctx = make_context(scenario.name)
    donor_a, donor_b, receiver = (
        _campaign_id(scenario, key) for key in ("donor-a", "donor-b", "receiver")
    )
    out = _run(ctx)
    assert out.allocations == (
        Allocation(("campaign", donor_a), ("campaign", receiver), Decimal("300.00")),
        Allocation(("campaign", donor_b), ("campaign", receiver), Decimal("100.00")),
    )
    b1 = _only(out, BUDGET, B1)
    increase = b1.evidence.extra["increase"]
    assert increase == Decimal("400.00")
    assert sum((allocation.amount for allocation in out.allocations), Decimal(0)) == increase
    names = {cid: ctx.campaigns[cid].name for cid in (donor_a, donor_b, receiver)}
    funding = budget.render_b1_funding(
        increase,
        [(names[donor_a], Decimal("300.00")), (names[donor_b], Decimal("100.00"))],
        "USD",
    )
    assert funding == (
        f"Raise by USD 400.00: USD 400.00 funded by reallocation from {names[donor_a]}, "
        f"{names[donor_b]}, USD 0.00 from new budget"
    )
    assert b1.evidence.extra["funding"] == funding
    assert b1.evidence.extra["funded_by_reallocation"] == Decimal("400.00")
    assert b1.evidence.extra["from_new_budget"] == Decimal("0.00")
    assert funding in b1.reason
    assert b1.action.startswith("Raise daily budget from ")

    donors = {item.entity_ref.id: item for item in _items(out, BUDGET, B3)}
    assert set(donors) == {donor_a, donor_b}
    for donor, amount, unallocated in (
        (donor_a, Decimal("300.00"), Decimal("0.00")),
        (donor_b, Decimal("100.00"), Decimal("100.00")),
    ):
        extra = donors[donor].evidence.extra
        text = budget.render_b3_allocation(amount, names[receiver], "USD")
        assert (
            text == f"Move {_money(amount)} to fund {names[receiver]}'s recommended increase (B1)"
        )
        (entry,) = extra["allocations"]
        assert entry["text"] == text
        assert entry["amount"] == amount
        assert entry["receiver"] == {"type": "campaign", "id": receiver}
        assert extra["unallocated"] == unallocated
        assert extra["allocated"] + extra["unallocated"] == extra["freed"]
        assert text in donors[donor].reason
        assert donors[donor].increases_spend_on == ()
    lowered = evidence_metrics(donors[donor_b])["proposed_daily_budget"]
    assert donors[donor_b].action == f"Lower daily budget to {_money(lowered)}"


def _ordered_parties(
    ctx: AnalysisContext,
) -> tuple[list[Candidate], list[Candidate]]:
    """Three donors and three receivers, listed in an order the allocation must not keep.

    Donors: "Yak" and "Ant" free 50.00, "Bee" frees 80.00; receivers: "Zeta" at a cost per
    outcome of 15, "Beta" and "Alpha" at 20; each receiver's B1 increase is 100.00.
    """
    donors = [
        _candidate(
            ctx,
            B3,
            BUDGET,
            GROUP_CHANGES,
            EntityRef("linkedin", ENTITY_CAMPAIGN, 9_100 + n, str(9_100 + n), name),
            subject=("campaign", 9_100 + n),
            extra={"freed": Decimal(freed), "currency": "USD"},
        )
        for n, (name, freed) in enumerate((("Yak", "50.00"), ("Ant", "50.00"), ("Bee", "80.00")))
    ]
    receivers = [
        _candidate(
            ctx,
            B1,
            BUDGET,
            GROUP_CHANGES,
            EntityRef("linkedin", ENTITY_CAMPAIGN, 9_000 + n, str(9_000 + n), name),
            targets=(("campaign", 9_000 + n),),
            subject=("campaign", 9_000 + n),
            extra={"increase": Decimal("100.00"), "currency": "USD"},
            metrics_shown=(
                Metric("cost_per_outcome", "Cost per lead", Decimal(cpo), "currency", "USD"),
            ),
        )
        for n, (name, cpo) in enumerate((("Beta", "20"), ("Alpha", "20"), ("Zeta", "15")))
    ]
    return donors, receivers


def test_b3_allocation_orders_donors_and_receivers_deterministically(
    make_context: ContextFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 allocation order: donors by freed amount descending, then name, then id; receivers
    by B1 cost per outcome ascending, then name, then id, whatever order the rules list them.
    """
    ctx = make_context("b3.no_receiver.case")
    donors, receivers = _ordered_parties(ctx)
    seen: list[tuple[list[SubjectKey], list[SubjectKey]]] = []
    allocate = precedence.allocate_b3

    def spy(
        donor_amounts: list[tuple[SubjectKey, Decimal]],
        receiver_amounts: list[tuple[SubjectKey, Decimal]],
    ) -> tuple[Allocation, ...]:
        seen.append(([key for key, _ in donor_amounts], [key for key, _ in receiver_amounts]))
        return allocate(donor_amounts, receiver_amounts)

    monkeypatch.setattr(precedence, "allocate_b3", spy)
    out = precedence.apply_precedence(RuleResult(candidates=(*donors, *receivers)), ctx)
    key = {
        item.entity_ref.name: ("campaign", item.entity_ref.id)
        for item in (*donors, *receivers)
        if item.entity_ref.id is not None
    }
    assert len(seen) == 1
    donor_order, receiver_order = seen[0]
    assert donor_order == [key["Bee"], key["Ant"], key["Yak"]]
    assert receiver_order == [key["Zeta"], key["Alpha"], key["Beta"]]
    assert [(a.donor, a.receiver, a.amount) for a in out.allocations] == [
        (key["Bee"], key["Zeta"], Decimal("80.00")),
        (key["Ant"], key["Zeta"], Decimal("20.00")),
        (key["Ant"], key["Alpha"], Decimal("30.00")),
        (key["Yak"], key["Alpha"], Decimal("50.00")),
    ]

    twins = [
        replace(donor, entity_ref=replace(donor.entity_ref, name="Same")) for donor in donors[:2]
    ]
    seen.clear()
    precedence.apply_precedence(RuleResult(candidates=(*reversed(twins), *receivers)), ctx)
    assert seen[0][0] == [("campaign", 9_100), ("campaign", 9_101)]


def _tied_party(
    ctx: AnalysisContext, rule_id: str, entity_id: int, external_id: str, name: str, amount: str
) -> Candidate:
    """A B3 donor freeing ``amount`` or a B1 receiver of increase ``amount`` (cost/lead 20)."""
    subject: SubjectKey = ("campaign", entity_id)
    entity = EntityRef("linkedin", ENTITY_CAMPAIGN, entity_id, external_id, name)
    if rule_id == B3:
        return _candidate(
            ctx,
            B3,
            BUDGET,
            GROUP_CHANGES,
            entity,
            subject=subject,
            extra={"freed": Decimal(amount), "currency": "USD"},
        )
    return _candidate(
        ctx,
        B1,
        BUDGET,
        GROUP_CHANGES,
        entity,
        targets=(subject,),
        subject=subject,
        extra={"increase": Decimal(amount), "currency": "USD"},
        metrics_shown=(
            Metric("cost_per_outcome", "Cost per lead", Decimal("20"), "currency", "USD"),
        ),
    )


@pytest.mark.parametrize(
    "ids",
    [((2, "200"), (10, "100")), ((2, "2"), (10, "10"))],
    ids=["reverse_correlated_external_ids", "different_length_ids"],
)
def test_b3_allocation_breaks_name_ties_by_numeric_entity_id_not_by_key(
    ids: tuple[tuple[int, str], tuple[int, str]],
    make_context: ContextFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3 allocation order: after freed amount (or cost per outcome) and name, the numeric id.

    Two donors freeing the same amount under the same name, and two receivers at the same
    cost per outcome under the same name, have internal ids 2 and 10. Their external ids are
    either "200" and "100" (reverse-correlated with the internal ids) or "2" and "10", whose
    entity keys order ``…:10`` before ``…:2``; in both layouts the key alone would put id 10
    first. Id 2 goes first, as in ranking (``Candidate.sort_key``), whatever order the rules
    list them in: the first donor gives first and the first receiver is funded first.
    """
    ctx = make_context("b3.no_receiver.case")
    (first, first_external), (second, second_external) = ids
    keys = [
        EntityRef("linkedin", ENTITY_CAMPAIGN, entity_id, external, "Same").key
        for entity_id, external in ids
    ]
    assert keys[1] < keys[0], "the entity key alone must order the tie the wrong way"
    assert not spend_increase_blocked(ctx)
    seen: list[tuple[list[SubjectKey], list[SubjectKey]]] = []
    allocate = precedence.allocate_b3

    def spy(
        donor_amounts: list[tuple[SubjectKey, Decimal]],
        receiver_amounts: list[tuple[SubjectKey, Decimal]],
    ) -> tuple[Allocation, ...]:
        seen.append(([key for key, _ in donor_amounts], [key for key, _ in receiver_amounts]))
        return allocate(donor_amounts, receiver_amounts)

    monkeypatch.setattr(precedence, "allocate_b3", spy)
    low, high = ("campaign", first), ("campaign", second)
    tied_donors = [
        _tied_party(ctx, B3, first, first_external, "Same", "50.00"),
        _tied_party(ctx, B3, second, second_external, "Same", "50.00"),
    ]
    zeta = _tied_party(ctx, B1, 9_000, "9000", "Zeta", "80.00")
    tied_receivers = [
        _tied_party(ctx, B1, first, first_external, "Same", "50.00"),
        _tied_party(ctx, B1, second, second_external, "Same", "50.00"),
    ]
    bee = _tied_party(ctx, B3, 9_100, "9100", "Bee", "80.00")
    for donors, receivers in (
        (tied_donors, tied_receivers),
        (tied_donors[::-1], tied_receivers[::-1]),
    ):
        seen.clear()
        out = precedence.apply_precedence(RuleResult(candidates=(*donors, zeta)), ctx)
        assert seen == [([low, high], [("campaign", 9_000)])]
        assert [(a.donor, a.receiver, a.amount) for a in out.allocations] == [
            (low, ("campaign", 9_000), Decimal("50.00")),
            (high, ("campaign", 9_000), Decimal("30.00")),
        ]

        seen.clear()
        out = precedence.apply_precedence(RuleResult(candidates=(bee, *receivers)), ctx)
        assert seen == [([("campaign", 9_100)], [low, high])]
        assert [(a.donor, a.receiver, a.amount) for a in out.allocations] == [
            (("campaign", 9_100), low, Decimal("50.00")),
            (("campaign", 9_100), high, Decimal("30.00")),
        ]


def test_over_allocating_trips_the_runner_assertion(
    make_context: ContextFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 runner assertion: allocations beyond the receiver's B1 increase fail the run."""
    ctx = make_context("b3.allocation.case")

    def everything_to_the_first(
        donor_amounts: list[tuple[SubjectKey, Decimal]],
        receiver_amounts: list[tuple[SubjectKey, Decimal]],
    ) -> tuple[Allocation, ...]:
        receiver = receiver_amounts[0][0]
        return tuple(Allocation(donor, receiver, freed) for donor, freed in donor_amounts)

    monkeypatch.setattr(precedence, "allocate_b3", everything_to_the_first)
    with pytest.raises(PrecedenceInvariantError, match="exceed its B1 increase"):
        _run(ctx)


def test_without_a_surviving_receiver_every_b3_stays_a_plain_decrease(
    make_context: ContextFactory,
) -> None:
    """B3 allocation: with no surviving B1 in the cohort no allocation is made or rendered."""
    scenario = SCENARIOS["b3.no_receiver.case"]
    ctx = make_context(scenario.name)
    out = _run(ctx)
    assert _items(out, BUDGET, B1) == []
    assert out.allocations == ()
    donors = _items(out, BUDGET, B3)
    assert sorted((item.evidence.extra["freed"] for item in donors), reverse=True) == [
        Decimal("300.00"),
        Decimal("200.00"),
    ]
    for item in donors:
        assert item.evidence.extra["allocations"] == ()
        assert "to fund" not in item.reason
        assert item.action.startswith("Lower daily budget to ")


_AD_DATASETS: Final[Mapping[Source, tuple[str, str]]] = {
    "google": ("google.entities", "google.metrics_daily"),
    "linkedin": ("linkedin.entities", "linkedin.analytics_daily"),
}
"""Each ad source's entity and daily metrics datasets: what a budget item of that source reads."""

type _Presented = list[tuple[list[SubjectKey], list[SubjectKey]]]
"""The donors and receivers of each cohort precedence presented to ``allocate_b3``."""


def _on_own_account(ctx: AnalysisContext, campaign_id: int) -> AnalysisContext:
    """Copy ``ctx`` with one campaign moved onto a copy of its account, as its only campaign.

    An ad account has one currency, so :func:`_in_currency` moves a campaign together with its
    whole account. On an account of its own the campaign changes currency alone, and the
    campaigns it shared the account with keep theirs.
    """
    campaign = ctx.campaigns[campaign_id]
    account = ctx.accounts[campaign.account_id]
    own = max(ctx.accounts) + 1
    copy = replace(account, id=own, external_id=f"{account.external_id}-{own}")
    return _fresh(
        ctx,
        accounts={**ctx.accounts, own: copy},
        campaigns={**ctx.campaigns, campaign_id: replace(campaign, account_id=own)},
    )


def _cohort_party(
    ctx: AnalysisContext, rule_id: str, source: Source, entity_id: int, name: str, amount: str
) -> Candidate:
    """A rule-shaped USD campaign party of ``source``: a B3 donor or a B1 receiver.

    A B3 donor frees ``amount``; a B1 receiver has ``amount`` as its increase (its headroom)
    and a cost per outcome of 20. It is built as :func:`_candidate` builds every rule-shaped
    party, with the evidence a budget item of ``source`` carries: the entity is of ``source``
    and the required datasets are that source's entity and daily metrics datasets.
    """
    subject: SubjectKey = ("campaign", entity_id)
    entity = EntityRef(source, ENTITY_CAMPAIGN, entity_id, str(entity_id), name)
    if rule_id == B3:
        party = _candidate(
            ctx,
            B3,
            BUDGET,
            GROUP_CHANGES,
            entity,
            subject=subject,
            extra={"freed": Decimal(amount), "currency": "USD"},
        )
    else:
        party = _candidate(
            ctx,
            B1,
            BUDGET,
            GROUP_CHANGES,
            entity,
            targets=(subject,),
            subject=subject,
            extra={"increase": Decimal(amount), "currency": "USD"},
            metrics_shown=(
                Metric("cost_per_outcome", "Cost per outcome", Decimal("20"), "currency", "USD"),
            ),
        )
    datasets = _AD_DATASETS[source]
    extra = dict(party.evidence.extra)
    headline = extra.pop("headline_metric")
    evidence = ctx.make_evidence(
        required_datasets=datasets,
        crm_scope=None,
        metrics=party.evidence.metrics,
        extra=extra,
        headline=headline,
    )
    return replace(party, evidence=evidence, required_datasets=datasets)


def _spy_on_allocation(monkeypatch: pytest.MonkeyPatch) -> _Presented:
    """Record each cohort's donors and receivers as precedence presents them to ``allocate_b3``.

    Precedence presents a (source, currency) cohort to the allocation step only when the
    cohort has both donors and receivers. An empty record therefore shows that the cohort
    split itself kept every donor from a receiver: no allocation was attempted, so neither the
    allocation step nor the runner assertion (``PrecedenceInvariantError``) refused one.
    """
    presented: _Presented = []
    allocate = precedence.allocate_b3

    def spy(
        donor_amounts: list[tuple[SubjectKey, Decimal]],
        receiver_amounts: list[tuple[SubjectKey, Decimal]],
    ) -> tuple[Allocation, ...]:
        presented.append(([key for key, _ in donor_amounts], [key for key, _ in receiver_amounts]))
        return allocate(donor_amounts, receiver_amounts)

    monkeypatch.setattr(precedence, "allocate_b3", spy)
    return presented


def _by_subject(items: Iterable[Candidate]) -> dict[SubjectKey, Candidate]:
    """Budget items by their decision subject."""
    return {item.decision_subject.key: item for item in items if item.decision_subject is not None}


def _built(ctx: AnalysisContext, rule_id: str) -> dict[SubjectKey, Candidate]:
    """The ``rule_id`` items ``budget.evaluate`` builds, before precedence, by subject."""
    return _by_subject(item for item in budget.evaluate(ctx).candidates if item.rule_id == rule_id)


def _check_plain_decrease(item: Candidate, built: Candidate, receiver: SubjectKey) -> None:
    """A B3 donor left exactly as its rule built it: a plain decrease that funds no B1.

    Nothing is allocated, listed or rendered, the whole freed amount stays unallocated, and
    the card neither names nor depends on ``receiver``.
    """
    extra = item.evidence.extra
    assert item.action == built.action
    assert item.reason == built.reason
    assert "to fund" not in item.reason
    assert extra.get("allocations") == built.evidence.extra.get("allocations")
    assert not extra.get("allocations")
    assert extra["allocated"] == 0
    assert extra["unallocated"] == extra["freed"] == built.evidence.extra["freed"]
    assert receiver not in {subject.key for subject in item.depends_on}
    assert item.increases_spend_on == ()


def _check_all_new_budget(
    item: Candidate, built: Candidate, funding: str, donors: Iterable[SubjectKey]
) -> None:
    """A surviving B1 receiver none of whose increase is funded by reallocation.

    ``funding`` is the card's exact sentence, with all of the increase from new budget; the
    reason and action stay the rule's own, and the card depends on none of ``donors``.
    """
    extra = item.evidence.extra
    increase = extra["increase"]
    assert increase == built.evidence.extra["increase"]
    assert funding == budget.render_b1_funding(increase, [], extra["currency"])
    assert extra["funding"] == funding
    assert extra["allocations"] == ()
    assert extra["funded_by_reallocation"] == 0
    assert extra["from_new_budget"] == increase
    assert item.reason == built.reason
    assert item.action == built.action
    assert not {subject.key for subject in item.depends_on} & set(donors)


def test_b3_never_funds_a_receiver_in_another_currency(
    make_context: ContextFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 cohort: a receiver is in its donor's currency (AAP 0.5.4.2), never in another one.

    ``b3.allocation``'s receiver is moved onto an account of its own (:func:`_on_own_account`).
    Still in USD, its donors' currency, it is funded 300 and 100 exactly as in
    :func:`test_b3_allocation_funds_the_b1_increase_and_never_more` (the control). Moved to
    EUR, the single input that changes, the budget rules still build its B1, now an increase of
    EUR 400.00, and it survives; but its cohort is (linkedin, EUR) and its donors' is
    (linkedin, USD). No cohort is presented for allocation, nothing is allocated, each donor
    stays the plain decrease the B3 rule built, and the B1 increase is all new budget.
    """
    scenario = SCENARIOS["b3.allocation.case"]
    donor_a: SubjectKey = ("campaign", _campaign_id(scenario, "donor-a"))
    donor_b: SubjectKey = ("campaign", _campaign_id(scenario, "donor-b"))
    receiver: SubjectKey = ("campaign", _campaign_id(scenario, "receiver"))
    alone = _on_own_account(make_context(scenario.name), receiver[1])
    presented = _spy_on_allocation(monkeypatch)

    assert alone.account_currency(receiver[1]) == "USD"
    out = _run(alone)
    assert presented == [([donor_a, donor_b], [receiver])]
    assert out.allocations == (
        Allocation(donor_a, receiver, Decimal("300.00")),
        Allocation(donor_b, receiver, Decimal("100.00")),
    )
    assert _only(out, BUDGET, B1).evidence.extra["from_new_budget"] == Decimal("0.00")

    ctx = _in_currency(alone, receiver[1], "EUR")
    assert ctx.account_currency(receiver[1]) == "EUR"
    assert {ctx.account_currency(donor[1]) for donor in (donor_a, donor_b)} == {"USD"}
    presented.clear()
    out = _run(ctx)
    assert out.allocations == ()
    assert presented == []
    b1 = _only(out, BUDGET, B1)
    assert b1.decision_subject == DecisionSubject.of(receiver)
    assert b1.evidence.extra["currency"] == "EUR"
    assert evidence_metrics(b1)["increase"] == Decimal("400.00")
    _check_all_new_budget(
        b1,
        _built(ctx, B1)[receiver],
        "Raise by EUR 400.00: EUR 0.00 funded by reallocation, EUR 400.00 from new budget",
        (donor_a, donor_b),
    )
    built = _built(ctx, B3)
    donors = _by_subject(_items(out, BUDGET, B3))
    assert set(donors) == {donor_a, donor_b}
    for donor, freed in ((donor_a, Decimal("300.00")), (donor_b, Decimal("200.00"))):
        assert donors[donor].evidence.extra["freed"] == freed
        _check_plain_decrease(donors[donor], built[donor], receiver)


def test_b3_never_funds_a_receiver_of_another_source(
    make_context: ContextFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 cohort: a receiver is of its donor's source (AAP 0.5.4.2), never of another one.

    ``b3.no_receiver`` builds the LinkedIn donors freeing USD 300.00 and 200.00, and no B1. A
    rule-shaped B1 receiver (:func:`_cohort_party`) with an increase of USD 400.00 is presented
    beside them. As a LinkedIn campaign it is funded 300 and 100 (the control). As a Google
    campaign, the single input that changes, with the same currency and headroom, no cohort is
    presented for allocation, nothing is allocated, each donor stays the plain decrease the B3
    rule built, and the B1 increase is all new budget.
    """
    scenario = SCENARIOS["b3.no_receiver.case"]
    ctx = make_context(scenario.name)
    donor_a: SubjectKey = ("campaign", _campaign_id(scenario, "donor-a"))
    donor_b: SubjectKey = ("campaign", _campaign_id(scenario, "donor-b"))
    names = {donor: ctx.campaigns[donor[1]].name for donor in (donor_a, donor_b)}
    receiver: SubjectKey = ("campaign", 9_000)
    presented = _spy_on_allocation(monkeypatch)

    linkedin = _cohort_party(ctx, B1, "linkedin", receiver[1], "Receiver", "400.00")
    out = _run(ctx, RuleResult(candidates=(linkedin,)))
    assert presented == [([donor_a, donor_b], [receiver])]
    assert out.allocations == (
        Allocation(donor_a, receiver, Decimal("300.00")),
        Allocation(donor_b, receiver, Decimal("100.00")),
    )
    assert _only(out, BUDGET, B1).evidence.extra["funding"] == (
        f"Raise by USD 400.00: USD 400.00 funded by reallocation from {names[donor_a]}, "
        f"{names[donor_b]}, USD 0.00 from new budget"
    )

    google = _cohort_party(ctx, B1, "google", receiver[1], "Receiver", "400.00")
    presented.clear()
    out = _run(ctx, RuleResult(candidates=(google,)))
    assert out.allocations == ()
    assert presented == []
    b1 = _only(out, BUDGET, B1)
    assert b1.entity_ref.source == "google"
    assert b1.decision_subject == DecisionSubject.of(receiver)
    _check_all_new_budget(
        b1,
        google,
        "Raise by USD 400.00: USD 0.00 funded by reallocation, USD 400.00 from new budget",
        (donor_a, donor_b),
    )
    built = _built(ctx, B3)
    donors = _by_subject(_items(out, BUDGET, B3))
    assert set(donors) == {donor_a, donor_b}
    for donor, item in donors.items():
        assert item.entity_ref.source == "linkedin"
        _check_plain_decrease(item, built[donor], receiver)


def test_b3_funds_a_shared_budget_receiver_only_from_its_own_source(
    make_context: ContextFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 cohort with a budget subject: a shared budget's B1 is funded only within its cohort.

    ``shared.poor_member.does_not_fire`` builds a surviving Google B1 on the shared budget
    ``pool``, decided as ``("budget", id)``, raising USD 1,000.00 to 1,200.00, and no B3. A
    rule-shaped B3 donor (:func:`_cohort_party`) freeing USD 150.00 is presented beside it. As
    a Google campaign it gives 150 of the 200 increase, which leaves 50 from new budget (the
    control). As a LinkedIn campaign, the single input that changes, with the same currency,
    nothing is presented for allocation or allocated, the donor stays a plain decrease, and
    the budget's increase is all new budget.
    """
    scenario = SCENARIOS["shared.poor_member.does_not_fire"]
    ctx = make_context(scenario.name)
    pool: SubjectKey = ("budget", _budget_id(scenario, "pool"))
    pool_name = ctx.budgets[pool[1]].name
    donor: SubjectKey = ("campaign", 9_100)
    assert ctx.budgets[pool[1]].currency == "USD"
    assert _built(ctx, B3) == {}
    presented = _spy_on_allocation(monkeypatch)

    google = _cohort_party(ctx, B3, "google", donor[1], "Donor", "150.00")
    out = _run(ctx, RuleResult(candidates=(google,)))
    assert presented == [([donor], [pool])]
    assert out.allocations == (Allocation(donor, pool, Decimal("150.00")),)
    b1 = _only(out, BUDGET, B1)
    assert b1.decision_subject == DecisionSubject.of(pool)
    assert b1.entity_ref.source == "google"
    extra = b1.evidence.extra
    assert extra["increase"] == Decimal("200.00")
    funding = budget.render_b1_funding(extra["increase"], [("Donor", Decimal("150.00"))], "USD")
    assert funding == (
        "Raise by USD 200.00: USD 150.00 funded by reallocation from Donor, "
        "USD 50.00 from new budget"
    )
    assert extra["funding"] == funding
    assert extra["funded_by_reallocation"] == Decimal("150.00")
    assert extra["from_new_budget"] == Decimal("50.00")
    assert funding in b1.reason
    assert donor in {subject.key for subject in b1.depends_on}
    (given,) = _items(out, BUDGET, B3)
    text = budget.render_b3_allocation(Decimal("150.00"), pool_name, "USD")
    assert text == f"Move USD 150.00 to fund {pool_name}'s recommended increase (B1)"
    assert given.evidence.extra["allocations"] == (
        {
            "receiver": {"type": "budget", "id": pool[1]},
            "receiver_name": pool_name,
            "amount": Decimal("150.00"),
            "text": text,
        },
    )
    assert given.evidence.extra["unallocated"] == 0
    assert text in given.reason
    assert pool in {subject.key for subject in given.depends_on}

    linkedin = _cohort_party(ctx, B3, "linkedin", donor[1], "Donor", "150.00")
    presented.clear()
    out = _run(ctx, RuleResult(candidates=(linkedin,)))
    assert out.allocations == ()
    assert presented == []
    b1 = _only(out, BUDGET, B1)
    assert b1.decision_subject == DecisionSubject.of(pool)
    _check_all_new_budget(
        b1,
        _built(ctx, B1)[pool],
        "Raise by USD 200.00: USD 0.00 funded by reallocation, USD 200.00 from new budget",
        (donor,),
    )
    (plain,) = _items(out, BUDGET, B3)
    assert plain.entity_ref.source == "linkedin"
    _check_plain_decrease(plain, linkedin, pool)


# ---------------------------------------------------------------------------
# Phase 6: B4 weekday / weekend pattern (AAP 0.5.4.5 B4 row)
# ---------------------------------------------------------------------------


def _b4_item(scenario: Scenario, ctx: AnalysisContext, source: str, key: str) -> Candidate:
    """The one surviving B4 item of a campaign of the scenario."""
    (item,) = _on(_items(_run(ctx), BUDGET, B4), ("campaign", _campaign_id(scenario, key, source)))
    return item


def _check_rate_test(ctx: AnalysisContext, item: Candidate) -> None:
    """The B4 card's test is the Poisson rate test on its own counts, below ``b4.alpha``."""
    shown = evidence_metrics(item)
    recorded = item.evidence.test
    assert recorded is not None
    assert recorded.name == budget.B4_TEST_NAME
    rerun = poisson_rate_test(
        shown["weekend_outcomes"],
        shown["weekend_spend"],
        shown["weekday_outcomes"],
        shown["weekday_spend"],
    )
    assert rerun is not None
    assert recorded.p_value == rerun.p_value
    assert recorded.alpha == ctx.thresholds.ratio("b4.alpha")
    assert recorded.p_value < recorded.alpha
    assert shown["weekend_days"] >= ctx.thresholds.int("b4.min_weekend_days")
    assert shown["weekdays"] >= ctx.thresholds.int("b4.min_weekdays")
    total = shown["weekend_outcomes"] + shown["weekday_outcomes"]
    assert total >= ctx.thresholds.int("b4.min_outcomes")
    assert item.evidence.sample is not None
    assert (item.evidence.sample.observed, item.evidence.sample.unit) == (total, "outcomes")
    gate = item.evidence.extra["gate"]
    assert gate["weekend_days"] == {
        "observed": shown["weekend_days"],
        "required": ctx.thresholds.int("b4.min_weekend_days"),
    }
    assert item.decision_subject is None
    assert item.increases_spend_on == ()


def test_b4_runs_the_rate_test_on_linkedin_leads(make_context: ContextFactory) -> None:
    """B4: LinkedIn leads are counts by definition, so the rate test runs and p < 0.05 fires.

    The card shows weekend and weekday cost per lead in the action, the day and outcome counts,
    the p-value against ``b4.alpha`` and LinkedIn's UTC day boundary.
    """
    scenario = SCENARIOS["b4.linkedin.fires"]
    ctx = make_context(scenario.name)
    item = _b4_item(scenario, ctx, "linkedin", "li-cap")
    shown = evidence_metrics(item)
    assert (shown["weekend_days"], shown["weekdays"]) == (8, 20)
    assert item.action == (
        "Adjust weekend schedule or bids: weekend cost/outcome "
        f"{_money(shown['weekend_cpo'])} vs weekday {_money(shown['weekday_cpo'])}"
    )
    assert item.action.endswith("weekend cost/outcome USD 50.00 vs weekday USD 10.00")
    # The pattern's card leads with the weekend cost per outcome its action cites first.
    assert item.evidence.extra["headline_metric"] == "weekend_cpo"
    assert shown["weekend_cpo"] == Decimal("50.00")
    _check_rate_test(ctx, item)
    assert item.evidence.extra["contract"] == "LinkedIn leads are unweighted event counts"
    assert item.evidence.extra["day_boundary"] == {"source": "linkedin", "boundary": "UTC"}
    assert "LinkedIn days end at midnight UTC" in item.reason


def test_b4_below_its_minimums_is_withheld_with_the_sample_sizes(
    make_context: ContextFactory,
) -> None:
    """B4: minimums missed are withheld with every shortfall and the first one as the sample.

    Three weekend days with spend read "3 weekend days of 4 required"; with only six leads in
    the window as well, "3 weekend days of 4 required; 6 outcomes of 10 required".
    """
    scenario = SCENARIOS["b4.linkedin.does_not_fire"]
    ctx = make_context(scenario.name)
    campaign = _campaign_id(scenario, "li-cap")
    minimum = ctx.thresholds.int("b4.min_weekend_days")
    out = _run(ctx)
    assert _on(_items(out, BUDGET, B4), ("campaign", campaign)) == []
    (line,) = _lines(out, B4, campaign)
    assert line.reason == f"3 weekend days of {minimum} required"
    assert (line.observed, line.required, line.unit) == (3, minimum, "weekend days")

    days = ctx.campaign_days[campaign]
    sparse = tuple(replace(day, leads=1 if n < 6 else 0) for n, day in enumerate(days))
    fewer = _fresh(ctx, campaign_days={**ctx.campaign_days, campaign: sparse})
    (line,) = _lines(_run(fewer), B4, campaign)
    outcomes = ctx.thresholds.int("b4.min_outcomes")
    assert line.reason == f"3 weekend days of {minimum} required; 6 outcomes of {outcomes} required"
    assert line.reason == "3 weekend days of 4 required; 6 outcomes of 10 required"
    assert (line.observed, line.required) == (3, minimum)


@dataclass(frozen=True, slots=True)
class _B4Side:
    """One side (weekend or weekday) of a campaign's window days, as B4 totals it.

    Attributes:
        outcomes: The side's outcomes, a whole event count.
        spend: The side's spend.
        spend_days: The side's days with spend > 0.
    """

    outcomes: int
    spend: Decimal
    spend_days: int


def _b4_sides(ctx: AnalysisContext, campaign_id: int) -> tuple[_B4Side, _B4Side]:
    """A campaign's weekend and weekday totals over the window, recomputed from its days.

    Saturday and Sunday of the campaign's native dates are the weekend, as B4 splits them.
    Every day must report a whole outcome, so nothing is rounded here.
    """
    group = ctx.campaigns[campaign_id].objective_group
    window = ctx.window
    days = [day for day in ctx.campaign_days[campaign_id] if window.start <= day.date <= window.end]
    sides: list[_B4Side] = []
    for weekend in (True, False):
        side = [day for day in days if (day.date.weekday() >= 5) is weekend]
        outcomes = [day.outcome(group) for day in side]
        whole = [value for value in outcomes if value is not None]
        assert len(whole) == len(side), "every window day reports its outcome"
        assert all(value == value.to_integral_value() for value in whole)
        sides.append(
            _B4Side(
                outcomes=int(sum(whole, Decimal(0))),
                spend=sum((day.cost for day in side), Decimal(0)),
                spend_days=sum(1 for day in side if day.cost > 0),
            )
        )
    return sides[0], sides[1]


def test_b4_with_every_minimum_met_is_withheld_with_the_sample_sizes_when_p_reaches_alpha(
    make_context: ContextFactory,
) -> None:
    """B4: an adequately sampled pattern whose rate test gives p ≥ alpha is withheld with its
    sample sizes.

    Both twins meet every minimum (weekend days and weekdays with spend, and outcomes) with
    spend on both sides, and differ only in the weekend leads. The ``fires`` twin's test gives
    p < ``b4.alpha`` and one B4 item. With more weekend leads the rates are no longer
    distinguishable (p ≥ ``b4.alpha``), so the pattern is no item: its one withheld line names
    the test, its p-value against alpha and the day and outcome counts, and its sample is the
    outcome total against ``b4.min_outcomes``.
    """
    fires = SCENARIOS["b4.rate_test.fires"]
    fired = make_context(fires.name)
    _check_rate_test(fired, _b4_item(fires, fired, "linkedin", "li-cap"))

    scenario = SCENARIOS["b4.rate_test.does_not_fire"]
    ctx = make_context(scenario.name)
    campaign = _campaign_id(scenario, "li-cap")
    weekend, weekday = _b4_sides(ctx, campaign)
    fired_weekend, fired_weekday = _b4_sides(fired, _campaign_id(fires, "li-cap"))
    assert fired_weekday == weekday
    assert (fired_weekend.spend, fired_weekend.spend_days) == (weekend.spend, weekend.spend_days)
    assert fired_weekend.outcomes < weekend.outcomes

    thresholds = ctx.thresholds
    alpha = thresholds.ratio("b4.alpha")
    minimum = thresholds.int("b4.min_outcomes")
    total = weekend.outcomes + weekday.outcomes
    assert weekend.spend > 0
    assert weekday.spend > 0
    assert weekend.spend_days >= thresholds.int("b4.min_weekend_days")
    assert weekday.spend_days >= thresholds.int("b4.min_weekdays")
    assert total >= minimum
    test = poisson_rate_test(weekend.outcomes, weekend.spend, weekday.outcomes, weekday.spend)
    assert test is not None
    assert test.p_value >= alpha

    out = _run(ctx)
    assert _items(out, BUDGET, B4) == []
    (line,) = _lines(out, B4, campaign)
    shown = test.p_value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
    assert shown >= alpha
    assert line.reason == (
        f"weekend and weekday rates not distinguishable ({budget.B4_TEST_NAME} p = {shown}, "
        f"alpha {alpha.normalize():f}): {weekend.spend_days} weekend days, "
        f"{weekday.spend_days} weekdays, {total} outcomes"
    )
    assert "alpha 0.05)" in line.reason
    assert (line.observed, line.required, line.unit) == (total, minimum, "outcomes")
    assert scenario.expect.withheld_reason is not None
    assert line.reason.startswith(scenario.expect.withheld_reason)


def test_b4_runs_on_google_only_under_a_verified_last_click_contract(
    make_context: ContextFactory,
) -> None:
    """B4 on Google: every included action last-click on every window day, whole daily values.

    The card names the contract ("all included actions last-click on all N days", N the
    window's days) and Google's day boundary, the account's time zone.
    """
    scenario = SCENARIOS["b4.google_last_click.fires"]
    ctx = make_context(scenario.name)
    campaign = _campaign_id(scenario, "weekly", "google")
    assert all(
        day.conversions is not None and day.conversions == day.conversions.to_integral_value()
        for day in ctx.campaign_days[campaign]
    )
    item = _b4_item(scenario, ctx, "google", "weekly")
    _check_rate_test(ctx, item)
    assert item.evidence.extra["contract"] == (
        f"all included actions last-click on all {ctx.window.days} days"
    )
    assert item.evidence.extra["contract"] == "all included actions last-click on all 28 days"
    boundary = item.evidence.extra["day_boundary"]
    assert boundary == {"source": "google", "boundary": ctx.window.boundary("google")}
    assert boundary["boundary"] != "UTC"


def _first_contract_snapshot(ctx: AnalysisContext) -> datetime:
    """The earliest Q-G8 conversion-action snapshot the context holds."""
    return min(
        observation.captured_at
        for (entity_type, _), observations in ctx.observations.items()
        if entity_type == "conversion_action"
        for observation in observations
    )


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("b4.google_last_click.does_not_fire", budget.WEIGHTED_REASON),
        ("b4.contract_unknown.case", budget.CONTRACT_UNKNOWN_REASON),
        ("b4.fractional.case", budget.FRACTIONAL_REASON),
        ("b4.no_exposure.case", budget.NO_EXPOSURE_REASON),
    ],
)
def test_b4_is_withheld_never_rounded_when_the_counts_are_not_valid(
    make_context: ContextFactory, name: str, reason: str
) -> None:
    """B4 withholds, never rounds: weighted attribution on any day (even with whole daily
    totals), an unknown counting contract, a fractional daily value, or no exposure."""
    scenario = SCENARIOS[name]
    ctx = make_context(scenario.name)
    campaign = _campaign_id(scenario, "weekly", "google")
    days = ctx.campaign_days[campaign]
    whole = [
        day.conversions is not None and day.conversions == day.conversions.to_integral_value()
        for day in days
    ]
    if reason == budget.WEIGHTED_REASON:
        assert all(whole)
    elif reason == budget.CONTRACT_UNKNOWN_REASON:
        assert _first_contract_snapshot(ctx).date() > ctx.window.start
    elif reason == budget.FRACTIONAL_REASON:
        assert not all(whole)
    else:
        assert sum((day.cost for day in days if day.date.weekday() >= 5), Decimal(0)) == 0
    out = _run(ctx)
    assert _on(_items(out, BUDGET, B4), ("campaign", campaign)) == []
    (line,) = _lines(out, B4, campaign)
    assert line.reason == reason
    assert scenario.expect.withheld_reason == reason


@pytest.mark.parametrize(
    ("name", "source", "key", "field", "label"),
    [
        ("b4.linkedin.fires", "linkedin", "li-cap", "leads", "leads"),
        ("b4.google_last_click.fires", "google", "weekly", "conversions", "conversions"),
    ],
)
def test_b4_is_withheld_never_zeroed_when_a_day_left_its_outcome_unreported(
    make_context: ContextFactory, name: str, source: str, key: str, field: str, label: str
) -> None:
    """B4 withholds, never zeroes or rounds, an unreported outcome (AAP 0.11.1).

    Every other gate of the two firing patterns holds: spend on both sides, unweighted counts
    (LinkedIn leads by definition; Google last-click on every day with whole values), the day
    and outcome minimums and p < ``b4.alpha``, so the catalogue twin is an item. With the
    window's first day reporting no outcome (``None``) instead of its count, the pattern is
    withheld "{label} unavailable on 1 of 28 days", with the reported days against the
    window's days as its sample, and is no item. With no day reporting one there is nothing
    to compare: no item and no line, never the "0 outcomes of 10 required" a known 0 on every
    day is withheld with.
    """
    scenario = SCENARIOS[name]
    base = make_context(scenario.name)
    campaign = _campaign_id(scenario, key, source)
    window = base.window
    _check_rate_test(base, _b4_item(scenario, base, source, key))
    known = base.campaign_days[campaign]
    assert sorted(day.date for day in known) == [
        window.start + timedelta(days=offset) for offset in range(window.days)
    ]
    assert all(getattr(day, field) is not None for day in known)

    def with_outcomes(value: Callable[[date], int | Decimal | None]) -> PrecedenceOutput:
        """Run the Budget rules with each window day's outcome set to ``value(date)``."""
        days = tuple(replace(day, **{field: value(day.date)}) for day in known)
        return _run(_fresh(base, campaign_days={**base.campaign_days, campaign: days}))

    reported = {day.date: getattr(day, field) for day in known}
    one_day = with_outcomes(lambda day: None if day == window.start else reported[day])
    assert _on(_items(one_day, BUDGET, B4), ("campaign", campaign)) == []
    (line,) = _lines(one_day, B4, campaign)
    assert line.reason == f"{label} unavailable on 1 of {window.days} days"
    assert line.reason == f"{label} unavailable on 1 of 28 days"
    assert (line.observed, line.required, line.unit) == (
        window.days - 1,
        window.days,
        f"days with {label} reported",
    )

    every_day = with_outcomes(lambda day: None)
    assert _on(_items(every_day, BUDGET, B4), ("campaign", campaign)) == []
    assert _lines(every_day, B4, campaign) == []

    # Google conversions are decimal amounts, LinkedIn leads whole counts.
    zero: int | Decimal = Decimal(0) if source == "google" else 0
    known_zero = with_outcomes(lambda day: zero)
    (line,) = _lines(known_zero, B4, campaign)
    assert line.reason == f"0 outcomes of {base.thresholds.int('b4.min_outcomes')} required"


def test_a_shared_members_b4_card_shows_the_shared_budgets_history(
    make_context: ContextFactory,
) -> None:
    """B4 card of a shared-budget member: a member has no utilization of its own, so beside
    its weekend pattern the card shows the pool's budget-active days, utilization and amount,
    and names every member; the member itself stays no budget subject.

    m1's weekend conversions are lowered to 5 a day (whole numbers under the pool's last-click
    action), which makes its weekend cost per conversion distinguishably higher.
    """
    scenario = SCENARIOS["shared.poor_member.does_not_fire"]
    base = make_context(scenario.name)
    pool: SubjectKey = ("budget", _budget_id(scenario, "pool"))
    m1 = _campaign_id(scenario, "m1", "google")
    days = tuple(
        replace(day, conversions=Decimal(5)) if day.date.weekday() >= 5 else day
        for day in base.campaign_days[m1]
    )
    ctx = _fresh(base, campaign_days={**base.campaign_days, m1: days})
    item = _b4_item(scenario, ctx, "google", "m1")
    _check_rate_test(ctx, item)
    history = budget_history(ctx, pool)
    assert history.window_utilization is not None
    extra = item.evidence.extra
    assert extra["budget_history"]["subject"] == {"type": "budget", "id": pool[1]}
    assert extra["budget_history"]["shared"] is True
    assert {entry["campaign_id"] for entry in extra["members"]} == set(subject_campaigns(ctx, pool))
    shown = evidence_metrics(item)
    assert shown["window_utilization"] == history.window_utilization.quantize(Decimal("0.0001"))
    assert shown["known_days"] == history.known_active
    assert shown["daily_budget"] == history.latest_daily_budget
    assert ("campaign", m1) not in budget_subjects(ctx)
    assert item.decision_subject is None


# ---------------------------------------------------------------------------
# Phase 7: lifetime and custom-period budgets (AAP 0.5.4.1 "Lifetime budgets")
# ---------------------------------------------------------------------------


def test_a_lifetime_budget_shows_no_utilization_and_gets_no_cap_rule(
    make_context: ContextFactory,
) -> None:
    """A LinkedIn lifetime budget: utilization "n/a", and no C2, B1, B2 or B3 at all.

    The campaign delivers 97.00 a day, which would be capped against a 100.00 daily budget;
    with a lifetime budget it is no budget subject, so no cap rule judges or withholds it.
    """
    scenario = SCENARIOS["budget.lifetime.case"]
    ctx = make_context(scenario.name)
    subject: SubjectKey = ("campaign", _campaign_id(scenario, "li-cap"))
    campaign = ctx.campaigns[subject[1]]
    assert campaign.daily_budget is None
    assert campaign.total_budget is not None
    history = budget_history(ctx, subject)
    assert not history.utilization_applicable
    assert history.window_utilization is None
    assert history.known_active == 0
    assert subject not in budget_subjects(ctx)
    out = _run(ctx)
    assert _items(out, CAMPAIGNS, C2) == []
    for rule_id in (B1, B2, B3):
        assert _items(out, BUDGET, rule_id) == []
        assert _lines(out, rule_id) == []


def _custom_period(ctx: AnalysisContext, campaign_id: int, total: Decimal) -> AnalysisContext:
    """Copy a Google context with one campaign's own budget moved to ``CUSTOM_PERIOD``.

    The campaign, its budget row and every snapshot of either (window and latest) state the
    custom period, so the history reads the amount as a lifetime one, never a daily cap.
    """
    campaign = ctx.campaigns[campaign_id]
    assert campaign.budget_id is not None
    budget_id = campaign.budget_id

    def moved(key: tuple[str, int], observation: Any) -> Any:
        if key == ("campaign", campaign_id):
            return replace(
                observation,
                budget_daily=None,
                budget_total=total,
                payload={**observation.payload, "budget_period": "CUSTOM_PERIOD"},
            )
        if key == ("budget", budget_id):
            return replace(observation, payload={**observation.payload, "period": "CUSTOM_PERIOD"})
        return observation

    return _fresh(
        ctx,
        campaigns={
            **ctx.campaigns,
            campaign_id: replace(
                campaign, budget_period="CUSTOM_PERIOD", daily_budget=None, total_budget=total
            ),
        },
        budgets={**ctx.budgets, budget_id: replace(ctx.budgets[budget_id], period="CUSTOM_PERIOD")},
        observations={
            key: tuple(moved(key, observation) for observation in observations)
            for key, observations in ctx.observations.items()
        },
        latest_observations={
            key: moved(key, observation) for key, observation in ctx.latest_observations.items()
        },
    )


def test_a_custom_period_budget_shows_utilization_na_and_gets_no_cap_rule(
    make_context: ContextFactory,
) -> None:
    """A Google ``CUSTOM_PERIOD`` budget: the B4 card shows utilization "n/a"; no cap rule.

    The weekly-pattern campaign keeps its delivery and its B4 pattern; with its own budget
    moved to a custom period it has no known-budget day, so C2, B1, B2 and B3 never judge it.
    The catalogue holds no custom-period world, so the case is a copy of a daily-budget one
    whose budget and snapshots state the custom period (:func:`_custom_period`).
    """
    scenario = SCENARIOS["b4.google_last_click.fires"]
    campaign = _campaign_id(scenario, "weekly", "google")
    ctx = _custom_period(make_context(scenario.name), campaign, Decimal("4200.00"))
    subject: SubjectKey = ("campaign", campaign)
    history = budget_history(ctx, subject)
    assert not history.utilization_applicable
    assert history.window_utilization is None
    assert history.latest_daily_budget is None
    assert subject not in budget_subjects(ctx)
    out = _run(ctx)
    assert _items(out, CAMPAIGNS, C2) == []
    for rule_id in (B1, B2, B3):
        assert _items(out, BUDGET, rule_id) == []
        assert _lines(out, rule_id) == []
    (card,) = _on(_items(out, BUDGET, B4), subject)
    shown = evidence_metrics(card)
    assert shown["window_utilization"] == "n/a"
    assert shown["known_days"] == 0
    assert shown["daily_budget"] is None
