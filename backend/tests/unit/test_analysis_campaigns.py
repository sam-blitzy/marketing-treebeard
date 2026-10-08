"""Unit tests for ``ads_engine.analysis.campaigns``: rules C1, C1b, C2 (Scale or Hold), C3a, C3b.

They hold the "Campaigns to pause or launch" rules to AAP 0.5.4.4 and the shared definitions
they read (0.5.4.1 window, outcome by source, efficiency, expected outcomes, budget-day gates
and the CRM pipeline veto; 0.3.2 segment windows): every catalogue scenario of
``acceptance/test_campaign_rules.py`` that names one of these rules is evaluated and compared
with its expectation, and targeted cases pin each item's action, group, evidence, flags,
decision affordance and G1 targets, every withheld line's observed against required, the
``segment_window_unavailable`` labels, the C3b coverage test, and the single
:func:`~ads_engine.analysis.campaigns.scale_outcomes` evaluation C2 and B1 share.

Contexts come from the scenario catalogue (``SCENARIOS[name].build_context()``); a few cases
copy a built context with :func:`dataclasses.replace` (always with a fresh ``cache``, so no
memoised table of the original leaks in) or tune its thresholds with ``thresholds_with``.
Pure: no database and no network.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import pytest

from ads_engine.analysis import campaigns
from ads_engine.analysis.attribution import VetoPopulation, attribution_for
from ads_engine.analysis.campaigns import (
    C1_THRESHOLD_KEYS,
    C1B_THRESHOLD_KEYS,
    C2_THRESHOLD_KEYS,
    C3A_THRESHOLD_KEYS,
    C3B_THRESHOLD_KEYS,
    HOLD_ACTION,
    PAUSE_ACTION,
    PIPELINE_PAUSE_ACTION,
    SCALE_ACTION,
    SCALE_CACHE_KEY,
    VETO_THRESHOLD_KEYS,
    ScaleOutcome,
    ScaleWithheld,
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
    ENTITY_BUDGET,
    ENTITY_CAMPAIGN,
    ENTITY_SEGMENT,
    ENTITY_TERM_CLUSTER,
    FINDING_NO_CRM_LINK,
    FINDING_PIPELINE_UNAVAILABLE,
    FINDING_SEGMENT_WINDOW_UNAVAILABLE,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_NO_CRM_LINK,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_SHARED_BUDGET,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    GROUP_LAUNCH,
    GROUP_PAUSE,
    GROUP_SCALE,
    OBJ_GOOGLE,
    OBJ_LINKEDIN_LEAD_GEN,
    OBJ_LINKEDIN_OTHER,
    AccountInfo,
    AnalysisContext,
    AssociationFetchInfo,
    CampaignInfo,
    Candidate,
    ContactInfo,
    CrmData,
    DatasetRef,
    DecisionSubject,
    EntityRef,
    LeadInfo,
    RuleResult,
    Source,
    Withheld,
)
from ads_engine.analysis.text import cluster_terms, contains_tokens, tokenize
from ads_engine.analysis.thresholds import CrmBindings, GoogleCrmBinding, ResolvedBinding
from ads_engine.simulation.scenarios import (
    FIXED_NOW,
    MODULE_CAMPAIGNS,
    SCENARIOS,
    Scenario,
    for_module,
    thresholds_with,
)

CAMPAIGN_RULES: Final = (C1, C1B, C2, C3A, C3B)
"""The rules ``campaigns.evaluate`` implements; C0 lives in ``discrepancy.py``."""

RULE_GROUPS: Final = {
    C1: GROUP_PAUSE,
    C1B: GROUP_PAUSE,
    C2: GROUP_SCALE,
    C3A: GROUP_LAUNCH,
    C3B: GROUP_LAUNCH,
}
"""AAP 0.5.4.4 group of each rule's items: Pause, Scale, Launch ideas."""

REPEAT_CONTACT: Final = "veto.repeat_contact.case"
"""Its two leads from one contact prove the eligible-contact de-duplication.

The expectation's values are the veto-population counts. Two outcomes are below
``scale.min_outcomes`` (5), so C2 is withheld (AAP 0.5.4.4 C2 eligibility): the guard checks
the withheld line, and a dedicated test below checks the counts.
"""

RULE_SCENARIOS: Final = tuple(
    scenario
    for scenario in for_module(MODULE_CAMPAIGNS)
    if scenario.expect.rule_id in CAMPAIGN_RULES
)

DATE_LISTS: Final = {
    "unobserved_days": "unobserved_dates",
    "changed_intraday_days": "changed_intraday_dates",
}
"""Expectation keys whose tuple values are the dated day lists in ``evidence.extra``."""

GOOGLE_CRM: Final = DatasetRef(3, "hubspot", "simulated")
"""HubSpot's primary dataset, which the Google-to-CRM link reads."""

LINKED: Final = CrmBindings(
    mql=ResolvedBinding(available=True, property="date_first_meeting_was_booked"),
    ql=ResolvedBinding(
        available=True,
        property="hs_v2_date_entered_1406943076",
        stage_id="1406943076",
        label="Qualified Lead",
    ),
    google=GoogleCrmBinding(
        available=True,
        campaign_property="google_campaign_id",
        touch_date_property="google_touch_at",
    ),
    verified_at=FIXED_NOW,
)
"""Bindings with a verified Google-to-CRM link (both Google contact properties confirmed)."""

UNLINKED_REASON: Final = "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured"

MATURED_TOUCH: Final = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
"""A Google touch inside the default window and more than 14 days before its end."""

GOOGLE_POPULATION_DATASETS: Final = (
    "hubspot.google_contacts",
    "hubspot.contacts",
    "hubspot.associations",
)


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


def _items(result: RuleResult, rule_id: str) -> list[Candidate]:
    return [item for item in result.candidates if item.rule_id == rule_id]


def _only(result: RuleResult, rule_id: str) -> Candidate:
    items = _items(result, rule_id)
    assert len(items) == 1, [item.entity_ref.name for item in items]
    return items[0]


def _withheld(result: RuleResult, rule_id: str) -> list[Withheld]:
    return [line for line in result.withheld if line.rule_id == rule_id]


def _metrics(item: Candidate) -> dict[str, object]:
    return {metric.key: metric.value for metric in item.evidence.metrics}


def _codes(result: RuleResult) -> list[str]:
    return [finding.code for finding in result.findings]


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
    if isinstance(value, list | tuple):
        return tuple(_normalised(item) for item in value)
    return value


def _evidence_value(item: Candidate, key: str, expected: object) -> object:
    """An expectation value's counterpart: a metric, else ``evidence.extra``."""
    if isinstance(expected, tuple) and key in DATE_LISTS:
        return item.evidence.extra[DATE_LISTS[key]]
    metrics = _metrics(item)
    if key in metrics:
        return metrics[key]
    return item.evidence.extra[key]


def _campaign_id(scenario: Scenario, source: str, key: str) -> int:
    return scenario.ids.id(source, "campaign", key)


def _google_crm(external_id: str, contacts: int, *, linked: bool = True) -> CrmData:
    """HubSpot's primary with ``contacts`` matured Google-touch contacts of one campaign.

    Every contact has a genuine zero of meetings and deals (``ok_empty`` fetches) and no
    Qualified Lead date, so the Google veto fires at ``crm.veto_min_leads`` contacts.
    """
    people = tuple(
        ContactInfo(
            f"{880_000 + n}",
            lifecycle_stage="lead",
            google_campaign_ref=external_id,
            google_touch_at=MATURED_TOUCH,
        )
        for n in range(contacts)
    )
    fetches = {
        ("contact", person.hs_id, to_type): AssociationFetchInfo(
            "contact", person.hs_id, to_type, "ok_empty"
        )
        for person in people
        for to_type in ("meeting", "deal")
    }
    bindings = (
        LINKED
        if linked
        else replace(LINKED, google=GoogleCrmBinding(available=False, reason=UNLINKED_REASON))
    )
    return CrmData(GOOGLE_CRM, bindings, fetches=fetches, google_contacts=people)


def _population(
    campaign_id: int,
    *,
    matured: int = 20,
    unresolved: int = 0,
    eligible: int | None = None,
    unknown: int = 0,
    ql: int | None = 0,
    meetings: int | None = 0,
    available: bool = True,
    reason: str | None = None,
    floor: bool = False,
    source: Source = "google",
) -> VetoPopulation:
    """A veto population; ``veto_fires`` follows AAP 0.5.4.1 for 20 required contacts."""
    contacts = matured - unresolved if eligible is None else eligible
    share = Decimal(unresolved) / Decimal(matured) if matured else None
    return VetoPopulation(
        source=source,
        campaign_id=campaign_id,
        matured_leads=matured,
        matched_leads=matured - unresolved,
        unresolved_leads=unresolved,
        eligible_contacts=contacts,
        unresolved_share=share,
        unknown_evidence_contacts=unknown,
        mql=0 if ql is not None else None,
        ql=ql,
        meetings=meetings,
        available=available,
        reason=reason,
        veto_fires=available and contacts >= 20 and ql == 0,
        leads=matured,
        attribution_floor=floor,
    )


def _linkedin_campaign(campaign_id: int) -> CampaignInfo:
    return CampaignInfo(
        campaign_id,
        "linkedin",
        9,
        f"9901{campaign_id:05d}",
        f"LinkedIn {campaign_id}",
        "active",
        "ACTIVE",
        "LEAD_GENERATION",
        OBJ_LINKEDIN_LEAD_GEN,
        None,
        None,
        Decimal("100.00"),
        None,
        None,
        "USD",
    )


# ---------------------------------------------------------------------------
# The catalogue guard (simulation/scenarios.py expectations)
# ---------------------------------------------------------------------------


