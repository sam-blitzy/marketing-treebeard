"""Unit tests for ``ads_engine.analysis.precedence``: G1, B3 allocation, P1-P8, feedback, ranking.

They hold :func:`~ads_engine.analysis.precedence.apply_precedence` to the fixed order of AAP
0.5.4.2 and the guarantees it carries: the G1 guard and its independent post-check, the
deterministic B3 headroom allocation with its funding sentences, P1 holds with the expanded
``depends_on`` the overlay reads, P2 tracking suppression, P3 data status, P4 stale and
incomplete flags from each item's own datasets (AAP 0.3.2), the P5 short-window flag, the
P6/P7 checks, feedback suppression with counts, and ranking that is identical for any input
order (AAP 0.5.4.1 "Ranking ties").

Contexts come from the scenario catalogue (``SCENARIOS[name].build_context()``, built fresh
per test so no memoised table leaks between tests); a few cases copy a context or candidate
with :func:`dataclasses.replace` to move one input across a threshold. Pure: no database and
no network.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from ads_engine.analysis import (
    budget,
    campaigns,
    creative,
    discrepancy,
    keywords,
    metrics,
    precedence,
    white_papers,
)
from ads_engine.analysis.context import (
    B1,
    B2,
    B3,
    BUDGET,
    C0,
    C1,
    C1B,
    C2,
    CAMPAIGNS,
    CR1,
    CREATIVE,
    DATA_MIXED,
    DATA_SIMULATED,
    DATA_STALE,
    DATA_STATUS_VALUES,
    ENTITY_AD_GROUP,
    ENTITY_BUDGET,
    ENTITY_CAMPAIGN,
    ENTITY_CREATIVE,
    ENTITY_CREATIVE_PAIR,
    ENTITY_KEYWORD,
    ENTITY_SEARCH_TERM,
    ENTITY_SEGMENT,
    FINDING_CONVERSION_TRACKING_ABSENT,
    FINDING_INCOMPLETE_DATASET,
    FINDING_NO_CRM_LINK,
    FINDING_SEGMENT_WINDOW_UNAVAILABLE,
    FLAG_INCOMPLETE,
    FLAG_NO_CRM_LINK,
    FLAG_NOT_DISTINGUISHABLE,
    FLAG_SHORT_WINDOW,
    FLAG_SIMULATED,
    FLAG_STALE,
    FLAG_UNVERIFIED_DISCREPANCIES,
    FULL_STATE_DATASETS,
    GROUP_CHANGES,
    HELD_DISCREPANCY_REASON,
    K1,
    KEYWORDS,
    SECTION_ORDER,
    SUPPRESS_FEEDBACK_DISMISS,
    SUPPRESS_G1_BLOCKED,
    SUPPRESS_TRACKING_ABSENT,
    W2,
    WHITE_PAPERS,
    AnalysisContext,
    Candidate,
    DecisionSubject,
    DiscrepancyInfo,
    EntityRef,
    FeedbackInfo,
    Finding,
    Flag,
    Freshness,
    Metric,
    ProvenanceEntry,
    RuleResult,
    Sample,
    Withheld,
    creative_group,
    unverified_message,
)
from ads_engine.analysis.precedence import (
    TRACKING_ABSENT_MESSAGE,
    Allocation,
    PrecedenceInvariantError,
    PrecedenceOutput,
    RankedItem,
    allocate_b3,
    apply_precedence,
)
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.simulation.scenarios import (
    MODULE_BUDGET,
    MODULE_CREATIVE,
    MODULE_PRECEDENCE,
    SCENARIOS,
)

PRECEDENCE_MODULES = frozenset({MODULE_BUDGET, MODULE_CREATIVE, MODULE_PRECEDENCE})
"""Catalogue modules whose scenarios exercise G1, the B3 allocation, P7 and precedence."""

RULE_MODULES = (discrepancy, keywords, campaigns, budget, creative, white_papers)

STALE_AFTER_KEY = "sources.stale_after_hours"
"""The threshold P4 judges each required dataset's age against."""

TRUSTED_MIN_DAYS_KEY = "window.trusted_min_days"
"""The threshold P5 judges the window's length against."""


def build(name: str) -> AnalysisContext:
    """A fresh context of one catalogue scenario."""
    return SCENARIOS[name].build_context()


def evaluate(ctx: AnalysisContext) -> list[RuleResult]:
    """Every rule module's result, in the runner's fixed order."""
    return [module.evaluate(ctx) for module in RULE_MODULES]


def run(name: str) -> tuple[AnalysisContext, PrecedenceOutput]:
    """Build a scenario, evaluate every rule and apply precedence."""
    ctx = build(name)
    return ctx, apply_precedence(evaluate(ctx), ctx)


def of_rule(out: PrecedenceOutput, section: str, rule_id: str | None = None) -> list[Candidate]:
    """The ranked candidates of a section, optionally of one rule."""
    return [
        item.candidate
        for item in out.items[section]
        if rule_id is None or item.candidate.rule_id == rule_id
    ]


def flag_message(candidate: Candidate, code: str) -> str:
    """The message of a candidate's flag ``code``."""
    return next(flag.message for flag in candidate.flags if flag.code == code)


def ranking(out: PrecedenceOutput) -> list[tuple[str, int, str, str]]:
    """Every item's section, rank, rule and entity key, in output order."""
    return [
        (section, item.rank, item.candidate.rule_id, item.candidate.entity_ref.key)
        for section in SECTION_ORDER
        for item in out.items[section]
    ]


def sample_candidate(ctx: AnalysisContext | None = None) -> tuple[AnalysisContext, Candidate]:
    """A K1 candidate of a plain simulated world, before precedence, with ``ctx`` or its own."""
    own = build("p3.simulated.case")
    k1 = [c for c in keywords.evaluate(own).candidates if c.rule_id == K1]
    return (own if ctx is None else ctx), k1[0]


def plain(candidate: Candidate, **changes: Any) -> Candidate:
    """A copy with no dependencies, decision subject or spend targets, then ``changes``."""
    base = replace(candidate, depends_on=(), decision_subject=None, increases_spend_on=())
    return replace(base, **changes)


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


def test_default_world_output_contract() -> None:
    _, out = run("default")
    assert tuple(out.items) == SECTION_ORDER
    assert tuple(out.findings) == SECTION_ORDER
    assert tuple(out.withheld) == SECTION_ORDER
    assert tuple(out.suppressed) == SECTION_ORDER
    assert out.all_items()
    for section in SECTION_ORDER:
        ranks = [item.rank for item in out.items[section]]
        assert ranks == list(range(1, len(ranks) + 1))
        assert all(item.section == section for item in out.items[section])
    for item in out.all_items():
        candidate = item.candidate
        assert item.group == candidate.group
        assert candidate.data_status in DATA_STATUS_VALUES
        assert isinstance(candidate.evidence.to_json()["extra"]["depends_on"], list)


def test_precedence_scenarios_pass_identically_in_any_order() -> None:
    for name, scenario in SCENARIOS.items():
        # The guard, allocation, P7 and precedence cases; the rule modules' own acceptance
        # tests run precedence over the rest of the catalogue.
        if scenario.module not in PRECEDENCE_MODULES:
            continue
        try:
            ctx = scenario.build_context()
        except ValueError:
            continue  # a scenario that expects its configuration to fail (wp.override_conflict)
        results = evaluate(ctx)
        first = apply_precedence(results, ctx)
        second = apply_precedence(list(reversed(results)), ctx)
        assert ranking(first) == ranking(second), name
        assert dict(first.findings) == dict(second.findings), name
        assert dict(first.withheld) == dict(second.withheld), name


