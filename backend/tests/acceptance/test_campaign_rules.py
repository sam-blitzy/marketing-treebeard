"""Acceptance tests of "Campaigns to pause or launch": C0, C1, C1b, C2, C3a and C3b.

AAP 0.11.2 row ``acceptance/test_campaign_rules.py`` (FR-5, FR-10), proving the rules of AAP
0.5.4.4, the shared definitions of 0.5.4.1 they read (budget history and the cap gate, the CRM
pipeline link and its veto), the decision shapes of 0.5.2 and safe absence (0.3.3):

* **Catalogue guard.** Every scenario of ``simulation/scenarios.py`` that names this module
  runs through the rule that owns it (``discrepancy.evaluate`` for C0, ``campaigns.evaluate``
  otherwise) and must show its :class:`~ads_engine.simulation.scenarios.Expectation`. Each
  ``fires`` scenario has a ``does_not_fire`` twin differing in one input.
* **C0 comparison (pure).** ``decisions.compare`` at native precision: one Google micro, one
  LinkedIn minor unit, a status; C0 items, their wording, order and resolution rows.
* **C0 lifecycle (persisted).** The Google source of a scenario world is bootstrapped through
  the real source-mode controller and sync jobs (simulated transport, PostgreSQL), the world's
  clock is moved past its :class:`~ads_engine.simulation.generator.PlatformChange`, and the
  entity sync's stored ``discrepancy`` rows are checked: the baseline, drift, resolutions by
  kind and decision shape (domain and ``POST /discrepancies/{id}/resolve``), safe absence,
  tombstones, auto-resolution, and C0's exemption from holds and feedback in a stored run.
  LinkedIn safe absence is proven the same way: the LinkedIn source of the
  ``c0.linkedin_cut_short`` twins (with its companion CRM dataset) is bootstrapped and run
  through the real LinkedIn entity sync, so an all-status campaign search cut short raises
  no ``missing``, leaves ``linkedin.entities`` incomplete and keeps the previous snapshot,
  while a complete search raises ``missing`` on the absent tracked campaign.
* **C1, C1b, the veto cohort and unresolved matured leads**, **C2 Scale or Hold with its cap
  evidence and budget-day classification**, and **launch ideas (C3a, C3b)**, on the
  catalogue's contexts, with thresholds read from ``ctx.thresholds``.

Division of labour: ``integration/test_runs_and_decisions.py`` owns the decision-patch 422
rules of ``POST /decisions`` and ``unit/test_budget_days.py`` the raw day classification; this
module asserts the rule-level outcomes and the evidence the cards show.

No test reaches a real network: every client runs on the simulator's mock transport (the root
conftest refuses real transports), and the database tests fail, never skip, without the
PostgreSQL 18 test database.
"""

import dataclasses
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, cast

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.support import (
    GOOGLE_LINK_UNVERIFIED_REASON,
    GoogleLinkState,
    evidence_metrics,
    numeric,
    with_google_link,
)

from ads_engine.analysis import budget, campaigns, discrepancy
from ads_engine.analysis.attribution import attribution_for
from ads_engine.analysis.campaigns import (
    HOLD_ACTION,
    PAUSE_ACTION,
    PIPELINE_PAUSE_ACTION,
    SCALE_ACTION,
    ScaleOutcome,
    scale_outcomes,
)
from ads_engine.analysis.context import (
    B1,
    BUDGET,
    C0,
    C1,
    C1B,
    C2,
    C3A,
    C3B,
    CAMPAIGNS,
    FINDING_NO_CRM_LINK,
    FINDING_PIPELINE_UNAVAILABLE,
    FINDING_SEGMENT_WINDOW_UNAVAILABLE,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_NO_CRM_LINK,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_SHARED_BUDGET,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    GROUP_DISCREPANCIES,
    GROUP_LAUNCH,
    GROUP_ORDER,
    GROUP_PAUSE,
    GROUP_SCALE,
    AnalysisContext,
    Candidate,
    DecisionSubject,
    RuleResult,
    Withheld,
)
from ads_engine.analysis.discrepancy import RESOLUTION_ORDER
from ads_engine.analysis.runner import default_window, run_analysis
from ads_engine.connectors.hubspot.client import RateLimiter
from ads_engine.db.models.analysis import Recommendation
from ads_engine.db.models.decisions import Decision, Discrepancy
from ads_engine.db.models.ops import DatasetFreshness, SourceDataset
from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.decisions import (
    RESOLUTIONS,
    DecisionSource,
    SubjectRef,
    SubjectState,
    compare,
    load_subject_states,
    resolve_discrepancy,
    to_micros,
    verified_match,
)
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.scenarios import (
    FIXED_ANCHOR,
    FIXED_NOW,
    MODULE_CAMPAIGNS,
    SCENARIOS,
    Scenario,
    for_module,
)
from ads_engine.sync.jobs import JOB_ENTITIES, JobResult, run_source_jobs
from ads_engine.sync.mode import SourceModeController

pytestmark = pytest.mark.anyio

type ContextFactory = Callable[..., AnalysisContext]
type WorldFactory = Callable[..., SimulatedWorld]
type Sessions = async_sessionmaker[AsyncSession]
type SetEnv = Callable[..., None]

# ---------------------------------------------------------------------------
# Catalogue selections and fixed instants
# ---------------------------------------------------------------------------

CAMPAIGN_SCENARIOS: Final[tuple[Scenario, ...]] = for_module(MODULE_CAMPAIGNS)
"""Every catalogue scenario this module runs, in name order."""

CAMPAIGN_RULES: Final = (C1, C1B, C2, C3A, C3B)
"""The rules ``campaigns.evaluate`` implements; C0 lives in ``discrepancy.py``."""

C0_SCENARIOS: Final = tuple(s for s in CAMPAIGN_SCENARIOS if s.expect.rule_id == C0)
RULE_SCENARIOS: Final = tuple(s for s in CAMPAIGN_SCENARIOS if s.expect.rule_id in CAMPAIGN_RULES)

RULE_GROUPS: Final[Mapping[str, str]] = {
    C1: GROUP_PAUSE,
    C1B: GROUP_PAUSE,
    C2: GROUP_SCALE,
    C3A: GROUP_LAUNCH,
    C3B: GROUP_LAUNCH,
}
"""AAP 0.5.4.4 group of each rule's items (group order Discrepancies, Pause, Scale, Launch)."""

MIDNIGHT: Final = datetime(FIXED_ANCHOR.year, FIXED_ANCHOR.month, FIXED_ANCHOR.day, tzinfo=UTC)
"""The anchor's UTC midnight: every scenario offset in hours is relative to it."""

NOW_OFFSET: Final = 12
""":data:`FIXED_NOW`, the instant every fixed-time scenario is read at, as an hour offset."""

DATE_LISTS: Final[Mapping[str, str]] = {
    "unobserved_days": "unobserved_dates",
    "changed_intraday_days": "changed_intraday_dates",
}
"""Expectation keys whose tuple values are the dated day lists in ``evidence.extra``."""

PLATFORM_OR_DECISION: Final = frozenset({"platform_correct", "decision_correct"})
ADOPT_OR_INTEND: Final = frozenset({"adopt_platform", "record_intended"})
REMOVAL: Final = frozenset({"acknowledge_removal", "decision_correct"})

EXPECTED_RESOLUTIONS: Final[Mapping[tuple[str, str], frozenset[str]]] = {
    ("status", "own_budget"): PLATFORM_OR_DECISION,
    ("status", "shared_member"): PLATFORM_OR_DECISION,
    ("budget", "own_budget"): PLATFORM_OR_DECISION,
    ("budget", "shared_member"): PLATFORM_OR_DECISION,
    ("budget", "budget"): PLATFORM_OR_DECISION,
    ("untracked", "own_budget"): ADOPT_OR_INTEND,
    ("untracked", "shared_member"): ADOPT_OR_INTEND,
    ("untracked", "budget"): ADOPT_OR_INTEND,
    ("missing", "own_budget"): REMOVAL,
    ("missing", "shared_member"): REMOVAL,
    ("missing", "budget"): REMOVAL,
}
"""AAP 0.5.4.4 "C0 resolutions by kind and subject type", one row per (kind, decision shape)."""

C0_KEYS_CHECKED_ON_CONTEXT: Final = frozenset(
    {
        "open",
        "kind",
        "subject_type",
        "shape",
        "source",
        "state",
        "resolutions",
        "decided",
        "platform",
        "decided_micros",
        "platform_micros",
        "tombstone",
        "subject_kinds",
        "budget_discrepancies",
        "member_discrepancies",
        "detected_at_offset_hours",
        "compares",
        "google.entities",
        "linkedin.entities",
    }
)
"""C0 expectation keys the catalogue guard checks on the converted context."""

C0_KEYS_CHECKED_PERSISTED: Final = frozenset(
    {
        "raw_status",
        "record_intended_fields",
        "record_intended_accepts",
        "record_intended_rejects",
        "member_record_intended_accepts",
        "held",
        "feedback_status",
        "resolved_by",
        "missing_raised_after_tombstone",
    }
)
"""C0 expectation keys only a stored run, snapshot or resolution proves (Phase 3 tests)."""


def _at(offset_hours: int) -> datetime:
    """The instant ``offset_hours`` after the anchor's UTC midnight."""
    return MIDNIGHT + timedelta(hours=offset_hours)


# ---------------------------------------------------------------------------
# Small readers of rule output
# ---------------------------------------------------------------------------


def _items(result: RuleResult, rule_id: str) -> list[Candidate]:
    return [item for item in result.candidates if item.rule_id == rule_id]


def _only(result: RuleResult, rule_id: str) -> Candidate:
    items = _items(result, rule_id)
    assert len(items) == 1, [(item.action, item.entity_ref.name) for item in items]
    return items[0]


def _withheld(result: RuleResult, rule_id: str) -> list[Withheld]:
    return [line for line in result.withheld if line.rule_id == rule_id]


def _only_withheld(result: RuleResult, rule_id: str) -> Withheld:
    lines = _withheld(result, rule_id)
    assert len(lines) == 1, [line.reason for line in lines]
    return lines[0]


def _codes(result: RuleResult) -> list[str]:
    return [finding.code for finding in result.findings]


def _evidence_value(item: Candidate, key: str, expected: object) -> object:
    """An expectation value's counterpart on a card: a metric, else ``evidence.extra``."""
    if isinstance(expected, tuple) and key in DATE_LISTS:
        return item.evidence.extra[DATE_LISTS[key]]
    metrics = evidence_metrics(item)
    if key in metrics:
        return metrics[key]
    return item.evidence.extra[key]


def _threshold(ctx: AnalysisContext, key: str) -> Decimal:
    """A configured threshold as an exact ``Decimal`` (integers and ratios alike)."""
    return Decimal(str(ctx.thresholds.value(key)))


def _side(item: Candidate, side: str) -> Mapping[str, Any]:
    """One side (``decided`` or ``platform``) of a C0 item's stored comparison."""
    value = item.evidence.extra[side]
    assert value is not None, (item.evidence.extra["kind"], side)
    return cast(Mapping[str, Any], value)


def _shown(item: Candidate, side: str) -> object:
    """What a C0 expectation's ``decided`` / ``platform`` names: a status, or an amount."""
    value = _side(item, side)
    if item.evidence.extra["kind"] != "budget":
        return value["status"]
    daily = value.get("daily_budget")
    return Decimal(cast(str, daily if daily is not None else value["total_budget"]))


# ---------------------------------------------------------------------------
# Phase 1: the catalogue guard (simulation/scenarios.py expectations)
# ---------------------------------------------------------------------------


def test_the_catalogue_covers_every_campaign_rule_firing_and_not_firing() -> None:
    """Every Campaigns rule has a scenario that fires and one that does not (AAP 0.11.2)."""
    rules = {scenario.expect.rule_id for scenario in CAMPAIGN_SCENARIOS}
    assert rules == {C0, *CAMPAIGN_RULES}
    for rule_id in rules:
        outcomes = {s.expect.fires for s in CAMPAIGN_SCENARIOS if s.expect.rule_id == rule_id}
        assert outcomes == {True, False}, rule_id
    for scenario in CAMPAIGN_SCENARIOS:
        assert scenario.module == MODULE_CAMPAIGNS
        if scenario.variant == "case":
            continue
        twin = SCENARIOS[cast(str, scenario.pair)]
        assert twin.pair == scenario.name
        assert {scenario.variant, twin.variant} == {"fires", "does_not_fire"}


def test_every_c0_expectation_key_is_checked_by_this_module() -> None:
    """No C0 expectation value goes unchecked: on the context here, or by a persisted test."""
    keys = {key for scenario in C0_SCENARIOS for key in scenario.expect.values}
    assert keys <= C0_KEYS_CHECKED_ON_CONTEXT | C0_KEYS_CHECKED_PERSISTED
    assert keys >= C0_KEYS_CHECKED_PERSISTED