def test_the_guard_covers_every_campaign_rule_firing_and_not_firing() -> None:
    fired = {scenario.expect.rule_id for scenario in RULE_SCENARIOS if scenario.expect.fires}
    withheld = {scenario.expect.rule_id for scenario in RULE_SCENARIOS if not scenario.expect.fires}
    assert fired == set(CAMPAIGN_RULES)
    assert withheld == set(CAMPAIGN_RULES)
    # C0 scenarios of the same module belong to discrepancy.py, never to this module.
    assert all(scenario.expect.rule_id != C0 for scenario in RULE_SCENARIOS)
    assert REPEAT_CONTACT in {scenario.name for scenario in RULE_SCENARIOS}


@pytest.mark.parametrize("scenario", RULE_SCENARIOS, ids=lambda scenario: scenario.name)
def test_evaluate_matches_the_catalogue_expectation(scenario: Scenario) -> None:
    expect = scenario.expect
    rule_id = expect.rule_id
    assert rule_id is not None
    result = campaigns.evaluate(scenario.build_context())
    items = _items(result, rule_id)
    if expect.fires:
        assert len(items) == 1, [item.action for item in items]
        item = items[0]
        assert (item.section, item.group) == (CAMPAIGNS, RULE_GROUPS[rule_id])
        for key, expected in expect.values.items():
            actual = _evidence_value(item, key, expected)
            assert _normalised(actual) == _normalised(expected), (key, actual, expected)
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


def test_two_leads_of_one_contact_count_once_and_stay_below_the_scale_minimum() -> None:
    """The expectation's values are veto-population counts; its withheld line is C2's."""
    scenario, ctx = _built(REPEAT_CONTACT)
    expect = scenario.expect
    assert (expect.rule_id, expect.fires) == (C2, False)
    values = expect.values
    campaign_id = _campaign_id(scenario, "linkedin", "li-pipeline")
    veto = attribution_for(ctx).veto[campaign_id]
    assert (veto.matured_leads, veto.matched_leads, veto.eligible_contacts) == (
        values["matured_leads"],
        values["matched_leads"],
        values["eligible_contacts"],
    )
    assert veto.unresolved_share == 0
    result = campaigns.evaluate(ctx)
    assert _items(result, C2) == []
    (c2,) = _withheld(result, C2)
    assert (c2.reason, c2.observed, c2.required, c2.unit) == (
        expect.withheld_reason,
        Decimal(2),
        Decimal(5),
        "leads",
    )
    (c1b,) = _withheld(result, C1B)
    assert (c1b.reason, c1b.observed, c1b.required) == ("eligible contacts 1 of 20 required", 1, 20)
    (entry,) = scale_outcomes(ctx)
    assert isinstance(entry, ScaleWithheld)
    assert entry.history is not None
    assert entry.gate is not None
    assert entry.gate.passed


# ---------------------------------------------------------------------------
# Settings, thresholds and formatting
# ---------------------------------------------------------------------------


def test_settings_read_every_campaign_threshold_once_per_run() -> None:
    _, ctx = _built("c1.expected.fires")
    settings = campaigns._Settings.of(ctx)
    assert campaigns._Settings.of(ctx) is settings
    assert ctx.cache["campaigns.settings"] is settings
    assert (settings.c1_min_window, settings.c1_spend_share, settings.c1_min_expected) == (
        14,
        Decimal("0.70"),
        Decimal(3),
    )
    assert (settings.c1b_min_window, settings.sustained_min) == (28, 7)
    assert (settings.veto_min, settings.maturity_days, settings.floor_share) == (
        20,
        14,
        Decimal("0.10"),
    )
    assert (settings.scale_min_outcomes, settings.scale_max_ratio, settings.b1_step) == (
        Decimal(5),
        Decimal("1.0"),
        Decimal("0.20"),
    )
    assert (settings.c3a_min_leads, settings.c3a_max_cpl_ratio, settings.c3a_max_share) == (
        5,
        Decimal("0.8"),
        Decimal("0.15"),
    )
    assert (
        settings.c3b_jaccard,
        settings.c3b_min_terms,
        settings.c3b_min_conversions,
        settings.c3b_head_share,
    ) == (Decimal("0.5"), 3, Decimal(5), Decimal("0.5"))


def test_actions_carry_the_plans_wording() -> None:
    assert PAUSE_ACTION == "Pause"
    assert PIPELINE_PAUSE_ACTION == (
        "Pause or rework targeting: leads are not becoming Qualified Leads"
    )
    assert SCALE_ACTION == "Scale"
    assert HOLD_ACTION == "Hold: platform outcomes are not reaching Qualified Lead"
    assert SCALE_CACHE_KEY == "scale_outcomes"


def test_every_declared_threshold_key_is_configured() -> None:
    _, ctx = _built("c1.expected.fires")
    for keys in (
        C1_THRESHOLD_KEYS,
        C1B_THRESHOLD_KEYS,
        C2_THRESHOLD_KEYS,
        VETO_THRESHOLD_KEYS,
        C3A_THRESHOLD_KEYS,
        C3B_THRESHOLD_KEYS,
    ):
        for key in keys:
            assert ctx.thresholds.value(key) is not None, key
    assert set(VETO_THRESHOLD_KEYS) <= set(C1B_THRESHOLD_KEYS)


def test_formatting_helpers_render_amounts_counts_and_shares() -> None:
    assert campaigns._money(None, "USD") == "unavailable"
    assert campaigns._money("n/a", "USD") == "n/a"
    assert campaigns._money(Decimal("1400"), "USD") == "USD 1,400.00"
    assert campaigns._money(Decimal("12.5"), "") == "12.50"
    assert campaigns._money(1000, "JPY") == "JPY 1,000"
    assert campaigns._count(None) == "unavailable"
    assert campaigns._count("n/a") == "n/a"
    assert campaigns._count(Decimal("6.000")) == "6"
    assert campaigns._count(Decimal("0.50")) == "0.5"
    assert campaigns._count(7) == "7"
    assert campaigns._percent(Decimal("0.1304")) == "13%"
    assert campaigns._percent(Decimal("0.0909")) == "9.1%"
    assert campaigns._one_place(Decimal("2.95")) == "2.9"
    assert campaigns._q_floor(Decimal("2.9999995")) == Decimal("2.999999")
    assert campaigns._share(Decimal(2) / Decimal(22)) == Decimal("0.0909")
    assert campaigns._sum_known([1, 2, 3]) == 6
    assert campaigns._sum_known([1, None, 3]) is None
    assert campaigns._singular("conversions") == "conversion"
    assert campaigns._singular("leads") == "lead"
    assert campaigns._singular("spend") == "spend"


# ---------------------------------------------------------------------------
# C1 Pause
# ---------------------------------------------------------------------------


def test_c1_pauses_a_steady_spender_with_no_output() -> None:
    scenario, ctx = _built("c1.expected.fires")
    dud = _campaign_id(scenario, "google", "dud")
    result = campaigns.evaluate(ctx)
    item = _only(result, C1)
    assert item.action == PAUSE_ACTION
    assert (item.entity_ref.type, item.entity_ref.id) == (ENTITY_CAMPAIGN, dud)
    assert item.entity_ref.name == "Search - No output"
    assert item.decision_subject == DecisionSubject("campaign", dud)
    assert item.increases_spend_on == ()
    assert item.depends_on == (
        DecisionSubject("campaign", dud),
        DecisionSubject("budget", scenario.ids.id("google", "budget", "dud-budget")),
    )
    assert item.evidence.extra == {"proposed_status": "paused"}
    assert item.rank_key == (-Decimal("50.00"),)
    assert _metrics(item) == {
        "spend": Decimal("50.00"),
        "spend_days": 28,
        "window_days": 28,
        "spend_day_share": Decimal("1.0000"),
        "outcomes": 0,
        "expected_outcomes": Decimal("3.000000"),
    }
    assert item.evidence.baselines[0].key == "baseline_cost_per_outcome"
    assert item.evidence.sample is not None
    assert (item.evidence.sample.observed, item.evidence.sample.required) == (
        Decimal(3),
        Decimal(3),
    )
    assert item.evidence.sample.unit == "expected conversions"
    assert set(item.evidence.thresholds) == set(C1_THRESHOLD_KEYS)
    assert item.required_datasets == ("google.entities", "google.metrics_daily")
    assert item.reason == (
        "Spent USD 50.00 on 28 of 28 days with 0 conversions; at the account's USD 16.67 per "
        "conversion, 3.0 conversions were expected."
    )
    # Without a verified Google-to-CRM link the item says so, and one finding counts it.
    assert item.flag_codes == (FLAG_NO_CRM_LINK,)
    (finding,) = [item for item in result.findings if item.code == FINDING_NO_CRM_LINK]
    assert finding.evidence == {"reason": "no active HubSpot dataset", "items": 1}


@pytest.mark.parametrize(
    ("name", "reason", "observed", "required", "unit", "entity"),
    [
        (
            "c1.expected.does_not_fire",
            "expected conversions 2.9 of 3 required",
            Decimal("2.9"),
            Decimal(3),
            "expected conversions",
            True,
        ),
        (
            "c1.spend_days.does_not_fire",
            "spend days 12 of 14 required (70% of 20 window days)",
            12,
            14,
            "days",
            True,
        ),
        ("c1.window.does_not_fire", "window 13 days of 14 required", 13, 14, "days", False),
    ],
)
def test_c1_near_patterns_are_withheld_with_observed_and_required(
    name: str, reason: str, observed: object, required: object, unit: str, entity: bool
) -> None:
    _, ctx = _built(name)
    result = campaigns.evaluate(ctx)
    assert _items(result, C1) == []
    (line,) = _withheld(result, C1)
    assert (line.reason, line.observed, line.required, line.unit) == (
        reason,
        observed,
        required,
        unit,
    )
    assert (line.entity is not None) is entity
    assert line.section == CAMPAIGNS