def test_type_and_value_validation() -> None:
    ctx, candidate = sample_candidate()
    with pytest.raises(TypeError, match="RuleResult"):
        apply_precedence([object()], ctx)  # type: ignore[list-item]
    with pytest.raises(TypeError, match="AnalysisContext"):
        apply_precedence([], object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Candidate"):
        RankedItem(object(), 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="int"):
        RankedItem(candidate, True)
    with pytest.raises(ValueError, match="1-based"):
        RankedItem(candidate, 0)
    with pytest.raises(TypeError, match="Decimal"):
        Allocation(("campaign", 1), ("campaign", 2), 5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        Allocation(("campaign", 1), ("campaign", 2), Decimal("NaN"))
    empty: dict[str, Any] = dict.fromkeys(SECTION_ORDER, ())
    with pytest.raises(TypeError, match="mapping"):
        PrecedenceOutput(items=(), findings=empty, withheld=empty, suppressed={})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="keyed by exactly"):
        PrecedenceOutput(items={}, findings=empty, withheld=empty, suppressed=empty)


def test_single_result_and_rule_level_suppression_counts() -> None:
    ctx, candidate = sample_candidate()
    out = apply_precedence(RuleResult(candidates=(candidate,), suppressed={"rule_reason": 2}), ctx)
    assert out.suppressed[KEYWORDS] == {"rule_reason": 2}
    assert out.suppressed_total("rule_reason") == 2
    two_sections = RuleResult(
        withheld=(
            Withheld(K1, KEYWORDS, None, "clicks 1 of 2 required", 1, 2, "clicks"),
            Withheld(C1, CAMPAIGNS, None, "window 6 days of 7", 6, 7, "days"),
        ),
        suppressed={"rule_reason": 1},
    )
    with pytest.raises(ValueError, match="exactly one section"):
        apply_precedence(two_sections, ctx)
    with pytest.raises(ValueError, match="none"):
        apply_precedence(RuleResult(suppressed={"rule_reason": 1}), ctx)


def test_allocation_json() -> None:
    allocation = Allocation(("campaign", 1), ("budget", 2), Decimal("12.50"))
    assert allocation.to_json() == {
        "donor": {"type": "campaign", "id": 1},
        "receiver": {"type": "budget", "id": 2},
        "amount": "12.50",
    }


# ---------------------------------------------------------------------------
# G1 guard (AAP 0.5.4.2 G1, FR-6)
# ---------------------------------------------------------------------------


def test_g1_drops_and_counts_blocked_increases() -> None:
    ctx, out = run("shared.poor_member.fires")
    blocked = metrics.spend_increase_blocked(ctx)
    assert blocked
    assert all(not item.candidate.spend_targets & blocked for item in out.all_items())
    assert out.suppressed[BUDGET][SUPPRESS_G1_BLOCKED] == 1
    assert out.suppressed[CAMPAIGNS][SUPPRESS_G1_BLOCKED] == 1
    assert not of_rule(out, BUDGET, B1)


def test_g1_b2_target_keeps_no_increase_while_its_partner_scales() -> None:
    _, blocked = run("g1.b2_target.fires")
    assert of_rule(blocked, BUDGET, B2)
    assert not of_rule(blocked, BUDGET, B1)
    _, partner = run("g1.b2_target.does_not_fire")
    assert of_rule(partner, BUDGET, B1)


def test_g1_bypassing_the_filter_step_trips_the_post_check(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = build("shared.poor_member.fires")
    results = evaluate(ctx)

    def pass_through(
        candidates: Sequence[Candidate], blocked: frozenset[Any]
    ) -> tuple[tuple[Candidate, ...], tuple[Candidate, ...]]:
        return tuple(candidates), ()

    monkeypatch.setattr(precedence, "apply_g1", pass_through)
    with pytest.raises(PrecedenceInvariantError, match="G1"):
        apply_precedence(results, ctx)


def test_g1_emptied_predicate_after_build_trips_the_post_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = build("shared.poor_member.fires")
    results = evaluate(ctx)
    monkeypatch.setattr(metrics, "spend_increase_blocked", lambda ctx: frozenset())
    with pytest.raises(PrecedenceInvariantError, match="survived the guard"):
        apply_precedence(results, ctx)


def test_apply_g1_partitions_by_declared_targets() -> None:
    _, candidate = sample_candidate()
    raising = plain(candidate, increases_spend_on=(DecisionSubject("campaign", 9),))
    kept, removed = precedence.apply_g1([candidate, raising], frozenset({("campaign", 9)}))
    assert kept == (candidate,)
    assert removed == (raising,)


# ---------------------------------------------------------------------------
# B3 allocation (AAP 0.5.4.2)
# ---------------------------------------------------------------------------


def test_b3_allocation_300_and_100_into_400() -> None:
    _, out = run("b3.allocation.case")
    donors = sorted(of_rule(out, BUDGET, B3), key=lambda c: -c.evidence.extra["freed"])
    assert [c.evidence.extra["freed"] for c in donors] == [Decimal("300.00"), Decimal("200.00")]
    assert [a.amount for a in out.allocations] == [Decimal("300.00"), Decimal("100.00")]
    assert donors[0].evidence.extra["unallocated"] == Decimal("0.00")
    assert donors[1].evidence.extra["allocated"] == Decimal("100.00")
    assert donors[1].evidence.extra["unallocated"] == Decimal("100.00")
    (receiver,) = of_rule(out, BUDGET, B1)
    for donor, amount in zip(donors, ("300.00", "100.00"), strict=True):
        (line,) = donor.evidence.extra["allocations"]
        assert line["amount"] == Decimal(amount)
        assert line["receiver"] == {"type": "campaign", "id": receiver.decision_subject.id}  # type: ignore[union-attr]
        assert line["text"] == budget.render_b3_allocation(
            Decimal(amount), receiver.entity_ref.name, "USD"
        )
        assert line["text"] in donor.reason
    extra = receiver.evidence.extra
    assert extra["increase"] == Decimal("400.00")
    assert extra["funded_by_reallocation"] == Decimal("400.00")
    assert extra["from_new_budget"] == Decimal("0.00")
    assert extra["funding"] == budget.render_b1_funding(
        Decimal("400.00"),
        [
            (donors[0].entity_ref.name, Decimal("300.00")),
            (donors[1].entity_ref.name, Decimal("100.00")),
        ],
        "USD",
    )
    assert extra["funding"] in receiver.reason
    assert receiver.action.startswith("Raise daily budget from ")


def test_b3_without_receiver_stays_a_plain_decrease() -> None:
    _, out = run("b3.no_receiver.case")
    donors = of_rule(out, BUDGET, B3)
    assert len(donors) == 2
    assert out.allocations == ()
    for donor in donors:
        assert donor.evidence.extra["allocations"] == ()
        assert donor.evidence.extra["unallocated"] == donor.evidence.extra["freed"]
        assert "Move " not in donor.reason


def test_b1_without_donor_is_funded_from_new_budget() -> None:
    _, out = run("g1.b2_target.does_not_fire")
    (receiver,) = of_rule(out, BUDGET, B1)
    extra = receiver.evidence.extra
    assert extra["funded_by_reallocation"] == Decimal(0)
    assert extra["from_new_budget"] == extra["increase"]
    assert extra["funding"] not in receiver.reason


def test_over_allocation_trips_the_post_check(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = build("b3.allocation.case")
    results = evaluate(ctx)

    def everything_to_the_first(
        donors: Sequence[tuple[Any, Decimal]], receivers: Sequence[tuple[Any, Decimal]]
    ) -> tuple[Allocation, ...]:
        return tuple(Allocation(donor, receivers[0][0], freed) for donor, freed in donors)

    monkeypatch.setattr(precedence, "allocate_b3", everything_to_the_first)
    with pytest.raises(PrecedenceInvariantError, match="exceed its B1 increase"):
        apply_precedence(results, ctx)


def test_allocate_b3_fills_receivers_in_order() -> None:
    a, b, c = ("campaign", 1), ("campaign", 2), ("campaign", 3)
    shared = ("budget", 4)
    made = allocate_b3(
        [(a, Decimal(300)), (b, Decimal(200)), (c, Decimal(10))],
        [(c, Decimal(250)), (shared, Decimal(100))],
    )
    assert [(x.donor, x.receiver, x.amount) for x in made] == [
        (a, c, Decimal(250)),
        (a, shared, Decimal(50)),
        (b, shared, Decimal(50)),
    ]
    assert allocate_b3([(a, Decimal(5))], [(a, Decimal(5))]) == ()


@pytest.mark.parametrize(
    ("allocations", "message"),
    [
        (("not an allocation",), "is not an Allocation"),
        ((Allocation(("campaign", 9), ("campaign", 3), Decimal(1)),), "not a B3 donor"),
        ((Allocation(("campaign", 1), ("campaign", 9), Decimal(1)),), "not a surviving B1"),
        ((Allocation(("campaign", 1), ("campaign", 4), Decimal(1)),), "G1-blocked"),
        ((Allocation(("campaign", 1), ("campaign", 1), Decimal(1)),), "cannot fund itself"),
        ((Allocation(("campaign", 1), ("campaign", 3), Decimal(0)),), "not positive"),
        (
            (
                Allocation(("campaign", 1), ("campaign", 3), Decimal(6)),
                Allocation(("campaign", 1), ("campaign", 5), Decimal(6)),
            ),
            "exceed its freed amount",
        ),
    ],
)
def test_allocation_post_check_rejects(allocations: tuple[Any, ...], message: str) -> None:
    donors = [(("campaign", 1), Decimal(10))]
    receivers = [
        (("campaign", 3), Decimal(100)),
        (("campaign", 4), Decimal(100)),
        (("campaign", 5), Decimal(100)),
        (("campaign", 1), Decimal(100)),
    ]
    with pytest.raises(PrecedenceInvariantError, match=message):
        precedence._check_allocations(allocations, donors, receivers, frozenset({("campaign", 4)}))


def test_allocation_currency_falls_back_to_the_decision_subject() -> None:
    ctx = build("b3.allocation.case")
    results = evaluate(ctx)
    stripped = [
        replace(
            result,
            candidates=tuple(
                replace(c, evidence=replace(c.evidence, extra={**c.evidence.extra, "currency": ""}))
                if c.rule_id in (B1, B3)
                else c
                for c in result.candidates
            ),
        )
        for result in results
    ]
    out = apply_precedence(stripped, ctx)
    assert [a.amount for a in out.allocations] == [Decimal("300.00"), Decimal("100.00")]


# ---------------------------------------------------------------------------
# P1 discrepancies first
# ---------------------------------------------------------------------------


def test_p1_holds_dependent_items_and_pins_c0() -> None:
    ctx, out = run("p1.holds.case")
    open_count = len(ctx.discrepancies)
    assert open_count == 1
    held = of_rule(out, KEYWORDS, K1)
    assert held
    for candidate in held:
        assert candidate.held
        assert candidate.held_reason == HELD_DISCREPANCY_REASON
        assert candidate.evidence.extra["blocking_discrepancies"] == (ctx.discrepancies[0].id,)
        assert flag_message(candidate, FLAG_UNVERIFIED_DISCREPANCIES) == unverified_message(
            open_count
        )
        # One open discrepancy reads in the singular, never "1 discrepancies are open".
        assert flag_message(candidate, FLAG_UNVERIFIED_DISCREPANCIES) == (
            "Unverified while 1 discrepancy is open"
        )
    first = out.items[CAMPAIGNS][0]
    assert first.rank == 1
    assert first.candidate.rule_id == C0
    for candidate in of_rule(out, CAMPAIGNS, C0):
        assert candidate.pinned
        assert not candidate.held
        assert not candidate.has_flag(FLAG_UNVERIFIED_DISCREPANCIES)
        assert candidate.evidence.extra["depends_on"]


def test_p1_c0_exempt() -> None:
    _, out = run("p1.c0_exempt.case")
    first = out.items[CAMPAIGNS][0].candidate
    assert (first.rule_id, first.pinned, first.held) == (C0, True, False)


def test_p1_partner_without_discrepancy_holds_nothing() -> None:
    ctx, out = run("p3.simulated.case")
    assert not ctx.discrepancies
    assert all(not item.candidate.held for item in out.all_items())
    assert all(
        not item.candidate.has_flag(FLAG_UNVERIFIED_DISCREPANCIES) for item in out.all_items()
    )


def test_p1_held_items_follow_unheld_ones_in_their_group() -> None:
    ctx, out = run("p1.holds.case")
    held = of_rule(out, KEYWORDS, K1)[0]
    loose = plain(
        held,
        held=False,
        held_reason=None,
        entity_ref=replace(held.entity_ref, external_id="zz|1|other"),
    )
    ranked = apply_precedence(RuleResult(candidates=(held, loose)), ctx)
    order = [item.candidate.held for item in ranked.items[KEYWORDS]]
    assert order == [False, True]


def test_p1_depends_on_reaches_the_campaign_behind_every_entity() -> None:
    ctx, candidate = sample_candidate(build("default"))
    keyword = next(iter(ctx.keywords.values()))
    ad_group = ctx.ad_groups[keyword.ad_group_id]
    creative_info = next(iter(ctx.creatives.values()))
    linkedin_campaign = ctx.campaigns[creative_info.campaign_id]
    cases = [
        (EntityRef("google", ENTITY_KEYWORD, keyword.id, "x|1", "k"), keyword.campaign_id),
        (
            EntityRef("google", ENTITY_KEYWORD, None, f"{ad_group.external_id}|1", "k"),
            ad_group.campaign_id,
        ),
        (EntityRef("google", ENTITY_AD_GROUP, ad_group.id, "x", "g"), ad_group.campaign_id),
        (
            EntityRef("google", ENTITY_AD_GROUP, None, ad_group.external_id, "g"),
            ad_group.campaign_id,
        ),
        (
            EntityRef("google", ENTITY_SEARCH_TERM, None, f"{ad_group.external_id}||t", "t"),
            ad_group.campaign_id,
        ),
        (
            EntityRef("linkedin", ENTITY_CREATIVE, creative_info.id, creative_info.urn, "c"),
            creative_info.campaign_id,
        ),
        (
            EntityRef("linkedin", ENTITY_CREATIVE, None, creative_info.urn, "c"),
            creative_info.campaign_id,
        ),
        (
            EntityRef(
                "linkedin", ENTITY_SEGMENT, None, f"{linkedin_campaign.external_id}|f|v", "s"
            ),
            linkedin_campaign.id,
        ),
        (
            EntityRef(
                "linkedin", ENTITY_CREATIVE_PAIR, None, f"{creative_info.urn}|urn:li:x:1", "p"
            ),
            creative_info.campaign_id,
        ),
        (
            EntityRef("linkedin", ENTITY_CAMPAIGN, None, linkedin_campaign.external_id, "c"),
            linkedin_campaign.id,
        ),
    ]
    for ref, campaign_id in cases:
        out = apply_precedence(RuleResult(candidates=(plain(candidate, entity_ref=ref),)), ctx)
        (item,) = out.all_items()
        assert {"type": "campaign", "id": campaign_id} in item.candidate.evidence.extra[
            "depends_on"
        ], ref
        budget_id = ctx.campaigns[campaign_id].budget_id
        if budget_id is not None:
            assert DecisionSubject("budget", budget_id) in item.candidate.depends_on


def test_p1_a_shared_budget_item_depends_on_every_member() -> None:
    ctx, candidate = sample_candidate(build("shared.poor_member.fires"))
    shared = next(b for b in ctx.budgets.values() if b.explicitly_shared)
    members = {c.id for c in ctx.campaigns.values() if c.budget_id == shared.id}
    ref = EntityRef("google", ENTITY_BUDGET, shared.id, shared.external_id, shared.name)
    out = apply_precedence(RuleResult(candidates=(plain(candidate, entity_ref=ref),)), ctx)
    (item,) = out.all_items()
    depends = {(subject.type, subject.id) for subject in item.candidate.depends_on}
    assert ("budget", shared.id) in depends
    assert {("campaign", cid) for cid in members} <= depends


def test_p1_member_items_do_not_depend_on_their_siblings() -> None:
    ctx, candidate = sample_candidate(build("shared.poor_member.fires"))
    shared = next(b for b in ctx.budgets.values() if b.explicitly_shared)
    member, sibling = sorted(c.id for c in ctx.campaigns.values() if c.budget_id == shared.id)
    one = plain(candidate, depends_on=(DecisionSubject("campaign", member),))
    out = apply_precedence(RuleResult(candidates=(one,)), ctx)
    depends = {(s.type, s.id) for s in out.all_items()[0].candidate.depends_on}
    assert ("budget", shared.id) in depends
    assert ("campaign", sibling) not in depends


def funding_card(
    ctx: AnalysisContext,
    rule_id: str,
    entity: EntityRef,
    subject: DecisionSubject,
    amount: Decimal,
    currency: str,
) -> Candidate:
    """A B1 (``extra.increase``) or B3 (``extra.freed``) card deciding on ``subject``."""
    datasets = ("google.entities", "google.metrics_daily")
    key = "increase" if rule_id == B1 else "freed"
    return Candidate(
        rule_id=rule_id,
        section=BUDGET,
        group=GROUP_CHANGES,
        entity_ref=entity,
        action=f"{rule_id} on {entity.name}",
        reason=f"{rule_id} proposed on {entity.name}.",
        evidence=ctx.make_evidence(
            required_datasets=datasets,
            crm_scope=None,
            metrics=(Metric(key, key.capitalize(), amount, "currency", currency=currency),),
            extra={key: amount, "currency": currency},
            headline=key,
        ),
        rank_key=(Decimal(0),),
        flags=(),
        required_datasets=datasets,
        increases_spend_on=(subject,) if rule_id == B1 else (),
        decision_subject=subject,
        depends_on=(subject,),
    )


@pytest.mark.parametrize("shared_role", ["receiver", "donor"])
def test_p1_a_shared_budget_allocation_partner_brings_its_member_campaigns(
    shared_role: str,
) -> None:
    ctx = build("shared.poor_member.does_not_fire")
    shared = next(b for b in ctx.budgets.values() if b.explicitly_shared)
    members = sorted(c.id for c in ctx.campaigns.values() if c.budget_id == shared.id)
    outsider = next(c for c in ctx.campaigns.values() if c.budget_id != shared.id)
    assert outsider.budget_id is not None
    assert len(members) == 2
    on_budget = (
        EntityRef("google", ENTITY_BUDGET, shared.id, shared.external_id, shared.name),
        DecisionSubject("budget", shared.id),
    )
    on_campaign = (
        EntityRef("google", ENTITY_CAMPAIGN, outsider.id, outsider.external_id, outsider.name),
        DecisionSubject("campaign", outsider.id),
    )
    if shared_role == "receiver":
        receiver, donor = on_budget, on_campaign
    else:
        receiver, donor = on_campaign, on_budget
    cards = (
        funding_card(ctx, B1, *receiver, Decimal("50.00"), shared.currency),
        funding_card(ctx, B3, *donor, Decimal("20.00"), shared.currency),
    )
    out = apply_precedence(RuleResult(candidates=cards), ctx)
    assert [(a.donor, a.receiver, a.amount) for a in out.allocations] == [
        (donor[1].key, receiver[1].key, Decimal("20.00"))
    ]
    by_entity = {c.entity_ref.type: c for c in of_rule(out, BUDGET)}
    outsider_card = by_entity[ENTITY_CAMPAIGN]
    depends = {s.key for s in outsider_card.depends_on}
    assert {("budget", shared.id), *(("campaign", member) for member in members)} <= depends
    assert {(s["type"], s["id"]) for s in outsider_card.evidence.extra["depends_on"]} == depends
    shared_card = by_entity[ENTITY_BUDGET]
    assert {("campaign", outsider.id), ("budget", outsider.budget_id)} <= {
        s.key for s in shared_card.depends_on
    }

    member = ctx.campaigns[members[-1]]
    info = DiscrepancyInfo(
        id=7,
        subject_type="campaign",
        subject_id=member.id,
        subject_name=member.name,
        source="google",
        shape="shared_member",
        kind="status",
        decided_value={"status": "active"},
        platform_value={"status": "paused"},
        detected_at=ctx.now - timedelta(hours=5),
        state="open",
    )
    discrepant = replace(ctx, discrepancies=(info,), cache={})
    held = apply_precedence(RuleResult(candidates=cards), discrepant)
    assert held.allocations == out.allocations
    for card in of_rule(held, BUDGET):
        assert card.held, card.rule_id
        assert card.held_reason == HELD_DISCREPANCY_REASON
        assert card.evidence.extra["blocking_discrepancies"] == (info.id,)


# ---------------------------------------------------------------------------
# P2 conversion tracking absent
# ---------------------------------------------------------------------------


def test_p2_records_the_finding_in_each_affected_section() -> None:
    _, out = run("p2.tracking_absent.fires")
    for section in (KEYWORDS, CAMPAIGNS, BUDGET):
        finding = out.findings[section][0]
        assert finding.code == FINDING_CONVERSION_TRACKING_ABSENT
        assert finding.message == TRACKING_ABSENT_MESSAGE
        assert finding.evidence["source"] == "google"
    assert not of_rule(out, KEYWORDS, K1)
    _, recorded = run("p2.tracking_absent.does_not_fire")
    assert of_rule(recorded, KEYWORDS, K1)
    assert all(
        finding.code != FINDING_CONVERSION_TRACKING_ABSENT
        for section in SECTION_ORDER
        for finding in recorded.findings[section]
    )


def test_p2_suppresses_zero_outcome_rules_of_that_source_only() -> None:
    _, recorded = run("p2.tracking_absent.does_not_fire")
    k1 = of_rule(recorded, KEYWORDS, K1)[0]
    ctx = build("p2.tracking_absent.fires")
    c1 = replace(k1, rule_id=C1, section=CAMPAIGNS, group="pause")
    b2_zero = replace(
        k1,
        rule_id=B2,
        section=BUDGET,
        group="changes",
        evidence=k1.evidence.with_extra(branch="zero_outcomes"),
    )
    b2_poor = replace(b2_zero, evidence=k1.evidence.with_extra(branch="poor_efficiency"))
    w2_linkedin = replace(
        k1,
        rule_id=W2,
        section=WHITE_PAPERS,
        group="cohort:USD",
        entity_ref=EntityRef("linkedin", "content_asset", None, "paper|USD", "Paper"),
    )
    out = apply_precedence(RuleResult(candidates=(k1, c1, b2_zero, b2_poor, w2_linkedin)), ctx)
    assert out.suppressed[KEYWORDS] == {SUPPRESS_TRACKING_ABSENT: 1}
    assert out.suppressed[CAMPAIGNS] == {SUPPRESS_TRACKING_ABSENT: 1}
    assert out.suppressed[BUDGET] == {SUPPRESS_TRACKING_ABSENT: 1}
    assert [c.rule_id for c in of_rule(out, BUDGET)] == [B2]
    assert [c.rule_id for c in of_rule(out, WHITE_PAPERS)] == [W2]
    assert out.findings[KEYWORDS][0].evidence["suppressed_items"] == 1


# ---------------------------------------------------------------------------
# P3 simulated data, P4 stale / incomplete
# ---------------------------------------------------------------------------


def test_p3_simulated_and_mixed() -> None:
    _, simulated = run("p3.simulated.case")
    k1 = of_rule(simulated, KEYWORDS, K1)
    assert k1
    assert all(c.data_status == DATA_SIMULATED for c in k1)
    assert flag_message(k1[0], FLAG_SIMULATED) == "Simulated data (Google)"
    _, mixed = run("p3.mixed.case")
    c1b = of_rule(mixed, CAMPAIGNS, C1B)
    assert c1b
    assert all(c.data_status == DATA_MIXED for c in c1b)
    assert (
        flag_message(c1b[0], FLAG_SIMULATED) == "Joins live and simulated data (HubSpot simulated)"
    )


def test_p3_all_live_is_live_and_stale_live_is_stale() -> None:
    ctx, candidate = sample_candidate()
    live_provenance = tuple(replace(e, mode="live") for e in candidate.evidence.provenance)
    live = replace(candidate, evidence=replace(candidate.evidence, provenance=live_provenance))
    out = apply_precedence(RuleResult(candidates=(live,)), ctx)
    assert out.all_items()[0].candidate.data_status == "live"
    old = {
        key: replace(row, last_success_at=ctx.now - timedelta(hours=30))
        for key, row in ctx.freshness.items()
    }
    stale_ctx = replace(ctx, freshness=old, cache={})
    stale = apply_precedence(RuleResult(candidates=(live,)), stale_ctx).all_items()[0].candidate
    assert stale.data_status == DATA_STALE
    assert stale.has_flag(FLAG_STALE)


@pytest.mark.parametrize(
    ("zone", "instant"),
    [
        ("UTC", "Sep 30, 2026, 4:58 PM UTC"),
        ("America/New_York", "Sep 30, 2026, 12:58 PM America/New_York"),
    ],
)
def test_p4_frozen_stale_flag_renders_its_instant_in_the_display_timezone(
    zone: str, instant: str
) -> None:
    """AAP 0.5.5: the frozen "Data as of …" instant is in ``DISPLAY_TIMEZONE``.

    Coverage ends stay ISO native dates (AAP 0.5.4.1): the zone formats instants only.
    """
    ctx, candidate = sample_candidate()
    last_success = datetime(2026, 9, 30, 16, 58, tzinfo=UTC)
    short_end = ctx.window.end - timedelta(days=1)
    old = {
        key: replace(row, last_success_at=last_success, covered_through=short_end)
        for key, row in ctx.freshness.items()
    }
    zoned = replace(ctx, freshness=old, display_timezone=zone, cache={})
    (item,) = apply_precedence(RuleResult(candidates=(candidate,)), zoned).all_items()
    shortfalls = "; ".join(
        f"{name} covers through {short_end.isoformat()}" for name in candidate.required_datasets
    )
    assert flag_message(item.candidate, FLAG_STALE) == f"Data as of {instant}; {shortfalls}"


@pytest.mark.parametrize("name", ["p4.stale.case", "p4.entity_sync_does_not_clear_metrics.case"])
def test_p4_stale_metrics_dataset(name: str) -> None:
    _, out = run(name)
    k1 = of_rule(out, KEYWORDS, K1)
    assert k1
    assert all(c.has_flag(FLAG_STALE) for c in k1)
    assert flag_message(k1[0], FLAG_STALE).startswith("Data as of ")
    for item in out.all_items():
        candidate = item.candidate
        if candidate.required_datasets == ("google.entities",):
            assert not candidate.has_flag(FLAG_STALE)


def test_p4_incomplete_dataset_flag_and_finding() -> None:
    _, out = run("p4.incomplete.case")
    k1 = of_rule(out, KEYWORDS, K1)
    assert k1
    assert all(c.has_flag(FLAG_INCOMPLETE) for c in k1)
    assert "result limit reached at finest partition" in flag_message(k1[0], FLAG_INCOMPLETE)
    finding = out.findings[KEYWORDS][-1]
    assert finding.code == FINDING_INCOMPLETE_DATASET
    assert finding.evidence["dataset"] == "google.search_terms"
    assert finding.evidence["items"] == len(k1)


def test_p4_stale_and_incomplete_reasons() -> None:
    ctx, candidate = sample_candidate()
    window = ctx.window
    google = ctx.datasets["google"].dataset_id
    success = ctx.now - timedelta(hours=1)
    variants: list[tuple[Freshness | None, tuple[str, ...], tuple[str, ...]]] = [
        (None, ("no successful sync of google.metrics_daily",), ("no freshness record",)),
        (
            Freshness(google, "google.metrics_daily", success, success, None, None, "complete"),
            ("covers no dates yet",),
            ("covers no dates",),
        ),
        (
            Freshness(
                google, "google.metrics_daily", success, success, None, window.end, "complete"
            ),
            (),
            ("records no coverage start",),
        ),
        (
            Freshness(
                google,
                "google.metrics_daily",
                success,
                success,
                window.start,
                None,
                "incomplete",
                "deferred",
            ),
            ("covers no dates yet",),
            ("deferred", "records no coverage end", "after the K1 baseline start"),
        ),
        (
            Freshness(
                google,
                "google.metrics_daily",
                success,
                success,
                date(2000, 1, 1),
                window.end - timedelta(days=1),
                "complete",
            ),
            (f"covers through {(window.end - timedelta(days=1)).isoformat()}",),
            ("before the window end",),
        ),
    ]
    for row, stale_texts, incomplete_texts in variants:
        freshness = dict(ctx.freshness)
        freshness.pop((google, "google.metrics_daily"))
        if row is not None:
            freshness[(google, "google.metrics_daily")] = row
        variant = replace(ctx, freshness=freshness, cache={})
        (item,) = apply_precedence(RuleResult(candidates=(candidate,)), variant).all_items()
        incomplete = flag_message(item.candidate, FLAG_INCOMPLETE)
        assert all(text in incomplete for text in incomplete_texts), incomplete
        if stale_texts:
            stale = flag_message(item.candidate, FLAG_STALE)
            assert all(text in stale for text in stale_texts), stale


def test_p4_full_state_dataset_is_judged_by_completeness_and_age() -> None:
    ctx, candidate = sample_candidate()
    google = ctx.datasets["google"].dataset_id
    entity = replace(candidate, required_datasets=("google.entities",))
    success = ctx.now - timedelta(hours=1)
    freshness = dict(ctx.freshness)
    freshness[(google, "google.entities")] = Freshness(
        google, "google.entities", success, success, None, None, "incomplete", "deferred page"
    )
    variant = replace(ctx, freshness=freshness, cache={})
    (item,) = apply_precedence(RuleResult(candidates=(entity,)), variant).all_items()
    assert flag_message(item.candidate, FLAG_INCOMPLETE) == (
        "Incomplete: google.entities (deferred page)"
    )
    assert not item.candidate.has_flag(FLAG_STALE)
    freshness[(google, "google.entities")] = Freshness(
        google, "google.entities", success, ctx.now - timedelta(hours=30), None, None, "complete"
    )
    variant = replace(ctx, freshness=freshness, cache={})
    (item,) = apply_precedence(RuleResult(candidates=(entity,)), variant).all_items()
    assert flag_message(item.candidate, FLAG_STALE).startswith("Data as of ")
    assert "covers" not in flag_message(item.candidate, FLAG_STALE)


def test_p4_k1_lookback_falls_back_to_the_baseline_days() -> None:
    ctx, candidate = sample_candidate()
    google = ctx.datasets["google"].dataset_id
    window = ctx.window
    row = ctx.freshness[(google, "google.metrics_daily")]
    freshness = {
        **ctx.freshness,
        (google, "google.metrics_daily"): replace(row, covered_from=window.start),
    }
    variant = replace(ctx, freshness=freshness, cache={})
    days = ctx.thresholds.int("k1.baseline_days")
    expected = (window.end - timedelta(days=days - 1)).isoformat()
    extras: list[dict[str, Any]] = [
        {"baseline_window": {"start": "not a date"}},
        {"baseline_window": {"start": ctx.now}},
        {"baseline_window": None},
    ]
    for extra in extras:
        evidence = replace(candidate.evidence, extra={**candidate.evidence.extra, **extra})
        for thresholds in (evidence.thresholds, {}):
            probe = replace(candidate, evidence=replace(evidence, thresholds=thresholds))
            (item,) = apply_precedence(RuleResult(candidates=(probe,)), variant).all_items()
            message = flag_message(item.candidate, FLAG_INCOMPLETE)
            assert f"after the K1 baseline start {expected}" in message
    iso = replace(
        candidate.evidence,
        extra={**candidate.evidence.extra, "baseline_window": {"start": window.start.isoformat()}},
    )
    (item,) = apply_precedence(
        RuleResult(candidates=(replace(candidate, evidence=iso),)), variant
    ).all_items()
    assert not item.candidate.has_flag(FLAG_INCOMPLETE)


def test_p4_hubspot_datasets_follow_the_recorded_provenance() -> None:
    ctx, out = run("p3.mixed.case")
    c1b = of_rule(out, CAMPAIGNS, C1B)[0]
    assert not c1b.has_flag(FLAG_INCOMPLETE)
    hubspot = ctx.datasets["hubspot"].dataset_id
    other = hubspot + 50
    moved = tuple(
        replace(entry, dataset_id=other) if entry.source == "hubspot" else entry
        for entry in c1b.evidence.provenance
    )
    probe = replace(c1b, evidence=replace(c1b.evidence, provenance=moved), flags=())
    (item,) = apply_precedence(RuleResult(candidates=(probe,)), ctx).all_items()
    assert "hubspot.contacts (no freshness record)" in flag_message(item.candidate, FLAG_INCOMPLETE)
    two = (
        *moved,
        ProvenanceEntry("hubspot", other + 1, "simulated", "primary"),
    )
    probe = replace(c1b, evidence=replace(c1b.evidence, provenance=two), flags=())
    (item,) = apply_precedence(RuleResult(candidates=(probe,)), ctx).all_items()
    assert not item.candidate.has_flag(FLAG_INCOMPLETE)
    google_scope = replace(
        c1b,
        required_datasets=("hubspot.contacts",),
        evidence=replace(
            c1b.evidence, provenance=(ProvenanceEntry("hubspot", hubspot, "simulated", "primary"),)
        ),
        flags=(),
    )
    (item,) = apply_precedence(RuleResult(candidates=(google_scope,)), ctx).all_items()
    assert item.candidate.data_status == DATA_SIMULATED


def test_p4_companion_crm_dataset_resolves_for_linkedin_items() -> None:
    ctx = build("default")
    companion = ctx.linkedin_crm
    assert companion is not None
    assert companion.dataset.role == "companion"
    _, candidate = sample_candidate(ctx)
    entry = ProvenanceEntry("linkedin", ctx.datasets["linkedin"].dataset_id, "simulated", "primary")
    probe = replace(
        candidate,
        required_datasets=("linkedin.leads", "hubspot.contacts"),
        evidence=replace(candidate.evidence, provenance=(entry,)),
    )
    (item,) = apply_precedence(RuleResult(candidates=(probe,)), ctx).all_items()
    assert not item.candidate.has_flag(FLAG_INCOMPLETE)
    missing = replace(
        ctx,
        linkedin_crm=None,
        cache={},
    )
    (item,) = apply_precedence(RuleResult(candidates=(probe,)), missing).all_items()
    assert "hubspot.contacts (no freshness record)" in flag_message(item.candidate, FLAG_INCOMPLETE)


# ---------------------------------------------------------------------------
# P5 short window, P6 and P7 checks
# ---------------------------------------------------------------------------


def test_p5_configured_minimum_flags_a_20_day_window() -> None:
    ctx, out = run("p5.configured_min.case")
    assert (ctx.window.days, ctx.window.trusted_min_days) == (20, 21)
    assert of_rule(out, KEYWORDS, K1)
    for item in out.all_items():
        assert flag_message(item.candidate, FLAG_SHORT_WINDOW) == (
            "Window of 20 days is below the 21-day trusted minimum"
        )
        assert item.candidate.evidence.thresholds[TRUSTED_MIN_DAYS_KEY] == 21
    _, default = run("p3.simulated.case")
    assert all(not item.candidate.has_flag(FLAG_SHORT_WINDOW) for item in default.all_items())
    assert all(
        item.candidate.evidence.thresholds[TRUSTED_MIN_DAYS_KEY] == 14
        for item in default.all_items()
    )


def test_p5_a_one_day_window_flag_reads_1_day() -> None:
    """P5: a one-day window's flag reads "Window of 1 day", never "1 days" (AAP 0.5.5).

    The "{trusted_min_days}-day" adjective stays singular, and a two-day window keeps the
    plural.
    """
    ctx, candidate = sample_candidate()
    end = ctx.window.end
    for days, message in (
        (1, "Window of 1 day is below the 14-day trusted minimum"),
        (2, "Window of 2 days is below the 14-day trusted minimum"),
    ):
        window = ctx.window.custom(end - timedelta(days=days - 1), end)
        short = replace(ctx, window=window, cache={})
        assert (short.window.days, short.window.trusted_min_days) == (days, 14)
        assert short.window.short_window
        (item,) = apply_precedence(RuleResult(candidates=(candidate,)), short).all_items()
        assert flag_message(item.candidate, FLAG_SHORT_WINDOW) == message


def test_p4_p5_freeze_the_applied_thresholds_into_the_evidence() -> None:
    """AAP 0.5.4.8 "the thresholds applied": P4's hours and P5's minimum join the evidence.

    They follow the rule's own thresholds, which keep their values and order, whatever the
    verdict; an item without required datasets is judged by P5 only; a key the rule already
    recorded with the same value stays where it is, and one with another value or type fails
    the run rather than being overwritten. The input candidate is never changed.
    """
    ctx, candidate = sample_candidate()
    own = candidate.evidence.thresholds
    assert own
    assert not {STALE_AFTER_KEY, TRUSTED_MIN_DAYS_KEY} & set(own)
    hours = ctx.thresholds.int(STALE_AFTER_KEY)
    minimum = ctx.window.trusted_min_days
    assert (hours, minimum) == (24, 14)

    (item,) = apply_precedence(RuleResult(candidates=(candidate,)), ctx).all_items()
    assert tuple(item.candidate.evidence.thresholds.items()) == (
        *own.items(),
        (STALE_AFTER_KEY, hours),
        (TRUSTED_MIN_DAYS_KEY, minimum),
    )
    rendered = item.candidate.evidence.to_json()["thresholds"]
    assert (rendered[STALE_AFTER_KEY], rendered[TRUSTED_MIN_DAYS_KEY]) == (24, 14)
    assert not {STALE_AFTER_KEY, TRUSTED_MIN_DAYS_KEY} & set(candidate.evidence.thresholds)

    old = {
        key: replace(row, last_success_at=ctx.now - timedelta(hours=30))
        for key, row in ctx.freshness.items()
    }
    raised = Thresholds.load(overrides={STALE_AFTER_KEY: 36})
    for config, stale in ((ctx.thresholds, True), (raised, False)):
        variant = replace(ctx, freshness=old, thresholds=config, cache={})
        (item,) = apply_precedence(RuleResult(candidates=(candidate,)), variant).all_items()
        assert item.candidate.has_flag(FLAG_STALE) is stale
        assert item.candidate.evidence.thresholds[STALE_AFTER_KEY] == config.int(STALE_AFTER_KEY)
    assert raised.int(STALE_AFTER_KEY) == 36

    bare = plain(candidate, required_datasets=())
    (item,) = apply_precedence(RuleResult(candidates=(bare,)), ctx).all_items()
    assert tuple(item.candidate.evidence.thresholds.items()) == (
        *own.items(),
        (TRUSTED_MIN_DAYS_KEY, minimum),
    )

    first = {STALE_AFTER_KEY: hours, **own}
    same = replace(candidate, evidence=replace(candidate.evidence, thresholds=first))
    (item,) = apply_precedence(RuleResult(candidates=(same,)), ctx).all_items()
    assert tuple(item.candidate.evidence.thresholds.items()) == (
        *first.items(),
        (TRUSTED_MIN_DAYS_KEY, minimum),
    )

    clashes: tuple[tuple[str, object], ...] = (
        (STALE_AFTER_KEY, hours + 1),
        (STALE_AFTER_KEY, str(hours)),
        (TRUSTED_MIN_DAYS_KEY, minimum - 1),
        (TRUSTED_MIN_DAYS_KEY, float(minimum)),
    )
    for key, value in clashes:
        conflicting = replace(candidate.evidence, thresholds={**own, key: value})
        clash = replace(candidate, evidence=conflicting)
        with pytest.raises(PrecedenceInvariantError, match=f"records {key} = ") as raised_error:
            apply_precedence(RuleResult(candidates=(clash,)), ctx)
        assert f"= {value!r} in evidence.thresholds" in str(raised_error.value)


def test_p6_withheld_lines_carry_sizes_and_never_become_items() -> None:
    _, out = run("p6.withheld_sizes.case")
    assert not of_rule(out, KEYWORDS, K1)
    lines = [w for w in out.withheld[KEYWORDS] if w.rule_id == K1 and w.entity is not None]
    assert any((w.observed, w.required, w.unit) == (60, 61, "clicks") for w in lines)


def test_p6_an_item_below_its_minimum_trips() -> None:
    ctx, candidate = sample_candidate()
    below = replace(candidate, evidence=replace(candidate.evidence, sample=Sample(1, 5, "clicks")))
    with pytest.raises(PrecedenceInvariantError, match="P6"):
        apply_precedence(RuleResult(candidates=(below,)), ctx)


def test_p7_not_distinguishable_pairs_carry_the_flag_and_no_winner_wording() -> None:
    ctx, out = run("p7.no_winner_wording.case")
    pairs = of_rule(out, CREATIVE, CR1)
    assert pairs
    assert all(pair.has_flag(FLAG_NOT_DISTINGUISHABLE) for pair in pairs)
    pair = pairs[0]
    with pytest.raises(PrecedenceInvariantError, match="calls a winner"):
        apply_precedence(
            RuleResult(candidates=(replace(pair, reason=f"{pair.reason} A is the winner."),)), ctx
        )
    with pytest.raises(PrecedenceInvariantError, match="lacks"):
        apply_precedence(RuleResult(candidates=(replace(pair, flags=()),)), ctx)
    named = replace(
        pair,
        entity_ref=replace(pair.entity_ref, name="Best practices vs Other"),
        action="Best practices vs Other: level, not yet distinguishable",
    )
    apply_precedence(RuleResult(candidates=(named,)), ctx)
    distinguishable = replace(
        pair,
        flags=(),
        evidence=pair.evidence.with_extra(distinguishable=True),
        reason=f"{pair.reason} B is better.",
    )
    apply_precedence(RuleResult(candidates=(distinguishable,)), ctx)


def test_p8_floor_flags_pass_through() -> None:
    _, out = run("default")
    papers = of_rule(out, WHITE_PAPERS)
    assert papers
    ctx = build("default")
    before = {c.entity_ref.key: c.flag_codes for c in white_papers.evaluate(ctx).candidates}
    for candidate in papers:
        assert set(before[candidate.entity_ref.key]) <= set(candidate.flag_codes)


# ---------------------------------------------------------------------------
# Feedback suppression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["dismiss", "snooze", "done"])
def test_feedback_suppresses_with_counts(kind: str) -> None:
    ctx, out = run(f"feedback.{kind}.case")
    assert not of_rule(out, KEYWORDS, K1)
    assert out.suppressed[KEYWORDS] == {f"feedback_{kind}": 1}
    expired = replace(ctx, now=ctx.feedback[0].until, cache={})
    again = apply_precedence(evaluate(expired), expired)
    assert not again.suppressed[KEYWORDS]


def test_feedback_never_suppresses_c0_and_the_latest_entry_wins() -> None:
    ctx = build("p1.c0_exempt.case")
    # The rule's own C0 item: precedence adds its headline, so it never takes one twice.
    c0 = discrepancy.evaluate(ctx).candidates[0]
    _, k1 = sample_candidate()
    snooze = SCENARIOS["feedback.snooze.case"].build_context().feedback[0]
    dismiss = replace(snooze, kind="dismiss")
    on_c0 = replace(
        snooze, rule_id=C0, entity_type=c0.entity_ref.type, entity_key=c0.entity_ref.key
    )
    variant = replace(ctx, feedback=(snooze, dismiss, on_c0), cache={})
    target = replace(
        k1, entity_ref=replace(k1.entity_ref, external_id=snooze.entity_key.split(":", 2)[2])
    )
    result = apply_precedence(RuleResult(candidates=(c0, target)), variant)
    assert [c.rule_id for c in of_rule(result, CAMPAIGNS)] == [C0]
    assert result.suppressed[KEYWORDS] == {SUPPRESS_FEEDBACK_DISMISS: 1}


# ---------------------------------------------------------------------------
# Headline numbers: C0 leads with the recommendations its discrepancy holds
# ---------------------------------------------------------------------------


HELD_METRIC = ("held_recommendations", "Recommendations held until resolved", "recommendations")
"""A C0 item's headline metric: key, label and unit."""


def held_count(candidate: Candidate) -> int:
    """A stored C0 item's headline value, checked to be its last ``held_recommendations`` metric."""
    assert candidate.rule_id == C0
    assert candidate.evidence.extra["headline_metric"] == HELD_METRIC[0]
    metric = candidate.evidence.metrics[-1]
    assert (metric.key, metric.label, metric.unit) == HELD_METRIC
    assert isinstance(metric.value, int)
    assert not isinstance(metric.value, bool)
    return metric.value


def held_by(ctx: AnalysisContext) -> tuple[list[Candidate], Candidate, Candidate, Candidate]:
    """``p1.holds.case``'s own C0 items and K1, a K1 twin on its ad group and an unrelated K1.

    The scenario's discrepancy holds its K1 item; the twin, on the same ad group, is held
    alike; the third item names an ad group of no campaign and depends on nothing.
    """
    results = evaluate(ctx)
    c0 = [c for result in results for c in result.candidates if c.rule_id == C0]
    (k1,) = [c for result in results for c in result.candidates if c.rule_id == K1]
    ad_group, criterion, _ = k1.entity_ref.external_id.split("|", 2)
    twin = replace(
        k1,
        entity_ref=replace(
            k1.entity_ref,
            external_id=f"{ad_group}|{criterion}|crm alternatives",
            name="crm alternatives",
        ),
    )
    loose = plain(k1, entity_ref=replace(k1.entity_ref, external_id="zz|1|other", name="other"))
    return c0, k1, twin, loose


def test_c0_leads_with_the_number_of_items_its_discrepancy_holds() -> None:
    """Two held items give 2; an unrelated item is not counted; nothing else on C0 changes."""
    ctx = build("p1.holds.case")
    (info,) = ctx.discrepancies
    c0, k1, twin, loose = held_by(ctx)
    (built,) = c0
    out = apply_precedence(RuleResult(candidates=(built, k1, twin, loose)), ctx)
    (stored,) = of_rule(out, CAMPAIGNS, C0)
    assert held_count(stored) == 2
    held = [c for c in of_rule(out, KEYWORDS) if c.held]
    assert sorted(c.entity_ref.name for c in held) == sorted(
        [k1.entity_ref.name, "crm alternatives"]
    )
    assert all(c.evidence.extra["blocking_discrepancies"] == (info.id,) for c in held)
    assert [c.held for c in of_rule(out, KEYWORDS) if c.entity_ref.name == "other"] == [False]
    # The rule's metrics come first, unchanged and in their order; the action and reason
    # still state the platform and decided values.
    assert stored.evidence.metrics[:-1] == built.evidence.metrics
    assert [metric.key for metric in stored.evidence.metrics] == [
        "platform",
        "decided",
        "held_recommendations",
    ]
    assert (stored.action, stored.reason) == (built.action, built.reason)
    assert "headline_metric" not in built.evidence.extra
    # The count depends only on which items survive, never their order.
    shuffled = apply_precedence(RuleResult(candidates=(loose, twin, built, k1)), ctx)
    assert held_count(of_rule(shuffled, CAMPAIGNS, C0)[0]) == 2


def test_c0_does_not_count_a_held_item_feedback_drops() -> None:
    """A held item that feedback hides is not stored, so its discrepancy no longer counts it."""
    ctx = build("p1.holds.case")
    c0, k1, twin, loose = held_by(ctx)
    dismiss = FeedbackInfo(
        rule_id=K1,
        entity_type=twin.entity_ref.type,
        entity_key=twin.entity_ref.key,
        kind="dismiss",
        until=ctx.now + timedelta(days=30),
    )
    dismissed = replace(ctx, feedback=(dismiss,), cache={})
    out = apply_precedence(RuleResult(candidates=(*c0, k1, twin, loose)), dismissed)
    assert out.suppressed[KEYWORDS] == {SUPPRESS_FEEDBACK_DISMISS: 1}
    assert "crm alternatives" not in {c.entity_ref.name for c in of_rule(out, KEYWORDS)}
    (stored,) = of_rule(out, CAMPAIGNS, C0)
    assert held_count(stored) == 1


def test_c0_leads_with_zero_when_nothing_depends_on_its_subject() -> None:
    """0 is a valid count: a discrepancy no stored item depends on still leads with a number."""
    ctx = build("p1.holds.case")
    c0, _, _, loose = held_by(ctx)
    out = apply_precedence(RuleResult(candidates=(*c0, loose)), ctx)
    (stored,) = of_rule(out, CAMPAIGNS, C0)
    assert held_count(stored) == 0
    assert stored.evidence.metrics[-1].to_json()["value"] == 0
    alone = apply_precedence(RuleResult(candidates=tuple(c0)), ctx)
    assert held_count(of_rule(alone, CAMPAIGNS, C0)[0]) == 0


def test_an_item_held_by_two_discrepancies_counts_for_both() -> None:
    """Each discrepancy counts every stored item it holds, shared ones included."""
    ctx = build("p1.holds.case")
    (status,) = ctx.discrepancies
    assert status.subject_type == "campaign"
    budget_change = replace(
        status,
        id=status.id + 1,
        kind="budget",
        decided_value={"daily_budget": "100.00"},
        platform_value={"daily_budget": "120.00"},
        detected_at=status.detected_at + timedelta(hours=1),
    )
    both = replace(ctx, discrepancies=(status, budget_change), cache={})
    c0, k1, twin, loose = held_by(both)
    assert len(c0) == 2
    out = apply_precedence(RuleResult(candidates=(*c0, k1, twin, loose)), both)
    held = [c for c in of_rule(out, KEYWORDS) if c.held]
    assert len(held) == 2
    assert all(
        c.evidence.extra["blocking_discrepancies"] == (status.id, budget_change.id) for c in held
    )
    stored = of_rule(out, CAMPAIGNS, C0)
    assert [c.evidence.extra["discrepancy_id"] for c in stored] == [status.id, budget_change.id]
    assert [held_count(c) for c in stored] == [2, 2]


def test_every_card_leaves_precedence_with_a_headline_of_its_own_metrics() -> None:
    """The headline post-check: a card without its headline metric fails the run."""
    ctx, out = run("default")
    for item in out.all_items():
        evidence = item.candidate.evidence
        if item.candidate.section == WHITE_PAPERS:
            assert "headline_metric" not in evidence.extra
        else:
            assert evidence.metric(evidence.extra["headline_metric"])
    _, k1 = sample_candidate(ctx)
    unnamed = replace(
        k1.evidence,
        extra={key: value for key, value in k1.evidence.extra.items() if key != "headline_metric"},
    )
    with pytest.raises(PrecedenceInvariantError, match=r"headline: K1 .* leads with None"):
        apply_precedence(RuleResult(candidates=(replace(k1, evidence=unnamed),)), ctx)
    missing = replace(
        k1.evidence,
        metrics=tuple(m for m in k1.evidence.metrics if m.key != "wasted_spend"),
    )
    with pytest.raises(PrecedenceInvariantError, match="leads with 'wasted_spend', which is not"):
        apply_precedence(RuleResult(candidates=(replace(k1, evidence=missing),)), ctx)
    # A white-paper row is a table row, not a card, and names no headline.
    (paper, *_) = of_rule(out, WHITE_PAPERS)
    assert "headline_metric" not in paper.evidence.extra


def test_a_c0_item_that_already_leads_with_a_number_is_refused() -> None:
    """Only precedence sets C0's headline, once: a C0 arriving with one is a broken contract."""
    ctx, out = run("p1.c0_exempt.case")
    (stored,) = of_rule(out, CAMPAIGNS, C0)
    with pytest.raises(
        ValueError, match="already names the headline metric 'held_recommendations'"
    ):
        apply_precedence(RuleResult(candidates=(stored,)), ctx)
    no_id = replace(
        stored,
        evidence=replace(
            discrepancy.evaluate(ctx).candidates[0].evidence,
            extra={"kind": "status"},
        ),
    )
    with pytest.raises(ValueError, match=r"no integer extra\.discrepancy_id"):
        apply_precedence(RuleResult(candidates=(no_id,)), ctx)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_ranking_ties_by_entity_name_then_key() -> None:
    ctx, out = run("ranking.deterministic.case")
    names = [c.entity_ref.name for c in of_rule(out, KEYWORDS, K1)]
    assert names[:2] == ["free crm templates", "free crm tools"]
    flat = RuleResult().merge(*evaluate(ctx))
    reversed_input = replace(flat, candidates=tuple(reversed(flat.candidates)))
    assert ranking(apply_precedence(reversed_input, ctx)) == ranking(out)


def test_rank_keys_of_mixed_kinds_still_order() -> None:
    ctx, candidate = sample_candidate()
    keys: list[tuple[Any, ...]] = [
        (None,),
        (Decimal("NaN"),),
        (1.5,),
        (float("inf"),),
        (True,),
        ("text",),
        (ctx.now,),
        (ctx.now.replace(tzinfo=None),),
        (date(2026, 1, 1),),
        ((1, 2),),
        (object,),
        (Decimal(1),),
    ]
    items = [
        replace(
            candidate,
            rank_key=key,
            entity_ref=replace(candidate.entity_ref, external_id=f"x|{n}|t"),
        )
        for n, key in enumerate(keys)
    ]
    out = apply_precedence(RuleResult(candidates=tuple(items)), ctx)
    assert len(out.items[KEYWORDS]) == len(keys)
    assert out.items[KEYWORDS][-1].candidate.rank_key == (None,)


def test_dynamic_groups_order_by_spend_currency_and_key() -> None:
    ctx, out = run("default")
    creative_items = out.items[CREATIVE]
    groups = list(dict.fromkeys(item.candidate.group for item in creative_items))
    campaign_ids = [int(group.split(":")[1]) for group in groups]
    currencies = [ctx.account_currency(campaign_id) for campaign_id in campaign_ids]
    # One contiguous block per currency, in currency-code order, spend descending in each.
    assert len(set(currencies)) >= 2
    assert currencies == sorted(currencies)
    for currency in set(currencies):
        spends = [
            metrics.window_totals(ctx, (campaign_id,)).spend
            for campaign_id, code in zip(campaign_ids, currencies, strict=True)
            if code == currency
        ]
        assert spends == sorted(spends, reverse=True)
    _, candidate = sample_candidate(ctx)
    unknown = replace(
        candidate,
        section=CREATIVE,
        group=creative_group(987654),
        evidence=candidate.evidence.with_extra(campaign_spend=Decimal("1")),
    )
    unpriced = replace(
        candidate,
        section=CREATIVE,
        group=creative_group(987655),
        entity_ref=replace(candidate.entity_ref, external_id="y|1|t"),
    )
    odd = replace(
        candidate,
        section=CREATIVE,
        group="zz-other",
        entity_ref=replace(candidate.entity_ref, external_id="z|1|t"),
    )
    paper_eur = replace(candidate, section=WHITE_PAPERS, group="cohort:EUR")
    paper_usd = replace(
        candidate,
        section=WHITE_PAPERS,
        group="cohort:USD",
        entity_ref=replace(candidate.entity_ref, external_id="u|1|t"),
    )
    ranked = apply_precedence(
        RuleResult(candidates=(odd, unpriced, unknown, paper_usd, paper_eur)), ctx
    )
    assert [i.candidate.group for i in ranked.items[CREATIVE]] == [
        creative_group(987654),
        creative_group(987655),
        "zz-other",
    ]
    assert [i.candidate.group for i in ranked.items[WHITE_PAPERS]] == ["cohort:EUR", "cohort:USD"]


def test_creative_groups_never_rank_spend_across_currencies() -> None:
    ctx, candidate = sample_candidate()

    def item(campaign_id: int, spend: str, currency: str | None) -> Candidate:
        return replace(
            candidate,
            section=CREATIVE,
            group=creative_group(campaign_id),
            entity_ref=replace(candidate.entity_ref, external_id=f"c|{campaign_id}|{currency}"),
            evidence=candidate.evidence.with_extra(
                campaign_spend=Decimal(spend), currency=currency
            ),
        )

    # Campaigns outside the context: the currency and spend their items froze decide the order.
    items = (
        item(987001, "5000", "usd "),
        item(987002, "100", "EUR"),
        item(987003, "9000", "USD"),
        item(987004, "1000000", "  "),
        item(987005, "500000", "JPY"),
        item(987006, "50", "USD"),
        item(987006, "50", "EUR"),
        item(987007, "2000000", None),
    )
    ranked = apply_precedence(RuleResult(candidates=tuple(reversed(items))), ctx)
    groups = list(dict.fromkeys(i.candidate.group for i in ranked.items[CREATIVE]))
    by_raw_spend = [
        creative_group(campaign_id)
        for campaign_id in (987007, 987004, 987005, 987003, 987001, 987002, 987006)
    ]
    assert groups != by_raw_spend
    assert groups == [
        creative_group(987002),
        creative_group(987006),
        creative_group(987005),
        creative_group(987003),
        creative_group(987001),
        creative_group(987007),
        creative_group(987004),
    ]


def test_merge_flag_keeps_one_badge_per_code() -> None:
    _, candidate = sample_candidate()
    existing = candidate.with_flags(Flag(FLAG_STALE, "Data as of rule text"))
    flags = precedence._merged_flags(existing.flags, Flag(FLAG_STALE, "precedence text"))
    merged = replace(existing, flags=flags)
    assert flag_message(merged, FLAG_STALE) == "Data as of rule text; precedence text"
    assert precedence._merged_flags(flags, Flag(FLAG_STALE, "precedence text")) is flags
    added = precedence._merged_flags(flags, Flag(FLAG_SIMULATED, "Simulated data"))
    assert added == (*flags, Flag(FLAG_SIMULATED, "Simulated data"))


# ---------------------------------------------------------------------------
# The no_crm_link finding counts the stored items (G1, feedback, holds, C0)
# ---------------------------------------------------------------------------


def built_no_crm_link(results: Sequence[RuleResult], section: str) -> Finding:
    """The ``no_crm_link`` finding a section's rules built, before precedence."""
    (finding,) = [
        finding
        for result in results
        for finding in result.findings
        if finding.code == FINDING_NO_CRM_LINK and finding.section == section
    ]
    return finding


def no_crm_link_counts(out: PrecedenceOutput, section: str) -> tuple[int, int]:
    """A section's stored ``no_crm_link`` ``items`` and how many stored items carry the flag."""
    (finding,) = [found for found in out.findings[section] if found.code == FINDING_NO_CRM_LINK]
    flagged = sum(1 for candidate in of_rule(out, section) if candidate.has_flag(FLAG_NO_CRM_LINK))
    return finding.evidence["items"], flagged


def test_no_crm_link_items_count_only_the_items_g1_leaves() -> None:
    ctx = build("shared.poor_member.fires")
    results = evaluate(ctx)
    out = apply_precedence(results, ctx)
    for section in (CAMPAIGNS, BUDGET):
        built = built_no_crm_link(results, section)
        # The rule counted the C2 Scale / B1 it built; G1 removed it, so no item is stored.
        assert built.evidence["items"] == 1
        assert out.suppressed[section][SUPPRESS_G1_BLOCKED] == 1
        assert no_crm_link_counts(out, section) == (0, 0)
        # The finding stays, with only its count changed and its evidence keys in order.
        (stored,) = [f for f in out.findings[section] if f.code == FINDING_NO_CRM_LINK]
        assert (stored.code, stored.section, stored.entity, stored.message) == (
            built.code,
            built.section,
            built.entity,
            built.message,
        )
        assert dict(stored.evidence) == {**built.evidence, "items": 0}
        assert list(stored.evidence) == list(built.evidence)


def test_no_crm_link_recount_leaves_other_findings_and_their_order_unchanged() -> None:
    ctx = build("default")
    results = evaluate(ctx)
    other = Finding(
        FINDING_SEGMENT_WINDOW_UNAVAILABLE,
        CAMPAIGNS,
        None,
        "No segment data matches the window",
        {"items": 99},
    )
    out = apply_precedence([RuleResult(findings=(other,)), *results], ctx)
    assert [f.code for f in out.findings[CAMPAIGNS]] == [
        FINDING_NO_CRM_LINK,
        FINDING_SEGMENT_WINDOW_UNAVAILABLE,
    ]
    assert out.findings[CAMPAIGNS][1] == other
    items, flagged = no_crm_link_counts(out, CAMPAIGNS)
    assert items == flagged == built_no_crm_link(results, CAMPAIGNS).evidence["items"]


def test_no_crm_link_items_drop_by_one_for_a_dismissed_google_item() -> None:
    ctx = build("default")
    before = apply_precedence(evaluate(ctx), ctx)
    items, flagged = no_crm_link_counts(before, CAMPAIGNS)
    assert items == flagged
    c1 = next(c for c in of_rule(before, CAMPAIGNS, C1) if c.has_flag(FLAG_NO_CRM_LINK))
    dismiss = FeedbackInfo(
        rule_id=C1,
        entity_type=c1.entity_ref.type,
        entity_key=c1.entity_ref.key,
        kind="dismiss",
        until=ctx.now + timedelta(days=30),
    )
    dismissed = replace(ctx, feedback=(dismiss,), cache={})
    results = evaluate(dismissed)
    # The rule still counts the C1 it built; precedence counts what the run stores.
    assert built_no_crm_link(results, CAMPAIGNS).evidence["items"] == items
    out = apply_precedence(results, dismissed)
    assert out.suppressed[CAMPAIGNS] == {SUPPRESS_FEEDBACK_DISMISS: 1}
    assert c1.entity_ref.key not in {c.entity_ref.key for c in of_rule(out, CAMPAIGNS, C1)}
    assert no_crm_link_counts(out, CAMPAIGNS) == (items - 1, flagged - 1)
    assert no_crm_link_counts(out, BUDGET) == no_crm_link_counts(before, BUDGET)


def test_no_crm_link_items_include_held_and_c0_items() -> None:
    ctx = build("default")
    scale = next(
        c
        for c in campaigns.evaluate(ctx).candidates
        if c.rule_id == C2 and c.has_flag(FLAG_NO_CRM_LINK)
    )
    assert scale.entity_ref.type == ENTITY_CAMPAIGN
    assert scale.entity_ref.id is not None
    campaign = ctx.campaigns[scale.entity_ref.id]
    info = DiscrepancyInfo(
        id=7,
        subject_type="campaign",
        subject_id=campaign.id,
        subject_name=campaign.name,
        source="google",
        shape="own_budget",
        kind="status",
        decided_value={"status": "active"},
        platform_value={"status": "paused"},
        detected_at=ctx.now - timedelta(hours=5),
        state="open",
    )
    discrepant = replace(ctx, discrepancies=(info,), cache={})
    results = evaluate(discrepant)
    out = apply_precedence(results, discrepant)
    flagged = [c for c in of_rule(out, CAMPAIGNS) if c.has_flag(FLAG_NO_CRM_LINK)]
    assert [c.rule_id for c in flagged if c.is_discrepancy] == [C0]
    assert [c.rule_id for c in flagged if c.held] == [C2]
    # The rule counts no C0 item; the stored count holds the C0 and the held Scale.
    assert built_no_crm_link(results, CAMPAIGNS).evidence["items"] == len(flagged) - 1
    assert no_crm_link_counts(out, CAMPAIGNS) == (len(flagged), len(flagged))
    held_budget = [c for c in of_rule(out, BUDGET) if c.held and c.has_flag(FLAG_NO_CRM_LINK)]
    assert held_budget
    items, stored = no_crm_link_counts(out, BUDGET)
    assert items == stored
    reordered = apply_precedence(list(reversed(results)), discrepant)
    assert dict(reordered.findings) == dict(out.findings)


# ---------------------------------------------------------------------------
# The incomplete_dataset finding counts the stored items (feedback)
# ---------------------------------------------------------------------------

SEARCH_TERMS = "google.search_terms"
METRICS = "google.metrics_daily"
RESULT_LIMIT = "result limit reached at finest partition"


def with_google_freshness(ctx: AnalysisContext, dataset: str, **changes: Any) -> AnalysisContext:
    """``ctx`` with the freshness row of the Google ``dataset`` changed by ``changes``."""
    key = (ctx.datasets["google"].dataset_id, dataset)
    row = replace(ctx.freshness[key], **changes)
    return replace(ctx, freshness={**ctx.freshness, key: row}, cache={})


def incomplete_world(name: str) -> AnalysisContext:
    """A world with an incomplete Google dataset read by several items of several sections.

    ``search_terms``: the default world with ``google.search_terms`` cut short at its finest
    partition. ``late_metrics``: the default world with ``google.metrics_daily`` covering from
    three days into the window, so K1 items, judged from their earlier baseline start, and
    every other item, judged from the window start, give the dataset different reasons. Any
    other name is a catalogue scenario.
    """
    if name == "search_terms":
        return with_google_freshness(
            build("default"), SEARCH_TERMS, completeness="incomplete", reason=RESULT_LIMIT
        )
    if name == "late_metrics":
        ctx = build("default")
        return with_google_freshness(
            ctx, METRICS, covered_from=ctx.window.start + timedelta(days=3)
        )
    return build(name)


def incomplete_dataset_findings(out: PrecedenceOutput) -> dict[str, tuple[Finding, ...]]:
    """Every section's ``incomplete_dataset`` findings, in their stored order."""
    return {
        section: tuple(f for f in out.findings[section] if f.code == FINDING_INCOMPLETE_DATASET)
        for section in SECTION_ORDER
    }


def incomplete_dataset_finding(out: PrecedenceOutput, section: str, dataset: str) -> Finding:
    """A section's one stored ``incomplete_dataset`` finding of ``dataset``."""
    (finding,) = [
        found
        for found in incomplete_dataset_findings(out)[section]
        if found.evidence["dataset"] == dataset
    ]
    return finding


def incomplete_readers(out: PrecedenceOutput, section: str, dataset: str) -> list[Candidate]:
    """A section's stored items whose ``incomplete`` flag names ``dataset``."""
    return [
        candidate
        for candidate in of_rule(out, section)
        if candidate.has_flag(FLAG_INCOMPLETE)
        and f"{dataset} (" in flag_message(candidate, FLAG_INCOMPLETE)
    ]


def dismissing(ctx: AnalysisContext, items: Sequence[Candidate]) -> AnalysisContext:
    """``ctx`` with an active dismiss on each of ``items``."""
    feedback = tuple(
        FeedbackInfo(
            rule_id=item.rule_id,
            entity_type=item.entity_ref.type,
            entity_key=item.entity_ref.key,
            kind="dismiss",
            until=ctx.now + timedelta(days=30),
        )
        for item in items
    )
    return replace(ctx, feedback=feedback, cache={})


def test_p4_a_finest_partition_day_outside_the_window_is_not_incomplete_over_it() -> None:
    """A dated Q-G6 shortfall flags only the windows that hold one of its days.

    Day at the result limit before the window: no item reads ``google.search_terms``
    incomplete and no section gets its ``incomplete_dataset`` finding. Add a day inside the
    window: every reader is flagged, and the flag and the finding name that day alone.
    """
    ctx = build("default")
    before = (ctx.window.start - timedelta(days=1)).isoformat()
    within = (ctx.window.start + timedelta(days=3)).isoformat()
    excluded = with_google_freshness(
        ctx,
        SEARCH_TERMS,
        completeness="incomplete",
        reason=f"{RESULT_LIMIT} ({before}, ad group 990200000000)",
    )
    clear = apply_precedence(evaluate(excluded), excluded)
    for section in SECTION_ORDER:
        assert not incomplete_readers(clear, section, SEARCH_TERMS)
        assert all(
            finding.evidence["dataset"] != SEARCH_TERMS
            for finding in incomplete_dataset_findings(clear)[section]
        )

    included = with_google_freshness(
        ctx,
        SEARCH_TERMS,
        completeness="incomplete",
        reason=f"{RESULT_LIMIT} ({before}, ad group 990200000000; {within}, ad group 5)",
    )
    flagged = apply_precedence(evaluate(included), included)
    named = f"{RESULT_LIMIT} ({within}, ad group 5)"
    readers = incomplete_readers(flagged, KEYWORDS, SEARCH_TERMS)
    assert readers
    assert readers == [
        candidate
        for candidate in of_rule(flagged, KEYWORDS)
        if SEARCH_TERMS in candidate.required_datasets
    ]
    for candidate in readers:
        message = flag_message(candidate, FLAG_INCOMPLETE)
        assert f"{SEARCH_TERMS} ({named})" in message
        assert before not in message
    finding = incomplete_dataset_finding(flagged, KEYWORDS, SEARCH_TERMS)
    assert finding.message == f"{SEARCH_TERMS} is not complete over the window: {named}"
    assert finding.evidence["reasons"] == (named,)
    assert finding.evidence["items"] == len(readers)


def test_incomplete_dataset_items_drop_by_one_for_a_dismissed_reader() -> None:
    ctx = incomplete_world("search_terms")
    before = apply_precedence(evaluate(ctx), ctx)
    built = incomplete_dataset_finding(before, KEYWORDS, SEARCH_TERMS)
    readers = incomplete_readers(before, KEYWORDS, SEARCH_TERMS)
    assert len(readers) >= 2
    assert built.evidence["items"] == len(readers)
    dismissed = dismissing(ctx, readers[:1])
    out = apply_precedence(evaluate(dismissed), dismissed)
    assert out.suppressed[KEYWORDS][SUPPRESS_FEEDBACK_DISMISS] == 1
    stored = incomplete_dataset_finding(out, KEYWORDS, SEARCH_TERMS)
    assert stored.evidence["items"] == len(incomplete_readers(out, KEYWORDS, SEARCH_TERMS))
    assert stored.evidence["items"] == len(readers) - 1
    # Only the count changes; the evidence keys keep their order.
    assert (stored.code, stored.section, stored.entity, stored.message) == (
        built.code,
        built.section,
        built.entity,
        built.message,
    )
    assert dict(stored.evidence) == {**built.evidence, "items": len(readers) - 1}
    assert list(stored.evidence) == list(built.evidence)
    # Every other finding of every section, and the findings' order, is unchanged.
    for section in SECTION_ORDER:
        assert out.findings[section] == tuple(
            stored if finding == built else finding for finding in before.findings[section]
        )


def test_incomplete_dataset_finding_stays_with_zero_items_when_every_reader_is_dismissed() -> None:
    ctx = incomplete_world("search_terms")
    before = apply_precedence(evaluate(ctx), ctx)
    built = incomplete_dataset_finding(before, KEYWORDS, SEARCH_TERMS)
    readers = incomplete_readers(before, KEYWORDS, SEARCH_TERMS)
    dismissed = dismissing(ctx, readers)
    out = apply_precedence(evaluate(dismissed), dismissed)
    assert out.suppressed[KEYWORDS][SUPPRESS_FEEDBACK_DISMISS] == len(readers)
    assert not incomplete_readers(out, KEYWORDS, SEARCH_TERMS)
    stored = incomplete_dataset_finding(out, KEYWORDS, SEARCH_TERMS)
    assert (stored.code, stored.section, stored.entity, stored.message) == (
        built.code,
        built.section,
        built.entity,
        built.message,
    )
    assert dict(stored.evidence) == {**built.evidence, "items": 0}
    # Campaigns' C3b still reads the dataset, so its finding keeps its count.
    assert incomplete_readers(before, CAMPAIGNS, SEARCH_TERMS)
    assert incomplete_dataset_finding(out, CAMPAIGNS, SEARCH_TERMS) == (
        incomplete_dataset_finding(before, CAMPAIGNS, SEARCH_TERMS)
    )


def test_incomplete_dataset_reasons_name_only_what_the_stored_readers_have() -> None:
    ctx = incomplete_world("late_metrics")
    start = ctx.window.start
    window_reason = (
        f"covers from {(start + timedelta(days=3)).isoformat()}, "
        f"after the window start {start.isoformat()}"
    )
    before = apply_precedence(evaluate(ctx), ctx)
    built = incomplete_dataset_finding(before, KEYWORDS, METRICS)
    (k1_reason,) = [r for r in built.evidence["reasons"] if "after the K1 baseline start" in r]
    assert set(built.evidence["reasons"]) == {k1_reason, window_reason}
    k1 = of_rule(before, KEYWORDS, K1)
    assert k1
    dismissed = dismissing(ctx, k1)
    out = apply_precedence(evaluate(dismissed), dismissed)
    readers = incomplete_readers(out, KEYWORDS, METRICS)
    assert readers
    assert K1 not in {candidate.rule_id for candidate in readers}
    stored = incomplete_dataset_finding(out, KEYWORDS, METRICS)
    assert stored.evidence["items"] == len(readers) == built.evidence["items"] - len(k1)
    # The K1 baseline reason went with the K1 items; the message names what is left.
    assert stored.evidence["reasons"] == (window_reason,)
    assert stored.message == f"{METRICS} is not complete over the window: {window_reason}"
    assert list(stored.evidence) == list(built.evidence)
    assert dict(stored.evidence) == {
        **built.evidence,
        "reasons": (window_reason,),
        "items": len(readers),
    }
    # With every reader dismissed, the finding keeps every reason it was built with.
    everyone = dismissing(ctx, incomplete_readers(before, KEYWORDS, METRICS))
    gone = incomplete_dataset_finding(
        apply_precedence(evaluate(everyone), everyone), KEYWORDS, METRICS
    )
    assert (gone.message, dict(gone.evidence)) == (built.message, {**built.evidence, "items": 0})


def test_incomplete_dataset_recount_is_identical_for_any_input_order() -> None:
    ctx = incomplete_world("late_metrics")
    before = apply_precedence(evaluate(ctx), ctx)
    targets = [of_rule(before, KEYWORDS, K1)[0], incomplete_readers(before, BUDGET, METRICS)[0]]
    dismissed = dismissing(ctx, targets)
    results = evaluate(dismissed)
    out = apply_precedence(results, dismissed)
    for section in (KEYWORDS, BUDGET):
        assert out.suppressed[section][SUPPRESS_FEEDBACK_DISMISS] == 1
        stored = incomplete_dataset_finding(out, section, METRICS)
        assert stored.evidence["items"] == len(incomplete_readers(out, section, METRICS))
        assert stored.evidence["items"] == (
            incomplete_dataset_finding(before, section, METRICS).evidence["items"] - 1
        )
    reordered = apply_precedence(list(reversed(results)), dismissed)
    assert dict(reordered.findings) == dict(out.findings)
    # The recount itself depends only on the set of stored items, never their order.
    built = incomplete_dataset_findings(before)
    survivors = [item.candidate for item in out.all_items()]
    forward = precedence._recount_incomplete_datasets(dismissed, built, survivors)
    assert forward == precedence._recount_incomplete_datasets(dismissed, built, survivors[::-1])
    assert forward == incomplete_dataset_findings(out)


@pytest.mark.parametrize("name", ["p4.incomplete.case", "search_terms", "late_metrics"])
def test_incomplete_dataset_findings_without_feedback_are_unchanged(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = incomplete_world(name)
    assert not ctx.feedback
    results = evaluate(ctx)
    out = apply_precedence(results, ctx)
    findings = incomplete_dataset_findings(out)
    assert any(findings.values())
    for section, found in findings.items():
        for finding in found:
            readers = incomplete_readers(out, section, finding.evidence["dataset"])
            assert finding.evidence["items"] == len(readers)

    def as_built(
        _ctx: AnalysisContext,
        built: Mapping[str, tuple[Finding, ...]],
        _candidates: Sequence[Candidate],
    ) -> dict[str, tuple[Finding, ...]]:
        """Step 7's findings exactly as built, with no recount."""
        return dict(built)

    monkeypatch.setattr(precedence, "_recount_incomplete_datasets", as_built)
    assert dict(apply_precedence(results, ctx).findings) == dict(out.findings)


# ---------------------------------------------------------------------------
# A section left with no item says which of its datasets do not cover the window (AAP 0.5.5)
# ---------------------------------------------------------------------------

EXPECTED_SECTION_DATASETS: Mapping[str, tuple[str, ...]] = {
    KEYWORDS: ("google.search_terms", "google.metrics_daily", "google.entities"),
    CAMPAIGNS: (
        "google.entities",
        "google.metrics_daily",
        "linkedin.entities",
        "linkedin.analytics_daily",
        "linkedin.leads",
        "hubspot.contacts",
        "hubspot.associations",
        "linkedin.segments",
        "google.search_terms",
    ),
    BUDGET: (
        "google.entities",
        "google.metrics_daily",
        "linkedin.entities",
        "linkedin.analytics_daily",
    ),
    CREATIVE: ("linkedin.analytics_daily", "linkedin.entities"),
    WHITE_PAPERS: (
        "linkedin.analytics_daily",
        "linkedin.leads",
        "linkedin.entities",
        "hubspot.contacts",
        "hubspot.associations",
        "hubspot.deal_search",
    ),
}
"""Every dataset each section's rules read, as the rule modules declare them."""

DEAL_SEARCH = "hubspot.deal_search"
CEILING = "search result ceiling reached"


def uncovered(ctx: AnalysisContext) -> AnalysisContext:
    """``ctx`` with every dated dataset's coverage starting 30 days after the window end.

    The shape of a custom window that ends before the backfill: every dated dataset is
    ``complete`` over what it covers, but none covers the window. Full-state entity datasets
    record no range, so they stay complete.
    """
    start = ctx.window.end + timedelta(days=30)
    freshness = {
        key: row
        if row.dataset in FULL_STATE_DATASETS
        else replace(row, covered_from=start, covered_through=start + timedelta(days=60))
        for key, row in ctx.freshness.items()
    }
    return replace(ctx, freshness=freshness, cache={})


def section_level(out: PrecedenceOutput, section: str) -> list[tuple[str, int | None]]:
    """A section's ``incomplete_dataset`` findings as ``(dataset, dataset_id)``, stored order."""
    return [
        (finding.evidence["dataset"], finding.evidence["dataset_id"])
        for finding in incomplete_dataset_findings(out)[section]
    ]


def test_section_datasets_are_the_union_each_rule_module_declares() -> None:
    assert EXPECTED_SECTION_DATASETS[KEYWORDS] == keywords.SECTION_DATASETS
    assert EXPECTED_SECTION_DATASETS[CAMPAIGNS] == campaigns.SECTION_DATASETS
    assert EXPECTED_SECTION_DATASETS[BUDGET] == budget.SECTION_DATASETS
    assert creative.SECTION_DATASETS == creative.REQUIRED_DATASETS
    assert EXPECTED_SECTION_DATASETS[CREATIVE] == creative.SECTION_DATASETS
    assert white_papers.SECTION_DATASETS == white_papers.REQUIRED_DATASETS
    assert EXPECTED_SECTION_DATASETS[WHITE_PAPERS] == white_papers.SECTION_DATASETS
    assert set(keywords.SECTION_DATASETS) == {
        *keywords.K1_DATASETS,
        *keywords.K2_DATASETS,
        *keywords.K3_DATASETS,
    }
    assert dict(precedence._SECTION_DATASETS) == dict(EXPECTED_SECTION_DATASETS)
    assert tuple(precedence._SECTION_DATASETS) == SECTION_ORDER


def test_an_empty_section_names_each_dataset_that_does_not_cover_the_window() -> None:
    ctx = uncovered(build("default"))
    window = ctx.window
    google = ctx.datasets["google"].dataset_id
    linkedin = ctx.datasets["linkedin"].dataset_id
    assert ctx.linkedin_crm is not None
    companion = ctx.linkedin_crm.dataset.dataset_id
    assert companion != ctx.datasets["hubspot"].dataset_id
    out = apply_precedence(RuleResult(), ctx)
    ids = {"google": google, "linkedin": linkedin, "hubspot": companion}
    for section in SECTION_ORDER:
        dated = sorted(
            name for name in EXPECTED_SECTION_DATASETS[section] if name not in FULL_STATE_DATASETS
        )
        assert section_level(out, section) == [(name, ids[name.split(".")[0]]) for name in dated]
        assert not out.items[section]
        for finding in incomplete_dataset_findings(out)[section]:
            name = finding.evidence["dataset"]
            row = ctx.freshness[(finding.evidence["dataset_id"], name)]
            assert row.covered_from is not None
            reason = (
                f"covers from {row.covered_from.isoformat()}, "
                f"after the window start {window.start.isoformat()}"
            )
            assert finding.section == section
            assert finding.entity is None
            assert finding.message == f"{name} is not complete over the window: {reason}"
            assert dict(finding.evidence) == {
                "dataset": name,
                "dataset_id": finding.evidence["dataset_id"],
                "completeness": "complete",
                "covered_from": row.covered_from,
                "covered_through": row.covered_through,
                "reason": None,
                "reasons": (reason,),
                "items": 0,
            }
    # The findings follow every other finding of their section.
    assert all(
        finding.code == FINDING_INCOMPLETE_DATASET
        for section in SECTION_ORDER
        for finding in out.findings[section]
    )


def test_an_empty_section_whose_datasets_cover_the_window_has_no_finding() -> None:
    ctx = build("default")
    out = apply_precedence(RuleResult(), ctx)
    assert all(not out.findings[section] for section in SECTION_ORDER)
    assert all(not out.items[section] for section in SECTION_ORDER)


def test_an_empty_section_judges_a_finest_partition_shortfall_by_its_days() -> None:
    """A dated Q-G6 shortfall reaches an empty section's finding only through its window days.

    With no item stored, a ``google.search_terms`` result limit on a day before the window adds
    no finding to any section. Add a day inside the window: Keywords and Campaigns, whose rules
    read the dataset, each get one finding with ``items`` 0 that names that day alone.
    """
    ctx = build("default")
    before = (ctx.window.start - timedelta(days=1)).isoformat()
    within = (ctx.window.start + timedelta(days=3)).isoformat()
    readers = [
        section for section in SECTION_ORDER if SEARCH_TERMS in EXPECTED_SECTION_DATASETS[section]
    ]
    assert readers == [KEYWORDS, CAMPAIGNS]
    excluded = with_google_freshness(
        ctx,
        SEARCH_TERMS,
        completeness="incomplete",
        reason=f"{RESULT_LIMIT} ({before}, ad group 990200000000)",
    )
    clear = apply_precedence(RuleResult(), excluded)
    assert all(not clear.findings[section] for section in SECTION_ORDER)

    included = with_google_freshness(
        ctx,
        SEARCH_TERMS,
        completeness="incomplete",
        reason=f"{RESULT_LIMIT} ({before}, ad group 990200000000; {within}, ad group 5)",
    )
    out = apply_precedence(RuleResult(), included)
    google = ctx.datasets["google"].dataset_id
    named = f"{RESULT_LIMIT} ({within}, ad group 5)"
    for section in SECTION_ORDER:
        assert not out.items[section]
        expected = [(SEARCH_TERMS, google)] if section in readers else []
        assert section_level(out, section) == expected
    for section in readers:
        finding = incomplete_dataset_finding(out, section, SEARCH_TERMS)
        assert finding.message == f"{SEARCH_TERMS} is not complete over the window: {named}"
        assert finding.evidence["reasons"] == (named,)
        assert finding.evidence["items"] == 0
        assert finding.evidence["completeness"] == "incomplete"


def test_a_section_with_items_gets_no_section_level_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = uncovered(build("default"))
    results = evaluate(ctx)
    out = apply_precedence(results, ctx)
    occupied = [section for section in SECTION_ORDER if out.items[section]]
    assert occupied == list(SECTION_ORDER)
    for section in occupied:
        for finding in incomplete_dataset_findings(out)[section]:
            readers = incomplete_readers(out, section, finding.evidence["dataset"])
            assert finding.evidence["items"] == len(readers) >= 1

    def unchanged(
        _ctx: AnalysisContext,
        built: Mapping[str, tuple[Finding, ...]],
        _candidates: Sequence[Candidate],
    ) -> dict[str, tuple[Finding, ...]]:
        """Step 10's recounted findings, with no section-level findings added."""
        return dict(built)

    monkeypatch.setattr(precedence, "_section_data_findings", unchanged)
    assert dict(apply_precedence(results, ctx).findings) == dict(out.findings)


def test_a_section_emptied_by_feedback_names_each_dataset_once() -> None:
    ctx = incomplete_world("search_terms")
    before = apply_precedence(evaluate(ctx), ctx)
    readers = of_rule(before, KEYWORDS)
    assert readers
    dismissed = dismissing(ctx, readers)
    out = apply_precedence(evaluate(dismissed), dismissed)
    assert not out.items[KEYWORDS]
    # The item-based finding stays with items 0; the section's complete datasets add none,
    # and the dataset it already names is never repeated.
    google = ctx.datasets["google"].dataset_id
    assert section_level(out, KEYWORDS) == [(SEARCH_TERMS, google)]
    stored = incomplete_dataset_finding(out, KEYWORDS, SEARCH_TERMS)
    built = incomplete_dataset_finding(before, KEYWORDS, SEARCH_TERMS)
    assert dict(stored.evidence) == {**built.evidence, "items": 0}
    # With the metrics dataset short too, the emptied section names it once, through the
    # finding its dismissed readers left (every reason they were built with kept).
    late = with_google_freshness(
        dismissed, METRICS, covered_from=ctx.window.start + timedelta(days=3)
    )
    out = apply_precedence(evaluate(late), late)
    assert section_level(out, KEYWORDS) == [(METRICS, google), (SEARCH_TERMS, google)]
    metrics_finding = incomplete_dataset_finding(out, KEYWORDS, METRICS)
    assert metrics_finding.evidence["items"] == 0
    window_reason = (
        f"covers from {(ctx.window.start + timedelta(days=3)).isoformat()}, "
        f"after the window start {ctx.window.start.isoformat()}"
    )
    assert window_reason in metrics_finding.evidence["reasons"]
    undismissed = replace(late, feedback=(), cache={})
    readers_before = apply_precedence(evaluate(undismissed), undismissed)
    assert dict(metrics_finding.evidence) == {
        **incomplete_dataset_finding(readers_before, KEYWORDS, METRICS).evidence,
        "items": 0,
    }


def test_keywords_without_an_active_google_dataset_say_so() -> None:
    ctx = build("default")
    no_google = replace(
        ctx,
        datasets={source: ref for source, ref in ctx.datasets.items() if source != "google"},
        campaigns={key: info for key, info in ctx.campaigns.items() if info.source != "google"},
        cache={},
    )
    assert keywords.evaluate(no_google) == RuleResult()
    out = apply_precedence(evaluate(no_google), no_google)
    assert not out.items[KEYWORDS]
    names = sorted(EXPECTED_SECTION_DATASETS[KEYWORDS])
    assert section_level(out, KEYWORDS) == [(name, None) for name in names]
    for finding in incomplete_dataset_findings(out)[KEYWORDS]:
        name = finding.evidence["dataset"]
        assert (
            finding.message == f"{name} is not complete over the window: no active Google dataset"
        )
        assert dict(finding.evidence) == {
            "dataset": name,
            "dataset_id": None,
            "completeness": "unknown",
            "covered_from": None,
            "covered_through": None,
            "reason": None,
            "reasons": ("no active Google dataset",),
            "items": 0,
        }


def test_hubspot_section_datasets_resolve_to_the_effective_crm_dataset() -> None:
    ctx = build("default")
    assert ctx.linkedin_crm is not None
    companion = ctx.linkedin_crm.dataset.dataset_id
    primary = ctx.datasets["hubspot"].dataset_id

    def with_row(target: int, **changes: Any) -> AnalysisContext:
        key = (target, DEAL_SEARCH)
        return replace(
            ctx, freshness={**ctx.freshness, key: replace(ctx.freshness[key], **changes)}, cache={}
        )

    # The companion's row is the one White papers reads while LinkedIn is simulated.
    capped = with_row(companion, completeness="incomplete", reason=CEILING)
    out = apply_precedence(RuleResult(), capped)
    assert section_level(out, WHITE_PAPERS) == [(DEAL_SEARCH, companion)]
    finding = incomplete_dataset_finding(out, WHITE_PAPERS, DEAL_SEARCH)
    assert finding.message == f"{DEAL_SEARCH} is not complete over the window: {CEILING}"
    assert finding.evidence["reasons"] == (CEILING,)
    assert finding.evidence["reason"] == CEILING
    assert finding.evidence["completeness"] == "incomplete"
    assert all(not section_level(out, section) for section in SECTION_ORDER[:-1])
    # HubSpot's primary row is not the effective CRM dataset, so it gates nothing here.
    out = apply_precedence(RuleResult(), with_row(primary, completeness="incomplete"))
    assert all(not out.findings[section] for section in SECTION_ORDER)
    # A dataset id with no freshness row keeps "no freshness record".
    freshness = dict(ctx.freshness)
    freshness.pop((companion, DEAL_SEARCH))
    out = apply_precedence(RuleResult(), replace(ctx, freshness=freshness, cache={}))
    finding = incomplete_dataset_finding(out, WHITE_PAPERS, DEAL_SEARCH)
    assert finding.evidence["dataset_id"] == companion
    assert finding.evidence["completeness"] == "unknown"
    assert finding.evidence["reasons"] == ("no freshness record",)
    # Without an active LinkedIn dataset there is no effective CRM dataset: LinkedIn is named.
    no_linkedin = replace(
        ctx,
        datasets={source: ref for source, ref in ctx.datasets.items() if source != "linkedin"},
        linkedin_crm=None,
        cache={},
    )
    out = apply_precedence(RuleResult(), no_linkedin)
    names = sorted(EXPECTED_SECTION_DATASETS[WHITE_PAPERS])
    assert section_level(out, WHITE_PAPERS) == [(name, None) for name in names]
    for finding in incomplete_dataset_findings(out)[WHITE_PAPERS]:
        assert finding.evidence["reasons"] == ("no active LinkedIn dataset",)
    # Without HubSpot's effective CRM dataset alone, HubSpot is named.
    no_crm = replace(ctx, linkedin_crm=None, cache={})
    out = apply_precedence(RuleResult(), no_crm)
    hubspot = [name for name in names if name.startswith("hubspot.")]
    assert section_level(out, WHITE_PAPERS) == [(name, None) for name in hubspot]
    for finding in incomplete_dataset_findings(out)[WHITE_PAPERS]:
        assert finding.evidence["reasons"] == ("no active HubSpot dataset",)


def test_section_level_findings_are_identical_for_any_input_order() -> None:
    ctx = incomplete_world("search_terms")
    before = apply_precedence(evaluate(ctx), ctx)
    dismissed = dismissing(ctx, [*of_rule(before, KEYWORDS), *of_rule(before, CREATIVE)])
    late = uncovered(dismissed)
    results = evaluate(late)
    out = apply_precedence(results, late)
    assert not out.items[KEYWORDS]
    assert not out.items[CREATIVE]
    assert section_level(out, CREATIVE)
    reordered = apply_precedence(list(reversed(results)), late)
    assert dict(reordered.findings) == dict(out.findings)
    built = incomplete_dataset_findings(out)
    survivors = [item.candidate for item in out.all_items()]
    forward = precedence._section_data_findings(late, built, survivors)
    assert forward == precedence._section_data_findings(late, built, survivors[::-1])
    assert forward == built