@pytest.mark.parametrize("scenario", C0_SCENARIOS, ids=lambda scenario: scenario.name)
def test_c0_catalogue_expectation_holds(scenario: Scenario, make_context: ContextFactory) -> None:
    """C0: each stored unresolved discrepancy is one pinned item as its scenario expects."""
    ctx = make_context(scenario)
    result = discrepancy.evaluate(ctx)
    items = list(result.candidates)
    values = scenario.expect.values
    assert bool(items) is scenario.expect.fires
    assert len(items) == len(ctx.discrepancies)
    assert (result.findings, result.withheld, dict(result.suppressed)) == ((), (), {})
    google_linked = ctx.google_crm is not None and ctx.google_crm.bindings.google.available
    for item in items:
        extra = item.evidence.extra
        assert (item.section, item.group, item.pinned) == (CAMPAIGNS, GROUP_DISCREPANCIES, True)
        assert item.decision_subject is None
        # AAP 0.3.3: a Google item carries no_crm_link without the verified Google-to-CRM link.
        unlinked_google = item.entity_ref.source == "google" and not google_linked
        assert item.flag_codes == ((FLAG_NO_CRM_LINK,) if unlinked_google else ())
        platform = item.evidence.metric("platform").value
        decided = item.evidence.metric("decided").value
        assert item.action == f"Resolve: platform shows {platform}, last decided {decided}"
        assert frozenset(extra["resolutions"]) == RESOLUTIONS[(extra["kind"], extra["shape"])]
        assert list(extra["resolutions"]) == sorted(
            extra["resolutions"], key=RESOLUTION_ORDER.index
        )
        assert extra["state"] in ("open", "awaiting_platform")
    if "open" in values:
        assert len(items) == values["open"]
    if "kind" in values:
        assert {item.evidence.extra["kind"] for item in items} == {values["kind"]}
    if "subject_type" in values:
        assert values["subject_type"] in {item.entity_ref.type for item in items}
    if "shape" in values:
        assert values["shape"] in {item.evidence.extra["shape"] for item in items}
    if "source" in values:
        assert {item.entity_ref.source for item in items} == {values["source"]}
    if "state" in values:
        assert {item.evidence.extra["state"] for item in items} == {values["state"]}
    if "resolutions" in values:
        assert all(
            tuple(item.evidence.extra["resolutions"]) == values["resolutions"] for item in items
        )
    for side in ("decided", "platform"):
        if side in values:
            for item in items:
                expected = values[side]
                wanted = (
                    Decimal(cast(str, expected))
                    if item.evidence.extra["kind"] == "budget"
                    else expected
                )
                assert _shown(item, side) == wanted, (item.entity_ref.name, side)
        if f"{side}_micros" in values:
            micros = {_side(item, side)["amount_micros"] for item in items}
            assert micros == {values[f"{side}_micros"]}
    if "tombstone" in values:
        tombstones = {_side(item, "decided").get("tombstone") for item in items}
        assert tombstones == {values["tombstone"]}
    if "subject_kinds" in values:
        found = sorted((item.entity_ref.type, item.evidence.extra["kind"]) for item in items)
        assert found == list(cast(tuple[tuple[str, str], ...], values["subject_kinds"]))
    if "budget_discrepancies" in values:
        on_budgets = [item for item in items if item.entity_ref.type == "budget"]
        assert len(on_budgets) == values["budget_discrepancies"]
    if "member_discrepancies" in values:
        on_members = [item for item in items if item.evidence.extra["shape"] == "shared_member"]
        assert len(on_members) == values["member_discrepancies"]
    if "detected_at_offset_hours" in values:
        offset = cast(int, values["detected_at_offset_hours"])
        detected = {item.evidence.extra["detected_at"] for item in items}
        assert detected == {_at(offset).isoformat()}
    if values.get("compares") == "budget_id":
        for item in items:
            decided_id = _side(item, "decided")["budget_id"]
            platform_id = _side(item, "platform")["budget_id"]
            assert decided_id != platform_id
    for dataset in ("google.entities", "linkedin.entities"):
        if dataset in values:
            rows = [row for (_, name), row in ctx.freshness.items() if name == dataset]
            assert {row.completeness for row in rows} == {values[dataset]}


@pytest.mark.parametrize("scenario", RULE_SCENARIOS, ids=lambda scenario: scenario.name)
def test_campaign_rule_catalogue_expectation_holds(
    scenario: Scenario, make_context: ContextFactory
) -> None:
    """C1-C3b: each scenario's rule fires (or does not) with the numbers its scenario names."""
    expect = scenario.expect
    rule_id = cast(str, expect.rule_id)
    result = campaigns.evaluate(make_context(scenario))
    items = _items(result, rule_id)
    if expect.fires:
        assert len(items) == 1, [item.action for item in items]
        item = items[0]
        assert (item.section, item.group) == (CAMPAIGNS, RULE_GROUPS[rule_id])
        assert not item.pinned
        for key, expected in expect.values.items():
            actual = _evidence_value(item, key, expected)
            assert numeric(actual) == numeric(expected), (key, actual, expected)
        assert set(expect.flags) <= set(item.flag_codes)
    else:
        assert items == []
        reported = {
            code for finding in result.findings for code in finding.evidence.get("flags", ())
        }
        reported.update(code for item in result.candidates for code in item.flag_codes)
        assert set(expect.flags) <= reported
    reasons = [line.reason for line in _withheld(result, rule_id)]
    if expect.withheld_reason is not None:
        assert any(reason.startswith(expect.withheld_reason) for reason in reasons), reasons
    if expect.finding is not None:
        assert expect.finding in _codes(result)
    if expect.label is not None:
        assert expect.label in [finding.message for finding in result.findings]
        assert expect.label in reasons


# ---------------------------------------------------------------------------
# Phase 2: the C0 comparison, pure (AAP 0.5.4.4 C0 row)
# ---------------------------------------------------------------------------

DECIDED_AT: Final = FIXED_NOW - timedelta(days=3)
"""When the transient decisions below were recorded (the baseline)."""

CAPTURED_AT: Final = FIXED_NOW - timedelta(hours=1)
"""When the transient snapshots below were observed: after the decision."""

GOOGLE_CAMPAIGN: Final = SubjectRef(type="campaign", id=7)
SHARED_BUDGET: Final = SubjectRef(type="budget", id=30)
LINKEDIN_CAMPAIGN: Final = SubjectRef(type="campaign", id=9)
OWN_BUDGET_ID: Final = 31
"""The Google budget, not explicitly shared, that :data:`GOOGLE_CAMPAIGN` draws from."""


def _snapshot(
    ref: SubjectRef,
    source: str,
    *,
    status: str | None = "active",
    daily: str | None = None,
    total: str | None = None,
    micros: int | None = None,
    budget_id: int | None = None,
) -> EntitySnapshot:
    """A transient ``entity_snapshot`` row (never stored), as an entity sync writes it."""
    return EntitySnapshot(
        dataset_id=1,
        source=source,
        entity_type=ref.type,
        entity_id=ref.id,
        captured_at=CAPTURED_AT,
        status=status,
        budget_daily=None if daily is None else Decimal(daily),
        budget_total=None if total is None else Decimal(total),
        budget_amount_micros=micros,
        budget_id=budget_id,
        payload={},
    )


def _decision(
    ref: SubjectRef,
    source: str,
    shape: str,
    *,
    status: str | None = "active",
    daily: str | None = None,
) -> Decision:
    """A transient baseline ``decision`` row (never stored) holding one complete shape."""
    return Decision(
        dataset_id=1,
        source=source,
        subject_type=ref.type,
        subject_id=ref.id,
        shape=shape,
        status=status,
        daily_budget=None if daily is None else Decimal(daily),
        total_budget=None,
        budget_id=None,
        tombstone=False,
        decided_at=DECIDED_AT,
        origin="baseline",
        patch={},
    )


def _state(
    ref: SubjectRef,
    source: str,
    shape: str,
    snapshot: EntitySnapshot,
    decision: Decision,
) -> SubjectState:
    """What ``load_subject_state`` reads for a daily-budget subject owning its amount."""
    return SubjectState(
        ref=ref,
        dataset_id=1,
        dataset_state="active",
        source="google" if source == "google" else "linkedin",
        currency="USD",
        name=f"{source} {ref.type} {ref.id}",
        shape="budget" if shape == "budget" else "own_budget",
        has_daily_budget=True,
        has_total_budget=False,
        budget_period="DAILY" if source == "google" else None,
        budget_id=None,
        budget_external_id=None,
        latest_snapshot=snapshot,
        current_decision=decision,
    )


def _compared(
    ref: SubjectRef, source: str, shape: str, snapshot: EntitySnapshot, decision: Decision
) -> tuple[list[Any], bool]:
    state = _state(ref, source, shape, snapshot, decision)
    return compare(snapshot, decision, state=state), verified_match(snapshot, decision, state=state)


@pytest.mark.parametrize(
    ("ref", "shape", "status", "budget_id"),
    [
        pytest.param(GOOGLE_CAMPAIGN, "own_budget", "active", OWN_BUDGET_ID, id="own-budget"),
        pytest.param(SHARED_BUDGET, "budget", None, None, id="shared-budget-subject"),
    ],
)
def test_c0_budget_precision_one_google_micro_differs_identical_micros_do_not(
    ref: SubjectRef, shape: str, status: str | None, budget_id: int | None
) -> None:
    """C0 budget precision: a Google budget differing by 1 micro / identical micros."""
    decided = Decimal("100.00")
    decision = _decision(ref, "google", shape, status=status, daily=str(decided))
    differing = _snapshot(
        ref,
        "google",
        status=status,
        daily="100.000001",
        micros=to_micros(decided) + 1,
        budget_id=budget_id,
    )
    mismatches, verified = _compared(ref, "google", shape, differing, decision)
    assert [mismatch.kind for mismatch in mismatches] == ["budget"]
    (mismatch,) = mismatches
    assert mismatch.decided_value is not None
    assert mismatch.platform_value is not None
    assert mismatch.decided_value["amount_micros"] == to_micros(decided) == 100_000_000
    assert mismatch.platform_value["amount_micros"] == 100_000_001
    assert not verified
    identical = _snapshot(
        ref,
        "google",
        status=status,
        daily="100.000000",
        micros=to_micros(decided),
        budget_id=budget_id,
    )
    mismatches, verified = _compared(ref, "google", shape, identical, decision)
    assert mismatches == []
    assert verified


def test_c0_budget_precision_one_linkedin_minor_unit_differs() -> None:
    """C0 budget precision: a LinkedIn budget differing by one minor unit (0.01 USD)."""
    ref = LINKEDIN_CAMPAIGN
    decision = _decision(ref, "linkedin", "own_budget", daily="50.00")
    raised = _snapshot(ref, "linkedin", daily="50.01")
    mismatches, verified = _compared(ref, "linkedin", "own_budget", raised, decision)
    assert [mismatch.kind for mismatch in mismatches] == ["budget"]
    (mismatch,) = mismatches
    assert mismatch.decided_value is not None
    assert mismatch.platform_value is not None
    assert Decimal(mismatch.decided_value["daily_budget"]) == Decimal("50.00")
    assert Decimal(mismatch.platform_value["daily_budget"]) == Decimal("50.01")
    assert "amount_micros" not in mismatch.platform_value
    assert not verified
    # The same amount at the stored scale (numeric(18, 6)) is the same amount: no tolerance,
    # and no false mismatch from trailing zeros either.
    same = _snapshot(ref, "linkedin", daily="50.000000")
    mismatches, verified = _compared(ref, "linkedin", "own_budget", same, decision)
    assert mismatches == []
    assert verified


def test_c0_status_mismatch_raises_only_a_status_discrepancy() -> None:
    """C0 status: a normalised status that differs, with the budget unchanged."""
    ref = GOOGLE_CAMPAIGN
    decision = _decision(ref, "google", "own_budget", daily="100.00")
    paused = _snapshot(
        ref,
        "google",
        status="paused",
        daily="100.000000",
        micros=100_000_000,
        budget_id=OWN_BUDGET_ID,
    )
    mismatches, verified = _compared(ref, "google", "own_budget", paused, decision)
    assert [(m.kind, m.decided_value, m.platform_value) for m in mismatches] == [
        ("status", {"status": "active"}, {"status": "paused"})
    ]
    assert not verified


def test_the_resolution_matrix_is_exactly_the_c0_table() -> None:
    """Only the resolutions valid for each (kind, subject shape) pair exist; none for a status
    on a shared budget, which has no status."""
    assert dict(RESOLUTIONS) == dict(EXPECTED_RESOLUTIONS)
    assert ("status", "budget") not in RESOLUTIONS
    assert set(RESOLUTION_ORDER) == set().union(*EXPECTED_RESOLUTIONS.values())


@pytest.mark.parametrize(
    ("kind", "resolutions", "shapes"),
    [
        ("status", PLATFORM_OR_DECISION, {"own_budget", "shared_member"}),
        ("budget", PLATFORM_OR_DECISION, {"own_budget", "shared_member", "budget"}),
        ("untracked", ADOPT_OR_INTEND, {"own_budget", "shared_member", "budget"}),
        ("missing", REMOVAL, {"own_budget"}),
    ],
)
def test_c0_items_offer_exactly_the_resolutions_of_their_kind(
    kind: str, resolutions: frozenset[str], shapes: set[str], make_context: ContextFactory
) -> None:
    """C0 untracked offers only Adopt platform state and Record intended state (campaigns of
    both shapes and budgets); missing only acknowledge_removal and decision_correct;
    status/budget only platform_correct and decision_correct.

    ``shapes`` are the decision shapes the catalogue raises each kind on; every item of the
    kind, whatever its shape, offers exactly the kind's row.
    """
    seen: set[str] = set()
    for scenario in C0_SCENARIOS:
        for item in discrepancy.evaluate(make_context(scenario)).candidates:
            extra = item.evidence.extra
            if extra["kind"] != kind:
                continue
            seen.add(cast(str, extra["shape"]))
            assert frozenset(extra["resolutions"]) == resolutions, scenario.name
            # The rule names no headline: precedence sets it once every hold is final.
            assert "headline_metric" not in extra, scenario.name
    assert seen == shapes