def test_c1_withholds_too_few_expected_outcomes_only_on_steady_spend() -> None:
    steady, steady_ctx = _built("c1.spend_days.fires")
    tuned = _tuned(steady, steady_ctx, {"c1.min_expected_outcomes": 4})
    result = campaigns.evaluate(tuned)
    assert _items(result, C1) == []
    (line,) = _withheld(result, C1)
    assert (line.reason, line.observed, line.required) == (
        "expected conversions 3.0 of 4 required",
        Decimal(3),
        Decimal(4),
    )
    unsteady, unsteady_ctx = _built("c1.spend_days.does_not_fire")
    quiet = campaigns.evaluate(_tuned(unsteady, unsteady_ctx, {"c1.min_expected_outcomes": 4}))
    # Neither steady nor expected to convert: nothing near the pattern, so no withheld line.
    assert _items(quiet, C1) == []
    assert _withheld(quiet, C1) == []


def test_c1_skips_campaigns_without_a_baseline_spend_or_activity() -> None:
    scenario, ctx = _built("c1.expected.fires")
    dud = _campaign_id(scenario, "google", "dud")
    # Alone in its account, the dud gives the account no outcomes, hence no baseline.
    alone = _fresh(ctx, campaigns={dud: ctx.campaigns[dud]})
    assert _items(campaigns.evaluate(alone), C1) == []
    assert _withheld(campaigns.evaluate(alone), C1) == []
    # Without fact rows it spent nothing in the window.
    no_facts = _fresh(ctx, campaign_days={**ctx.campaign_days, dud: ()})
    assert _items(campaigns.evaluate(no_facts), C1) == []
    # Without an active campaign C1 says nothing, not even on a short window.
    _, short_ctx = _built("c1.window.does_not_fire")
    paused = {cid: replace(c, status="paused") for cid, c in short_ctx.campaigns.items()}
    silent = campaigns.evaluate(_fresh(short_ctx, campaigns=paused))
    assert _items(silent, C1) == []
    assert _withheld(silent, C1) == []


def test_google_items_read_a_verified_crm_link() -> None:
    scenario, ctx = _built("c1.expected.fires")
    dud = _campaign_id(scenario, "google", "dud")
    external = scenario.ids.external_id("google", "campaign", "dud")
    linked = _fresh(ctx, google_crm=_google_crm(external, 20))
    result = campaigns.evaluate(linked)
    item = _only(result, C1)
    assert FLAG_NO_CRM_LINK not in item.flag_codes
    assert _metrics(item)["eligible_contacts"] == 20
    assert _metrics(item)["ql"] == 0
    assert set(item.evidence.thresholds) == {*C1_THRESHOLD_KEYS, *VETO_THRESHOLD_KEYS}
    assert item.required_datasets == (
        "google.entities",
        "google.metrics_daily",
        *GOOGLE_POPULATION_DATASETS,
    )
    assert FINDING_NO_CRM_LINK not in _codes(result)
    assert attribution_for(linked).veto[dud].veto_fires


def test_an_unverified_google_link_names_its_reason() -> None:
    scenario, ctx = _built("c1.expected.fires")
    external = scenario.ids.external_id("google", "campaign", "dud")
    unlinked = _fresh(ctx, google_crm=_google_crm(external, 20, linked=False))
    result = campaigns.evaluate(unlinked)
    item = _only(result, C1)
    (flag,) = item.flags
    assert (flag.code, flag.message) == (
        FLAG_NO_CRM_LINK,
        f"No CRM link for Google campaigns: {UNLINKED_REASON}",
    )
    (finding,) = [found for found in result.findings if found.code == FINDING_NO_CRM_LINK]
    assert finding.message == (
        f"No CRM link for Google campaigns: {UNLINKED_REASON}. No Google Qualified Lead counts "
        "or pipeline veto are computed."
    )
    assert campaigns._google_link_gap(unlinked) == UNLINKED_REASON
    assert campaigns._google_link_gap(_fresh(ctx, google_crm=_google_crm(external, 0))) is None


# ---------------------------------------------------------------------------
# C1b Pipeline pause
# ---------------------------------------------------------------------------


def test_c1b_pauses_a_campaign_whose_matured_leads_reach_no_pipeline() -> None:
    scenario, ctx = _built("c1b.eligible.fires")
    campaign_id = _campaign_id(scenario, "linkedin", "li-pipeline")
    result = campaigns.evaluate(ctx)
    item = _only(result, C1B)
    assert item.action == PIPELINE_PAUSE_ACTION
    assert item.group == GROUP_PAUSE
    assert item.decision_subject == DecisionSubject("campaign", campaign_id)
    assert item.increases_spend_on == ()
    assert item.flags == ()
    assert item.evidence.extra == {"proposed_status": "paused", "matured_ql": 0}
    assert item.required_datasets == (
        "linkedin.leads",
        "linkedin.analytics_daily",
        "hubspot.contacts",
        "hubspot.associations",
        "linkedin.entities",
    )
    assert set(item.evidence.thresholds) == set(C1B_THRESHOLD_KEYS)
    assert item.evidence.sample is not None
    assert (item.evidence.sample.observed, item.evidence.sample.required) == (20, 20)
    # The pipeline counts sit side by side and are never blended (AAP 0.5.4.4 C1b).
    assert list(_metrics(item)) == [
        "spend",
        "leads",
        "matured_leads",
        "matched_leads",
        "unresolved",
        "unresolved_share",
        "eligible_contacts",
        "unknown_evidence_share",
        "mql",
        "ql",
        "meetings",
        "young_leads",
        "young_ql",
    ]
    mql = item.evidence.metric("mql")
    assert mql.label == f"MQL ({attribution_for(ctx).mql_label})"
    assert item.reason == (
        "20 eligible contacts from 20 matured leads reached 0 Qualified Leads and 0 meetings "
        "(MQL 0) after USD 2,716.00 of spend in the window."
    )
    assert item.rank_key == (-Decimal("2716.00"),)


@pytest.mark.parametrize(
    ("name", "reason", "observed", "required", "unit"),
    [
        (
            "c1b.eligible.does_not_fire",
            "eligible contacts 19 of 20 required",
            19,
            20,
            "contacts",
        ),
        (
            "c1b.floor.does_not_fire",
            "attribution floor: 13% of matured leads unresolved in HubSpot (floor 10%)",
            Decimal("0.1304"),
            Decimal("0.10"),
            "unresolved share of matured leads (must be below the floor)",
        ),
        (
            "c1b.unknown_evidence.does_not_fire",
            "unknown CRM evidence for 20 of 20 eligible contacts",
            Decimal(1),
            0,
            "share of eligible contacts with unknown CRM evidence (must be 0)",
        ),
        (
            "c2.qlstage_unresolved.case",
            "pipeline unavailable: Qualified Lead stage unavailable",
            None,
            None,
            "contacts",
        ),
    ],
)
def test_c1b_is_withheld_unless_the_zero_is_judged_on_known_evidence(
    name: str, reason: str, observed: object, required: object, unit: str
) -> None:
    _, ctx = _built(name)
    result = campaigns.evaluate(ctx)
    assert _items(result, C1B) == []
    (line,) = _withheld(result, C1B)
    assert line.reason.startswith(reason), line.reason
    assert (line.observed, line.required, line.unit) == (observed, required, unit)
    assert line.entity is not None
    assert line.entity.type == ENTITY_CAMPAIGN


@pytest.mark.parametrize(
    ("name", "flags", "share", "unknown"),
    [
        (
            "c1b.floor.does_not_fire",
            [FLAG_PIPELINE_UNAVAILABLE, FLAG_ATTRIBUTION_FLOOR],
            Decimal("0.1304"),
            0,
        ),
        (
            "c1b.unknown_evidence.does_not_fire",
            [FLAG_PIPELINE_UNAVAILABLE, FLAG_UNKNOWN_CRM_EVIDENCE],
            Decimal(0),
            20,
        ),
    ],
)
def test_a_pipeline_that_cannot_be_judged_is_named_in_one_finding(
    name: str, flags: list[str], share: Decimal, unknown: int
) -> None:
    scenario, ctx = _built(name)
    result = campaigns.evaluate(ctx)
    (finding,) = [item for item in result.findings if item.code == FINDING_PIPELINE_UNAVAILABLE]
    assert finding.section == CAMPAIGNS
    assert finding.entity is not None
    assert finding.entity.id == _campaign_id(scenario, "linkedin", "li-pipeline")
    assert finding.message.startswith("Pipeline unavailable for Pipeline Report - Lead gen: ")
    assert finding.evidence["flags"] == flags
    assert len(finding.evidence["flag_messages"]) == len(flags)
    assert finding.evidence["unresolved_share"] == share
    assert finding.evidence["unknown_evidence_contacts"] == unknown
    assert finding.evidence["eligible_contacts"] == 20


def test_c1b_ignores_campaigns_whose_contacts_reached_qualified_lead() -> None:
    _, ctx = _built("c2.outcome_scale.case")
    result = campaigns.evaluate(ctx)
    assert _items(result, C1B) == []
    assert _withheld(result, C1B) == []


def test_c1b_says_nothing_about_a_campaign_without_matured_leads() -> None:
    scenario, ctx = _built("c1b.eligible.fires")
    campaign_id = _campaign_id(scenario, "linkedin", "li-pipeline")
    leadless = _fresh(ctx, leads=())
    assert attribution_for(leadless).veto[campaign_id].matured_leads == 0
    result = campaigns.evaluate(leadless)
    assert _items(result, C1B) == []
    assert _withheld(result, C1B) == []


