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
* **Spending-day gates** (AAP 0.5.4.1 table) and the utilization-beside-efficiency card.
* **Shared budgets** (AAP 0.5.4.5): dated membership, the member safety predicate, the
  budget subject, mixed objective groups and, against the database, a shared-budget decision
  that raises no discrepancy on any member at the next entity sync.
* **B3 allocation** (AAP 0.5.4.2): 300 and 200 against a 400 increase, the deterministic
  orders, the over-allocation assertion and the plain decrease without a receiver.
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
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Final

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    SubjectKey,
    Withheld,
    cohort_group,
    content_asset_external_id,
)
from ads_engine.analysis.metrics import (
    BudgetHistory,
    assess_b2,
    budget_history,
    budget_subjects,
    member_poor_efficiency,
    spend_increase_blocked,
    subject_campaigns,
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


def _metrics(item: Candidate) -> dict[str, Any]:
    """An item's evidence metrics by key."""
    return {metric.key: metric.value for metric in item.evidence.metrics}


def _campaign_id(scenario: Scenario, key: str, source: str = "linkedin") -> int:
    """The internal id the catalogue assigns to a campaign key."""
    return scenario.ids.id(source, "campaign", key)


def _budget_id(scenario: Scenario, key: str) -> int:
    """The internal id the catalogue assigns to a Google budget key."""
    return scenario.ids.id("google", "budget", key)


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


def _number(value: object) -> object:
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
    if isinstance(value, list | tuple):
        return tuple(_number(item) for item in value)
    return value


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
        return tuple(extra["unknown_dates"])
    metrics_by_key = _metrics(item)
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
            assert _number(actual) == _number(expected), (key, actual, expected)
    else:
        assert _on(items, _focus(scenario)) == [], [item.action for item in items]
        for key, expected in expect.values.items():
            actual = _silent_value(scenario, ctx, out, rule_id, key)
            assert _number(actual) == _number(expected), (key, actual, expected)
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
        assert _number(actual) == _number(expected), (key, actual, expected)


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
        assert _number(actual) == _number(expected), (key, actual, expected)


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
    carrying those fields stands for any rule output of its kind.
    """
    datasets = ("linkedin.entities", "linkedin.analytics_daily")
    evidence = ctx.make_evidence(
        required_datasets=datasets,
        crm_scope=None,
        metrics=tuple(metrics_shown),
        extra=dict(extra or {}),
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
    shown = _metrics(item)
    assert shown["daily_budget"] == current == item.evidence.extra["current"]
    assert shown["proposed_daily_budget"] == proposed == item.evidence.extra["proposed"]
    assert shown["increase"] == proposed - current == item.evidence.extra["increase"]
    assert item.evidence.extra["step"] == step
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
    current = _metrics(item)["daily_budget"]
    assert item.evidence.extra["step"] == Decimal("0.25")
    assert _metrics(item)["proposed_daily_budget"] == round_to_minor_unit(
        current * Decimal("1.25"), "USD"
    )
    assert _metrics(item)["proposed_daily_budget"] == Decimal("104.16")


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
    """B1: a zero-decimal currency proposes whole units; a three-decimal one keeps mils."""
    scenario = SCENARIOS["b1.step.case"]
    campaign_id = _campaign_id(scenario, "li-cap")
    ctx = _in_currency(make_context(scenario.name), campaign_id, currency)
    item = _only(_run(ctx), BUDGET, B1)
    shown = _metrics(item)
    assert shown["proposed_daily_budget"] == proposed
    assert shown["proposed_daily_budget"].as_tuple().exponent == -minor_unit_exponent(currency)
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
    shown = _metrics(item)
    assert shown["spend_percentile"] == observed
    assert shown["proposed_daily_budget"] == proposed == Decimal("88.00")
    assert item.action == f"Lower daily budget to {_money(proposed)}"
    assert shown["freed"] == current - proposed == item.evidence.extra["freed"]
    assert (shown["idle_days"], shown["under_utilized_days"]) == (4, 8)
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
    shown = _metrics(item)
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
    shown = _metrics(item)
    assert item.evidence.extra["branch"] == "zero_outcomes"
    assert shown["outcomes"] == 0
    assert shown["cost_per_outcome"] is None
    assert shown["expected_outcomes"] >= ctx.thresholds.ratio("b2.min_expected_outcomes")
    assert ("campaign", hot) in spend_increase_blocked(ctx)


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
    assert _metrics(item)["changed_intraday_days"] == 1
    assert _metrics(item)["idle_days"] == 0


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
            assert sample.observed == _metrics(item)["known_days"]
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
    assert tuple(item.evidence.extra["unknown_dates"]) == ("2026-09-27",)


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
    current = _metrics(item)["daily_budget"]
    assert _metrics(item)["proposed_daily_budget"] == round_to_minor_unit(
        current * (1 + step), "USD"
    )
    assert _metrics(item)["proposed_daily_budget"] == Decimal("1200.00")
    assert item.decision_subject == DecisionSubject("budget", pool_id)


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
        assert _metrics(item)["known_days"] == history.known_active
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
    assert _metrics(item)["cost_per_outcome"] is not None


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
    assert (
        donors[donor_b].action
        == f"Lower daily budget to {_money(_metrics(donors[donor_b])['proposed_daily_budget'])}"
    )


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


# ---------------------------------------------------------------------------
# Phase 6: B4 weekday / weekend pattern (AAP 0.5.4.5 B4 row)
# ---------------------------------------------------------------------------


def _b4_item(scenario: Scenario, ctx: AnalysisContext, source: str, key: str) -> Candidate:
    """The one surviving B4 item of a campaign of the scenario."""
    (item,) = _on(_items(_run(ctx), BUDGET, B4), ("campaign", _campaign_id(scenario, key, source)))
    return item


def _check_rate_test(ctx: AnalysisContext, item: Candidate) -> None:
    """The B4 card's test is the Poisson rate test on its own counts, below ``b4.alpha``."""
    shown = _metrics(item)
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
    shown = _metrics(item)
    assert (shown["weekend_days"], shown["weekdays"]) == (8, 20)
    assert item.action == (
        "Adjust weekend schedule or bids: weekend cost/outcome "
        f"{_money(shown['weekend_cpo'])} vs weekday {_money(shown['weekday_cpo'])}"
    )
    assert item.action.endswith("weekend cost/outcome USD 50.00 vs weekday USD 10.00")
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
    shown = _metrics(item)
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
    shown = _metrics(card)
    assert shown["window_utilization"] == "n/a"
    assert shown["known_days"] == 0
    assert shown["daily_budget"] is None