def test_c0_items_are_ordered_oldest_detection_first(make_context: ContextFactory) -> None:
    """C0 is pinned first, oldest detected_at first, whatever order the store returns."""
    ctx = make_context("c0.whole_snapshot.fires")
    assert len(ctx.discrepancies) == 2
    newer, older = (
        dataclasses.replace(ctx.discrepancies[0], detected_at=_at(-6)),
        dataclasses.replace(ctx.discrepancies[1], detected_at=_at(-50)),
    )
    reordered = dataclasses.replace(ctx, discrepancies=(newer, older), cache={})
    items = discrepancy.evaluate(reordered).candidates
    assert [item.evidence.extra["discrepancy_id"] for item in items] == [older.id, newer.id]
    assert [item.rank_key for item in items] == [
        (older.detected_at, older.id),
        (newer.detected_at, newer.id),
    ]
    assert all(item.pinned and item.depends_on for item in items)


def test_c0_turns_awaiting_platform_discrepancies_into_items(make_context: ContextFactory) -> None:
    """An awaiting_platform discrepancy stays a C0 item until a snapshot matches."""
    (item,) = discrepancy.evaluate(make_context("c0.decision_correct_missing.fires")).candidates
    assert item.evidence.extra["state"] == "awaiting_platform"
    assert item.evidence.extra["kind"] == "missing"
    assert item.evidence.metric("platform").value == discrepancy.NOT_RETURNED_TEXT
    assert item.reason.endswith("Awaiting a matching platform snapshot.")


# ---------------------------------------------------------------------------
# Phase 3: the C0 lifecycle, persisted (simulated Google and LinkedIn sources on PostgreSQL)
# ---------------------------------------------------------------------------


class _Clock:
    """The one clock the simulated world, the source-mode controller and every write read.

    Entity reads return the world's state at this instant (every ``PlatformChange`` at or
    before it applied); tests move it forward between syncs and resolutions, so a snapshot is
    always observed after the decision it is judged against (AAP 0.5.4.4 "a later snapshot").
    """

    def __init__(self, offset_hours: int) -> None:
        self.at = _at(offset_hours)

    def __call__(self) -> datetime:
        return self.at

    def move(self, offset_hours: int) -> datetime:
        """Advance to ``offset_hours`` after the anchor's UTC midnight."""
        target = _at(offset_hours)
        assert target > self.at, f"the clock only moves forward ({target} <= {self.at})"
        self.at = target
        return target


async def _no_sleep(seconds: float) -> None:
    """Retry backoff without waiting: a faulted query fails its three attempts at once."""
    del seconds


class _PaceClock:
    """Monotonic seconds that only the HubSpot throttle's own sleeps advance (virtual time)."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds

    async def sleep(self, seconds: float) -> None:
        """Advance by ``seconds`` at once instead of waiting for them."""
        self.seconds += max(seconds, 0.0)


def _controller(world: SimulatedWorld, clock: _Clock, db: Sessions) -> SourceModeController:
    """A source-mode controller serving ``world`` on the test database, with no backoff sleeps.

    The retry policy is the product's own (three attempts) with an instant sleep, so a
    scenario ``Fault`` is classified exactly as in production without slowing the suite. The
    HubSpot throttle is the product's own 4 requests/second limiter on virtual time: the
    companion CRM backfill a simulated LinkedIn dataset needs before it activates (one CRM
    Search call per backfill day) keeps its request spacing without sleeping through it.
    """
    plain = SourceModeController(world=world, clock=clock, sessionmaker=db)
    fast = dataclasses.replace(plain.retry, sleep=_no_sleep)
    pace = _PaceClock()
    return SourceModeController(
        world=world,
        clock=clock,
        sessionmaker=db,
        retry=fast,
        hubspot_limiter=RateLimiter(clock=pace, sleep=pace.sleep),
    )


def _comparable(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A discrepancy value without internal row ids, which are numbered per store."""
    if value is None:
        return None
    return {key: item for key, item in value.items() if key != "budget_id"}


@dataclasses.dataclass(slots=True)
class PersistedGoogle:
    """One scenario's source, bootstrapped into the test database and synced on demand.

    The source is Google unless ``source`` names LinkedIn, whose safe absence the LinkedIn
    cut-short twins prove through the same real entity sync.

    Attributes:
        scenario: The catalogue scenario whose world the source serves.
        clock: The shared clock.
        db: The application's session factory on the test database.
        controller: The controller serving the bootstrap world.
        make_world: The root ``make_world`` fixture, for syncs against a twin's world.
        source: The bootstrapped and synced source: ``google`` or ``linkedin``.
    """

    scenario: Scenario
    clock: _Clock
    db: Sessions
    controller: SourceModeController
    make_world: WorldFactory
    source: DecisionSource = "google"

    async def sync(
        self, offset_hours: int, *, world_of: str | None = None, cut_short: bool = False
    ) -> JobResult:
        """Run one entity sync of the source at ``offset_hours``.

        Args:
            offset_hours: The sync instant, after the anchor's UTC midnight.
            world_of: Serve this scenario's world instead (a twin with a ``Fault``); its
                spec differs from the bootstrap world's in that input only.
            cut_short: Whether the served world's transient ``Fault`` cuts the sync short.
                The job then ends ``failed`` with the error's reason, its read recorded as
                a cut-short ``incomplete`` success all the same; otherwise it succeeds.
        """
        self.clock.move(offset_hours)
        controller = self.controller
        if world_of is not None:
            world = self.make_world(world_of, clock=self.clock)
            controller = _controller(world, self.clock, self.db)
        report = await run_source_jobs(
            self.source,
            trigger="manual",
            jobs=(JOB_ENTITIES,),
            controller=controller,
            now=self.clock.at,
        )
        (result,) = report.results
        if not cut_short:
            assert result.status == "succeeded", result.to_json()
            return result
        assert (result.status, result.disposition, result.completeness) == (
            "failed",
            "stale",
            "incomplete",
        ), result.to_json()
        assert result.error is not None
        assert all(
            outcome.succeeded and outcome.cut_short for outcome in result.freshness.values()
        ), result.to_json()
        return result

    async def resolve(
        self,
        row: Discrepancy,
        resolution: str,
        intended: Mapping[str, object] | None = None,
        *,
        at: int,
    ) -> Discrepancy:
        """Apply a C0 resolution through ``ads_engine.decisions`` at ``at`` hours."""
        self.clock.move(at)
        return await resolve_discrepancy(
            row.id,
            cast(Any, resolution),
            intended,
            now=self.clock.at,
        )

    async def discrepancies(self, *, include_resolved: bool = False) -> list[Discrepancy]:
        """The stored discrepancy rows, oldest first; unresolved ones unless told otherwise."""
        async with self.db() as session:
            rows = list(
                (await session.execute(select(Discrepancy).order_by(Discrepancy.id))).scalars()
            )
        return [row for row in rows if include_resolved or row.state != "resolved"]

    async def states(self) -> dict[tuple[str, int], SubjectState]:
        """Every decision subject's state, as ``load_subject_states`` reads it for the API."""
        async with self.db() as session:
            pairs = await session.execute(
                select(EntitySnapshot.entity_type, EntitySnapshot.entity_id)
                .where(EntitySnapshot.entity_type.in_(("campaign", "budget")))
                .distinct()
            )
            refs = {SubjectRef(type=cast(Any, kind), id=entity) for kind, entity in pairs.all()}
            loaded = await load_subject_states(session, refs)
        return {(ref.type, ref.id): state for ref, state in loaded.states.items()}

    async def subject(self, entity: str, key: str) -> SubjectState:
        """The stored subject a spec key names: a campaign by name, a shared budget by id."""
        ids = self.scenario.ids
        if entity == "campaign":
            name = ids.name(self.source, "campaign", key)
        else:
            name = f"Shared budget {ids.external_id(self.source, 'budget', key)}"
        found = [state for state in (await self.states()).values() if state.name == name]
        assert len(found) == 1, (name, [state.name for state in (await self.states()).values()])
        return found[0]

    async def decisions_of(self, state: SubjectState) -> list[Decision]:
        """The subject's decisions, oldest first; the last one is its expected state."""
        async with self.db() as session:
            rows = await session.execute(
                select(Decision)
                .where(
                    Decision.subject_type == state.ref.type,
                    Decision.subject_id == state.ref.id,
                )
                .order_by(Decision.decided_at, Decision.id)
            )
            return list(rows.scalars())

    async def freshness(self, dataset: str) -> DatasetFreshness:
        """The source's active primary dataset's freshness row of ``dataset``."""
        async with self.db() as session:
            active = (
                await session.execute(
                    select(SourceDataset.id).where(
                        SourceDataset.source == self.source,
                        SourceDataset.state == "active",
                        SourceDataset.role == "primary",
                    )
                )
            ).scalar_one()
            return (
                await session.execute(
                    select(DatasetFreshness).where(
                        DatasetFreshness.dataset_id == active,
                        DatasetFreshness.dataset == dataset,
                    )
                )
            ).scalar_one()

    async def campaign_snapshots_at(self, offset_hours: int) -> list[EntitySnapshot]:
        """Every stored campaign snapshot captured at ``offset_hours``, of any dataset."""
        async with self.db() as session:
            rows = await session.execute(
                select(EntitySnapshot).where(
                    EntitySnapshot.entity_type == "campaign",
                    EntitySnapshot.captured_at == _at(offset_hours),
                )
            )
            return list(rows.scalars())


type Persist = Callable[..., Any]


@pytest.fixture
def persist(db: Sessions, make_world: WorldFactory, set_env: SetEnv) -> Persist:
    """Return ``await persist(scenario, at, *, world_of=None, source="google")``.

    Under ``DATA_MODE=simulated`` the scenario's ``source`` (Google by default, or LinkedIn
    with its companion CRM dataset) is built, backfilled and activated by
    ``SourceModeController.ensure_dataset`` at ``at`` hours; its first entity sync writes the
    baseline decisions. ``world_of`` bootstraps from another scenario's world (the fault-free
    twin of a scenario whose ``Fault`` would otherwise stop the baseline). The result is a
    :class:`PersistedGoogle` serving that source.
    """

    async def _persist(
        name: str, at: int, *, world_of: str | None = None, source: DecisionSource = "google"
    ) -> PersistedGoogle:
        set_env(DATA_MODE="simulated")
        scenario = SCENARIOS[name]
        clock = _Clock(at)
        controller = _controller(make_world(world_of or name, clock=clock), clock, db)
        outcome = await controller.ensure_dataset(source)
        assert outcome is not None, f"the {source} dataset was not built"
        assert (outcome.activated, outcome.mode) == (True, "simulated"), outcome
        return PersistedGoogle(scenario, clock, db, controller, make_world, source)

    return _persist


def _names(states: Mapping[tuple[str, int], SubjectState]) -> dict[tuple[str, int], str]:
    return {key: state.name for key, state in states.items()}