def test_c1b_needs_a_long_window_and_an_active_linkedin_campaign() -> None:
    _, ctx = _built("c2.cap_evidence.fires")
    (line,) = _withheld(campaigns.evaluate(ctx), C1B)
    assert (line.entity, line.reason, line.observed, line.required, line.unit) == (
        None,
        "window 7 days of 28 required",
        7,
        28,
        "days",
    )
    paused = {cid: replace(c, status="paused") for cid, c in ctx.campaigns.items()}
    silent = campaigns.evaluate(_fresh(ctx, campaigns=paused))
    assert _withheld(silent, C1B) == []
    assert _withheld(silent, C3A) == []
    assert silent.findings == ()


def test_c1b_shortfall_reads_the_population_in_order() -> None:
    _, ctx = _built("c1b.eligible.fires")
    settings = campaigns._Settings.of(ctx)
    assert campaigns._c1b_shortfall(settings, _population(1, source="linkedin")) is None
    unavailable = _population(1, available=False, reason="no CRM dataset", source="linkedin")
    assert campaigns._c1b_shortfall(settings, unavailable) == (
        "pipeline unavailable: no CRM dataset",
        None,
        None,
        "contacts",
    )
    no_meetings = _population(1, meetings=None, source="linkedin")
    assert campaigns._c1b_shortfall(settings, no_meetings) == (
        "pipeline unavailable: meetings unavailable",
        None,
        None,
        "contacts",
    )
    # Unknown evidence with no eligible contact reads as a whole unknown share.
    unknown = _population(1, eligible=0, unknown=1, available=False, reason="x", source="linkedin")
    assert campaigns._c1b_shortfall(settings, unknown) == (
        "unknown CRM evidence for 1 of 0 eligible contacts",
        Decimal(1),
        0,
        "share of eligible contacts with unknown CRM evidence (must be 0)",
    )
    no_ql = _population(1, ql=None, available=False, reason="QL", source="linkedin")
    assert campaigns._c1b_shortfall(settings, no_ql) == (
        "pipeline unavailable: QL",
        None,
        None,
        "contacts",
    )


def test_a_c1b_item_needs_a_population() -> None:
    scenario, ctx = _built("c1b.eligible.fires")
    campaign = ctx.campaigns[_campaign_id(scenario, "linkedin", "li-pipeline")]
    with pytest.raises(ValueError, match="C1b needs a pipeline population"):
        campaigns._c1b_candidate(
            ctx,
            campaigns._Settings.of(ctx),
            attribution_for(ctx),
            campaign,
            campaigns._campaign_ref(campaign),
            campaigns._CrmView(None, None),
        )


# ---------------------------------------------------------------------------
# C2 Scale or Hold
# ---------------------------------------------------------------------------


def test_c2_scale_links_its_b1_item_and_declares_its_g1_targets() -> None:
    scenario, ctx = _built("c2.outcome_scale.case")
    campaign_id = _campaign_id(scenario, "linkedin", "li-pipeline")
    result = campaigns.evaluate(ctx)
    item = _only(result, C2)
    subject = DecisionSubject("campaign", campaign_id)
    assert (item.action, item.group) == (SCALE_ACTION, GROUP_SCALE)
    assert item.decision_subject == subject
    assert item.increases_spend_on == (subject,)
    assert item.depends_on == (subject,)
    assert item.flags == ()
    extra = item.evidence.extra
    assert (extra["outcome"], extra["b1"], extra["matured_ql"]) == ("Scale", True, 3)
    assert extra["b1_link"] == {
        "rule_id": B1,
        "subject": {"type": "campaign", "id": campaign_id},
        "current_daily_budget": Decimal("100.00"),
        "proposed_daily_budget": Decimal("120.00"),
    }
    assert extra["gate"]["known_budget_days"] == {"observed": 28, "required": 7}
    assert extra["observation_assumption"].startswith("unchanged across ")
    assert _metrics(item)["proposed_daily_budget"] == Decimal("120.00")
    assert set(item.evidence.thresholds) == {*C2_THRESHOLD_KEYS, *VETO_THRESHOLD_KEYS}
    assert item.evidence.sample is not None
    assert item.evidence.sample.unit == "known-budget days"
    assert [baseline.key for baseline in item.evidence.baselines] == [
        "baseline_cost_per_outcome",
        "max_cost_per_outcome",
    ]
    assert item.rank_key == (Decimal("129.333333"),)
    assert item.reason == (
        "Capped on 28 of 28 known-budget days with 21 leads at USD 129.33 per lead (account "
        "USD 129.33): raise the daily budget from USD 100.00 to USD 120.00 (B1)."
    )
    assert item.required_datasets == (
        "linkedin.entities",
        "linkedin.analytics_daily",
        "linkedin.leads",
        "hubspot.contacts",
        "hubspot.associations",
    )


def test_c2_hold_under_the_veto_offers_no_b1_and_raises_no_spend() -> None:
    _, ctx = _built("c2.outcome_hold.case")
    item = _only(campaigns.evaluate(ctx), C2)
    assert (item.action, item.group) == (HOLD_ACTION, GROUP_SCALE)
    assert item.decision_subject is None
    assert item.increases_spend_on == ()
    assert "proposed_daily_budget" not in _metrics(item)
    assert "b1_link" not in item.evidence.extra
    assert (item.evidence.extra["outcome"], item.evidence.extra["b1"]) == ("Hold", False)
    assert item.reason == (
        "Capped on 28 of 28 known-budget days with 21 leads at USD 129.33 per lead (account "
        "USD 129.33), but 21 eligible contacts from matured leads reached 0 Qualified Leads: "
        "hold the budget."
    )
    (entry,) = scale_outcomes(ctx)
    assert isinstance(entry, ScaleOutcome)
    assert entry.kind == "hold"
    assert entry.veto is not None
    assert entry.veto.veto_fires


@pytest.mark.parametrize(
    ("name", "matched", "unresolved"),
    [("c2.qlstage_unresolved.case", 21, 0), ("veto.unresolved.fires", 22, 3)],
)
def test_c2_scales_with_a_flag_when_the_pipeline_cannot_be_judged(
    name: str, matched: int, unresolved: int
) -> None:
    _, ctx = _built(name)
    result = campaigns.evaluate(ctx)
    item = _only(result, C2)
    assert item.action == SCALE_ACTION
    assert item.has_flag(FLAG_PIPELINE_UNAVAILABLE)
    assert item.flags[0].message.startswith("Pipeline unavailable: ")
    metrics = _metrics(item)
    assert (metrics["matched_leads"], metrics["unresolved"]) == (matched, unresolved)
    assert metrics["matured_leads"] == matched + unresolved
    assert FINDING_PIPELINE_UNAVAILABLE in _codes(result)
    assert item.evidence.extra["b1"] is True


def test_the_veto_cohort_shows_young_leads_beside_the_judged_population() -> None:
    _, ctx = _built("veto.cohort.case")
    item = _only(campaigns.evaluate(ctx), C2)
    assert item.action == HOLD_ACTION
    young = item.evidence.metric("young_leads")
    young_ql = item.evidence.metric("young_ql")
    assert (young.value, young_ql.value) == (2, 2)
    assert young.label == "Leads younger than 14 days (not judged)"
    assert young_ql.label == "Qualified Leads from those leads (not judged)"
    assert item.evidence.extra["matured_ql"] == 0
    assert item.evidence.metric("matured_leads").label == (
        "Matured leads (at least 14 days before window end)"
    )


def test_young_leads_reach_ql_only_through_contacts_not_already_judged() -> None:
    _, ctx = _built("veto.cohort.case")
    cutoff = ctx.window.utc_bounds()[1] - timedelta(days=14)
    young = sorted(
        (lead for lead in ctx.leads if lead.submitted_at > cutoff), key=lambda lead: lead.id
    )
    matured_contact = next(
        lead.contact_hs_id for lead in ctx.leads if lead.submitted_at <= cutoff and lead.matched
    )
    assert len(young) == 2
    assert all(lead.matched for lead in young)
    # One young lead is unmatched; the other belongs to a contact already eligible through a
    # matured lead. Neither can add a young Qualified Lead, yet both stay young leads.
    changed = {
        young[0].id: replace(young[0], match_status="no_match", contact_hs_id=None),
        young[1].id: replace(young[1], contact_hs_id=matured_contact),
    }
    leads = tuple(changed.get(lead.id, lead) for lead in ctx.leads)
    item = _only(campaigns.evaluate(_fresh(ctx, leads=leads)), C2)
    assert (_metrics(item)["young_leads"], _metrics(item)["young_ql"]) == (2, 0)


class _CountingLeads(tuple[LeadInfo, ...]):
    """``ctx.leads`` that counts the passes made over it, to prove a single scan per run."""

    passes = 0

    def __iter__(self) -> Iterator[LeadInfo]:
        self.passes += 1
        return super().__iter__()


def test_the_young_cohort_scans_the_window_leads_once_per_run() -> None:
    _, built = _built("veto.cohort.case")
    (campaign_id,) = {lead.campaign_id for lead in built.leads}
    # Spread the leads over three campaigns, so the cohort reads several groups of one run.
    ids = (campaign_id, campaign_id + 1000, campaign_id + 2000)
    spread = tuple(
        replace(lead, campaign_id=ids[index % len(ids)]) for index, lead in enumerate(built.leads)
    )
    leads = _CountingLeads(spread)
    ctx = _fresh(built, leads=leads)
    settings = campaigns._Settings.of(ctx)
    absent = campaign_id + 3000
    cohorts = [campaigns._young_cohort(ctx, settings, cid) for cid in (*ids, absent, *ids)]
    assert leads.passes == 1
    by_campaign = ctx.cache["campaigns.leads_by_campaign"]
    for cid in ids:
        own = [lead for lead in spread if lead.campaign_id == cid]
        assert by_campaign[cid] == tuple(own)
        assert campaigns._young_cohort(ctx, settings, cid) == (
            campaigns._young_cohort_counts(ctx, settings, own)
        )
    assert cohorts[3] == (0, 0)
    assert cohorts[4:] == cohorts[:3]
    assert leads.passes == 1


def test_every_veto_card_of_a_run_shares_one_scan_of_the_window_leads() -> None:
    _, built = _built("veto.cohort.case")
    leads = _CountingLeads(built.leads)
    ctx = _fresh(built, leads=leads)
    attribution_for(ctx)
    leads.passes = 0  # the attribution table's own passes are not the young cohort's
    result = campaigns.evaluate(ctx)
    cards = [item for item in result.candidates if "young_leads" in _metrics(item)]
    assert sorted(item.rule_id for item in cards) == [C1B, C2]
    assert {(_metrics(item)["young_leads"], _metrics(item)["young_ql"]) for item in cards} == {
        (2, 2)
    }
    assert leads.passes == 1


def test_repeated_young_cohort_reads_of_a_campaign_reuse_one_memoised_result() -> None:
    _, ctx = _built("veto.cohort.case")
    (campaign_id,) = {lead.campaign_id for lead in ctx.leads}
    settings = campaigns._Settings.of(ctx)
    first = campaigns._young_cohort(ctx, settings, campaign_id)
    assert first == (2, 2)
    assert campaigns._young_cohort(ctx, settings, campaign_id) is first
    assert ctx.cache["campaigns.young_cohort"] == {(campaign_id, 14): first}
    fresh = _fresh(ctx)
    assert campaigns._young_cohort(fresh, campaigns._Settings.of(fresh), campaign_id) == first
    # The memo is keyed by the maturity too, so other settings never read the 14-day cohort.
    wider = replace(settings, maturity_days=28)
    assert campaigns._young_cohort(ctx, wider, campaign_id) == (22, 2)
    assert campaigns._young_cohort(_fresh(ctx), wider, campaign_id) == (22, 2)
    assert campaigns._young_cohort(ctx, settings, campaign_id) is first
    no_crm = _fresh(ctx, linkedin_crm=None)
    assert campaigns._young_cohort(no_crm, settings, campaign_id) == (2, None)


def test_a_subject_without_a_latest_daily_budget_proposes_no_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, ctx = _built("c2.outcome_scale.case")
    real = campaigns.budget_history

    def unknown_budget(context: AnalysisContext, subject: Any) -> Any:
        return replace(real(context, subject), latest_daily_budget=None)

    monkeypatch.setattr(campaigns, "budget_history", unknown_budget)
    assert scale_outcomes(ctx) == ()
    assert _items(campaigns.evaluate(_fresh(ctx)), C2) == []


def test_c2_scales_a_shared_budget_without_a_crm_link_at_budget_level() -> None:
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool = scenario.ids.id("google", "budget", "pool")
    members = (_campaign_id(scenario, "google", "m1"), _campaign_id(scenario, "google", "m2"))
    result = campaigns.evaluate(ctx)
    item = _only(result, C2)
    budget = DecisionSubject("budget", pool)
    assert (item.entity_ref.type, item.entity_ref.id) == (ENTITY_BUDGET, pool)
    assert item.action == SCALE_ACTION
    assert item.flag_codes == (FLAG_NO_CRM_LINK, FLAG_SHARED_BUDGET)
    assert item.flags[1].message == "Shared budget across 2 campaigns: decided at budget level"
    assert item.decision_subject == budget
    assert item.increases_spend_on == (budget, *(DecisionSubject("campaign", m) for m in members))
    assert item.evidence.extra["members"] == [
        {"campaign_id": members[0], "name": "Generic - m1", "member_days": 28},
        {"campaign_id": members[1], "name": "Generic - m2", "member_days": 28},
    ]
    assert "matured_ql" not in item.evidence.extra
    assert set(item.evidence.thresholds) == set(C2_THRESHOLD_KEYS)
    assert item.required_datasets == ("google.entities", "google.metrics_daily")
    assert "budget-active days" in item.reason
    (finding,) = [found for found in result.findings if found.code == FINDING_NO_CRM_LINK]
    assert finding.evidence["items"] == 1
    (entry,) = scale_outcomes(ctx)
    assert isinstance(entry, ScaleOutcome)
    assert entry.shared
    assert entry.decision_subject == budget
    assert entry.members == members
    assert (entry.current_daily_budget, entry.proposed_daily_budget) == (
        Decimal(1000),
        Decimal("1200.00"),
    )


def test_a_member_that_joined_mid_window_lists_only_its_member_days() -> None:
    scenario, ctx = _built("shared.membership_mid_window.case")
    item = _only(campaigns.evaluate(ctx), C2)
    m3 = _campaign_id(scenario, "google", "m3")
    (member,) = [row for row in item.evidence.extra["members"] if row["campaign_id"] == m3]
    assert member["member_days"] == scenario.expect.values["member_days"]
    assert item.evidence.extra["changed_intraday_dates"] == (scenario.expect.values["changed_day"],)
    assert _metrics(item)["changed_intraday_days"] == 1


def test_a_g1_blocked_scale_is_still_built_for_the_post_filter() -> None:
    scenario, ctx = _built("shared.poor_member.fires")
    pool = scenario.ids.id("google", "budget", "pool")
    (entry,) = scale_outcomes(ctx)
    assert isinstance(entry, ScaleOutcome)
    assert (entry.subject, entry.kind, entry.g1_blocked) == (("budget", pool), "scale", True)
    item = _only(campaigns.evaluate(ctx), C2)
    assert item.evidence.extra["g1_blocked"] is True
    assert DecisionSubject("budget", pool) in item.increases_spend_on


def test_a_shared_budget_holds_on_its_members_combined_google_veto() -> None:
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool = scenario.ids.id("google", "budget", "pool")
    m1 = _campaign_id(scenario, "google", "m1")
    external = scenario.ids.external_id("google", "campaign", "m1")
    linked = _fresh(ctx, google_crm=_google_crm(external, 20))
    item = _only(campaigns.evaluate(linked), C2)
    assert (item.entity_ref.id, item.action) == (pool, HOLD_ACTION)
    assert item.flag_codes == (FLAG_SHARED_BUDGET,)
    assert item.decision_subject is None
    assert item.increases_spend_on == ()
    metrics = _metrics(item)
    assert (metrics["leads"], metrics["matured_leads"], metrics["eligible_contacts"]) == (
        20,
        20,
        20,
    )
    assert (metrics["mql"], metrics["ql"], metrics["meetings"]) == (0, 0, 0)
    assert "young_leads" not in metrics
    assert item.evidence.metric("leads").label == "Google touch-date contacts"
    assert item.evidence.metric("mql").label == "MQL"
    assert item.evidence.metric("matured_leads").unit == "contacts"
    assert set(item.evidence.thresholds) == {*C2_THRESHOLD_KEYS, *VETO_THRESHOLD_KEYS}
    assert item.required_datasets == (
        "google.entities",
        "google.metrics_daily",
        *GOOGLE_POPULATION_DATASETS,
    )
    (entry,) = scale_outcomes(linked)
    assert isinstance(entry, ScaleOutcome)
    assert (entry.kind, entry.crm_scope) == ("hold", "google")
    assert entry.veto is not None
    assert entry.veto.campaign_id == m1


def test_a_hold_on_a_g1_blocked_subject_is_not_a_scale_candidate() -> None:
    scenario, ctx = _built("shared.poor_member.fires")
    external = scenario.ids.external_id("google", "campaign", "m1")
    linked = _fresh(ctx, google_crm=_google_crm(external, 20))
    assert scale_outcomes(linked) == ()
    assert _items(campaigns.evaluate(linked), C2) == []


@pytest.mark.parametrize(
    ("name", "reason", "observed", "required", "unit", "history"),
    [
        (
            "c2.cap_evidence_short.case",
            "known-budget days 4 of 7 required",
            4,
            7,
            "known-budget days",
            {"known_active": 4, "unobserved": 3},
        ),
        (
            "c2.cap_evidence.does_not_fire",
            "spending days 1 of 5 required",
            1,
            5,
            "spending days",
            {"known_active": 7, "spending": 1, "capped": 1, "idle": 6},
        ),
    ],
)
def test_short_cap_evidence_is_withheld_with_observed_and_required(
    name: str,
    reason: str,
    observed: int,
    required: int,
    unit: str,
    history: dict[str, int],
) -> None:
    _, ctx = _built(name)
    result = campaigns.evaluate(ctx)
    assert _items(result, C2) == []
    (entry,) = scale_outcomes(ctx)
    assert isinstance(entry, ScaleWithheld)
    assert entry.kind == "withheld"
    assert entry.reason.startswith(reason)
    assert (entry.observed, entry.required, entry.unit) == (observed, required, unit)
    assert entry.history is not None
    assert entry.gate is not None
    assert not entry.gate.passed
    assert entry.reason == entry.gate.describe()
    for attribute, count in history.items():
        assert getattr(entry.history, attribute) == count, attribute
    (line,) = _withheld(result, C2)
    assert line == entry.to_withheld(C2, CAMPAIGNS)
    budget_line = entry.to_withheld(B1, BUDGET)
    assert (budget_line.rule_id, budget_line.section, budget_line.entity) == (
        B1,
        BUDGET,
        entry.entity,
    )
    assert (budget_line.reason, budget_line.observed, budget_line.required) == (
        entry.reason,
        observed,
        required,
    )


def test_c2_is_withheld_below_the_sustained_window() -> None:
    _, ctx = _built("p5.sustained_min.does_not_fire")
    (entry,) = scale_outcomes(ctx)
    assert isinstance(entry, ScaleWithheld)
    assert (entry.reason, entry.observed, entry.required, entry.unit) == (
        "window 6 days of 7 required",
        6,
        7,
        "days",
    )
    assert (entry.history, entry.gate) == (None, None)
    (line,) = _withheld(campaigns.evaluate(ctx), C2)
    assert line.reason == "window 6 days of 7 required"