def _identity(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """Sort key of a compared discrepancy: subject name, kind and state (values are dicts)."""
    return row[:3]


async def _stored_and_replayed(
    persisted: PersistedGoogle, make_context: ContextFactory
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """The unresolved stored discrepancies and those the catalogue's replay computes.

    Both lists hold ``(subject name, kind, state, decided value, platform value)`` without
    internal row ids, sorted by subject, kind and state, so the real entity sync's rows can
    be compared with the replay of the scenario the source serves.
    """
    names = _names(await persisted.states())
    stored = [
        (
            names[(row.subject_type, row.subject_id)],
            row.kind,
            row.state,
            _comparable(row.decided_value),
            _comparable(row.platform_value),
        )
        for row in await persisted.discrepancies()
    ]
    stored.sort(key=_identity)
    replayed = [
        (
            info.subject_name,
            info.kind,
            info.state,
            _comparable(info.decided_value),
            _comparable(info.platform_value),
        )
        for info in make_context(persisted.scenario).discrepancies
    ]
    replayed.sort(key=_identity)
    return stored, replayed


DETECTION_CASES: Final[tuple[tuple[str, str | None, int, tuple[int, ...]], ...]] = (
    ("c0.google_budget_micro.fires", None, -36, (NOW_OFFSET,)),
    ("c0.google_budget_micro.does_not_fire", None, -36, (NOW_OFFSET,)),
    ("c0.status.fires", None, -36, (NOW_OFFSET,)),
    ("c0.status.does_not_fire", None, -36, (NOW_OFFSET,)),
    ("c0.shared_budget_amount.case", None, -36, (NOW_OFFSET,)),
    ("c0.member_moved.case", None, -36, (NOW_OFFSET,)),
    ("c0.removed.case", None, -36, (NOW_OFFSET,)),
    ("c0.untracked_campaign.fires", None, -60, (NOW_OFFSET,)),
    ("c0.untracked_campaign.does_not_fire", None, -60, (NOW_OFFSET,)),
    ("c0.untracked_budget.case", None, -60, (NOW_OFFSET,)),
    ("c0.missing.fires", None, -36, (NOW_OFFSET,)),
    ("c0.missing.does_not_fire", "c0.missing.fires", -36, (NOW_OFFSET,)),
    ("c0.missing_qg2b_failed.case", "c0.missing.fires", -36, (NOW_OFFSET,)),
    ("c0.whole_snapshot.fires", None, -66, (-57, NOW_OFFSET)),
    ("c0.whole_snapshot.does_not_fire", None, -66, (-57, NOW_OFFSET)),
    ("c0.refreshed_evidence.case", None, -66, (-60, NOW_OFFSET)),
)
"""Google C0 scenarios whose history needs no resolution: (scenario, bootstrap world, bootstrap
hour, sync hours). The bootstrap precedes every platform change; a scenario carrying a
``Fault`` is bootstrapped from its fault-free twin and synced against its own world. The
LinkedIn cut-short twins (``c0.linkedin_cut_short.*``) run the same way through the real
LinkedIn entity sync, in the LinkedIn safe-absence tests below, which add the snapshot and
freshness checks of an interrupted all-status search."""


@pytest.mark.parametrize(
    ("name", "bootstrap_world", "bootstrap_at", "sync_at"),
    DETECTION_CASES,
    ids=[case[0] for case in DETECTION_CASES],
)
async def test_persisted_detection_matches_the_catalogue(
    persist: Persist,
    name: str,
    bootstrap_world: str | None,
    bootstrap_at: int,
    sync_at: tuple[int, ...],
    make_context: ContextFactory,
) -> None:
    """C0 detection through the real entity sync agrees with the scenario's expectation and with
    the catalogue's replay: no discrepancy right after the baseline, then exactly the kinds,
    subjects and compared values the platform changes raise."""
    google = await persist(name, bootstrap_at, world_of=bootstrap_world)
    assert await google.discrepancies(include_resolved=True) == []
    for offset in sync_at:
        faulted = bootstrap_world is not None
        await google.sync(offset, world_of=name if faulted else None, cut_short=faulted)
    stored = await google.discrepancies()
    scenario = SCENARIOS[name]
    values = scenario.expect.values
    assert bool(stored) is scenario.expect.fires
    if "open" in values:
        assert len(stored) == values["open"]
    if "kind" in values:
        assert {row.kind for row in stored} == {values["kind"]}
    for dataset in ("google.entities",):
        if dataset in values:
            assert (await google.freshness(dataset)).completeness == values[dataset]
    if "detected_at_offset_hours" in values:
        offset = cast(int, values["detected_at_offset_hours"])
        assert {row.detected_at for row in stored} == {_at(offset)}
    persisted, replayed = await _stored_and_replayed(google, make_context)
    assert persisted == replayed


async def test_baseline_decisions_raise_no_discrepancy_at_first_start(persist: Persist) -> None:
    """C0 lifecycle: no discrepancy right after the baseline; every subject is baselined in the
    shape its current state fixes (AAP 0.5.2 "Decisions: baseline")."""
    google = await persist("c0.lifecycle_baseline.case", NOW_OFFSET)
    assert await google.discrepancies(include_resolved=True) == []
    states = await google.states()
    shapes: dict[str, str] = {}
    for state in states.values():
        (decision,) = await google.decisions_of(state)
        assert decision.origin == "baseline"
        assert decision.shape == state.shape
        shapes[state.name] = state.shape
    ids = google.scenario.ids
    expected = {
        ids.name("google", "campaign", "brand"): "own_budget",
        ids.name("google", "campaign", "member-a"): "shared_member",
        ids.name("google", "campaign", "member-b"): "shared_member",
        ids.name("google", "campaign", "member-c"): "shared_member",
        f"Shared budget {ids.external_id('google', 'budget', 'pool')}": "budget",
        f"Shared budget {ids.external_id('google', 'budget', 'pool-b')}": "budget",
    }
    assert shapes == expected
    # The brand campaign's own budget is not explicitly shared, so it is no decision subject.
    brand_budget = ids.external_id("google", "budget", "brand-budget")
    assert f"Shared budget {brand_budget}" not in shapes


async def test_status_drift_raises_one_discrepancy_and_platform_correct_closes_it(
    persist: Persist,
) -> None:
    """C0 lifecycle: a status drift raises one status discrepancy; platform_correct writes a
    decision with the platform value, keeping the rest of the shape, and closes at once."""
    google = await persist("c0.status.fires", -36)
    await google.sync(-33)
    assert await google.discrepancies() == [], "nothing changed before -30 h"
    await google.sync(NOW_OFFSET)
    (row,) = await google.discrepancies()
    brand = await google.subject("campaign", "brand")
    assert (row.subject_type, row.subject_id) == ("campaign", brand.ref.id)
    assert (row.kind, row.state) == ("status", "open")
    assert (row.decided_value, row.platform_value) == ({"status": "active"}, {"status": "paused"})
    resolved = await google.resolve(row, "platform_correct", at=NOW_OFFSET + 1)
    assert (resolved.state, resolved.resolution) == ("resolved", "platform_correct")
    assert resolved.resolved_at == _at(NOW_OFFSET + 1)
    baseline, correction = await google.decisions_of(brand)
    assert (correction.origin, correction.shape, correction.status) == (
        "resolution",
        "own_budget",
        "paused",
    )
    assert correction.daily_budget == baseline.daily_budget == Decimal("100.00")
    await google.sync(NOW_OFFSET + 3)
    assert await google.discrepancies() == []


async def test_decision_correct_waits_as_awaiting_platform_until_a_snapshot_matches(
    persist: Persist,
) -> None:
    """C0 lifecycle: decision_correct sets awaiting_platform, which auto-resolves when a later
    snapshot matches the decision; nothing is written meanwhile."""
    google = await persist("c0.lifecycle_awaiting.case", -106)
    await google.sync(-99)
    (row,) = await google.discrepancies()
    assert row.kind == "budget"
    assert row.decided_value is not None
    assert row.platform_value is not None
    assert (row.decided_value["amount_micros"], row.platform_value["amount_micros"]) == (
        100_000_000,
        120_000_000,
    )
    brand = await google.subject("campaign", "brand")
    waiting = await google.resolve(row, "decision_correct", at=-98)
    assert (waiting.state, waiting.resolution) == ("awaiting_platform", "decision_correct")
    assert [d.origin for d in await google.decisions_of(brand)] == ["baseline"]
    await google.sync(-60)
    (still,) = await google.discrepancies()
    assert (still.id, still.state) == (row.id, "awaiting_platform")
    await google.sync(NOW_OFFSET)
    assert await google.discrepancies() == []
    (closed,) = await google.discrepancies(include_resolved=True)
    assert (closed.state, closed.resolution, closed.resolved_at) == (
        "resolved",
        "decision_correct",
        _at(NOW_OFFSET),
    )


async def test_shared_budget_amount_change_is_one_budget_subject_discrepancy(
    persist: Persist,
) -> None:
    """C0 by subject type: a shared-budget amount change raises exactly one budget discrepancy
    on the budget subject and none on its members; correcting it writes only the budget."""
    google = await persist("c0.shared_budget_amount.case", -36)
    await google.sync(NOW_OFFSET)
    pool = await google.subject("budget", "pool")
    members = [await google.subject("campaign", key) for key in ("member-a", "member-b")]
    assert {state.shape for state in members} == {"shared_member"}
    assert {state.budget_id for state in members} == {pool.ref.id}
    (row,) = await google.discrepancies()
    assert (row.subject_type, row.subject_id, row.kind) == ("budget", pool.ref.id, "budget")
    assert row.platform_value is not None
    assert row.platform_value["amount_micros"] == to_micros(Decimal("550.00"))
    assert "status" not in row.platform_value
    await google.resolve(row, "platform_correct", at=NOW_OFFSET + 1)
    _, adopted = await google.decisions_of(pool)
    assert (adopted.shape, adopted.status, adopted.daily_budget) == (
        "budget",
        None,
        Decimal("550.00"),
    )
    for member in members:
        assert [d.origin for d in await google.decisions_of(member)] == ["baseline"]
    await google.sync(NOW_OFFSET + 3)
    assert await google.discrepancies() == []


async def test_member_moved_to_another_shared_budget_is_an_assignment_discrepancy(
    persist: Persist,
) -> None:
    """C0 by subject type: a member moved to another shared budget raises a member budget
    (assignment) discrepancy, never an amount one; platform_correct adopts the new budget."""
    google = await persist("c0.member_moved.case", -36)
    await google.sync(NOW_OFFSET)
    ids = google.scenario.ids
    member = await google.subject("campaign", "member-b")
    pool_b = await google.subject("budget", "pool-b")
    (row,) = await google.discrepancies()
    assert (row.subject_type, row.subject_id, row.kind) == ("campaign", member.ref.id, "budget")
    assert member.shape == "shared_member"
    assert row.decided_value is not None
    assert row.platform_value is not None
    assert row.decided_value["budget_external_id"] == ids.external_id("google", "budget", "pool")
    assert row.platform_value["budget_external_id"] == ids.external_id("google", "budget", "pool-b")
    assert not {"daily_budget", "total_budget", "amount_micros"} & {
        *row.decided_value,
        *row.platform_value,
    }
    await google.resolve(row, "platform_correct", at=NOW_OFFSET + 1)
    _, adopted = await google.decisions_of(member)
    assert (adopted.shape, adopted.status, adopted.budget_id) == (
        "shared_member",
        "active",
        pool_b.ref.id,
    )
    assert (adopted.daily_budget, adopted.total_budget) == (None, None)
    await google.sync(NOW_OFFSET + 3)
    assert await google.discrepancies() == []


async def test_untracked_campaign_adopt_platform_closes_at_once(persist: Persist) -> None:
    """C0 untracked: an entity first seen after the baseline raises untracked; adopt_platform
    closes it at once with a decision in the subject's shape."""
    google = await persist("c0.untracked_campaign.fires", -60)
    await google.sync(NOW_OFFSET)
    late = await google.subject("campaign", "late")
    (row,) = await google.discrepancies()
    assert (row.subject_id, row.kind, row.decided_value) == (late.ref.id, "untracked", None)
    assert late.current_decision is None
    resolved = await google.resolve(row, "adopt_platform", at=NOW_OFFSET + 1)
    assert (resolved.state, resolved.resolution) == ("resolved", "adopt_platform")
    (adopted,) = await google.decisions_of(late)
    assert (adopted.origin, adopted.shape, adopted.status, adopted.daily_budget) == (
        "resolution",
        "own_budget",
        "active",
        Decimal("100.00"),
    )
    await google.sync(NOW_OFFSET + 3)
    assert await google.discrepancies() == []


async def test_untracked_record_intended_waits_then_closes_when_the_platform_matches(
    persist: Persist,
) -> None:
    """C0 untracked: record_intended takes an intended state complete for the shape, then waits
    as awaiting_platform and auto-closes when the platform matches."""
    google = await persist("c0.untracked_campaign.fires", -60)
    await google.sync(-45)
    (row,) = await google.discrepancies()
    assert row.kind == "untracked"
    intended = {"status": "active", "daily_budget": "100.00"}
    waiting = await google.resolve(row, "record_intended", intended, at=-40)
    assert (waiting.state, waiting.resolution) == ("awaiting_platform", "record_intended")
    late = await google.subject("campaign", "late")
    (recorded,) = await google.decisions_of(late)
    assert (recorded.shape, recorded.status, recorded.daily_budget) == (
        "own_budget",
        "active",
        Decimal("100.00"),
    )
    await google.sync(-36)
    assert await google.discrepancies() == []
    (closed,) = await google.discrepancies(include_resolved=True)
    assert (closed.state, closed.resolution) == ("resolved", "record_intended")


async def _resolve_api(
    client: httpx.AsyncClient,
    row: Discrepancy,
    resolution: str,
    intended: Mapping[str, object] | None = None,
) -> httpx.Response:
    """``POST /api/v1/discrepancies/{id}/resolve`` as the resolver UI sends it."""
    body: dict[str, object] = {"resolution": resolution}
    if intended is not None:
        body["intended_state"] = dict(intended)
    return await client.post(f"/api/v1/discrepancies/{row.id}/resolve", json=body)


def _assert_refused(response: httpx.Response, code: str) -> None:
    """A 422 in the API's error shape with the domain's machine code."""
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == code, error
    assert error["message"]


def _by_subject(rows: Iterable[Discrepancy], state: SubjectState) -> Discrepancy:
    (row,) = [r for r in rows if (r.subject_type, r.subject_id) == (state.ref.type, state.ref.id)]
    return row


async def test_untracked_shared_budget_and_member_accept_only_their_shape_through_the_api(
    persist: Persist, authed_client: httpx.AsyncClient
) -> None:
    """C0 by subject type: record_intended on an untracked DAILY budget accepts {daily_budget}
    and rejects status or total_budget with 422; on a shared member it accepts {status} only.
    A resolution not valid for the (kind, shape) pair is 422 as well."""
    google = await persist("c0.untracked_budget.case", -60)
    await google.sync(NOW_OFFSET)
    expect = google.scenario.expect.values
    pool = await google.subject("budget", "pool-late")
    member = await google.subject("campaign", "late-member")
    assert (pool.shape, pool.budget_period, member.shape) == ("budget", "DAILY", "shared_member")
    rows = await google.discrepancies()
    assert {row.kind for row in rows} == {"untracked"}
    on_budget, on_member = _by_subject(rows, pool), _by_subject(rows, member)

    listed = await authed_client.get("/api/v1/discrepancies")
    assert listed.status_code == 200, listed.text
    offered = {item["id"]: item["resolutions"] for item in listed.json()}
    assert offered == {
        on_budget.id: list(expect["resolutions"]),
        on_member.id: list(expect["resolutions"]),
    }

    # The UI shows the 422 as it is, so it names the resolutions in words (AAP 0.5.5).
    for resolution, label in (
        ("platform_correct", "Platform is correct"),
        ("decision_correct", "Decision is correct"),
        ("acknowledge_removal", "Acknowledge removal"),
    ):
        response = await _resolve_api(authed_client, on_budget, resolution)
        _assert_refused(response, "resolution_not_allowed")
        assert response.json()["error"]["message"] == (
            f"{label} is not valid for an untracked discrepancy on {pool.name}; the valid "
            "resolutions are Adopt platform state, Record intended state."
        )
    response = await _resolve_api(
        authed_client, on_budget, "adopt_platform", {"daily_budget": "250.00"}
    )
    _assert_refused(response, "intended_state_invalid")
    assert response.json()["error"]["message"] == (
        f"An intended_state for {pool.name} is sent only with Record intended state, not with "
        "Adopt platform state."
    )
    amount = "250.00"
    rejected = cast(tuple[str, ...], expect["record_intended_rejects"])
    assert rejected == ("status", "total_budget")
    # A budget subject has no status, and a DAILY budget owns only its daily amount: each
    # rejected field is refused alone and beside the accepted one.
    for refused in (
        {"status": "active"},
        {"status": "active", "daily_budget": amount},
        {"total_budget": amount},
        {"daily_budget": amount, "total_budget": amount},
    ):
        assert set(refused) - {"daily_budget"} <= set(rejected)
        _assert_refused(
            await _resolve_api(authed_client, on_budget, "record_intended", refused),
            "intended_state_invalid",
        )
    response = await _resolve_api(
        authed_client, on_member, "record_intended", {"status": "active", "daily_budget": "10"}
    )
    _assert_refused(response, "intended_state_invalid")
    assert response.json()["error"]["message"] == (
        f"The intended state of {member.name}, a campaign on a shared budget, takes exactly "
        "these fields: status."
    )

    accepted_budget = dict.fromkeys(
        cast(tuple[str, ...], expect["record_intended_accepts"]), amount
    )
    response = await _resolve_api(authed_client, on_budget, "record_intended", accepted_budget)
    assert response.status_code == 200, response.text
    assert (response.json()["state"], response.json()["shape"]) == ("awaiting_platform", "budget")
    member_fields = cast(tuple[str, ...], expect["member_record_intended_accepts"])
    accepted_member = dict.fromkeys(member_fields, "active")
    response = await _resolve_api(authed_client, on_member, "record_intended", accepted_member)
    assert response.status_code == 200, response.text
    assert (response.json()["state"], response.json()["shape"]) == (
        "awaiting_platform",
        "shared_member",
    )
    (budget_decision,) = await google.decisions_of(pool)
    assert (budget_decision.status, budget_decision.daily_budget) == (None, Decimal(amount))
    (member_decision,) = await google.decisions_of(member)
    assert (member_decision.status, member_decision.budget_id) == ("active", pool.ref.id)
    assert (member_decision.daily_budget, member_decision.total_budget) == (None, None)


async def test_untracked_own_budget_campaign_needs_a_complete_intended_state(
    persist: Persist, authed_client: httpx.AsyncClient
) -> None:
    """C0 untracked: the intended state must be complete for the campaign's own_budget shape
    (status and its daily budget); an incomplete one is 422 and nothing is written."""
    google = await persist("c0.untracked_campaign.fires", -60)
    await google.sync(NOW_OFFSET)
    (row,) = await google.discrepancies()
    fields = cast(tuple[str, ...], google.scenario.expect.values["record_intended_fields"])
    complete = {"status": "active", "daily_budget": "100.00"}
    assert tuple(complete) == fields
    for missing in fields:
        partial = {key: value for key, value in complete.items() if key != missing}
        _assert_refused(
            await _resolve_api(authed_client, row, "record_intended", partial),
            "intended_state_invalid",
        )
    _assert_refused(
        await _resolve_api(
            authed_client, row, "record_intended", {**complete, "total_budget": "9"}
        ),
        "intended_state_invalid",
    )
    late = await google.subject("campaign", "late")
    response = await _resolve_api(authed_client, row, "record_intended")
    _assert_refused(response, "intended_state_invalid")
    assert response.json()["error"]["message"] == (
        f"Record intended state needs an intended_state for {late.name}."
    )
    assert await google.decisions_of(late) == []
    response = await _resolve_api(authed_client, row, "record_intended", complete)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "awaiting_platform"
    assert response.json()["resolutions"] == ["adopt_platform", "record_intended"]


async def test_removed_campaign_is_snapshotted_as_ended_not_missing(persist: Persist) -> None:
    """C0 missing: Q-G2b returning a tracked campaign as REMOVED gives an ended snapshot and a
    status discrepancy against its active decision, never a missing one."""
    google = await persist("c0.removed.case", -36)
    await google.sync(NOW_OFFSET)
    brand = await google.subject("campaign", "brand")
    snapshot = brand.latest_snapshot
    assert snapshot is not None
    assert snapshot.captured_at == _at(NOW_OFFSET)
    expect = google.scenario.expect.values
    assert (snapshot.status, snapshot.payload["raw_status"]) == (
        expect["platform"],
        expect["raw_status"],
    )
    (row,) = await google.discrepancies()
    assert (row.kind, row.decided_value, row.platform_value) == (
        "status",
        {"status": "active"},
        {"status": "ended"},
    )
    assert (await google.freshness("google.entities")).completeness == "complete"


@pytest.mark.parametrize(
    "faulted", ["c0.missing.does_not_fire", "c0.missing_qg2b_failed.case"], ids=str
)
async def test_missing_is_raised_only_after_a_complete_enumeration(
    persist: Persist, faulted: str
) -> None:
    """C0 missing: a deferred Q-G2 keyset page or a failed Q-G2b chunk raises no missing and
    leaves google.entities incomplete; the next complete enumeration raises it."""
    google = await persist("c0.missing.fires", -36)
    result = await google.sync(NOW_OFFSET, world_of=faulted, cut_short=True)
    assert result.completeness == "incomplete"
    row = await google.freshness("google.entities")
    assert row.completeness == SCENARIOS[faulted].expect.values["google.entities"] == "incomplete"
    assert row.reason is not None
    assert row.reason.startswith("interrupted: google search")
    assert await google.discrepancies(include_resolved=True) == []
    await google.sync(NOW_OFFSET + 3)
    (missing,) = await google.discrepancies()
    brand = await google.subject("campaign", "brand")
    assert (missing.subject_id, missing.kind, missing.platform_value) == (
        brand.ref.id,
        "missing",
        None,
    )
    assert missing.decided_value is not None
    assert missing.decided_value["status"] == "active"
    assert (await google.freshness("google.entities")).completeness == "complete"


LINKEDIN_FIRES: Final = "c0.linkedin_cut_short.fires"
"""``li-brand`` leaves LinkedIn's all-status campaign list at -30 h; every search completes."""

LINKEDIN_CUT_SHORT: Final = "c0.linkedin_cut_short.does_not_fire"
"""The same world, with every ``adCampaigns`` search failing 503 through the retries."""


async def _linkedin_baseline(persist: Persist, name: str) -> tuple[PersistedGoogle, SubjectState]:
    """Bootstrap ``name``'s LinkedIn source from the fault-free world at -36 h.

    The bootstrap precedes ``li-brand``'s disappearance at -30 h, so its first entity sync
    snapshots and baselines the campaign as active and raises nothing (C0 lifecycle).

    Returns:
        The persisted LinkedIn source and ``li-brand``'s subject state after the baseline.
    """
    linkedin = await persist(name, -36, world_of=LINKEDIN_FIRES, source="linkedin")
    assert await linkedin.discrepancies(include_resolved=True) == []
    brand = await linkedin.subject("campaign", "li-brand")
    assert brand.source == "linkedin"
    assert brand.latest_snapshot is not None
    assert brand.latest_snapshot.captured_at == _at(-36)
    (decision,) = await linkedin.decisions_of(brand)
    assert (decision.origin, decision.status) == ("baseline", "active")
    return linkedin, brand


async def test_linkedin_search_cut_short_raises_no_missing_until_a_complete_search(
    persist: Persist, make_context: ContextFactory
) -> None:
    """C0 missing (LinkedIn safe absence): an all-status campaign search cut short by a
    transient error raises no missing, leaves linkedin.entities incomplete with the
    interruption as its reason, keeps the previous snapshot and fails the job with that
    reason; the next complete search raises missing on the absent tracked campaign."""
    linkedin, brand = await _linkedin_baseline(persist, LINKEDIN_CUT_SHORT)
    baseline = brand.latest_snapshot
    assert baseline is not None
    assert [row.id for row in await linkedin.campaign_snapshots_at(-36)] == [baseline.id]
    result = await linkedin.sync(NOW_OFFSET, world_of=LINKEDIN_CUT_SHORT, cut_short=True)
    # The interrupted listing is recorded as an incomplete success of the dataset, while the
    # job fails with the listing's error.
    assert (result.source, result.status, result.completeness) == (
        "linkedin",
        "failed",
        "incomplete",
    )
    assert result.error is not None
    assert result.error.startswith("linkedin ad_campaigns: SERVICE_UNAVAILABLE")
    row = await linkedin.freshness("linkedin.entities")
    expected = linkedin.scenario.expect.values["linkedin.entities"]
    assert row.completeness == expected == "incomplete"
    assert row.reason is not None
    assert row.reason.startswith("linkedin ad_campaigns: SERVICE_UNAVAILABLE")
    assert await linkedin.discrepancies(include_resolved=True) == []
    assert await _stored_and_replayed(linkedin, make_context) == ([], [])
    # The previous snapshot stands: nothing was observed of any campaign at this sync.
    kept = (await linkedin.subject("campaign", "li-brand")).latest_snapshot
    assert kept is not None
    assert (kept.id, kept.captured_at) == (baseline.id, _at(-36))
    assert await linkedin.campaign_snapshots_at(NOW_OFFSET) == []

    complete = await linkedin.sync(NOW_OFFSET + 3)
    assert complete.completeness == "complete"
    (missing,) = await linkedin.discrepancies()
    assert (missing.subject_type, missing.subject_id, missing.kind, missing.state) == (
        "campaign",
        brand.ref.id,
        "missing",
        "open",
    )
    assert missing.platform_value is None
    assert missing.decided_value is not None
    assert missing.decided_value["status"] == "active"
    assert missing.detected_at == _at(NOW_OFFSET + 3)
    row = await linkedin.freshness("linkedin.entities")
    assert (row.completeness, row.reason) == ("complete", None)


async def test_linkedin_complete_search_raises_missing_as_the_catalogue_replays(
    persist: Persist, make_context: ContextFactory
) -> None:
    """C0 missing (LinkedIn): a tracked campaign absent from a complete all-status search
    raises one missing discrepancy, as the scenario expects and the catalogue replays."""
    linkedin, brand = await _linkedin_baseline(persist, LINKEDIN_FIRES)
    result = await linkedin.sync(NOW_OFFSET)
    assert (result.source, result.completeness) == ("linkedin", "complete")
    (row,) = await linkedin.discrepancies()
    values = linkedin.scenario.expect.values
    assert (row.kind, brand.source) == (values["kind"], values["source"]) == ("missing", "linkedin")
    assert (row.subject_type, row.subject_id, row.state, row.platform_value) == (
        "campaign",
        brand.ref.id,
        "open",
        None,
    )
    assert row.decided_value is not None
    assert row.decided_value["status"] == "active"
    freshness = await linkedin.freshness("linkedin.entities")
    assert (freshness.completeness, freshness.reason) == ("complete", None)
    stored, replayed = await _stored_and_replayed(linkedin, make_context)
    assert len(stored) == 1
    assert stored == replayed


@pytest.mark.parametrize(
    ("name", "returns"),
    [("c0.acknowledge.fires", True), ("c0.acknowledge.does_not_fire", False)],
    ids=["returned-by-a-complete-enumeration", "stays-absent"],
)
async def test_acknowledge_removal_writes_a_tombstone_and_resolves_at_once(
    persist: Persist, name: str, returns: bool
) -> None:
    """C0 acknowledgement: acknowledge_removal writes a tombstone and resolves with no later
    snapshot; no new missing is raised while the entity stays absent; when a complete
    enumeration returns it, a status discrepancy is raised against the tombstone."""
    google = await persist(name, -106)
    await google.sync(-99)
    (row,) = await google.discrepancies()
    assert row.kind == "missing"
    resolved = await google.resolve(row, "acknowledge_removal", at=-98)
    assert (resolved.state, resolved.resolution) == ("resolved", "acknowledge_removal")
    brand = await google.subject("campaign", "brand")
    _, tombstone = await google.decisions_of(brand)
    assert (tombstone.tombstone, tombstone.status, tombstone.origin) == (
        True,
        "ended",
        "resolution",
    )
    for offset in (-96, -60, -33):
        await google.sync(offset)
        assert await google.discrepancies() == [], offset
    await google.sync(NOW_OFFSET)
    every = await google.discrepancies(include_resolved=True)
    missing_rows = [r for r in every if r.kind == "missing"]
    assert [r.id for r in missing_rows] == [row.id], "no missing raised after the tombstone"
    open_rows = await google.discrepancies()
    if not returns:
        values = google.scenario.expect.values
        assert values["missing_raised_after_tombstone"] is (len(missing_rows) > 1)
        assert len(open_rows) == values["open"] == 0
        return
    (reappeared,) = open_rows
    assert (reappeared.kind, reappeared.decided_value, reappeared.platform_value) == (
        "status",
        {"status": "ended", "tombstone": True},
        {"status": "active"},
    )


async def test_a_tombstoned_campaign_returned_as_removed_contradicts_its_tombstone(
    persist: Persist,
) -> None:
    """C0 acknowledgement: a complete enumeration returning the tombstoned campaign, even as
    REMOVED (snapshotted as ended), raises a status discrepancy against the tombstone."""
    google = await persist("c0.tombstone_ended.fires", -106)
    await google.sync(-99)
    (row,) = await google.discrepancies()
    await google.resolve(row, "acknowledge_removal", at=-98)
    await google.sync(-60)
    assert await google.discrepancies() == []
    await google.sync(NOW_OFFSET)
    (reappeared,) = await google.discrepancies()
    assert (reappeared.kind, reappeared.decided_value, reappeared.platform_value) == (
        "status",
        {"status": "ended", "tombstone": True},
        {"status": "ended"},
    )


@pytest.mark.parametrize(
    ("name", "closes"),
    [
        ("c0.decision_correct_missing.fires", False),
        ("c0.decision_correct_missing.does_not_fire", True),
    ],
    ids=["stays-absent", "returned-and-matching"],
)
async def test_decision_correct_on_missing_waits_for_a_complete_enumeration(
    persist: Persist, name: str, closes: bool
) -> None:
    """C0 acknowledgement: decision_correct on missing stays awaiting_platform until a complete
    enumeration returns the entity with a snapshot matching the decision."""
    google = await persist(name, -106)
    await google.sync(-99)
    (row,) = await google.discrepancies()
    assert row.kind == "missing"
    waiting = await google.resolve(row, "decision_correct", at=-98)
    assert waiting.state == "awaiting_platform"
    await google.sync(-60)
    (still,) = await google.discrepancies()
    assert (still.id, still.kind, still.state) == (row.id, "missing", "awaiting_platform")
    await google.sync(NOW_OFFSET)
    if closes:
        assert await google.discrepancies() == []
        (closed,) = await google.discrepancies(include_resolved=True)
        assert (closed.state, closed.resolution) == ("resolved", "decision_correct")
        # Closed by the returned entity's matching snapshot, not by a resolution write.
        assert google.scenario.expect.values["resolved_by"] == "matching snapshot"
        assert closed.resolved_at == _at(NOW_OFFSET)
        brand = await google.subject("campaign", "brand")
        assert [d.origin for d in await google.decisions_of(brand)] == ["baseline"]
        assert brand.latest_snapshot is not None
        assert brand.latest_snapshot.captured_at == _at(NOW_OFFSET)
    else:
        (left,) = await google.discrepancies()
        assert (left.id, left.state) == (row.id, "awaiting_platform")


async def _campaign_groups(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """The Campaigns groups of ``GET /api/v1/recommendations`` (the latest default run)."""
    page = await client.get("/api/v1/recommendations")
    assert page.status_code == 200, page.text
    (section,) = [s for s in page.json()["sections"] if s["key"] == CAMPAIGNS]
    return cast(list[dict[str, Any]], section["groups"])


async def test_c0_is_never_held_pinned_first_and_exempt_from_feedback(
    persist: Persist, db: Sessions, authed_client: httpx.AsyncClient
) -> None:
    """C0 lifecycle: in a stored run the C0 item is never held and is pinned first in
    Campaigns, and dismiss, snooze and done on it are refused with 422."""
    google = await persist("c0.lifecycle_exempt.case", -36)
    await google.sync(NOW_OFFSET)
    (row,) = await google.discrepancies()
    expect = google.scenario.expect.values
    assert row.kind == expect["kind"]
    window = await default_window(now=google.clock.at)
    run = await run_analysis(window, "default", "manual", now=google.clock.at)
    assert run.status == "succeeded"
    async with db() as session:
        stored = list(
            (
                await session.execute(
                    select(Recommendation)
                    .where(Recommendation.run_id == run.id)
                    .order_by(Recommendation.rank)
                )
            ).scalars()
        )
    in_campaigns = [item for item in stored if item.section == CAMPAIGNS]
    assert in_campaigns, "the run stored no Campaigns item"
    first = in_campaigns[0]
    assert (first.rule_id, first.group, first.held, first.held_reason) == (
        C0,
        GROUP_DISCREPANCIES,
        expect["held"],
        None,
    )
    assert first.decision_subject is None
    assert first.evidence["extra"]["discrepancy_id"] == row.id
    assert all(item.rule_id in (C3A, C3B) for item in stored if item.group == GROUP_LAUNCH)

    groups = await _campaign_groups(authed_client)
    assert [group["key"] for group in groups] == list(GROUP_ORDER[CAMPAIGNS])
    pinned = groups[0]["items"][0]
    assert (pinned["id"], pinned["rule_id"], pinned["held"]) == (first.id, C0, False)
    assert pinned["current"]["held_now"] is False
    assert pinned["current"]["hidden_by"] is None
    assert pinned.get("decision_subject") is None
    for kind in ("dismiss", "snooze", "done"):
        refused = await authed_client.post(
            f"/api/v1/recommendations/{first.id}/feedback", json={"kind": kind}
        )
        assert refused.status_code == expect["feedback_status"], refused.text
        assert refused.json()["error"]["code"] == "feedback_not_allowed"
    # The refusals stored nothing: the item is not hidden and its discrepancy is untouched.
    (unchanged,) = (await _campaign_groups(authed_client))[0]["items"]
    assert unchanged["current"]["hidden_by"] is None
    assert [(r.id, r.state) for r in await google.discrepancies()] == [(row.id, "open")]


# ---------------------------------------------------------------------------
# Phase 4: C1 Pause and C1b Pipeline pause (AAP 0.5.4.4, 0.5.4.1)
# ---------------------------------------------------------------------------


def test_c1_fires_at_three_expected_outcomes_and_not_at_two_point_nine(
    make_context: ContextFactory,
) -> None:
    """C1: 3.0 expected outcomes fire "Pause" / 2.9 are withheld with observed and required."""
    scenario = SCENARIOS["c1.expected.fires"]
    ctx = make_context(scenario)
    minimum = _threshold(ctx, "c1.min_expected_outcomes")
    item = _only(campaigns.evaluate(ctx), C1)
    dud = scenario.ids.id("google", "campaign", "dud")
    assert (item.section, item.group, item.action) == (CAMPAIGNS, GROUP_PAUSE, PAUSE_ACTION)
    assert item.decision_subject == DecisionSubject("campaign", dud)
    assert item.increases_spend_on == ()
    metrics = evidence_metrics(item)
    assert metrics["outcomes"] == 0
    assert metrics["spend_days"] == metrics["window_days"] == ctx.window.days
    expected = cast(Decimal, metrics["expected_outcomes"])
    assert expected >= minimum
    assert expected.quantize(Decimal("0.1")) == Decimal(
        cast(str, scenario.expect.values["expected_outcomes"])
    )
    spend = cast(Decimal, metrics["spend"])
    (baseline,) = item.evidence.baselines
    assert baseline.key == "baseline_cost_per_outcome"
    # No metric pairs with the baseline, so it is money in the spend's account currency itself.
    assert (baseline.unit, baseline.currency) == (
        "currency",
        item.evidence.metric("spend").currency,
    )
    assert baseline.currency is not None
    # Expected outcomes = spend at the account's baseline cost per outcome (AAP 0.5.4.1).
    assert abs(spend / cast(Decimal, baseline.value) - expected) < Decimal("0.0001")
    assert item.rank_key == (-spend,)
    assert item.evidence.sample is not None
    assert (item.evidence.sample.observed, item.evidence.sample.required) == (expected, minimum)
    assert item.evidence.extra["proposed_status"] == "paused"
    assert dict(item.evidence.thresholds)["c1.min_expected_outcomes"] == ctx.thresholds.value(
        "c1.min_expected_outcomes"
    )
    # A Google campaign without a verified CRM link shows no QL count, never a zero.
    assert FLAG_NO_CRM_LINK in item.flag_codes
    assert "ql" not in metrics
    assert FINDING_NO_CRM_LINK in _codes(campaigns.evaluate(ctx))

    twin = make_context("c1.expected.does_not_fire")
    result = campaigns.evaluate(twin)
    assert _items(result, C1) == []
    line = _only_withheld(result, C1)
    assert line.observed is not None
    assert line.required is not None
    assert line.observed < minimum == line.required
    assert Decimal(line.observed).quantize(Decimal("0.1")) == Decimal(
        cast(str, SCENARIOS["c1.expected.does_not_fire"].expect.values["expected_outcomes"])
    )
    assert line.reason == f"expected conversions 2.9 of {minimum} required"


@pytest.mark.parametrize("unreported", ["known_zero", "one_day", "every_day"])
def test_c1_never_pauses_a_campaign_whose_outcomes_are_unreported(
    make_context: ContextFactory, unreported: str
) -> None:
    """C1 and unknown outcomes: unreported conversions are unavailable, never 0 (AAP 0.11.1).

    ``c1.expected.fires`` pauses dud, which spends on every window day with a known 0
    conversions at 3.0 expected. With dud's conversions unreported (``None``) on its first
    window day, or on every day, and 0 on the rest, its spend and spending days are unchanged
    but its outcomes are unavailable: C1 neither pauses it nor lists it as withheld. dud is
    part of its own account baseline, so that baseline is unavailable as well.
    """
    scenario = SCENARIOS["c1.expected.fires"]
    base = make_context(scenario)
    dud = scenario.ids.id("google", "campaign", "dud")
    days = sorted(base.campaign_days[dud], key=lambda day: day.date)
    assert len(days) == base.window.days
    assert all(day.conversions == 0 and day.cost > 0 for day in days)
    count = {"known_zero": 0, "one_day": 1, "every_day": len(days)}[unreported]
    changed = tuple(
        dataclasses.replace(day, conversions=None) if n < count else day
        for n, day in enumerate(days)
    )
    ctx = dataclasses.replace(base, campaign_days={**base.campaign_days, dud: changed}, cache={})
    result = campaigns.evaluate(ctx)
    paused = [item for item in _items(result, C1) if item.entity_ref.id == dud]
    if not count:
        (item,) = paused
        assert item.action == PAUSE_ACTION
        assert evidence_metrics(item)["outcomes"] == 0
        assert _withheld(result, C1) == []
        return
    assert paused == []
    assert _withheld(result, C1) == []
    assert [day.conversions for day in ctx.campaign_days[dud]].count(None) == count


def test_c1_needs_spend_on_seventy_percent_of_window_days(make_context: ContextFactory) -> None:
    """C1: spend on 70% of the window's days fires / 60% does not."""
    ctx = make_context("c1.spend_days.fires")
    share = _threshold(ctx, "c1.min_spend_day_share")
    item = _only(campaigns.evaluate(ctx), C1)
    metrics = evidence_metrics(item)
    assert metrics["window_days"] == ctx.window.days == 20
    assert Decimal(cast(int, metrics["spend_days"])) / ctx.window.days >= share
    assert metrics["spend_day_share"] == Decimal("0.7000")

    twin = make_context("c1.spend_days.does_not_fire")
    result = campaigns.evaluate(twin)
    assert _items(result, C1) == []
    line = _only_withheld(result, C1)
    required = (share * twin.window.days).to_integral_value(rounding="ROUND_CEILING")
    assert (line.observed, line.required, line.unit) == (12, required, "days")
    assert line.reason == f"spend days 12 of {required} required (70% of 20 window days)"


def test_c1_needs_a_window_of_fourteen_days(make_context: ContextFactory) -> None:
    """C1: a 14-day window fires / a 13-day window is withheld."""
    ctx = make_context("c1.window.fires")
    minimum = ctx.thresholds.int("c1.min_window_days")
    assert ctx.window.days == minimum == 14
    assert _only(campaigns.evaluate(ctx), C1).action == PAUSE_ACTION
    twin = make_context("c1.window.does_not_fire")
    result = campaigns.evaluate(twin)
    assert _items(result, C1) == []
    line = _only_withheld(result, C1)
    assert (line.observed, line.required) == (twin.window.days, minimum)
    assert line.reason == f"window 13 days of {minimum} required"


VETO_COUNT_METRICS: Final = (
    "leads",
    "matured_leads",
    "matched_leads",
    "unresolved",
    "eligible_contacts",
    "mql",
    "ql",
    "meetings",
)
"""Pipeline counts every C1b and Hold card shows side by side, never blended (AAP 0.5.5)."""


def test_c1b_fires_on_twenty_eligible_contacts_without_ql_or_meetings(
    make_context: ContextFactory,
) -> None:
    """C1b: 20 eligible matured contacts with QL 0 and meetings 0 fire / 19 are withheld."""
    scenario = SCENARIOS["c1b.eligible.fires"]
    ctx = make_context(scenario)
    minimum = ctx.thresholds.int("crm.veto_min_leads")
    assert ctx.window.days >= ctx.thresholds.int("c1b.min_window_days")
    item = _only(campaigns.evaluate(ctx), C1B)
    campaign = scenario.ids.id("linkedin", "campaign", "li-pipeline")
    assert (item.group, item.action) == (GROUP_PAUSE, PIPELINE_PAUSE_ACTION)
    assert item.decision_subject == DecisionSubject("campaign", campaign)
    metrics = evidence_metrics(item)
    assert [key for key in VETO_COUNT_METRICS if key in metrics] == list(VETO_COUNT_METRICS)
    # A pause card shows spend, outcomes, expected outcomes and CRM QL (AAP 0.5.5), the platform
    # outcomes apart from the CRM counts; expected outcomes are the spend at the account's
    # baseline cost per outcome (AAP 0.5.4.1).
    assert {"spend", "outcomes", "expected_outcomes", "ql"} <= set(metrics)
    (baseline,) = item.evidence.baselines
    assert baseline.key == "baseline_cost_per_outcome"
    # No metric pairs with the baseline, so it is money in the spend's account currency itself.
    assert (baseline.unit, baseline.currency) == (
        "currency",
        item.evidence.metric("spend").currency,
    )
    assert baseline.currency is not None
    spend = cast(Decimal, metrics["spend"])
    expected = cast(Decimal, metrics["expected_outcomes"])
    assert abs(spend / cast(Decimal, baseline.value) - expected) < Decimal("0.0001")
    assert metrics["outcomes"] is not None
    assert (metrics["eligible_contacts"], metrics["ql"], metrics["meetings"]) == (minimum, 0, 0)
    assert metrics["matured_leads"] == metrics["matched_leads"] == minimum
    assert (metrics["unresolved"], metrics["unknown_evidence_share"]) == (0, Decimal("0.0000"))
    assert item.evidence.sample is not None
    assert (item.evidence.sample.observed, item.evidence.sample.required) == (minimum, minimum)
    assert item.flag_codes == ()

    result = campaigns.evaluate(make_context("c1b.eligible.does_not_fire"))
    assert _items(result, C1B) == []
    line = _only_withheld(result, C1B)
    assert (line.observed, line.required, line.unit) == (19, minimum, "contacts")
    assert line.reason == f"eligible contacts 19 of {minimum} required"


def test_c1b_is_withheld_at_the_attribution_floor(make_context: ContextFactory) -> None:
    """C1b: unmatched matured leads below the 10% floor fire / at or above it are withheld,
    because the zero may be an artefact of unmatched contacts."""
    ctx = make_context("c1b.floor.fires")
    floor = _threshold(ctx, "attribution.floor_share")
    item = _only(campaigns.evaluate(ctx), C1B)
    assert cast(Decimal, evidence_metrics(item)["unresolved_share"]) < floor

    twin = make_context("c1b.floor.does_not_fire")
    result = campaigns.evaluate(twin)
    assert _items(result, C1B) == []
    line = _only_withheld(result, C1B)
    assert line.reason.startswith("attribution floor")
    assert line.observed is not None
    assert line.observed >= floor
    assert line.required == floor
    (scale,) = _items(result, C2)
    assert {FLAG_ATTRIBUTION_FLOOR, FLAG_PIPELINE_UNAVAILABLE} <= set(scale.flag_codes)
    assert FINDING_PIPELINE_UNAVAILABLE in _codes(result)


def test_c1b_is_withheld_while_any_crm_evidence_is_unknown(make_context: ContextFactory) -> None:
    """C1b: known CRM evidence fires / an unknown contact-to-meeting fetch withholds it, since
    a failed association read can never be counted as zero meetings."""
    known = _only(campaigns.evaluate(make_context("c1b.unknown_evidence.fires")), C1B)
    assert known.action == PIPELINE_PAUSE_ACTION
    result = campaigns.evaluate(make_context("c1b.unknown_evidence.does_not_fire"))
    assert _items(result, C1B) == []
    line = _only_withheld(result, C1B)
    assert line.reason.startswith("unknown CRM evidence")
    assert (line.observed, line.required) == (Decimal("1.0000"), 0)
    (scale,) = _items(result, C2)
    assert {FLAG_UNKNOWN_CRM_EVIDENCE, FLAG_PIPELINE_UNAVAILABLE} <= set(scale.flag_codes)


def test_c1b_needs_a_twenty_eight_day_window(make_context: ContextFactory) -> None:
    """C1b: a window shorter than c1b.min_window_days is withheld with observed and required."""
    ctx = make_context("c2.cap_evidence.fires")
    minimum = ctx.thresholds.int("c1b.min_window_days")
    line = _only_withheld(campaigns.evaluate(ctx), C1B)
    assert (line.observed, line.required) == (ctx.window.days, minimum)
    assert line.reason == f"window {ctx.window.days} days of {minimum} required"


def test_veto_cohort_young_leads_reaching_ql_never_enter_the_veto(
    make_context: ContextFactory,
) -> None:
    """Veto cohort: 20 eligible contacts with QL 0, plus young leads whose contacts reach QL 2,
    still veto: outcomes of leads younger than crm.lead_maturity_days never enter it."""
    scenario = SCENARIOS["veto.cohort.case"]
    ctx = make_context(scenario)
    values = scenario.expect.values
    campaign = scenario.ids.id("linkedin", "campaign", "li-pipeline")
    veto = attribution_for(ctx).veto[campaign]
    assert (veto.eligible_contacts, veto.ql, veto.veto_fires) == (
        values["eligible_contacts"],
        0,
        True,
    )
    result = campaigns.evaluate(ctx)
    hold = _only(result, C2)
    assert (hold.action, hold.evidence.extra["outcome"]) == (HOLD_ACTION, values["outcome"])
    metrics = evidence_metrics(hold)
    assert (metrics["ql"], metrics["young_leads"], metrics["young_ql"]) == (
        values["matured_ql"],
        2,
        values["young_ql"],
    )
    assert _only(result, C1B).action == PIPELINE_PAUSE_ACTION
    assert not [item for item in budget.evaluate(ctx).candidates if item.rule_id == B1]


@pytest.mark.parametrize(
    ("name", "available"),
    [("veto.unresolved.fires", False), ("veto.unresolved.does_not_fire", True)],
    ids=["three-of-25-unresolved", "two-of-25-unresolved"],
)
def test_unresolved_matured_leads_stay_in_the_veto_denominator(
    make_context: ContextFactory, name: str, available: bool
) -> None:
    """Unresolved matured leads: 25 matured with 3 unresolved (12%) make the pipeline
    unavailable although every matched lead is fully resolved / 2 unresolved (8%) evaluate the
    veto; matched, unresolved and eligible counts are shown separately."""
    scenario = SCENARIOS[name]
    ctx = make_context(scenario)
    values = scenario.expect.values
    floor = _threshold(ctx, "attribution.floor_share")
    campaign = scenario.ids.id("linkedin", "campaign", "li-pipeline")
    veto = attribution_for(ctx).veto[campaign]
    assert (veto.matured_leads, veto.matched_leads, veto.unresolved_leads) == (
        values["matured_leads"],
        values["matched_leads"],
        values["unresolved"],
    )
    assert veto.unresolved_share == Decimal(veto.unresolved_leads) / Decimal(veto.matured_leads)
    assert veto.unknown_evidence_contacts == 0, "every matched lead is fully resolved"
    assert (veto.unresolved_share >= floor) is not available
    assert veto.available is available
    result = campaigns.evaluate(ctx)
    item = _only(result, C2)
    metrics = evidence_metrics(item)
    for key in ("matured_leads", "matched_leads", "unresolved", "unresolved_share"):
        assert numeric(metrics[key]) == numeric(values[key]), key
    assert item.evidence.extra["outcome"] == values["outcome"]
    if available:
        assert veto.veto_fires
        assert metrics["eligible_contacts"] == values["eligible_contacts"]
        assert FLAG_PIPELINE_UNAVAILABLE not in item.flag_codes
    else:
        assert not veto.veto_fires
        assert FLAG_PIPELINE_UNAVAILABLE in item.flag_codes
        assert FINDING_PIPELINE_UNAVAILABLE in _codes(result)


def test_two_leads_of_one_contact_count_once_among_eligible_contacts(
    make_context: ContextFactory,
) -> None:
    """Unresolved matured leads: two leads from one contact count once among eligible contacts
    but twice in the unresolved share's denominator."""
    scenario = SCENARIOS["veto.repeat_contact.case"]
    ctx = make_context(scenario)
    values = scenario.expect.values
    campaign = scenario.ids.id("linkedin", "campaign", "li-pipeline")
    veto = attribution_for(ctx).veto[campaign]
    assert (veto.matured_leads, veto.matched_leads, veto.eligible_contacts) == (
        values["matured_leads"],
        values["matched_leads"],
        values["eligible_contacts"],
    )
    assert veto.matured_leads == 2 * veto.eligible_contacts
    assert veto.unresolved_share == 0
    result = campaigns.evaluate(ctx)
    line = _only_withheld(result, C1B)
    assert (line.observed, line.required) == (1, ctx.thresholds.int("crm.veto_min_leads"))


# ---------------------------------------------------------------------------
# Phase 5: C2 Scale or Hold, cap evidence and budget-day uncertainty (AAP 0.5.4.4, 0.5.4.1)
# ---------------------------------------------------------------------------


def _b1_items(ctx: AnalysisContext) -> list[Candidate]:
    return [item for item in budget.evaluate(ctx).candidates if item.rule_id == B1]


def test_c2_scale_is_linked_to_the_b1_item_carrying_the_amount(
    make_context: ContextFactory,
) -> None:
    """C2 outcomes: an eligible subject whose veto passes is "Scale" plus a B1 item that
    carries the amount; ``scale_outcomes`` is the one evaluation both read."""
    scenario = SCENARIOS["c2.outcome_scale.case"]
    ctx = make_context(scenario)
    values = scenario.expect.values
    step = _threshold(ctx, "b1.step")
    scale = _only(campaigns.evaluate(ctx), C2)
    assert (scale.group, scale.action) == (GROUP_SCALE, SCALE_ACTION)
    extra = scale.evidence.extra
    assert (extra["outcome"], extra["b1"]) == (values["outcome"], values["b1"])
    # Scale leads with the capped budget's utilization, one of the card's own metrics.
    assert extra["headline_metric"] == "window_utilization"
    assert "window_utilization" in evidence_metrics(scale)
    subject = cast(DecisionSubject, scale.decision_subject)
    assert scale.increases_spend_on == (subject,)
    link = extra["b1_link"]
    assert (link["rule_id"], dict(link["subject"])) == (B1, subject.to_json())
    current = cast(Decimal, link["current_daily_budget"])
    proposed = cast(Decimal, link["proposed_daily_budget"])
    assert proposed == (current * (1 + step)).quantize(Decimal("0.01"))
    assert proposed == Decimal(cast(str, values["proposed_daily_budget"]))
    assert evidence_metrics(scale)["proposed_daily_budget"] == proposed
    (b1,) = _b1_items(ctx)
    assert (b1.section, b1.decision_subject, b1.increases_spend_on) == (
        BUDGET,
        subject,
        (subject,),
    )
    assert evidence_metrics(b1)["proposed_daily_budget"] == proposed
    assert b1.action == f"Raise daily budget from USD {current:,.2f} to USD {proposed:,.2f}"
    (outcome,) = scale_outcomes(ctx)
    assert isinstance(outcome, ScaleOutcome)
    assert (outcome.kind, outcome.subject, outcome.proposed_daily_budget) == (
        "scale",
        subject.key,
        proposed,
    )


def test_c2_holds_with_no_b1_when_the_veto_fires(make_context: ContextFactory) -> None:
    """C2 outcomes: the veto firing turns the eligible subject into "Hold" and no B1 exists."""
    scenario = SCENARIOS["c2.outcome_hold.case"]
    ctx = make_context(scenario)
    values = scenario.expect.values
    hold = _only(campaigns.evaluate(ctx), C2)
    assert (hold.group, hold.action) == (GROUP_SCALE, HOLD_ACTION)
    assert (hold.evidence.extra["outcome"], hold.evidence.extra["b1"]) == (values["outcome"], False)
    assert "b1_link" not in hold.evidence.extra
    # Hold leads with the Qualified Leads the veto found missing.
    assert hold.evidence.extra["headline_metric"] == "ql"
    assert (hold.decision_subject, hold.increases_spend_on) == (None, ())
    metrics = evidence_metrics(hold)
    assert (metrics["eligible_contacts"], metrics["ql"]) == (
        values["eligible_contacts"],
        values["ql"],
    )
    assert cast(int, metrics["eligible_contacts"]) >= ctx.thresholds.int("crm.veto_min_leads")
    assert "proposed_daily_budget" not in metrics
    assert _b1_items(ctx) == []
    (outcome,) = scale_outcomes(ctx)
    assert isinstance(outcome, ScaleOutcome)
    assert outcome.kind == "hold"


def test_c2_scales_flagged_pipeline_unavailable_when_the_ql_stage_is_unresolved(
    make_context: ContextFactory,
) -> None:
    """C2 outcomes: with the QL stage unresolved the veto is not evaluated; "Scale" and its B1
    both carry pipeline_unavailable, and QL shows as unavailable, never 0."""
    scenario = SCENARIOS["c2.qlstage_unresolved.case"]
    ctx = make_context(scenario)
    result = campaigns.evaluate(ctx)
    scale = _only(result, C2)
    assert scale.action == SCALE_ACTION
    assert FLAG_PIPELINE_UNAVAILABLE in scale.flag_codes
    assert evidence_metrics(scale)["ql"] is None
    assert FINDING_PIPELINE_UNAVAILABLE in _codes(result)
    (b1,) = _b1_items(ctx)
    assert FLAG_PIPELINE_UNAVAILABLE in b1.flag_codes
    assert b1.decision_subject == scale.decision_subject


def test_c2_scales_a_google_subject_without_a_crm_link_flagged_no_crm_link(
    make_context: ContextFactory,
) -> None:
    """C2 outcomes: a Google subject without a verified CRM link scales with its B1, both
    flagged no_crm_link, and no Google veto is evaluated."""
    ctx = make_context("shared.decision_subject.case")
    result = campaigns.evaluate(ctx)
    scale = _only(result, C2)
    assert (scale.entity_ref.source, scale.action) == ("google", SCALE_ACTION)
    assert {FLAG_NO_CRM_LINK, FLAG_SHARED_BUDGET} <= set(scale.flag_codes)
    assert "eligible_contacts" not in evidence_metrics(scale)
    assert FINDING_NO_CRM_LINK in _codes(result)
    assert scale.decision_subject is not None
    assert scale.decision_subject.type == scale.entity_ref.type == "budget"
    (b1,) = _b1_items(ctx)
    assert FLAG_NO_CRM_LINK in b1.flag_codes
    assert b1.decision_subject == scale.decision_subject


def _gate(item: Candidate) -> dict[str, tuple[object, object]]:
    return {
        key: (count["observed"], count["required"])
        for key, count in item.evidence.extra["gate"].items()
    }


def test_c2_cap_evidence_five_capped_of_seven_known_days_is_eligible(
    make_context: ContextFactory,
) -> None:
    """C2 cap evidence: 5 capped of 7 known-budget days (5 spending) is eligible / one capped
    day followed by six idle days is not."""
    scenario = SCENARIOS["c2.cap_evidence.fires"]
    ctx = make_context(scenario)
    item = _only(campaigns.evaluate(ctx), C2)
    metrics = evidence_metrics(item)
    for key, value in scenario.expect.values.items():
        assert metrics[key] == value, key
    gate = _gate(item)
    assert gate["known_budget_days"] == (7, ctx.thresholds.int("budget.min_known_days"))
    assert gate["spending_days"] == (5, ctx.thresholds.int("budget.min_spending_days"))
    assert gate["capped_days"] == (5, ctx.thresholds.int("budget.min_capped_days"))
    share = _threshold(ctx, "budget.sustained_share")
    assert Decimal(5) / Decimal(7) >= share

    twin = SCENARIOS["c2.cap_evidence.does_not_fire"]
    result = campaigns.evaluate(make_context(twin))
    assert _items(result, C2) == []
    line = _only_withheld(result, C2)
    assert line.reason.startswith("spending days 1 of 5 required; capped days 1 of 5 required")
    assert (line.observed, line.required, line.unit) == (1, 5, "spending days")


def test_c2_with_four_known_budget_days_is_withheld_with_observed_and_required(
    make_context: ContextFactory,
) -> None:
    """C2 cap evidence: only 4 known-budget days withhold C2 with observed and required."""
    scenario = SCENARIOS["c2.cap_evidence_short.case"]
    ctx = make_context(scenario)
    result = campaigns.evaluate(ctx)
    assert _items(result, C2) == []
    line = _only_withheld(result, C2)
    values = scenario.expect.values
    assert line.reason.startswith(cast(str, scenario.expect.withheld_reason))
    assert (line.observed, line.required, line.unit) == (
        values["known_days"],
        values["required"],
        "known-budget days",
    )
    assert line.required == ctx.thresholds.int("budget.min_known_days")


INTRADAY_CASES: Final = (
    "budget_days.intraday.case",
    "budget_days.first_snapshot_noon.case",
    "budget_days.gap_7h.fires",
    "budget_days.gap_7h.does_not_fire",
    "budget_days.before_first_snapshot.case",
)


@pytest.mark.parametrize("name", INTRADAY_CASES)
def test_intraday_budget_uncertainty_shows_in_the_c2_and_budget_day_counts(
    make_context: ContextFactory, name: str
) -> None:
    """Intraday budget uncertainty: a budget raised at noon makes that day changed-intraday; a
    first snapshot at noon or a 7-hour observation gap makes the day unobserved; a day bracketed
    by unchanged observations at most 6 h apart is known; days before the first snapshot are
    unknown. Unknown days are excluded from utilization and cap counts, counted separately."""
    scenario = SCENARIOS[name]
    ctx = make_context(scenario)
    values = scenario.expect.values
    item = _only(campaigns.evaluate(ctx), C2)
    metrics = evidence_metrics(item)
    extra = item.evidence.extra
    for key, expected in values.items():
        if isinstance(expected, tuple):
            assert tuple(extra[DATE_LISTS[key]]) == expected, key
            assert metrics[key] == len(expected), key
        else:
            assert metrics[key] == expected, key
    known = cast(int, metrics["known_days"])
    unknown = cast(int, metrics["changed_intraday_days"]) + cast(int, metrics["unobserved_days"])
    assert known + unknown == ctx.window.days
    gate = _gate(item)
    assert gate["known_budget_days"][0] == known
    assert cast(int, gate["capped_days"][0]) <= known
    unknown_dates = {*extra["changed_intraday_dates"], *extra["unobserved_dates"]}
    assert len(unknown_dates) == unknown
    gap = ctx.thresholds.int("budget.max_observation_gap_hours")
    assert extra["observation_assumption"].endswith(f"at most {gap} h apart")
    (b1,) = _b1_items(ctx)
    counts = b1.evidence.extra["day_counts"]
    assert (counts["known_active"], counts["changed_intraday"], counts["unobserved"]) == (
        known,
        metrics["changed_intraday_days"],
        metrics["unobserved_days"],
    )
    assert set(b1.evidence.extra["unknown_dates"]) == unknown_dates


# ---------------------------------------------------------------------------
# Phase 6: launch ideas, C3a and C3b (AAP 0.5.4.4, 0.3.2 segment windows)
# ---------------------------------------------------------------------------


def test_c3a_launches_an_underused_proven_segment_on_a_preset_window(
    make_context: ContextFactory,
) -> None:
    """C3a: on a preset window ending on the default window end, a segment with at least 5
    leads, a CPL at most 0.8x the campaign's and under 15% of its impressions is a launch idea."""
    scenario = SCENARIOS["c3a.preset.case"]
    ctx = make_context(scenario)
    assert ctx.window.end == ctx.window.default_end
    assert ctx.window.days in ctx.window.presets
    item = _only(campaigns.evaluate(ctx), C3A)
    extra = item.evidence.extra
    campaign = scenario.ids.name("linkedin", "campaign", "li-seg")
    assert (item.group, item.decision_subject, item.increases_spend_on) == (GROUP_LAUNCH, None, ())
    assert item.action == (
        f"Launch a campaign targeting {extra['facet']}: {extra['value']} with {campaign}'s offer"
    )
    assert (extra["facet"], extra["value"]) == (
        scenario.expect.values["facet"],
        scenario.expect.values["value"],
    )
    assert dict(extra["segment_window"]) == {"start": ctx.window.start, "end": ctx.window.end}
    metrics = evidence_metrics(item)
    assert cast(int, metrics["segment_leads"]) >= ctx.thresholds.int("c3a.min_leads")
    assert cast(Decimal, metrics["cpl_ratio"]) <= _threshold(ctx, "c3a.max_cpl_ratio")
    assert cast(Decimal, metrics["impression_share"]) < _threshold(ctx, "c3a.max_impression_share")
    assert item.rank_key[0] == -cast(int, metrics["segment_leads"])
    # The launch idea leads with the segment's cost per lead, the proof the segment is good.
    assert extra["headline_metric"] == "segment_cpl"
    assert "segment_cpl" in metrics


def test_c3a_needs_an_impression_share_below_fifteen_percent(make_context: ContextFactory) -> None:
    """C3a: a 14% impression share fires / a 16% share does not."""
    item = _only(campaigns.evaluate(make_context("c3a.share.fires")), C3A)
    assert evidence_metrics(item)["impression_share"] == Decimal("0.1400")
    assert _items(campaigns.evaluate(make_context("c3a.share.does_not_fire")), C3A) == []


def test_c3a_does_not_fire_on_a_pivot_without_a_lead_metric(make_context: ContextFactory) -> None:
    """C3a: a pivot that returns no lead metric gives no launch idea (unavailable, not zero)."""
    fires = _only(campaigns.evaluate(make_context("c3a.no_lead_pivot.fires")), C3A)
    facet = fires.evidence.extra["facet"]
    ctx = make_context("c3a.no_lead_pivot.does_not_fire")
    pivot = [row for row in ctx.segments if row.facet == facet]
    assert pivot
    assert all(row.leads is None for row in pivot)
    assert _items(campaigns.evaluate(ctx), C3A) == []


def test_c3a_is_withheld_while_the_segment_window_is_not_yet_available(
    make_context: ContextFactory,
) -> None:
    """C3a: when the latest segment fetch ends one day before the default window end (first
    start, Google account time zone behind UTC), C3a is withheld with segment_window_unavailable
    and never reuses the earlier window's segments."""
    ctx = make_context("c3a.not_yet_available.case")
    end = ctx.window.end
    # "Latest available" is the latest fetched end of the run window's own preset length.
    latest = max(
        window_end
        for window_start, window_end in ctx.segment_windows
        if (window_end - window_start).days + 1 == ctx.window.days
    )
    assert latest == end - timedelta(days=1)
    # The fetched segments of the same length ending a day earlier would qualify on their own.
    assert (latest - timedelta(days=ctx.window.days - 1), latest) in ctx.segment_windows
    result = campaigns.evaluate(ctx)
    assert _items(result, C3A) == []
    label = (
        f"Segment data for the {ctx.window.days}-day window ending {end.isoformat()} not yet "
        f"available; latest available ends {latest.isoformat()}"
    )
    (finding,) = [f for f in result.findings if f.code == FINDING_SEGMENT_WINDOW_UNAVAILABLE]
    assert finding.message == label
    assert _only_withheld(result, C3A).reason == label


def test_c3a_on_a_custom_window_names_the_preset_windows_only(make_context: ContextFactory) -> None:
    """C3a: a 30-day custom window is withheld with "Segment data exists only for the preset
    windows {list} ending {end}"."""
    ctx = make_context("c3a.custom_window.case")
    assert (ctx.run_kind, ctx.window.days) == ("custom", 30)
    assert ctx.window.days not in ctx.window.presets
    result = campaigns.evaluate(ctx)
    assert _items(result, C3A) == []
    presets = ", ".join(str(days) for days in ctx.thresholds.value("window.presets"))
    label = (
        f"Segment data exists only for the preset windows {presets} ending "
        f"{ctx.window.default_end.isoformat()}"
    )
    assert label in [
        f.message for f in result.findings if f.code == FINDING_SEGMENT_WINDOW_UNAVAILABLE
    ]
    assert _only_withheld(result, C3A).reason == label


def test_c3b_launches_a_converting_cluster_no_campaign_covers(make_context: ContextFactory) -> None:
    """C3b: a cluster (token Jaccard >= 0.5) of >= 3 converting terms with >= 5 conversions whose
    head tokens appear in no keyword, ad group or campaign name / its head token in an ad group
    name gives no launch idea."""
    scenario = SCENARIOS["c3b.cluster.fires"]
    ctx = make_context(scenario)
    item = _only(campaigns.evaluate(ctx), C3B)
    extra = item.evidence.extra
    assert (item.group, item.decision_subject) == (GROUP_LAUNCH, None)
    # The suggested name lists the head tokens in the order the member terms are searched;
    # the evidence keeps them alphabetical, the stable form.
    assert item.action == "Create ad group or campaign for 'hubspot alternative'"
    assert tuple(extra["head_tokens"]) == scenario.expect.values["head_tokens"]
    members = list(extra["members"])
    assert len(members) == evidence_metrics(item)["members"]
    assert len(members) >= ctx.thresholds.int("c3b.min_terms")
    assert {member["term"] for member in members} == {
        "hubspot alternative",
        "hubspot alternative free",
        "cheap hubspot alternative",
    }
    assert all(member["spend"] > 0 and member["conversions"] > 0 for member in members)
    conversions = cast(Decimal, evidence_metrics(item)["conversions"])
    assert conversions == sum(member["conversions"] for member in members)
    assert conversions >= _threshold(ctx, "c3b.min_conversions")
    # The uncovered cluster's card leads with its conversions.
    assert extra["headline_metric"] == "conversions"
    assert _items(campaigns.evaluate(make_context("c3b.cluster.does_not_fire")), C3B) == []


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("absent", "no active HubSpot dataset"),
        ("unverified", GOOGLE_LINK_UNVERIFIED_REASON),
        ("verified", None),
    ],
)
def test_c3b_carries_no_crm_link_and_counts_in_the_finding_without_the_verified_link(
    make_context: ContextFactory, state: GoogleLinkState, reason: str | None
) -> None:
    """C3b is a Google item: without the verified Google-to-CRM link it carries no_crm_link and
    the Campaigns no_crm_link finding counts it (AAP 0.3.3) / with the link it carries neither."""
    ctx = with_google_link(make_context("c3b.cluster.fires"), state)
    result = campaigns.evaluate(ctx)
    item = _only(result, C3B)
    assert [candidate.rule_id for candidate in result.candidates] == [C3B]
    found = [finding for finding in result.findings if finding.code == FINDING_NO_CRM_LINK]
    if reason is None:
        assert item.flags == ()
        assert found == []
        return
    assert [(flag.code, flag.message) for flag in item.flags] == [
        (FLAG_NO_CRM_LINK, f"No CRM link for Google campaigns: {reason}")
    ]
    (finding,) = found
    assert (finding.section, finding.entity) == (CAMPAIGNS, None)
    assert finding.evidence == {"reason": reason, "items": 1}


@pytest.mark.parametrize("state", ["absent", "unverified", "verified"])
def test_every_google_campaigns_item_carries_no_crm_link_only_without_the_verified_link(
    make_context: ContextFactory, state: GoogleLinkState
) -> None:
    """AAP 0.3.3: Google items carry no_crm_link while the link is unavailable; LinkedIn items
    never do; the finding counts every flagged item, C3b included / the verified link flags none.
    """
    scenario = SCENARIOS["default"]
    built = make_context(scenario, now=None if scenario.fixed_time else FIXED_NOW)
    ctx = with_google_link(built, state)
    result = campaigns.evaluate(ctx)
    sources = {item.entity_ref.source for item in result.candidates}
    assert sources == {"google", "linkedin"}, "the default world has items of both sources"
    google = [item for item in result.candidates if item.entity_ref.source == "google"]
    google_rules = {item.rule_id for item in google}
    assert C3B in google_rules
    unlinked = state != "verified"
    flagged = [item for item in result.candidates if item.has_flag(FLAG_NO_CRM_LINK)]
    for item in result.candidates:
        expected = unlinked and item.entity_ref.source == "google"
        assert item.has_flag(FLAG_NO_CRM_LINK) is expected, (item.rule_id, item.entity_ref.key)
    found = [finding for finding in result.findings if finding.code == FINDING_NO_CRM_LINK]
    if not unlinked:
        assert (flagged, found) == ([], [])
        return
    (finding,) = found
    assert finding.evidence["items"] == len(flagged)
    assert {item.rule_id for item in flagged} == google_rules


@pytest.mark.parametrize("scenario", CAMPAIGN_SCENARIOS, ids=lambda scenario: scenario.name)
def test_no_other_rule_emits_a_launch_idea(
    scenario: Scenario, make_context: ContextFactory
) -> None:
    """Launch ideas come only from C3a and C3b: no other Campaigns, C0 or Budget item is placed
    in the launch group, and the group exists in the Campaigns section alone."""
    ctx = make_context(scenario)
    for result in (discrepancy.evaluate(ctx), campaigns.evaluate(ctx), budget.evaluate(ctx)):
        for item in result.candidates:
            if item.group == GROUP_LAUNCH:
                assert (item.section, item.rule_id in (C3A, C3B)) == (CAMPAIGNS, True)
            if item.rule_id in (C3A, C3B):
                assert item.group == GROUP_LAUNCH
    assert [section for section, groups in GROUP_ORDER.items() if GROUP_LAUNCH in groups] == [
        CAMPAIGNS
    ]