def test_an_uncapped_or_inefficient_subject_is_no_scale_candidate() -> None:
    scenario, ctx = _built("c2.outcome_scale.case")
    # Every known day is below the capped-day ratio: a full history that is simply uncapped.
    uncapped = _tuned(scenario, ctx, {"budget.capped_day_ratio": 0.99})
    assert scale_outcomes(uncapped) == ()
    assert _withheld(campaigns.evaluate(uncapped), C2) == []
    # Cost per outcome above the allowed multiple of the baseline: not good output.
    costly = _tuned(scenario, ctx, {"scale.max_cpa_ratio": 0.5})
    assert scale_outcomes(costly) == ()


def test_a_subject_with_no_active_member_is_skipped() -> None:
    _, ctx = _built("shared.poor_member.does_not_fire")
    paused = {cid: replace(c, status="paused") for cid, c in ctx.campaigns.items()}
    assert scale_outcomes(_fresh(ctx, campaigns=paused)) == ()


def test_members_in_two_currencies_are_withheld_as_efficiency_na() -> None:
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool = scenario.ids.id("google", "budget", "pool")
    m2 = _campaign_id(scenario, "google", "m2")
    euro = AccountInfo(7, "google", "9900000007", "Euro account", "EUR", "Europe/Berlin")
    moved = {**ctx.campaigns, m2: replace(ctx.campaigns[m2], account_id=7, currency="EUR")}
    mixed = _fresh(ctx, campaigns=moved, accounts={**ctx.accounts, 7: euro})
    entries = [entry for entry in scale_outcomes(mixed) if entry.subject == ("budget", pool)]
    (entry,) = entries
    assert isinstance(entry, ScaleWithheld)
    assert (entry.reason, entry.observed, entry.required, entry.unit) == (
        "members span more than one currency: efficiency n/a",
        None,
        None,
        "currencies",
    )
    assert entry.members == (_campaign_id(scenario, "google", "m1"), m2)


def test_members_in_two_objective_groups_are_withheld_as_efficiency_na(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool = ("budget", scenario.ids.id("google", "budget", "pool"))

    def no_totals(context: AnalysisContext, subject: object) -> None:
        return None

    def two_groups(context: AnalysisContext, subject: object) -> frozenset[str]:
        return frozenset({OBJ_GOOGLE, OBJ_LINKEDIN_LEAD_GEN})

    monkeypatch.setattr(campaigns, "subject_totals", no_totals)
    monkeypatch.setattr(campaigns, "subject_objective_groups", two_groups)
    (entry,) = [entry for entry in scale_outcomes(ctx) if entry.subject == pool]
    assert isinstance(entry, ScaleWithheld)
    assert (entry.reason, entry.observed, entry.required, entry.unit) == (
        "members span 2 objective groups: efficiency n/a",
        2,
        1,
        "objective groups",
    )


def test_the_outcome_label_falls_back_without_a_loaded_member() -> None:
    scenario, ctx = _built("c2.outcome_scale.case")
    campaign_id = _campaign_id(scenario, "linkedin", "li-pipeline")
    assert campaigns._outcome_label(ctx, (campaign_id,)) == "leads"
    assert campaigns._outcome_label(ctx, (999_999,)) == "outcomes"


def test_combined_veto_sums_members_and_names_why_it_cannot_judge() -> None:
    _, ctx = _built("shared.poor_member.does_not_fire")
    settings = campaigns._Settings.of(ctx)
    assert campaigns._combined_veto(ctx, settings, []) is None
    judged = campaigns._combined_veto(
        ctx,
        settings,
        [_population(5, matured=12), _population(3, matured=10, unresolved=1)],
    )
    assert judged is not None
    assert (judged.campaign_id, judged.source, judged.leads) == (3, "google", 22)
    assert (judged.matured_leads, judged.matched_leads, judged.unresolved_leads) == (22, 21, 1)
    assert judged.unresolved_share == Decimal(1) / Decimal(22)
    assert (judged.eligible_contacts, judged.ql, judged.mql, judged.meetings) == (21, 0, 0, 0)
    assert (judged.available, judged.reason, judged.attribution_floor) == (True, None, False)
    assert judged.veto_fires
    unavailable = campaigns._combined_veto(
        ctx,
        settings,
        [_population(2), _population(999, available=False, reason="QL stage unresolved")],
    )
    assert unavailable is not None
    assert (unavailable.available, unavailable.veto_fires) == (False, False)
    assert unavailable.reason == "Campaign #999: QL stage unresolved"
    floored = campaigns._combined_veto(
        ctx, settings, [_population(2, matured=10, unresolved=1), _population(3, unresolved=3)]
    )
    assert floored is not None
    assert floored.attribution_floor
    assert floored.reason == "13.3% of matured leads unresolved in HubSpot (floor 10%)"
    unknown_ql = campaigns._combined_veto(ctx, settings, [_population(2), _population(3, ql=None)])
    assert unknown_ql is not None
    assert (unknown_ql.ql, unknown_ql.mql, unknown_ql.veto_fires) == (None, None, False)
    empty = campaigns._combined_veto(ctx, settings, [_population(2, matured=0)])
    assert empty is not None
    assert (empty.unresolved_share, empty.attribution_floor, empty.available) == (
        None,
        False,
        True,
    )


def test_veto_flags_explain_every_reason_the_pipeline_cannot_be_judged() -> None:
    floored = _population(
        1, matured=20, unresolved=4, unknown=2, available=False, reason="20% unresolved", floor=True
    )
    flags = campaigns._veto_flags(floored)
    assert [(flag.code, flag.message) for flag in flags] == [
        (FLAG_PIPELINE_UNAVAILABLE, "Pipeline unavailable: 20% unresolved"),
        (FLAG_ATTRIBUTION_FLOOR, "Floor: 20% of matured leads unmatched in HubSpot"),
        (FLAG_UNKNOWN_CRM_EVIDENCE, "Unknown CRM evidence for 2 of 16 eligible contacts"),
    ]
    assert campaigns._veto_flags(_population(1)) == ()


def test_a_floored_population_without_a_reason_still_gets_a_finding() -> None:
    _, ctx = _built("c1b.eligible.fires")
    entity = EntityRef("linkedin", ENTITY_CAMPAIGN, 1, "990010000", "Pipeline Report")
    notes = campaigns._Notes()
    population = _population(1, matured=10, unresolved=2, floor=True, source="linkedin")
    notes.note_view(ctx, entity, campaigns._CrmView(population, "linkedin"))
    notes.note_view(ctx, entity, campaigns._CrmView(population, "linkedin"))
    (finding,) = notes.findings()
    assert finding.message == "Pipeline unavailable for Pipeline Report: attribution floor reached"
    assert finding.evidence["unresolved_share"] == Decimal("0.2000")
    assert finding.evidence["unknown_evidence_share"] == Decimal(0)
    assert campaigns._CrmView(None, "linkedin").datasets == ()
    no_contacts = _population(2, matured=0, available=False, reason="none", source="linkedin")
    other = EntityRef("linkedin", ENTITY_CAMPAIGN, 2, "990010002", "Other")
    notes.note_view(ctx, other, campaigns._CrmView(no_contacts, "linkedin"))
    (second,) = [found for found in notes.findings() if found.entity == other]
    assert second.message == "Pipeline unavailable for Other: none"
    assert (second.evidence["unresolved_share"], second.evidence["unknown_evidence_share"]) == (
        None,
        None,
    )


# ---------------------------------------------------------------------------
# C3a Launch: underused proven segment
# ---------------------------------------------------------------------------


def test_c3a_launches_an_underused_proven_segment() -> None:
    scenario, ctx = _built("c3a.preset.case")
    campaign_id = _campaign_id(scenario, "linkedin", "li-seg")
    item = _only(campaigns.evaluate(ctx), C3A)
    assert item.action == (
        "Launch a campaign targeting seniority: urn:li:seniority:6 with "
        "Pipeline Report - Lead gen's offer"
    )
    assert item.group == GROUP_LAUNCH
    assert item.entity_ref.type == ENTITY_SEGMENT
    assert item.entity_ref.name == "Pipeline Report - Lead gen: seniority urn:li:seniority:6"
    assert (item.decision_subject, item.increases_spend_on, item.flags) == (None, (), ())
    assert item.depends_on == (DecisionSubject("campaign", campaign_id),)
    assert item.rank_key == (-20,)
    assert item.reason == (
        "Seniority urn:li:seniority:6 produced 20 leads at USD 25.00 per lead, 0.5x the "
        "campaign's USD 50.00, from 10% of its impressions."
    )
    assert item.evidence.extra["segment_window"] == {
        "start": ctx.window.start,
        "end": ctx.window.end,
    }
    assert item.evidence.extra["campaign_id"] == campaign_id
    assert [(baseline.key, baseline.value) for baseline in item.evidence.baselines] == [
        ("campaign_cpl", Decimal("50.000000")),
        ("max_segment_cpl", Decimal("40.000000")),
    ]
    assert set(item.evidence.thresholds) == set(C3A_THRESHOLD_KEYS)
    assert item.required_datasets == (
        "linkedin.segments",
        "linkedin.analytics_daily",
        "linkedin.entities",
    )


@pytest.mark.parametrize(
    ("name", "case"),
    [
        ("c3a.not_yet_available.case", "not_yet_available"),
        ("c3a.custom_window.case", "preset_windows_only"),
    ],
)
def test_c3a_is_withheld_when_no_segment_window_matches_the_run(name: str, case: str) -> None:
    scenario, ctx = _built(name)
    result = campaigns.evaluate(ctx)
    assert _items(result, C3A) == []
    (finding,) = [
        item for item in result.findings if item.code == FINDING_SEGMENT_WINDOW_UNAVAILABLE
    ]
    assert finding.message == scenario.expect.label
    assert finding.entity is None
    assert finding.evidence["case"] == case
    assert finding.evidence["window"] == {
        "start": ctx.window.start,
        "end": ctx.window.end,
        "days": ctx.window.days,
    }
    assert finding.evidence["presets"] == (14, 28, 56, 90)
    assert finding.evidence["fetched_windows"] == [
        {"start": start, "end": end} for start, end in sorted(ctx.segment_windows)
    ]
    (line,) = _withheld(result, C3A)
    assert (line.entity, line.reason, line.observed, line.required, line.unit) == (
        None,
        scenario.expect.label,
        None,
        None,
        "segment windows",
    )


def test_c3a_never_reuses_another_window_and_reports_when_none_was_fetched() -> None:
    _, ctx = _built("c3a.preset.case")
    unfetched = _fresh(ctx, segment_windows=())
    result = campaigns.evaluate(unfetched)
    assert _items(result, C3A) == []
    (line,) = _withheld(result, C3A)
    assert line.reason == (
        f"Segment data for the 28-day window ending {ctx.window.end.isoformat()} not yet "
        "available; latest available ends none"
    )


@pytest.mark.parametrize(
    ("name", "check"),
    [("c3a.share.does_not_fire", "share"), ("c3a.no_lead_pivot.does_not_fire", "leads")],
)
def test_c3a_does_not_fire_on_a_large_share_or_a_pivot_without_leads(name: str, check: str) -> None:
    scenario, ctx = _built(name)
    campaign_id = _campaign_id(scenario, "linkedin", "li-seg")
    (row,) = [
        row
        for row in ctx.segments
        if row.campaign_id == campaign_id and row.value == "urn:li:seniority:6"
    ]
    if check == "share":
        assert row.impressions is not None
        share = Decimal(row.impressions) / Decimal(100_000)
        assert share == Decimal(scenario.expect.values["impression_share"])
    else:
        assert row.leads is scenario.expect.values["segment_leads"] is None
    result = campaigns.evaluate(ctx)
    assert _items(result, C3A) == []
    assert all(
        line.entity is None or "urn:li:seniority:6" not in line.entity.name
        for line in _withheld(result, C3A)
    )


def test_c3a_cost_and_lead_minimums() -> None:
    scenario, ctx = _built("c3a.preset.case")
    dearer = campaigns.evaluate(_tuned(scenario, ctx, {"c3a.max_cpl_ratio": 0.4}))
    assert _items(dearer, C3A) == []
    assert _withheld(dearer, C3A) == []
    fewer = campaigns.evaluate(_tuned(scenario, ctx, {"c3a.min_leads": 21}))
    assert _items(fewer, C3A) == []
    (line,) = _withheld(fewer, C3A)
    assert (line.reason, line.observed, line.required, line.unit) == (
        "segment leads 20 of 21 required",
        20,
        21,
        "leads",
    )
    assert line.entity is not None
    assert line.entity.type == ENTITY_SEGMENT


def test_c3a_reads_only_lead_gen_campaigns_their_window_and_their_leads() -> None:
    scenario, ctx = _built("c3a.preset.case")
    campaign_id = _campaign_id(scenario, "linkedin", "li-seg")
    campaign = ctx.campaigns[campaign_id]
    other = replace(
        campaign, objective_or_channel="WEBSITE_VISIT", objective_group=OBJ_LINKEDIN_OTHER
    )
    not_lead_gen = _fresh(ctx, campaigns={**ctx.campaigns, campaign_id: other})
    assert _items(campaigns.evaluate(not_lead_gen), C3A) == []
    shifted = tuple(
        replace(row, window_start=row.window_start - timedelta(days=1)) for row in ctx.segments
    )
    assert _items(campaigns.evaluate(_fresh(ctx, segments=shifted)), C3A) == []
    no_facts = _fresh(ctx, campaign_days={**ctx.campaign_days, campaign_id: ()})
    assert _items(campaigns._c3a(no_facts, campaigns._Settings.of(no_facts)), C3A) == []


def test_c3a_needs_an_active_linkedin_campaign() -> None:
    _, ctx = _built("c3a.custom_window.case")
    paused = {cid: replace(c, status="paused") for cid, c in ctx.campaigns.items()}
    result = campaigns._c3a(_fresh(ctx, campaigns=paused), campaigns._Settings.of(ctx))
    assert result == RuleResult()


def _without_campaign_spend(
    scenario: Scenario, ctx: AnalysisContext, *, segment_cost: Decimal | None
) -> AnalysisContext:
    """``ctx`` with zero spend on every day of ``li-seg``, its leads and impressions kept.

    With ``segment_cost``, the proven segment (seniority 6) costs that instead of its own cost.
    """
    campaign_id = _campaign_id(scenario, "linkedin", "li-seg")
    days = tuple(replace(day, cost=Decimal(0)) for day in ctx.campaign_days[campaign_id])
    segments = tuple(
        replace(row, cost=segment_cost)
        if segment_cost is not None and row.value == "urn:li:seniority:6"
        else row
        for row in ctx.segments
    )
    unspent = _fresh(ctx, campaign_days={**ctx.campaign_days, campaign_id: days}, segments=segments)
    window_days = unspent.campaign_days[campaign_id]
    assert sum(day.cost for day in window_days) == 0
    assert sum(day.leads or 0 for day in window_days) > 0
    assert sum(day.impressions for day in window_days) > 0
    return unspent


def test_c3a_withholds_a_zero_cost_segment_when_the_campaign_has_no_spend() -> None:
    """A zero cost per lead gives no baseline: no 0/0 ratio and no item, a "no exposure" line."""
    scenario, ctx = _built("c3a.preset.case")
    unspent = _without_campaign_spend(scenario, ctx, segment_cost=Decimal(0))
    result = campaigns.evaluate(unspent)
    assert _items(result, C3A) == []
    (line,) = _withheld(result, C3A)
    assert (line.reason, line.observed, line.required, line.unit) == (
        "no exposure: zero campaign spend in the window, so segment cost per lead cannot be "
        "compared",
        0,
        1,
        "campaign days with spend",
    )
    assert line.entity is not None
    assert line.entity.type == ENTITY_SEGMENT
    assert line.entity.name == "Pipeline Report - Lead gen: seniority urn:li:seniority:6"
    fewer = campaigns.evaluate(_tuned(scenario, unspent, {"c3a.min_leads": 21}))
    assert _items(fewer, C3A) == []
    assert [line.reason for line in _withheld(fewer, C3A)] == ["segment leads 20 of 21 required"]


def test_c3a_stays_silent_on_a_costly_segment_when_the_campaign_has_no_spend() -> None:
    scenario, ctx = _built("c3a.preset.case")
    assert all(row.cost is not None and row.cost > 0 for row in ctx.segments)
    result = campaigns.evaluate(_without_campaign_spend(scenario, ctx, segment_cost=None))
    assert _items(result, C3A) == []
    assert _withheld(result, C3A) == []


def test_c3a_declares_the_entities_it_reads_so_their_freshness_reaches_the_item() -> None:
    """Fresh segments and analytics never hide a stale, incomplete ``linkedin.entities`` (P4)."""
    _, ctx = _built("c3a.preset.case")
    dataset_id = ctx.dataset_id_for("linkedin.entities", crm_scope=None)
    assert dataset_id is not None
    key = (dataset_id, "linkedin.entities")
    behind = ctx.window.end - timedelta(days=3)
    lagging = replace(
        ctx.freshness[key],
        completeness="incomplete",
        covered_through=behind,
        reason="campaign search cut short",
    )
    item = _only(campaigns.evaluate(_fresh(ctx, freshness={**ctx.freshness, key: lagging})), C3A)
    assert "linkedin.entities" in item.required_datasets
    statuses = {status.dataset: status for status in item.evidence.datasets}
    assert set(statuses) == set(item.required_datasets)
    entities = statuses["linkedin.entities"]
    assert (entities.completeness, entities.covered_through) == ("incomplete", behind)
    for name in ("linkedin.segments", "linkedin.analytics_daily"):
        assert (statuses[name].completeness, statuses[name].covered_through) == (
            "complete",
            ctx.window.end,
        )


# ---------------------------------------------------------------------------
# C3b Launch: keyword cluster without a campaign
# ---------------------------------------------------------------------------


def test_c3b_launches_an_uncovered_converting_cluster() -> None:
    scenario, ctx = _built("c3b.cluster.fires")
    search = _campaign_id(scenario, "google", "search")
    result = campaigns.evaluate(ctx)
    item = _only(result, C3B)
    assert item.action == "Create ad group or campaign for 'alternative hubspot'"
    assert item.group == GROUP_LAUNCH
    assert item.entity_ref.type == ENTITY_TERM_CLUSTER
    assert item.entity_ref.name == "Search-term cluster 'alternative hubspot'"
    assert (item.decision_subject, item.increases_spend_on, item.flags) == (None, (), ())
    assert item.rank_key == (-Decimal(6),)
    extra = item.evidence.extra
    assert extra["head_tokens"] == ("alternative", "hubspot")
    assert [member["term"] for member in extra["members"]] == [
        "cheap hubspot alternative",
        "hubspot alternative",
        "hubspot alternative free",
    ]
    assert extra["campaign_ids"] == [search]
    assert extra["source_label"] == "from your search-term report"
    assert _metrics(item) == {
        "members": 3,
        "conversions": Decimal(6),
        "spend": Decimal("60.00"),
        "clicks": 60,
        "cost_per_conversion": Decimal("10.000000"),
    }
    assert item.reason == (
        "3 converting search terms share 'alternative hubspot' with 6 conversions at USD 10.00 "
        "per conversion; no keyword, ad group or campaign name contains those words."
    )
    assert item.depends_on[0] == DecisionSubject("campaign", search)
    assert set(item.evidence.thresholds) == set(C3B_THRESHOLD_KEYS)
    assert item.required_datasets == ("google.search_terms", "google.entities")
    assert result.findings == ()


def test_c3b_does_not_fire_when_a_name_already_holds_a_head_token() -> None:
    scenario, ctx = _built("c3b.cluster.does_not_fire")
    converting = sorted(
        {" ".join(row.search_term.lower().split()) for row in ctx.search_terms if row.conversions}
    )
    clusters = cluster_terms(converting, threshold=Decimal("0.5"), min_head_share=Decimal("0.5"))
    head = frozenset(scenario.expect.values["head_tokens"])
    assert head in [cluster.head_tokens for cluster in clusters]
    assert contains_tokens(campaigns._coverage_texts(ctx), head)
    result = campaigns.evaluate(ctx)
    assert _items(result, C3B) == []
    assert _withheld(result, C3B) == []


@pytest.mark.parametrize(
    ("overrides", "reason", "observed", "required", "unit"),
    [
        (
            {"c3b.min_terms": 4},
            "converting search terms 3 of 4 required",
            3,
            4,
            "converting search terms",
        ),
        (
            {"c3b.min_conversions": 7},
            "conversions 6 of 7 required",
            Decimal(6),
            Decimal(7),
            "conversions",
        ),
    ],
)
def test_c3b_withholds_an_uncovered_cluster_below_its_minimums(
    overrides: dict[str, object], reason: str, observed: object, required: object, unit: str
) -> None:
    scenario, ctx = _built("c3b.cluster.fires")
    result = campaigns.evaluate(_tuned(scenario, ctx, overrides))
    assert _items(result, C3B) == []
    (line,) = _withheld(result, C3B)
    assert (line.reason, line.observed, line.required, line.unit) == (
        reason,
        observed,
        required,
        unit,
    )
    assert line.entity is not None
    assert line.entity.name == "Search-term cluster 'alternative hubspot'"


def test_c3b_reads_only_converting_google_terms() -> None:
    _, ctx = _built("c3b.cluster.fires")
    settings = campaigns._Settings.of(ctx)
    flat = tuple(replace(row, conversions=Decimal(0)) for row in ctx.search_terms)
    assert campaigns._c3b(_fresh(ctx, search_terms=flat), settings) == RuleResult()
    linkedin = _linkedin_campaign(500)
    moved = tuple(replace(row, campaign_id=500) for row in ctx.search_terms)
    elsewhere = _fresh(ctx, campaigns={**ctx.campaigns, 500: linkedin}, search_terms=moved)
    assert campaigns._c3b(elsewhere, settings) == RuleResult()


def test_c3b_currency_falls_back_to_the_google_account_then_to_none() -> None:
    _, ctx = _built("c3b.cluster.fires")
    assert campaigns._google_currency(ctx, [999_999]) == "USD"
    bare = _fresh(ctx, campaigns={}, accounts={})
    assert campaigns._google_currency(bare, [1]) == ""
    result = campaigns._c3b(bare, campaigns._Settings.of(bare))
    item = _only(result, C3B)
    assert item.evidence.metric("spend").currency is None
    assert "at 10.00 per conversion" in item.reason
    assert item.depends_on == (DecisionSubject("campaign", 1),)


_C3B_EXTRA_THEMES: Final = (("pipedrive", "pricing"), ("salesforce", "migration"))
"""Two more themes, each copied from the 'hubspot alternative' terms into its own cluster."""


def _with_extra_clusters(
    ctx: AnalysisContext, themes: tuple[tuple[str, str], ...]
) -> AnalysisContext:
    """Copy ``ctx`` with the 'hubspot alternative' search terms repeated once per theme."""
    cluster_rows = [row for row in ctx.search_terms if "hubspot" in row.search_term]
    extra = tuple(
        replace(
            row, search_term=row.search_term.replace("hubspot", head).replace("alternative", tail)
        )
        for head, tail in themes
        for row in cluster_rows
    )
    return _fresh(ctx, search_terms=(*ctx.search_terms, *extra))


@pytest.mark.parametrize("name", ["c3b.cluster.fires", "c3b.cluster.does_not_fire"])
def test_c3b_coverage_tokens_are_the_union_of_the_coverage_text_tokens(name: str) -> None:
    _, ctx = _built(name)
    texts = campaigns._coverage_texts(ctx)
    expected: set[str] = set()
    for text in texts:
        expected |= tokenize(text)
    covered = campaigns._coverage_tokens(ctx)
    assert covered == frozenset(expected)
    assert texts
    assert covered
    converting = sorted(
        {" ".join(row.search_term.lower().split()) for row in ctx.search_terms if row.conversions}
    )
    clusters = cluster_terms(converting, threshold=Decimal("0.5"), min_head_share=Decimal("0.5"))
    for cluster in clusters:
        assert (not covered.isdisjoint(cluster.head_tokens)) == contains_tokens(
            texts, cluster.head_tokens
        )


@pytest.mark.parametrize(
    ("name", "themes", "launched"),
    [
        ("c3b.cluster.fires", (), ["alternative hubspot"]),
        ("c3b.cluster.does_not_fire", (), []),
        (
            "c3b.cluster.fires",
            _C3B_EXTRA_THEMES,
            ["alternative hubspot", "migration salesforce", "pipedrive pricing"],
        ),
        (
            "c3b.cluster.does_not_fire",
            _C3B_EXTRA_THEMES,
            ["migration salesforce", "pipedrive pricing"],
        ),
    ],
)
def test_c3b_tokenizes_each_coverage_text_once_whatever_the_cluster_count(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    themes: tuple[tuple[str, str], ...],
    launched: list[str],
) -> None:
    _, built = _built(name)
    ctx = _with_extra_clusters(built, themes)
    converting = sorted(
        {" ".join(row.search_term.lower().split()) for row in ctx.search_terms if row.conversions}
    )
    clusters = cluster_terms(converting, threshold=Decimal("0.5"), min_head_share=Decimal("0.5"))
    assert len([c for c in clusters if c.size >= 2 and c.head_tokens]) == 1 + len(themes)
    calls: list[str] = []

    def counting_tokenize(text: str) -> frozenset[str]:
        calls.append(text)
        return tokenize(text)

    monkeypatch.setattr(campaigns, "tokenize", counting_tokenize)
    result = campaigns.evaluate(ctx)
    assert Counter(calls) == Counter(campaigns._coverage_texts(ctx))
    assert sorted(item.action for item in _items(result, C3B)) == [
        f"Create ad group or campaign for '{label}'" for label in launched
    ]
    assert _withheld(result, C3B) == []


# ---------------------------------------------------------------------------
# scale_outcomes: one memoised evaluation for C2 and B1
# ---------------------------------------------------------------------------


def test_scale_outcomes_is_memoised_per_context() -> None:
    _, ctx = _built("c2.outcome_scale.case")
    first = scale_outcomes(ctx)
    assert scale_outcomes(ctx) is first
    assert ctx.cache[SCALE_CACHE_KEY].entries is first
    (entry,) = first
    assert isinstance(entry, ScaleOutcome)
    assert (entry.kind, entry.shared, entry.g1_blocked, entry.crm_scope) == (
        "scale",
        False,
        False,
        "linkedin",
    )
    assert entry.increases_spend_on == (entry.decision_subject,)
    assert entry.proposed_daily_budget == Decimal("120.00")
    # A fresh context recomputes the same evaluation rather than reading another run's memo.
    again = scale_outcomes(_fresh(ctx))
    assert again is not first
    assert again == first


@pytest.mark.parametrize(
    "name",
    [
        "c2.outcome_scale.case",
        "c2.outcome_hold.case",
        "c2.cap_evidence_short.case",
        "veto.unresolved.fires",
        REPEAT_CONTACT,
        "shared.poor_member.fires",
        "shared.poor_member.does_not_fire",
        "p5.sustained_min.does_not_fire",
    ],
)
def test_c2_items_and_withheld_lines_are_the_scale_outcomes(name: str) -> None:
    _, ctx = _built(name)
    entries = scale_outcomes(ctx)
    result = campaigns.evaluate(ctx)
    outcomes = [entry for entry in entries if isinstance(entry, ScaleOutcome)]
    withheld = [entry for entry in entries if isinstance(entry, ScaleWithheld)]
    items = _items(result, C2)
    assert [(item.entity_ref, item.action) for item in items] == [
        (entry.entity, SCALE_ACTION if entry.kind == "scale" else HOLD_ACTION) for entry in outcomes
    ]
    assert _withheld(result, C2) == [entry.to_withheld(C2, CAMPAIGNS) for entry in withheld]
    for item, entry in zip(items, outcomes, strict=True):
        assert item.flags == entry.flags
        assert item.evidence.metric("outcomes").value == entry.outcomes
        if entry.kind == "scale":
            assert item.increases_spend_on == entry.increases_spend_on
            assert item.evidence.extra["b1_link"]["proposed_daily_budget"] == (
                entry.proposed_daily_budget
            )


# ---------------------------------------------------------------------------
# evaluate: rule order, groups and findings
# ---------------------------------------------------------------------------


def test_evaluate_emits_items_in_rule_order_with_their_groups() -> None:
    _, pipeline = _built("c1b.eligible.fires")
    result = campaigns.evaluate(pipeline)
    assert [(item.rule_id, item.group) for item in result.candidates] == [
        (C1B, GROUP_PAUSE),
        (C2, GROUP_SCALE),
    ]
    _, segment = _built("c3a.preset.case")
    assert [(item.rule_id, item.group) for item in campaigns.evaluate(segment).candidates] == [
        (C1B, GROUP_PAUSE),
        (C3A, GROUP_LAUNCH),
    ]
    assert all(item.section == CAMPAIGNS for item in result.candidates)
    assert all(line.section == CAMPAIGNS for line in result.withheld)
