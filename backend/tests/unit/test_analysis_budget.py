"""Unit tests for ``ads_engine.analysis.budget``: rules B1, B2, B3 and B4.

They hold the "Budget changes" rules to AAP 0.5.4.5 (card fields, shared budgets, the B1-B4
table) and the shared definitions they read (0.5.4.1 budget history, utilization, daily
predicates, history gates, short history and lifetime budgets; 0.5.4.2 G1 targets and the
B3 allocation sentences): every catalogue scenario of ``acceptance/test_budget_rules.py``
that names B1, B2, B3 or B4 is evaluated and compared with its expectation, and targeted cases
pin each item's amount, action, evidence, flags, decision affordance and spend-increase
targets, every withheld line's observed against required, the B4 counting-contract reasons,
the CRM evidence of a budget card and the funding sentences rendered for a B3 reallocation.

Contexts come from the scenario catalogue (``SCENARIOS[name].build_context()``); a few cases
copy a built context with :func:`dataclasses.replace` (always with a fresh ``cache``, so no
memoised table of the original leaks in) to move one input across a threshold. Thresholds
are read from ``ctx.thresholds``, never restated as literals. Pure: no database and no
network.
"""

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Final, cast
from zoneinfo import ZoneInfo

import pytest

from ads_engine.analysis import budget
from ads_engine.analysis.attribution import VetoPopulation, attribution_for
from ads_engine.analysis.budget import (
    B1_THRESHOLD_KEYS,
    B2_RULE_THRESHOLD_KEYS,
    B3_THRESHOLD_KEYS,
    B4_TEST_NAME,
    B4_THRESHOLD_KEYS,
    CONTRACT_UNKNOWN_REASON,
    FRACTIONAL_REASON,
    NO_EXPOSURE_REASON,
    WEIGHTED_REASON,
    BudgetCrm,
    budget_crm,
    budget_evidence,
    render_b1_funding,
    render_b3_allocation,
)
from ads_engine.analysis.campaigns import VETO_THRESHOLD_KEYS, cost_per_outcome_value
from ads_engine.analysis.context import (
    B1,
    B2,
    B3,
    B4,
    BUDGET,
    C2,
    ENTITY_BUDGET,
    ENTITY_CAMPAIGN,
    FINDING_NO_CRM_LINK,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_NO_CRM_LINK,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_SHARED_BUDGET,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    GROUP_CHANGES,
    OBJ_LINKEDIN_LEAD_GEN,
    AccountInfo,
    AnalysisContext,
    AssociationFetchInfo,
    Candidate,
    ContactInfo,
    CrmData,
    DatasetRef,
    DayFact,
    DecisionSubject,
    Metric,
    RuleResult,
    SubjectKey,
    Withheld,
    google_link_gap,
)
from ads_engine.analysis.metrics import (
    BUDGET_THRESHOLD_KEYS,
    GateResult,
    budget_history,
    budget_subjects,
    high_utilization,
    member_poor_efficiency,
    subject_baseline_outcomes,
    subject_campaigns,
    under_utilized,
)
from ads_engine.analysis.stats import percentile
from ads_engine.analysis.thresholds import (
    ConfigError,
    CrmBindings,
    GoogleCrmBinding,
    ResolvedBinding,
    Thresholds,
)
from ads_engine.decisions import round_to_minor_unit
from ads_engine.simulation.scenarios import (
    FIXED_NOW,
    MODULE_BUDGET,
    SCENARIOS,
    Scenario,
    for_module,
    thresholds_with,
)

BUDGET_RULES: Final = (B1, B2, B3, B4)
"""The rules ``budget.evaluate`` implements; G1 and the B3 allocation live in precedence."""

RULE_SCENARIOS: Final = tuple(
    scenario for scenario in for_module(MODULE_BUDGET) if scenario.expect.rule_id in BUDGET_RULES
)

PRECEDENCE_KEYS: Final = frozenset(
    {"allocations", "unallocated", "funded_by_reallocation", "from_new_budget"}
)
"""Expectation keys that are outcomes of the AAP 0.5.4.2 allocation precedence performs.

``budget.evaluate`` supplies its inputs (each B3 item's ``extra.freed``, each B1 item's
``extra.increase``) and the renderers; dedicated tests below pin those.
"""

CRM_DATASETS: Final = {
    "linkedin": ("linkedin.leads", "hubspot.contacts", "hubspot.associations"),
    "google": ("hubspot.google_contacts", "hubspot.contacts", "hubspot.associations"),
}
"""The effective CRM datasets a Qualified Lead count reads, per scope (AAP 0.5.4.1)."""

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


def _with_days(ctx: AnalysisContext, campaign_id: int, days: Iterable[DayFact]) -> AnalysisContext:
    """Copy ``ctx`` with one campaign's daily facts replaced."""
    return _fresh(ctx, campaign_days={**ctx.campaign_days, campaign_id: tuple(days)})


def _items(result: RuleResult, rule_id: str) -> list[Candidate]:
    return [item for item in result.candidates if item.rule_id == rule_id]


def _only(result: RuleResult, rule_id: str) -> Candidate:
    items = _items(result, rule_id)
    assert len(items) == 1, [item.entity_ref.name for item in items]
    return items[0]


def _withheld(result: RuleResult, rule_id: str) -> list[Withheld]:
    return [line for line in result.withheld if line.rule_id == rule_id]


def _withheld_on(result: RuleResult, rule_id: str, entity_id: int) -> Withheld:
    lines = [
        line
        for line in _withheld(result, rule_id)
        if line.entity is not None and line.entity.id == entity_id
    ]
    assert len(lines) == 1, [line.reason for line in _withheld(result, rule_id)]
    return lines[0]


def _metrics(item: Candidate) -> dict[str, Any]:
    return {metric.key: metric.value for metric in item.evidence.metrics}


def _codes(result: RuleResult) -> list[str]:
    return [finding.code for finding in result.findings]


def _campaign_id(scenario: Scenario, source: str, key: str) -> int:
    return scenario.ids.id(source, "campaign", key)


def _budget_id(scenario: Scenario, key: str) -> int:
    return scenario.ids.id("google", "budget", key)


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


def _item_value(scenario: Scenario, item: Candidate, key: str, expected: object) -> object:
    """The counterpart of one expectation value on a firing item.

    Subject and member keys name catalogue entities and resolve to their internal ids; the
    date keys are the dated day lists of ``evidence.extra``; every other key is a metric or,
    failing that, an ``evidence.extra`` entry.
    """
    extra = item.evidence.extra
    if key == "subject":
        assert extra["subject"]["type"] == "budget", extra["subject"]
        assert extra["subject"]["id"] == _budget_id(scenario, str(expected))
        return expected
    if key == "subject_type":
        return extra["subject"]["type"]
    if key == "member":
        member = _campaign_id(scenario, "google", str(expected))
        assert member in [entry["campaign_id"] for entry in extra["members"]]
        return expected
    if key == "member_days":
        member = _campaign_id(scenario, "google", str(scenario.expect.values["member"]))
        (entry,) = [entry for entry in extra["members"] if entry["campaign_id"] == member]
        return entry["member_days"]
    if key == "changed_day":
        assert expected in extra["changed_intraday_dates"], extra["changed_intraday_dates"]
        return expected
    if key == "unknown_days":
        return extra["unknown_dates"]
    metrics = _metrics(item)
    if key in metrics:
        return metrics[key]
    assert key in extra, f"no counterpart for expectation key {key!r}"
    return extra[key]


def _check_firing_values(scenario: Scenario, result: RuleResult, rule_id: str) -> None:
    """Compare every firing expectation value except the precedence allocation outcomes."""
    items = _items(result, rule_id)
    for key, expected in scenario.expect.values.items():
        if key in PRECEDENCE_KEYS:
            continue
        actual: object
        if key == "freed":
            actual = tuple(sorted((item.evidence.extra["freed"] for item in items), reverse=True))
        elif key == "b1_increase":
            actual = _only(result, B1).evidence.extra["increase"]
        else:
            assert len(items) == 1, [item.entity_ref.name for item in items]
            actual = _item_value(scenario, items[0], key, expected)
        assert _normalised(actual) == _normalised(expected), (key, actual, expected)


def _google_crm(external_ids: Iterable[str], contacts: int, *, linked: bool = True) -> CrmData:
    """HubSpot's primary with ``contacts`` matured Google-touch contacts per campaign.

    Every contact has a genuine zero of meetings and deals (``ok_empty`` fetches) and no
    Qualified Lead date.
    """
    people = tuple(
        ContactInfo(
            f"{880_000 + 100 * index + n}",
            lifecycle_stage="lead",
            google_campaign_ref=external_id,
            google_touch_at=MATURED_TOUCH,
        )
        for index, external_id in enumerate(external_ids)
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
    unknown: int = 0,
    ql: int | None = 0,
    available: bool = True,
    reason: str | None = None,
    floor: bool = False,
) -> VetoPopulation:
    """A Google veto population of ``matured`` leads, ``unresolved`` of them unmatched."""
    contacts = matured - unresolved
    return VetoPopulation(
        source="google",
        campaign_id=campaign_id,
        matured_leads=matured,
        matched_leads=contacts,
        unresolved_leads=unresolved,
        eligible_contacts=contacts,
        unresolved_share=Decimal(unresolved) / Decimal(matured) if matured else None,
        unknown_evidence_contacts=unknown,
        mql=0 if ql is not None else None,
        ql=ql,
        meetings=0,
        available=available,
        reason=reason,
        veto_fires=False,
        leads=matured,
        attribution_floor=floor,
    )


class _OneRatio:
    """A configuration reading one ratio key as another value; every other key is real.

    The loader rejects out-of-range ``b3.spend_percentile`` and ``b4.alpha`` itself, so this
    is the only way to reach the budget rules' own guard on those two keys.
    """

    def __init__(self, base: Thresholds, key: str, value: Decimal) -> None:
        self._base = base
        self._key = key
        self._value = value

    def ratio(self, key: str) -> Decimal:
        """The overridden value for its key, else the real configuration's ratio."""
        return self._value if key == self._key else self._base.ratio(key)

    def int(self, key: str) -> int:
        """The real configuration's integer value."""
        return self._base.int(key)


# ---------------------------------------------------------------------------
# The catalogue guard (simulation/scenarios.py expectations)
# ---------------------------------------------------------------------------


def test_the_guard_covers_every_budget_rule_firing_and_not_firing() -> None:
    """AAP 0.11.2: each of B1-B4 has a catalogue case that fires and one that does not."""
    fired = {scenario.expect.rule_id for scenario in RULE_SCENARIOS if scenario.expect.fires}
    silent = {scenario.expect.rule_id for scenario in RULE_SCENARIOS if not scenario.expect.fires}
    assert fired == set(BUDGET_RULES)
    assert silent == set(BUDGET_RULES)
    assert {scenario.module for scenario in RULE_SCENARIOS} == {MODULE_BUDGET}


@pytest.mark.parametrize("scenario", RULE_SCENARIOS, ids=lambda scenario: scenario.name)
def test_evaluate_matches_the_catalogue_expectation(scenario: Scenario) -> None:
    """Every B1-B4 catalogue case fires or stays silent as its expectation states (0.11.2).

    A firing case carries its expected values and flags on a "changes" item of the Budget
    section; a silent case has no item, and its withheld line leads with the expected reason.
    """
    expect = scenario.expect
    rule_id = expect.rule_id
    assert rule_id is not None
    result = budget.evaluate(scenario.build_context())
    items = _items(result, rule_id)
    if expect.fires:
        assert items, [line.reason for line in _withheld(result, rule_id)]
        for item in items:
            assert (item.section, item.group) == (BUDGET, GROUP_CHANGES)
            assert set(expect.flags) <= set(item.flag_codes)
        _check_firing_values(scenario, result, rule_id)
    else:
        assert items == []
    if expect.withheld_reason is not None:
        reasons = [line.reason for line in _withheld(result, rule_id)]
        assert any(reason.startswith(expect.withheld_reason) for reason in reasons), reasons
    if expect.finding is not None:
        assert expect.finding in _codes(result)


def test_withheld_lines_and_items_never_mix_and_items_rank_by_window_spend() -> None:
    """AAP 0.5.4.5: items rank by window spend, descending; P6 keeps withheld patterns apart."""
    _, ctx = _built("b3.allocation.case")
    result = budget.evaluate(ctx)
    assert result.candidates
    for item in result.candidates:
        assert item.rank_key == (-_metrics(item)["spend"],)
    withheld_rules = {(line.rule_id, line.entity) for line in result.withheld}
    for item in result.candidates:
        assert (item.rule_id, item.entity_ref) not in withheld_rules


# ---------------------------------------------------------------------------
# Settings, thresholds and formatting
# ---------------------------------------------------------------------------


def test_settings_read_every_budget_threshold_once_per_run() -> None:
    """The budget rules read their thresholds from the configuration, once per run (0.5.4)."""
    _, ctx = _built("b1.step.case")
    settings = budget._Settings.of(ctx)
    assert budget._Settings.of(ctx) is settings
    assert ctx.cache["budget.settings"] is settings
    thresholds = ctx.thresholds
    assert settings.b1_step == thresholds.ratio("b1.step")
    assert settings.scale_max_ratio == thresholds.ratio("scale.max_cpa_ratio")
    assert settings.poor_ratio == thresholds.ratio("b2.poor_efficiency_ratio")
    assert settings.b3_percentile == thresholds.ratio("b3.spend_percentile")
    assert settings.b3_quantile == thresholds.ratio("b3.spend_percentile") / 100
    assert settings.b3_headroom == thresholds.ratio("b3.headroom")
    assert settings.b4_min_weekend_days == thresholds.int("b4.min_weekend_days")
    assert settings.b4_min_weekdays == thresholds.int("b4.min_weekdays")
    assert settings.b4_min_outcomes == thresholds.int("b4.min_outcomes")
    assert settings.b4_alpha == thresholds.ratio("b4.alpha")
    assert settings.sustained_min == thresholds.int("window.sustained_min_days")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("b3.spend_percentile", Decimal(101)),
        ("b4.alpha", Decimal(0)),
        ("b4.alpha", Decimal(1)),
    ],
)
def test_settings_reject_a_percentile_or_alpha_out_of_range(key: str, value: Decimal) -> None:
    """B3's percentile must lie in 0-100 and B4's alpha strictly in (0, 1), or config fails."""
    _, ctx = _built("b1.step.case")
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        budget._Settings.from_thresholds(cast(Thresholds, _OneRatio(ctx.thresholds, key, value)))


def test_every_declared_threshold_key_is_configured() -> None:
    """Every key a budget rule copies into evidence exists in config/thresholds.yaml (0.5.4)."""
    _, ctx = _built("b1.step.case")
    for keys in (
        B1_THRESHOLD_KEYS,
        B2_RULE_THRESHOLD_KEYS,
        B3_THRESHOLD_KEYS,
        B4_THRESHOLD_KEYS,
        BUDGET_THRESHOLD_KEYS,
    ):
        for key in keys:
            assert ctx.thresholds.value(key) is not None, key
    assert set(BUDGET_THRESHOLD_KEYS) <= set(B1_THRESHOLD_KEYS)
    assert "b1.step" in B2_RULE_THRESHOLD_KEYS
    assert {"b3.spend_percentile", "b3.headroom"} <= set(B3_THRESHOLD_KEYS)


def test_b4_reason_constants_carry_the_plans_wording() -> None:
    """AAP 0.5.4.5 B4: the four withheld reasons and the test name use the plan's words."""
    assert WEIGHTED_REASON == "weighted attribution: rate test not valid"
    assert CONTRACT_UNKNOWN_REASON == "conversion counting contract unknown"
    assert FRACTIONAL_REASON == "fractional conversions"
    assert NO_EXPOSURE_REASON == "no exposure"
    assert B4_TEST_NAME == "two-sample Poisson rate test"


def test_formatting_helpers_render_amounts_counts_shares_and_p_values() -> None:
    """Budget card amounts render at the minor unit; unknown values never render as zero; a
    p-value reads on the side of alpha it lies on."""
    assert budget._money(None, "USD") == "unavailable"
    assert budget._money("n/a", "USD") == "n/a"
    assert budget._money(Decimal("1400"), "USD") == "USD 1,400.00"
    assert budget._money(Decimal("12.5"), "") == "12.50"
    assert budget._money(1000, "JPY") == "JPY 1,000"
    # A known zero outcome count is a zero denominator ("—"); an unreported one is unavailable.
    # Budget cards record their costs through the campaigns module's one rendering of them.
    assert cost_per_outcome_value(None, Decimal(0)) == "—"
    assert cost_per_outcome_value(None, 0) == "—"
    assert cost_per_outcome_value(None, None) is None
    assert cost_per_outcome_value(Decimal(5), None) is None
    assert cost_per_outcome_value(Decimal(10) / Decimal(3), Decimal(3)) == Decimal("3.333333")
    assert cost_per_outcome_value(None, Decimal(3)) is None
    assert budget._money("—", "USD") == "—"
    assert budget._percent(Decimal("0.1304")) == "13%"
    assert budget._percent(Decimal("0.0909")) == "9.1%"
    assert budget._number(None) == "unavailable"
    assert budget._number("n/a") == "n/a"
    assert budget._number(Decimal("6.000")) == "6"
    assert budget._number(Decimal("1.10")) == "1.1"
    assert budget._number(7) == "7"
    alpha = Decimal("0.05")
    assert budget._p_value(Decimal("0.00001"), alpha) == "p < 0.0001"
    assert budget._p_value(Decimal("0.0123"), alpha) == "p = 0.0123"
    # Four places would show 0.0500, which is not below alpha: places are added instead.
    assert budget._p_value(Decimal("0.04999"), alpha) == "p = 0.04999"
    assert budget._p_value(alpha, alpha) == "p = 0.0500"
    # The "p <" bound never exceeds alpha: below 0.0001 it moves to the next power of ten.
    assert budget._p_value(Decimal("0.000004"), Decimal("0.00005")) == "p < 0.00001"
    assert budget._singular("conversions") == "conversion"
    assert budget._singular("leads") == "lead"
    assert budget._singular("spend") == "spend"
    assert budget._q(Decimal("1.2345675")) == Decimal("1.234568")
    assert budget._share(Decimal(5) / Decimal(7)) == Decimal("0.7143")


@pytest.mark.parametrize(
    ("rank", "text"),
    [
        (Decimal(90), "90th"),
        (Decimal(1), "1st"),
        (Decimal(2), "2nd"),
        (Decimal(3), "3rd"),
        (Decimal(11), "11th"),
        (Decimal(13), "13th"),
        (Decimal(21), "21st"),
        (Decimal("92.5"), "92.5th"),
    ],
)
def test_ordinal_renders_the_b3_percentile_rank(rank: Decimal, text: str) -> None:
    """B3 names its configured percentile as an ordinal, never a literal "90th"."""
    assert budget._ordinal(rank) == text


def test_require_amount_rejects_non_decimal_negative_and_non_finite_amounts() -> None:
    """Allocation amounts are finite, non-negative Decimals; anything else is an error."""
    assert budget._require_amount(Decimal("1.50"), "x") == Decimal("1.50")
    with pytest.raises(TypeError, match="must be a Decimal"):
        budget._require_amount(1.5, "x")
    with pytest.raises(ValueError, match="non-negative"):
        budget._require_amount(Decimal(-1), "x")
    with pytest.raises(ValueError, match="finite"):
        budget._require_amount(Decimal("NaN"), "x")


# ---------------------------------------------------------------------------
# B3 allocation sentences (AAP 0.5.4.2 "Presenting reallocations")
# ---------------------------------------------------------------------------


def test_b1_funding_presents_reallocations_as_funding_for_the_increase() -> None:
    """AAP 0.5.4.2: "Raise by {B1 increase}: {Σ allocations} funded by reallocation from
    {donors}, {remainder} from new budget", each donor named once."""
    assert render_b1_funding(
        Decimal("400.00"),
        [("Donor A", Decimal("300.00")), ("Donor B", Decimal("50.00")), (" Donor B ", Decimal(50))],
        "USD",
    ) == (
        "Raise by USD 400.00: USD 400.00 funded by reallocation from Donor A, Donor B, "
        "USD 0.00 from new budget"
    )
    assert render_b1_funding(Decimal("400.00"), [("Donor A", Decimal("300.00"))], "USD") == (
        "Raise by USD 400.00: USD 300.00 funded by reallocation from Donor A, "
        "USD 100.00 from new budget"
    )
    assert render_b1_funding(Decimal("400.00"), [], "USD") == (
        "Raise by USD 400.00: USD 0.00 funded by reallocation, USD 400.00 from new budget"
    )


def test_b1_funding_rejects_overfunding_and_malformed_allocations() -> None:
    """The sum of allocations into a receiver never exceeds its B1 increase (AAP 0.5.4.2)."""
    with pytest.raises(ValueError, match="exceed the B1 increase"):
        render_b1_funding(Decimal("400.00"), [("A", Decimal("300.00")), ("B", Decimal(101))], "USD")
    with pytest.raises(ValueError, match="donor name"):
        render_b1_funding(Decimal("400.00"), [("  ", Decimal(1))], "USD")
    with pytest.raises(ValueError, match="non-negative"):
        render_b1_funding(Decimal("400.00"), [("A", Decimal(-1))], "USD")
    with pytest.raises(TypeError, match="increase must be a Decimal"):
        render_b1_funding(cast(Decimal, 400), [], "USD")


def test_b3_allocation_names_the_receivers_recommended_increase() -> None:
    """AAP 0.5.4.2: "Move {allocation} to fund {receiver}'s recommended increase (B1)"."""
    assert render_b3_allocation(Decimal("300.00"), " Receiver - Lead gen ", "USD") == (
        "Move USD 300.00 to fund Receiver - Lead gen's recommended increase (B1)"
    )
    with pytest.raises(ValueError, match="receiver_name"):
        render_b3_allocation(Decimal("300.00"), " ", "USD")
    with pytest.raises(ValueError, match="non-negative"):
        render_b3_allocation(Decimal("-0.01"), "Receiver", "USD")


def test_b3_allocation_case_supplies_donors_and_receiver_headroom() -> None:
    """AAP 0.11.2 B3 allocation: donors free 300 and 200, the receiver's B1 increase is 400.

    ``budget.evaluate`` leaves ``extra.allocations`` empty for precedence to fill. Allocating
    the freed amounts by the 0.5.4.2 rule (donors by freed amount descending, each giving
    min(remaining freed, remaining headroom)) gives 300 and 100 with 100 left as a plain
    decrease, and the renderers present it as funding of the 400 increase.
    """
    scenario, ctx = _built("b3.allocation.case")
    values = scenario.expect.values
    result = budget.evaluate(ctx)
    receiver = _only(result, B1)
    donors = sorted(
        _items(result, B3),
        key=lambda item: (-item.evidence.extra["freed"], item.entity_ref.name),
    )
    assert [item.entity_ref.id for item in donors] == [
        _campaign_id(scenario, "linkedin", "donor-a"),
        _campaign_id(scenario, "linkedin", "donor-b"),
    ]
    freed = [item.evidence.extra["freed"] for item in donors]
    assert _normalised(tuple(freed)) == _normalised(values["freed"])
    for item in donors:
        extra = item.evidence.extra
        assert extra["allocations"] == ()
        assert extra["freed"] == extra["current"] - extra["proposed"]
        assert _metrics(item)["freed"] == extra["freed"]
        assert extra["currency"] == receiver.evidence.extra["currency"]
    headroom = receiver.evidence.extra["increase"]
    assert _normalised(headroom) == _normalised(values["b1_increase"])
    assert receiver.evidence.extra["allocations"] == ()
    remaining = headroom
    allocations: list[tuple[str, Decimal]] = []
    for item, amount in zip(donors, freed, strict=True):
        moved = min(amount, remaining)
        remaining -= moved
        allocations.append((item.entity_ref.name, moved))
    assert _normalised(tuple(amount for _, amount in allocations)) == _normalised(
        values["allocations"]
    )
    assert _normalised(sum(freed) - headroom) == _normalised(values["unallocated"])
    currency = receiver.evidence.extra["currency"]
    funded = render_b1_funding(headroom, allocations, currency)
    donor_names = ", ".join(name for name, _ in allocations)
    assert funded == (
        f"Raise by {currency} {values['b1_increase']}: {currency} "
        f"{values['funded_by_reallocation']} funded by reallocation from {donor_names}, "
        f"{currency} {values['from_new_budget']} from new budget"
    )
    for (_, moved), wanted in zip(allocations, values["allocations"], strict=True):
        assert render_b3_allocation(moved, receiver.entity_ref.name, currency) == (
            f"Move {currency} {wanted} to fund {receiver.entity_ref.name}'s recommended "
            "increase (B1)"
        )


def test_b3_without_a_receiver_stays_a_plain_decrease() -> None:
    """AAP 0.11.2 B3 allocation: with no B1 receiver the freed amounts are plain decreases."""
    scenario, ctx = _built("b3.no_receiver.case")
    result = budget.evaluate(ctx)
    assert _items(result, B1) == []
    donors = _items(result, B3)
    assert _normalised(
        tuple(sorted((item.evidence.extra["freed"] for item in donors), reverse=True))
    ) == _normalised(scenario.expect.values["freed"])
    for item in donors:
        assert item.evidence.extra["allocations"] == scenario.expect.values["allocations"]
        assert item.action == f"Lower daily budget to USD {item.evidence.extra['proposed']:,.2f}"
        assert item.increases_spend_on == ()


# ---------------------------------------------------------------------------
# B1 Increase (AAP 0.5.4.5 B1 row; C2 "Scale" in 0.5.4.4)
# ---------------------------------------------------------------------------


def test_b1_raises_the_latest_effective_daily_budget_by_the_configured_step() -> None:
    """AAP 0.5.4.5 B1: current x (1 + b1.step) at the currency's minor unit, as one Scale item."""
    scenario, ctx = _built("b1.step.case")
    campaign = _campaign_id(scenario, "linkedin", "li-cap")
    item = _only(budget.evaluate(ctx), B1)
    extra = item.evidence.extra
    metrics = _metrics(item)
    history = budget_history(ctx, ("campaign", campaign))
    current = extra["current"]
    assert current == metrics["daily_budget"] == ctx.campaigns[campaign].daily_budget
    assert _normalised(current) == _normalised(scenario.expect.values["current"])
    proposed = round_to_minor_unit(current * (1 + ctx.thresholds.ratio("b1.step")), "USD")
    assert extra["proposed"] == proposed == metrics["proposed_daily_budget"]
    assert _normalised(proposed) == _normalised(scenario.expect.values["proposed"])
    assert extra["step"] == ctx.thresholds.ratio("b1.step")
    assert extra["increase"] == metrics["increase"] == proposed - current
    assert extra["allocations"] == ()
    assert extra["outcome"] == "Scale"
    assert extra["c2_link"] == {"rule_id": C2, "subject": {"type": "campaign", "id": campaign}}
    assert "g1_blocked" not in extra
    assert item.action == "Raise daily budget from USD 83.33 to USD 100.00"
    assert (item.entity_ref.type, item.entity_ref.id) == (ENTITY_CAMPAIGN, campaign)
    assert item.decision_subject == DecisionSubject("campaign", campaign)
    assert DecisionSubject("campaign", campaign) in item.increases_spend_on
    assert DecisionSubject("campaign", campaign) in item.depends_on
    assert set(B1_THRESHOLD_KEYS) <= set(item.evidence.thresholds)
    assert item.evidence.sample is not None
    assert (item.evidence.sample.observed, item.evidence.sample.required) == (
        history.known_active,
        ctx.thresholds.int("budget.min_known_days"),
    )
    assert metrics["capped_days"] == history.capped
    assert item.evidence.baselines[1].key == "max_cost_per_outcome"
    assert item.required_datasets[:2] == ("linkedin.entities", "linkedin.analytics_daily")


def test_b1_follows_scale_and_never_appears_under_a_veto_hold() -> None:
    """AAP 0.5.4.5 B1: an item per C2 Scale outcome / none when the pipeline veto Holds."""
    scale_scenario, scale_ctx = _built("c2.outcome_scale.case")
    item = _only(budget.evaluate(scale_ctx), B1)
    assert item.entity_ref.id == _campaign_id(scale_scenario, "linkedin", "li-pipeline")
    assert _normalised(item.evidence.extra["proposed"]) == _normalised(
        scale_scenario.expect.values["proposed_daily_budget"]
    )
    _, hold_ctx = _built("c2.outcome_hold.case")
    held = budget.evaluate(hold_ctx)
    assert _items(held, B1) == []
    assert _withheld(held, B1) == []


def test_b1_short_history_is_withheld_with_observed_and_required() -> None:
    """AAP 0.5.4.1 short history: 4 known-budget days withhold B1 with "4 of 7 required"."""
    scenario, ctx = _built("c2.cap_evidence_short.case")
    result = budget.evaluate(ctx)
    assert _items(result, B1) == []
    line = _withheld_on(result, B1, _campaign_id(scenario, "linkedin", "li-cap"))
    required = ctx.thresholds.int("budget.min_known_days")
    assert (line.observed, line.required, line.section) == (4, required, BUDGET)
    assert line.reason.startswith(f"known-budget days 4 of {required} required")


def test_b1_carries_pipeline_unavailable_and_floor_flags_of_its_scale_outcome() -> None:
    """AAP 0.5.4.4 C2: an unavailable pipeline still Scales with B1, flagged; QL is never 0."""
    _, ctx = _built("c2.qlstage_unresolved.case")
    item = _only(budget.evaluate(ctx), B1)
    assert FLAG_PIPELINE_UNAVAILABLE in item.flag_codes
    assert _metrics(item)["ql"] is None
    _, floor_ctx = _built("veto.unresolved.fires")
    floored = _only(budget.evaluate(floor_ctx), B1)
    assert {FLAG_PIPELINE_UNAVAILABLE, FLAG_ATTRIBUTION_FLOOR} <= set(floored.flag_codes)


def test_g1_b2_target_meets_b2_and_no_budget_item_raises_its_spend() -> None:
    """AAP 0.11.2 G1 on a B2 target: B2 fires and nothing raises it / the twin scales via B1."""
    scenario, ctx = _built("g1.b2_target.fires")
    hot = DecisionSubject("campaign", _campaign_id(scenario, "linkedin", "hot"))
    result = budget.evaluate(ctx)
    b2 = _only(result, B2)
    assert b2.decision_subject == hot
    assert b2.increases_spend_on == ()
    assert all(hot not in item.increases_spend_on for item in result.candidates)
    twin, twin_ctx = _built("g1.b2_target.does_not_fire")
    twin_result = budget.evaluate(twin_ctx)
    assert _items(twin_result, B2) == []
    b1 = _only(twin_result, B1)
    assert DecisionSubject("campaign", _campaign_id(twin, "linkedin", "hot")) in (
        b1.increases_spend_on
    )


# ---------------------------------------------------------------------------
# B2 Do not increase (AAP 0.5.4.5 B2 row; high-utilization gate in 0.5.4.1)
# ---------------------------------------------------------------------------


def test_b2_fires_on_five_of_seven_high_utilization_days_with_poor_efficiency() -> None:
    """AAP 0.11.2 B2: 5 of 7 high-utilization days (71%) fire / 4 of 7 (57%) do not."""
    scenario, ctx = _built("b2.share.fires")
    hot = _campaign_id(scenario, "linkedin", "hot")
    item = _only(budget.evaluate(ctx), B2)
    metrics = _metrics(item)
    extra = item.evidence.extra
    share = ctx.thresholds.ratio("budget.sustained_share")
    values = scenario.expect.values
    assert item.entity_ref.id == hot
    assert (metrics["high_utilization_days"], metrics["known_days"]) == (
        values["high_utilization_days"],
        values["known_days"],
    )
    assert Decimal(metrics["high_utilization_days"]) / Decimal(metrics["known_days"]) >= share
    assert metrics["spending_days"] >= ctx.thresholds.int("budget.min_spending_days")
    assert metrics["known_days"] >= ctx.thresholds.int("budget.min_known_days")
    assert extra["branch"] == "poor_efficiency"
    cpo = metrics["cost_per_outcome"]
    assert isinstance(cpo, Decimal)
    baseline = item.evidence.baselines[0].value
    assert isinstance(baseline, Decimal)
    assert cpo >= ctx.thresholds.ratio("b2.poor_efficiency_ratio") * baseline
    step = ctx.thresholds.ratio("b1.step")
    assert extra["proposed"] == round_to_minor_unit(extra["current"] * (1 - step), "USD")
    assert extra["decrease_step"] == step
    assert item.action == (
        f"Do not increase. Lower daily budget by {budget._percent(step)} or fix efficiency first"
    )
    assert item.increases_spend_on == ()
    assert item.decision_subject == DecisionSubject("campaign", hot)
    assert set(B2_RULE_THRESHOLD_KEYS) <= set(item.evidence.thresholds)
    assert _metrics(item)["expected_outcomes"] is not None

    twin, twin_ctx = _built("b2.share.does_not_fire")
    assert _items(budget.evaluate(twin_ctx), B2) == []
    history = budget_history(twin_ctx, ("campaign", _campaign_id(twin, "linkedin", "hot")))
    assert (history.high, history.known_active) == (
        twin.expect.values["high_utilization_days"],
        twin.expect.values["known_days"],
    )
    assert Decimal(history.high) / Decimal(history.known_active) < share


def test_b2_zero_outcome_branch_fires_when_expected_outcomes_reach_the_minimum() -> None:
    """AAP 0.5.4.5 B2: 0 outcomes with expected outcomes >= b2.min_expected_outcomes fire.

    The reason states the evidence's six-place expected outcomes at one decimal place, rounded
    down, with the outcome label, as C1 does: 80.219409 reads "80.2 leads were expected".
    """
    scenario, ctx = _built("g1.b2_target.fires")
    hot = _campaign_id(scenario, "linkedin", "hot")
    silent = _with_days(ctx, hot, (replace(day, leads=0) for day in ctx.campaign_days[hot]))
    item = _only(budget.evaluate(silent), B2)
    metrics = _metrics(item)
    assert item.evidence.extra["branch"] == "zero_outcomes"
    assert metrics["outcomes"] == 0
    # A known 0 leads is a zero denominator: "—", never flagged unavailable (AAP 0.5.5), while
    # the account baseline, whose leads are known and positive, keeps its finite cost.
    assert metrics["cost_per_outcome"] == "—"
    cost = item.evidence.metric("cost_per_outcome").to_json()
    assert cost["value"] == "—"
    assert "unavailable" not in cost
    assert isinstance(item.evidence.baselines[0].value, Decimal)
    assert item.evidence.extra["headline_metric"] == "outcomes"
    assert "unavailable" not in item.reason
    expected = metrics["expected_outcomes"]
    assert isinstance(expected, Decimal)
    assert expected >= silent.thresholds.ratio("b2.min_expected_outcomes")
    shown = expected.quantize(Decimal("0.1"), rounding=ROUND_FLOOR)
    assert shown != expected
    assert f"with 0 leads where {shown} leads were expected at the account's " in item.reason
    assert format(expected, "f") not in item.reason
    assert budget._one_place(Decimal("2.95")) == "2.9"
    assert budget._one_place(Decimal("3.000000")) == "3.0"
    assert (budget._one_place(None), budget._one_place("n/a")) == ("unavailable", "n/a")


def test_b2_and_b3_are_withheld_on_four_spending_days_of_five_required() -> None:
    """AAP 0.11.2: 7 known-budget days with 4 spending days withhold B2 and B3 "4 of 5"."""
    scenario, ctx = _built("b2.spending_days.case")
    hot = _campaign_id(scenario, "linkedin", "hot")
    result = budget.evaluate(ctx)
    required = ctx.thresholds.int("budget.min_spending_days")
    assert _items(result, B2) == []
    assert all(item.entity_ref.id != hot for item in _items(result, B3))
    b2 = _withheld_on(result, B2, hot)
    assert (b2.observed, b2.required, b2.unit) == (4, required, "spending days")
    assert b2.reason.startswith(f"spending days 4 of {required} required")
    b3 = _withheld_on(result, B3, hot)
    assert f"spending days 4 of {required} required" in b3.reason
    assert scenario.expect.values["b3_withheld"] is True
    history = budget_history(ctx, ("campaign", hot))
    assert (history.known_active, history.spending) == (
        scenario.expect.values["known_days"],
        scenario.expect.values["spending_days"],
    )


# ---------------------------------------------------------------------------
# B3 Under-utilized (AAP 0.5.4.5 B3 row; under-utilized gate in 0.5.4.1)
# ---------------------------------------------------------------------------


def test_b3_proposes_the_spend_percentile_times_headroom_with_idle_days_counted() -> None:
    """AAP 0.11.2 B3: 10 known days, 6 spending and 4 idle, u_d <= 0.5 on 8, fire B3."""
    scenario, ctx = _built("b3.idle_known.fires")
    slack = _campaign_id(scenario, "linkedin", "slack")
    item = _only(budget.evaluate(ctx), B3)
    metrics = _metrics(item)
    extra = item.evidence.extra
    history = budget_history(ctx, ("campaign", slack))
    spends = [day.spend for day in history.days if day.known_active]
    observed = percentile(spends, ctx.thresholds.ratio("b3.spend_percentile") / 100)
    assert observed is not None
    proposed = round_to_minor_unit(observed * ctx.thresholds.ratio("b3.headroom"), "USD")
    assert extra["proposed"] == proposed == metrics["proposed_daily_budget"]
    assert _normalised(proposed) == _normalised(scenario.expect.values["proposed"])
    assert metrics["spend_percentile"] == observed
    assert extra["freed"] == extra["current"] - proposed
    assert extra["headroom"] == ctx.thresholds.ratio("b3.headroom")
    assert extra["under_utilization"] == ctx.thresholds.ratio("budget.under_utilization")
    values = scenario.expect.values
    for key in ("known_days", "spending_days", "idle_days", "under_utilized_days"):
        assert metrics[key] == values[key], key
    assert len(extra["idle_dates"]) == values["idle_days"]
    assert item.action == "Lower daily budget to USD 88.00"
    assert item.increases_spend_on == ()
    assert item.decision_subject == DecisionSubject("campaign", slack)
    assert set(B3_THRESHOLD_KEYS) <= set(item.evidence.thresholds)
    rank = budget._ordinal(ctx.thresholds.ratio("b3.spend_percentile"))
    assert f"the {rank} percentile of daily spend is" in item.reason


def test_b3_is_silent_when_headroom_lifts_the_proposal_to_the_current_budget() -> None:
    """B3 lowers a budget only: a headroom reaching the current budget yields no item."""
    scenario, ctx = _built("b3.idle_known.fires")
    item = _only(budget.evaluate(ctx), B3)
    current = item.evidence.extra["current"]
    observed = _metrics(item)["spend_percentile"]
    assert isinstance(observed, Decimal)
    tuned = _tuned(scenario, ctx, {"b3.headroom": float(current / observed)})
    assert _items(budget.evaluate(tuned), B3) == []


@pytest.mark.parametrize("leads", ["known_zero", "unreported"])
def test_b3_costs_read_a_dash_at_known_zero_outcomes_and_unavailable_when_unreported(
    leads: str,
) -> None:
    """AAP 0.5.5 and 0.11.1: a zero denominator reads "—"; only an unknown one is unavailable.

    B3 is judged on utilization alone, so it fires whatever slack's leads are. slack is the only
    campaign of its account and objective group, so its leads are also the account baseline's
    denominator: at a known 0 leads both costs read "—" and neither is flagged unavailable;
    with one day's leads unreported both are ``None``, which the card marks unavailable.
    """
    scenario, ctx = _built("b3.idle_known.fires")
    slack = _campaign_id(scenario, "linkedin", "slack")
    subject: SubjectKey = ("campaign", slack)
    days = ctx.campaign_days[slack]
    window = ctx.window
    first = min(day.date for day in days if window.start <= day.date <= window.end)
    unreported = first if leads == "unreported" else None
    changed = _with_days(
        ctx, slack, (replace(day, leads=None if day.date == unreported else 0) for day in days)
    )
    item = _only(budget.evaluate(changed), B3)
    cost = item.evidence.metric("cost_per_outcome")
    baseline = item.evidence.baselines[0]
    assert baseline.key == "baseline_cost_per_outcome"
    if leads == "known_zero":
        assert _metrics(item)["outcomes"] == 0
        assert subject_baseline_outcomes(changed, subject) == 0
        assert (cost.value, baseline.value) == ("—", "—")
        assert cost.to_json() == {
            "key": "cost_per_outcome",
            "label": "Cost per lead",
            "value": "—",
            "unit": "currency",
            "currency": "USD",
        }
        assert baseline.to_json()["value"] == "—"
        return
    assert _metrics(item)["outcomes"] is None
    assert subject_baseline_outcomes(changed, subject) is None
    assert (cost.value, baseline.value) == (None, None)
    assert cost.to_json()["unavailable"] is True
    assert baseline.to_json()["value"] is None


def test_b3_never_reallocates_from_a_budget_that_only_idles() -> None:
    """AAP 0.11.2: 10 known idle days and no spending day are idle, not under-utilized."""
    scenario, ctx = _built("b3.idle_known.does_not_fire")
    slack = _campaign_id(scenario, "linkedin", "slack")
    assert _items(budget.evaluate(ctx), B3) == []
    history = budget_history(ctx, ("campaign", slack))
    values = scenario.expect.values
    assert (history.known_active, history.spending, history.idle) == (
        values["known_days"],
        values["spending_days"],
        values["idle_days"],
    )
    assert not under_utilized(history).passed


def test_b3_never_counts_unknown_days_as_idle() -> None:
    """AAP 0.11.2: unobserved days are unknown, never idle; 6 known days withhold "6 of 7"."""
    scenario, ctx = _built("budget.unknown_not_idle.case")
    slack = _campaign_id(scenario, "linkedin", "slack")
    result = budget.evaluate(ctx)
    assert _items(result, B3) == []
    line = _withheld_on(result, B3, slack)
    required = ctx.thresholds.int("budget.min_known_days")
    assert (line.observed, line.required, line.unit) == (6, required, "known-budget days")
    history = budget_history(ctx, ("campaign", slack))
    values = scenario.expect.values
    assert (history.known_active, history.idle, history.unobserved) == (
        values["known_days"],
        values["idle_days"],
        values["unobserved_days"],
    )


# ---------------------------------------------------------------------------
# Sustained window and inactive subjects (AAP 0.5.4.1)
# ---------------------------------------------------------------------------


def test_sustained_rules_are_withheld_below_the_sustained_window_minimum() -> None:
    """AAP 0.5.4.1: below window.sustained_min_days B1-B4 are withheld with observed days.

    A 6-day window withholds every budget rule; the 7-day twin withholds none for the window.
    """
    _, ctx = _built("p5.sustained_min.does_not_fire")
    required = ctx.thresholds.int("window.sustained_min_days")
    result = budget.evaluate(ctx)
    assert result.candidates == ()
    for rule_id in (B2, B3, B4):
        (line,) = _withheld(result, rule_id)
        assert (line.entity, line.observed, line.required, line.unit) == (
            None,
            ctx.window.days,
            required,
            "days",
        )
        assert line.reason == f"window {ctx.window.days} days of {required} required"
    assert ctx.window.days < required
    _, twin = _built("p5.sustained_min.fires")
    assert twin.window.days >= required
    reasons = [line.reason for line in budget.evaluate(twin).withheld]
    assert not any(reason.startswith("window ") for reason in reasons), reasons


@pytest.mark.parametrize(
    ("window", "reason"),
    [
        ((-1, -1), "window 1 day of 7 required"),
        ((-2, -1), "window 2 days of 7 required"),
        ((-6, -1), "window 6 days of 7 required"),
    ],
)
def test_the_window_withheld_line_states_the_day_count_in_number(
    window: tuple[int, int], reason: str
) -> None:
    """B2, B3 and B4 read "window 1 day of 7 required" on a one-day window, "days" otherwise.

    The run-wide lines match the per-subject B1 line (C2's) in the same Budget section;
    ``observed`` and ``required`` stay the bare counts and ``unit`` stays "days".
    """
    scenario = SCENARIOS["p5.sustained_min.does_not_fire"]
    ctx = replace(scenario, context=replace(scenario.context, window=window)).build_context()
    required = ctx.thresholds.int("window.sustained_min_days")
    assert required == 7
    result = budget.evaluate(ctx)
    assert result.candidates == ()
    expected = (reason, ctx.window.days, required, "days")
    for rule_id in (B2, B3, B4):
        (line,) = _withheld(result, rule_id)
        assert line.entity is None, rule_id
        assert (line.reason, line.observed, line.required, line.unit) == expected, rule_id
    (b1,) = _withheld(result, B1)
    assert b1.entity is not None
    assert (b1.reason, b1.observed, b1.required, b1.unit) == expected


def test_paused_subjects_get_no_budget_change() -> None:
    """B2, B3 and B4 judge only active subjects; a paused campaign yields nothing."""
    _, ctx = _built("b4.linkedin.fires")
    paused = _fresh(
        ctx, campaigns={cid: replace(c, status="paused") for cid, c in ctx.campaigns.items()}
    )
    assert budget._b2_b3(paused, budget._Settings.of(paused)) == RuleResult()
    assert budget._b4(paused, budget._Settings.of(paused)) == RuleResult()
    assert budget.evaluate(paused).candidates == ()


# ---------------------------------------------------------------------------
# Lifetime budgets (AAP 0.5.4.1 "Lifetime budgets")
# ---------------------------------------------------------------------------


def test_lifetime_budgets_show_no_utilization_and_are_excluded_from_cap_rules() -> None:
    """AAP 0.11.2 Exclusion: a lifetime budget is no budget subject / its daily twin scales."""
    scenario, ctx = _built("budget.lifetime.case")
    campaign_id = _campaign_id(scenario, "linkedin", "li-cap")
    campaign = ctx.campaigns[campaign_id]
    assert (campaign.daily_budget, campaign.total_budget) == (None, Decimal("5000.00"))
    assert ("campaign", campaign_id) not in budget_subjects(ctx)
    result = budget.evaluate(ctx)
    for rule_id in (B1, B2, B3):
        assert _items(result, rule_id) == []
        assert all(
            line.entity is None or line.entity.id != campaign_id
            for line in result.withheld
            if line.rule_id == rule_id
        )
    history = budget_history(ctx, ("campaign", campaign_id))
    assert (history.utilization_applicable, history.window_utilization) == (False, None)
    # B4 is not a cap rule, so its card still shows the campaign's own history: "n/a".
    assert budget._b4_history_subject(ctx, campaign) == ("campaign", campaign_id)
    card = {metric.key: metric.value for metric in budget._history_metrics(history, None)}
    assert card["window_utilization"] == scenario.expect.values["utilization"]
    assert (card["known_days"], card["spending_day_share"]) == (0, None)
    evidence = budget_evidence(
        ctx,
        ("campaign", campaign_id),
        history,
        under_utilized(history),
        outcomes=None,
        cpo=None,
        baseline=None,
        headline="window_utilization",
    )
    assert evidence.metric("window_utilization").value == "n/a"
    assert evidence.extra["headline_metric"] == "window_utilization"
    _, twin = _built("b1.step.case")
    assert _only(budget.evaluate(twin), B1).evidence.extra["current"] is not None


# ---------------------------------------------------------------------------
# Shared budgets (AAP 0.5.4.5 "Shared budgets")
# ---------------------------------------------------------------------------


def test_a_shared_budget_is_the_subject_and_names_every_member() -> None:
    """AAP 0.5.4.5: the shared budget is the subject; members get no individual item."""
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool = _budget_id(scenario, "pool")
    members = (_campaign_id(scenario, "google", "m1"), _campaign_id(scenario, "google", "m2"))
    result = budget.evaluate(ctx)
    item = _only(result, B1)
    extra = item.evidence.extra
    assert (item.entity_ref.type, item.entity_ref.id) == (ENTITY_BUDGET, pool)
    assert item.decision_subject == DecisionSubject("budget", pool)
    assert extra["subject"] == {"type": "budget", "id": pool}
    assert extra["shared"] is True
    assert [entry["campaign_id"] for entry in extra["members"]] == list(members)
    assert all(entry["member_days"] == ctx.window.days for entry in extra["members"])
    assert {
        DecisionSubject("budget", pool),
        *(DecisionSubject("campaign", m) for m in members),
    } <= set(item.increases_spend_on)
    assert FLAG_SHARED_BUDGET in item.flag_codes
    assert f"across {len(members)} campaigns" in next(
        flag.message for flag in item.flags if flag.code == FLAG_SHARED_BUDGET
    )
    known = next(metric for metric in item.evidence.metrics if metric.key == "known_days")
    assert known.label == "Budget-active days"
    proposed = round_to_minor_unit(extra["current"] * (1 + ctx.thresholds.ratio("b1.step")), "USD")
    assert extra["proposed"] == proposed
    assert _normalised(proposed) == _normalised(scenario.expect.values["proposed"])
    for rule_id in (B1, B2, B3):
        for candidate in _items(result, rule_id):
            assert (candidate.entity_ref.type, candidate.entity_ref.id) not in {
                (ENTITY_CAMPAIGN, m) for m in members
            }
    assert "g1_blocked" not in extra


def test_a_poor_member_marks_the_shared_budget_increase_as_g1_blocked() -> None:
    """AAP 0.11.2: a poor-efficiency member in a high-utilization shared budget blocks its B1.

    The B1 is still built with its targets, so precedence removes and counts it; with a
    good-efficiency member (the twin) it is not marked.
    """
    scenario, ctx = _built("shared.poor_member.fires")
    pool = _budget_id(scenario, "pool")
    poor = _campaign_id(scenario, "google", str(scenario.expect.values["poor_member"]))
    item = _only(budget.evaluate(ctx), B1)
    assert item.evidence.extra["g1_blocked"] is True
    assert DecisionSubject("budget", pool) in item.increases_spend_on
    assert member_poor_efficiency(ctx, pool, poor)
    assert not member_poor_efficiency(ctx, pool, _campaign_id(scenario, "google", "m1"))
    _, twin = _built("shared.poor_member.does_not_fire")
    assert "g1_blocked" not in _only(budget.evaluate(twin), B1).evidence.extra


def test_an_aggregate_b2_on_a_shared_budget_is_decided_at_budget_level() -> None:
    """AAP 0.11.2 Shared budgets: an aggregate B2 is a budget-subject item naming its members."""
    scenario, ctx = _built("shared.aggregate_b2.case")
    pool = _budget_id(scenario, "pool")
    item = _only(budget.evaluate(ctx), B2)
    assert item.decision_subject == DecisionSubject("budget", pool)
    assert item.increases_spend_on == ()
    assert [entry["campaign_id"] for entry in item.evidence.extra["members"]] == list(
        subject_campaigns(ctx, ("budget", pool))
    )
    assert DecisionSubject("budget", pool) in item.depends_on
    assert {FLAG_SHARED_BUDGET, FLAG_NO_CRM_LINK} <= set(item.flag_codes)


def test_a_member_that_joined_mid_window_counts_only_on_its_member_days() -> None:
    """AAP 0.11.2: a campaign joining mid-window counts only on its member days."""
    scenario, ctx = _built("shared.membership_mid_window.case")
    joiner = _campaign_id(scenario, "google", "m3")
    item = _only(budget.evaluate(ctx), B1)
    days = {entry["campaign_id"]: entry["member_days"] for entry in item.evidence.extra["members"]}
    assert days[joiner] == scenario.expect.values["member_days"]
    assert days[_campaign_id(scenario, "google", "m1")] == ctx.window.days
    assert scenario.expect.values["changed_day"] in item.evidence.extra["changed_intraday_dates"]


def test_a_member_changing_budgets_during_a_day_makes_that_day_unknown() -> None:
    """AAP 0.11.2: a member moving budgets during a day leaves the shared budget's day unknown."""
    scenario, ctx = _built("shared.member_changed_intraday.case")
    item = _only(budget.evaluate(ctx), B1)
    unknown = item.evidence.extra["unknown_dates"]
    assert _normalised(unknown) == _normalised(scenario.expect.values["unknown_days"])
    metrics = _metrics(item)
    assert metrics["known_days"] == ctx.window.days - len(unknown)
    assert metrics["changed_intraday_days"] == len(unknown)


def test_a_shared_amount_changed_during_a_day_leaves_that_day_without_a_known_budget() -> None:
    """AAP 0.11.2 Shared budgets: budget-active days need a whole-day-known amount.

    The pool's amount changes between two observations, so every day those observations
    bracket is changed-intraday, excluded from the gates; the card still decides the budget
    subject at its latest effective amount.
    """
    scenario, ctx = _built("shared.decision_subject.case")
    pool = _budget_id(scenario, "pool")
    (change,) = [
        item
        for item in scenario.spec.platform_changes
        if (item.entity, item.key, item.field) == ("budget", "pool", "amount")
    ]
    assert scenario.spec.anchor is not None
    changed_at = change.instant(scenario.spec.anchor)
    timezone = next(acct.timezone for acct in ctx.accounts.values() if acct.source == "google")
    assert timezone is not None
    item = _only(budget.evaluate(ctx), B1)
    extra = item.evidence.extra
    unknown = extra["unknown_dates"]
    assert changed_at.astimezone(ZoneInfo(timezone)).date().isoformat() in unknown
    assert extra["changed_intraday_dates"] == unknown
    assert _metrics(item)["known_days"] == ctx.window.days - len(unknown)
    assert extra["current"] == change.value
    assert item.decision_subject == DecisionSubject("budget", pool)


def _mixed_members(ctx: AnalysisContext, member: int, how: str) -> AnalysisContext:
    """Copy a shared-budget context with one member in another currency or objective group."""
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
    ("how", "reason", "observed"),
    [
        ("objective_group", "members span 2 objective groups: efficiency n/a", 2),
        ("currency", "members span more than one currency: efficiency n/a", None),
    ],
)
def test_mixed_members_withhold_b1_b2_and_b3_for_the_shared_budget(
    how: str, reason: str, observed: int | None
) -> None:
    """AAP 0.5.4.5 Mixed objective groups: efficiency "n/a"; B1, B2 and B3 are withheld."""
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool = _budget_id(scenario, "pool")
    mixed = _mixed_members(ctx, _campaign_id(scenario, "google", "m2"), how)
    result = budget.evaluate(mixed)
    for rule_id in (B1, B2, B3):
        assert all(item.entity_ref.id != pool for item in _items(result, rule_id))
        line = _withheld_on(result, rule_id, pool)
        assert line.reason == reason
        assert line.observed == observed
    assert budget._b2_b3(mixed, budget._Settings.of(mixed)).candidates == ()
    _, twin = _built("shared.mixed_objectives.case")
    assert _only(budget.evaluate(twin), B1).evidence.extra["objective_groups"] == ("google",)


def test_a_shared_budget_member_shows_no_utilization_of_its_own() -> None:
    """AAP 0.5.4.5: a member has no utilization of its own; the shared amount is its only cap.

    No history is computed for the member itself, so its B4 card shows the shared budget's
    history: utilization over the budget-active days and the latest shared amount.
    """
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool = _budget_id(scenario, "pool")
    member = ctx.campaigns[_campaign_id(scenario, "google", "m1")]
    assert ("campaign", member.id) not in budget_subjects(ctx)
    with pytest.raises(ValueError, match="draws from explicitly shared budget"):
        budget_history(ctx, ("campaign", member.id))
    subject = budget._b4_history_subject(ctx, member)
    assert subject == ("budget", pool)
    shared = budget_history(ctx, subject)
    assert shared.shared
    assert shared.window_utilization is not None
    card = {metric.key: metric for metric in budget._history_metrics(shared, None)}
    assert card["window_utilization"].value == budget._share(shared.window_utilization)
    assert card["known_days"].label == "Budget-active days"
    assert shared.latest_daily_budget == ctx.budgets[pool].amount
    extra = budget._b4_history_extra(ctx, subject, shared)
    assert extra["budget_history"]["subject"] == {"type": "budget", "id": pool}
    assert member.id in {entry["campaign_id"] for entry in extra["members"]}
    assert budget._subject_active(ctx, ("budget", pool))
    paused = _fresh(
        ctx, campaigns={cid: replace(c, status="paused") for cid, c in ctx.campaigns.items()}
    )
    assert not budget._subject_active(paused, ("budget", pool))


# ---------------------------------------------------------------------------
# B4 Weekday / weekend pattern (AAP 0.5.4.5 B4 row)
# ---------------------------------------------------------------------------


def test_b4_runs_the_rate_test_on_linkedin_leads_and_reports_sample_sizes() -> None:
    """AAP 0.11.2 B4: LinkedIn leads run the rate test; p < alpha fires with sample sizes."""
    scenario, ctx = _built("b4.linkedin.fires")
    campaign = _campaign_id(scenario, "linkedin", "li-cap")
    result = budget.evaluate(ctx)
    item = _only(result, B4)
    metrics = _metrics(item)
    thresholds = ctx.thresholds
    assert metrics["weekend_days"] >= thresholds.int("b4.min_weekend_days")
    assert metrics["weekdays"] >= thresholds.int("b4.min_weekdays")
    total = metrics["weekend_outcomes"] + metrics["weekday_outcomes"]
    assert total >= thresholds.int("b4.min_outcomes")
    test = item.evidence.test
    assert test is not None
    assert test.name == B4_TEST_NAME
    assert test.p_value is not None
    assert test.p_value < thresholds.ratio("b4.alpha") == test.alpha
    sample = item.evidence.sample
    assert sample is not None
    assert (sample.observed, sample.required) == (total, thresholds.int("b4.min_outcomes"))
    assert item.evidence.extra["contract"] == "LinkedIn leads are unweighted event counts"
    assert item.evidence.extra["day_boundary"] == {"source": "linkedin", "boundary": "UTC"}
    assert item.evidence.extra["gate"]["weekend_days"] == {
        "observed": metrics["weekend_days"],
        "required": thresholds.int("b4.min_weekend_days"),
    }
    values = scenario.expect.values
    assert item.action == (
        f"Adjust weekend schedule or bids: weekend cost/outcome USD {values['weekend_cpo']} vs "
        f"weekday USD {values['weekday_cpo']}"
    )
    assert budget._p_value(test.p_value, test.alpha) in item.reason
    assert item.decision_subject is None
    assert item.increases_spend_on == ()
    assert item.depends_on == (DecisionSubject("campaign", campaign),)
    assert set(B4_THRESHOLD_KEYS) <= set(item.evidence.thresholds)
    assert set(VETO_THRESHOLD_KEYS) <= set(item.evidence.thresholds)
    assert {"eligible_contacts", "ql"} <= set(metrics)
    assert set(CRM_DATASETS["linkedin"]) <= set(item.required_datasets)
    assert metrics["daily_budget"] == ctx.campaigns[campaign].daily_budget


def test_b4_below_the_weekend_minimum_is_withheld_with_its_sample_sizes() -> None:
    """AAP 0.11.2 B4: 3 weekend days of 4 required are withheld with observed and required."""
    scenario, ctx = _built("b4.linkedin.does_not_fire")
    result = budget.evaluate(ctx)
    assert _items(result, B4) == []
    line = _withheld_on(result, B4, _campaign_id(scenario, "linkedin", "li-cap"))
    required = ctx.thresholds.int("b4.min_weekend_days")
    assert (line.observed, line.required, line.unit) == (3, required, "weekend days")
    assert line.reason.startswith(f"3 weekend days of {required} required")


def test_b4_names_the_google_counting_contract_when_every_action_is_last_click() -> None:
    """AAP 0.11.2 B4: Google with all included actions last-click on every day runs the test."""
    scenario, ctx = _built("b4.google_last_click.fires")
    result = budget.evaluate(ctx)
    item = _only(result, B4)
    assert item.evidence.extra["contract"] == scenario.expect.values["contract"]
    timezone = next(acct.timezone for acct in ctx.accounts.values() if acct.source == "google")
    assert item.evidence.extra["day_boundary"] == {"source": "google", "boundary": timezone}
    assert f"Google days end at midnight {timezone}" in item.reason
    assert item.flag_codes == (FLAG_NO_CRM_LINK,)
    (finding,) = [finding for finding in result.findings if finding.code == FINDING_NO_CRM_LINK]
    assert finding.evidence == {"reason": "no active HubSpot dataset", "items": 1}
    assert finding.section == BUDGET


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("b4.google_last_click.does_not_fire", WEIGHTED_REASON),
        ("b4.contract_unknown.case", CONTRACT_UNKNOWN_REASON),
        ("b4.fractional.case", FRACTIONAL_REASON),
        ("b4.no_exposure.case", NO_EXPOSURE_REASON),
    ],
)
def test_b4_is_withheld_never_rounded_when_counts_are_not_valid(name: str, reason: str) -> None:
    """AAP 0.5.4.5 B4: weighted attribution, unknown contract, fractional values and no
    exposure each withhold the pattern rather than round it into the test."""
    scenario, ctx = _built(name)
    result = budget.evaluate(ctx)
    assert _items(result, B4) == []
    line = _withheld_on(result, B4, _campaign_id(scenario, "google", "weekly"))
    assert line.reason.startswith(reason), line.reason
    assert line.section == BUDGET


def test_b4_weighted_attribution_is_withheld_even_when_daily_totals_are_whole() -> None:
    """AAP 0.11.2 B4: a data-driven action on one day withholds although every total is whole."""
    scenario, ctx = _built("b4.google_last_click.does_not_fire")
    campaign = _campaign_id(scenario, "google", "weekly")
    values = [day.conversions for day in ctx.campaign_days[campaign]]
    assert all(value is not None and value == value.to_integral_value() for value in values)
    line = _withheld_on(budget.evaluate(ctx), B4, campaign)
    assert line.reason.startswith(WEIGHTED_REASON)


def test_b4_zero_weekday_spend_is_no_exposure() -> None:
    """AAP 0.5.4.5 B4: zero spend on the weekday side withholds "no exposure" too; the sample
    names the side."""
    scenario, ctx = _built("b4.google_last_click.fires")
    campaign = _campaign_id(scenario, "google", "weekly")
    weekend_only = _with_days(
        ctx,
        campaign,
        (
            day if day.date.weekday() >= 5 else replace(day, cost=Decimal(0))
            for day in ctx.campaign_days[campaign]
        ),
    )
    line = _withheld_on(budget.evaluate(weekend_only), B4, campaign)
    assert line.reason == NO_EXPOSURE_REASON
    assert (line.observed, line.required, line.unit) == (0, 1, "weekdays with spend")


def test_b4_missing_outcomes_on_a_day_are_unavailable_never_zero() -> None:
    """AAP 0.11.1: an absent daily outcome is unavailable, so B4 is withheld, not tested."""
    scenario, ctx = _built("b4.linkedin.fires")
    campaign = _campaign_id(scenario, "linkedin", "li-cap")
    days = list(ctx.campaign_days[campaign])
    days[3] = replace(days[3], leads=None)
    result = budget.evaluate(_with_days(ctx, campaign, days))
    assert _items(result, B4) == []
    line = _withheld_on(result, B4, campaign)
    assert (line.observed, line.required) == (len(days) - 1, len(days))
    assert line.reason.startswith("leads unavailable on 1 of")


def test_b4_indistinguishable_rates_are_withheld_with_the_p_value() -> None:
    """AAP 0.5.4.5 B4: p >= alpha withholds the pattern with its p-value and sample sizes."""
    scenario, ctx = _built("b1.step.case")
    result = budget.evaluate(ctx)
    assert _items(result, B4) == []
    line = _withheld_on(result, B4, _campaign_id(scenario, "linkedin", "li-cap"))
    assert line.reason.startswith("weekend and weekday rates not distinguishable")
    assert B4_TEST_NAME in line.reason
    assert line.required == ctx.thresholds.int("b4.min_outcomes")
    assert line.observed is not None
    assert line.observed >= line.required


def test_b4_counting_validity_reads_linkedin_counts_by_definition() -> None:
    """AAP 0.5.4.5 B4: LinkedIn outcomes are event counts; Google needs a verified contract."""
    scenario, ctx = _built("b4.linkedin.fires")
    campaign = ctx.campaigns[_campaign_id(scenario, "linkedin", "li-cap")]
    entity = budget._campaign_ref(campaign)
    days = list(ctx.campaign_days[campaign.id])
    assert budget._counting_validity(ctx, campaign, entity, days) == (
        "LinkedIn leads are unweighted event counts"
    )
    google, google_ctx = _built("b4.google_last_click.fires")
    weekly = google_ctx.campaigns[_campaign_id(google, "google", "weekly")]
    no_actions = _fresh(
        google_ctx,
        conversion_actions={},
        observations={
            key: rows
            for key, rows in google_ctx.observations.items()
            if key[0] != "conversion_action"
        },
    )
    found = budget._counting_validity(
        no_actions,
        weekly,
        budget._campaign_ref(weekly),
        list(google_ctx.campaign_days[weekly.id]),
    )
    assert isinstance(found, Withheld)
    assert found.reason.startswith(CONTRACT_UNKNOWN_REASON)


# ---------------------------------------------------------------------------
# CRM Qualified Leads where linked (AAP 0.5.4.1 "CRM pipeline link")
# ---------------------------------------------------------------------------


def test_budget_crm_links_linkedin_campaigns_through_lead_attribution() -> None:
    """AAP 0.5.4.1: LinkedIn campaigns link to Qualified Leads through lead attribution."""
    scenario, ctx = _built("c2.outcome_scale.case")
    campaign = _campaign_id(scenario, "linkedin", "li-pipeline")
    view = budget_crm(ctx, ("campaign", campaign))
    veto = attribution_for(ctx).veto[campaign]
    assert view.linked
    assert (view.scope, view.eligible_contacts, view.ql) == (
        "linkedin",
        veto.eligible_contacts,
        veto.ql,
    )
    assert view.datasets == CRM_DATASETS["linkedin"]
    item = _only(budget.evaluate(ctx), B1)
    metrics = _metrics(item)
    assert (metrics["eligible_contacts"], metrics["ql"]) == (veto.eligible_contacts, veto.ql)
    assert set(CRM_DATASETS["linkedin"]) <= set(item.required_datasets)


def test_budget_crm_gives_google_no_crm_link_without_a_verified_link() -> None:
    """AAP 0.5.4.1: without the verified Google-to-CRM link, Google items carry no_crm_link."""
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool: SubjectKey = ("budget", _budget_id(scenario, "pool"))
    view = budget_crm(ctx, pool)
    assert (view.scope, view.eligible_contacts, view.ql, view.linked) == (None, None, None, False)
    assert view.datasets == ()
    assert [flag.code for flag in view.flags] == [FLAG_NO_CRM_LINK]
    assert "no active HubSpot dataset" in view.flags[0].message
    unverified = _fresh(ctx, google_crm=_google_crm((), 0, linked=False))
    (flag,) = budget_crm(unverified, pool).flags
    assert (flag.code, UNLINKED_REASON in flag.message) == (FLAG_NO_CRM_LINK, True)
    assert google_link_gap(unverified) == UNLINKED_REASON
    with pytest.raises(KeyError, match="not in the analysis context"):
        budget_crm(ctx, ("budget", 999_999))


def test_budget_crm_combines_the_members_of_a_linked_shared_budget() -> None:
    """AAP 0.5.4.5: a shared budget's card shows its members' combined CRM population."""
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool: SubjectKey = ("budget", _budget_id(scenario, "pool"))
    members = subject_campaigns(ctx, pool)
    externals = [ctx.campaigns[cid].external_id for cid in members]
    linked = _fresh(ctx, google_crm=_google_crm(externals, 3))
    assert google_link_gap(linked) is None
    view = budget_crm(linked, pool)
    veto = attribution_for(linked).veto
    assert view.scope == "google"
    assert view.eligible_contacts == sum(veto[cid].eligible_contacts for cid in members)
    assert view.eligible_contacts == 3 * len(members)
    assert view.ql == sum(veto[cid].ql or 0 for cid in members)
    assert view.datasets == CRM_DATASETS["google"]
    result = budget.evaluate(linked)
    item = _only(result, B1)
    metrics = _metrics(item)
    assert (metrics["eligible_contacts"], metrics["ql"]) == (view.eligible_contacts, view.ql)
    assert FLAG_NO_CRM_LINK not in item.flag_codes
    assert FINDING_NO_CRM_LINK not in _codes(result)
    assert set(CRM_DATASETS["google"]) <= set(item.required_datasets)


def test_budget_crm_flags_explain_untrustworthy_counts() -> None:
    """AAP 0.5.4.1: unavailable pipelines, the attribution floor and unknown evidence are flagged.

    A card's population keeps its own verdicts, exactly as the Campaigns section shows them. A
    shared budget reads one population over its members' member days
    (:func:`~ads_engine.analysis.attribution.shared_budget_veto`), whose floor is already
    judged on all of those matured leads together, so the card never pools members itself.
    """
    scenario, _ = _built("shared.poor_member.does_not_fire")
    m1 = _campaign_id(scenario, "google", "m1")
    flagged = budget._population_flags(
        _population(
            m1, unresolved=2, unknown=1, floor=True, available=False, reason="floor reached"
        )
    )
    assert [flag.code for flag in flagged] == [
        FLAG_PIPELINE_UNAVAILABLE,
        FLAG_ATTRIBUTION_FLOOR,
        FLAG_UNKNOWN_CRM_EVIDENCE,
    ]
    assert flagged[0].message == "Pipeline unavailable: floor reached"
    share = budget._percent(Decimal(2) / Decimal(20))
    assert flagged[1].message == f"Floor: {share} of matured leads unmatched in HubSpot"
    assert flagged[2].message == "Unknown CRM evidence for 1 of 18 eligible contacts"
    # An unresolved share below the floor verdict, on a judged population, flags nothing.
    assert budget._population_flags(_population(m1, unresolved=1)) == ()
    assert BudgetCrm.from_veto(None, "google") == BudgetCrm(None, None, None)
    assert BudgetCrm.from_veto(_population(m1, ql=4), "google") == BudgetCrm("google", 20, 4)


# ---------------------------------------------------------------------------
# Budget card evidence (AAP 0.5.4.5 "Every budget item shows these side by side")
# ---------------------------------------------------------------------------


def test_budget_cards_show_utilization_efficiency_outcomes_budget_and_window() -> None:
    """AAP 0.5.4.5: every budget card shows utilization day counts observed against required,
    efficiency against the baseline, outcomes, the daily budget and the window boundaries."""
    scenario, ctx = _built("b3.idle_known.fires")
    item = _only(budget.evaluate(ctx), B3)
    evidence = item.evidence
    extra = evidence.extra
    keys = [metric.key for metric in evidence.metrics]
    for key in (
        "spend",
        "window_days",
        "outcomes",
        "cost_per_outcome",
        "daily_budget",
        "proposed_daily_budget",
        "window_utilization",
        "known_days",
        "spending_days",
        "spending_day_share",
        "under_utilized_days",
        "under_utilized_days_share",
        "idle_days",
        "changed_intraday_days",
        "unobserved_days",
    ):
        assert key in keys, key
    assert evidence.baselines[0].key == "baseline_cost_per_outcome"
    thresholds = ctx.thresholds
    values = scenario.expect.values
    gate = extra["gate"]
    assert gate["known_budget_days"] == {
        "observed": values["known_days"],
        "required": thresholds.int("budget.min_known_days"),
    }
    assert gate["spending_days"] == {
        "observed": values["spending_days"],
        "required": thresholds.int("budget.min_spending_days"),
    }
    assert gate["under_utilized_days"]["observed"] == values["under_utilized_days"]
    assert extra["gate_shortfalls"] == ()
    assert extra["day_counts"]["idle"] == values["idle_days"]
    max_gap = thresholds.int("budget.max_observation_gap_hours")
    assert extra["observation_assumption"].endswith(f"at most {max_gap} h apart")
    assert extra["unknown_dates"] == ()
    assert extra["day_boundary"] == {"source": "linkedin", "boundary": "UTC"}
    assert extra["objective_groups"] == (OBJ_LINKEDIN_LEAD_GEN,)
    assert extra["currency"] == "USD"
    assert evidence.window["days"] == ctx.window.days
    assert {entry["source"] for entry in evidence.window["source_days"]} >= {"linkedin"}
    assert set(BUDGET_THRESHOLD_KEYS) <= set(evidence.thresholds)
    assert item.required_datasets[:2] == ("linkedin.entities", "linkedin.analytics_daily")
    utilization = _metrics(item)["window_utilization"]
    assert isinstance(utilization, Decimal)
    assert f"window utilization {budget._percent(utilization)}" in item.reason
    assert "members" not in extra
    assert item.entity_ref.id == _campaign_id(scenario, "linkedin", "slack")


def test_budget_evidence_defaults_and_rejects_another_subjects_history() -> None:
    """``budget_evidence`` reads spend and CRM from the subject; a foreign history is refused."""
    scenario, ctx = _built("b3.idle_known.fires")
    subject: SubjectKey = ("campaign", _campaign_id(scenario, "linkedin", "slack"))
    history = budget_history(ctx, subject)
    minimum = ctx.thresholds.int("budget.min_known_days")
    no_predicate = GateResult(True, (), {"known_budget_days": (history.known_active, minimum)})
    evidence = budget_evidence(
        ctx,
        subject,
        history,
        no_predicate,
        outcomes=Decimal(12),
        cpo=None,
        baseline=None,
        headline=None,
    )
    keys = [metric.key for metric in evidence.metrics]
    assert not {"capped_days", "high_utilization_days", "under_utilized_days"} & set(keys)
    assert evidence.metric("spend").value == history.window_spend
    assert evidence.metric("eligible_contacts").value == budget_crm(ctx, subject).eligible_contacts
    assert evidence.baselines[0].value is None
    assert "proposed" not in evidence.extra
    assert "headline_metric" not in evidence.extra
    with pytest.raises(ValueError, match="cannot evidence subject"):
        budget_evidence(
            ctx,
            ("campaign", subject[1] + 1),
            history,
            no_predicate,
            outcomes=None,
            cpo=None,
            baseline=None,
            headline=None,
        )
    # The headline is checked against the card's own metrics, the rule metrics included.
    with pytest.raises(ValueError, match="'capped_days' is not one of the evidence metrics"):
        budget_evidence(
            ctx,
            subject,
            history,
            no_predicate,
            outcomes=Decimal(12),
            cpo=None,
            baseline=None,
            headline="capped_days",
        )
    rule_metric = budget_evidence(
        ctx,
        subject,
        history,
        no_predicate,
        outcomes=Decimal(12),
        cpo=None,
        baseline=None,
        metrics=(Metric("freed", "Freed daily amount", Decimal("5.00"), "currency", "USD"),),
        headline="freed",
    )
    assert rule_metric.extra["headline_metric"] == "freed"


def test_gate_withheld_refuses_a_gate_that_passed() -> None:
    """A passed gate has no shortfall, so it can never become a withheld line (P6)."""
    scenario, ctx = _built("b3.idle_known.fires")
    subject: SubjectKey = ("campaign", _campaign_id(scenario, "linkedin", "slack"))
    history = budget_history(ctx, subject)
    gate = under_utilized(history)
    assert gate.passed
    with pytest.raises(ValueError, match="no shortfall"):
        budget._gate_withheld(B3, budget._subject_ref(ctx, subject), history, gate)
    assert not budget._history_short(gate)


def test_gate_withheld_samples_the_requirement_its_first_shortfall_states() -> None:
    """P6: the sample carries the requirement the reason states, the stricter share included.

    With 10 known-budget days, 4 spending days fail both the minimum (5) and the 70% share
    (7); the one clause and the sample both name 7. With 7 days both ask for 5.
    """
    scenario, ctx = _built("b2.spending_days.case")
    subject: SubjectKey = ("campaign", _campaign_id(scenario, "linkedin", "hot"))
    entity = budget._subject_ref(ctx, subject)
    week = budget_history(ctx, subject)
    assert (week.known_active, week.spending) == (7, 4)
    line = budget._gate_withheld(B2, entity, week, high_utilization(week))
    assert line.reason.startswith("spending days 4 of 5 required; high-utilization days")
    assert (line.observed, line.required, line.unit) == (4, 5, "spending days")
    longer = replace(week, known_active=10, idle=10 - week.spending)
    gate = high_utilization(longer)
    assert gate.counts["spending_days"] == (4, 5)
    line = budget._gate_withheld(B2, entity, longer, gate)
    assert line.reason.startswith("spending days 4 of 7 required (70% of 10 known-budget days)")
    assert line.reason.count("spending days") == 1
    assert (line.observed, line.required, line.unit) == (4, 7, "spending days")


def test_subject_helpers_resolve_campaigns_and_shared_budgets() -> None:
    """Budget subjects resolve to their entity, source, currency and dependent subjects (P1)."""
    scenario, ctx = _built("shared.poor_member.does_not_fire")
    pool = _budget_id(scenario, "pool")
    m1 = _campaign_id(scenario, "google", "m1")
    ref = budget._subject_ref(ctx, ("budget", pool))
    assert (ref.source, ref.type, ref.id) == ("google", ENTITY_BUDGET, pool)
    assert budget._subject_source(ctx, ("budget", pool)) == "google"
    assert budget._subject_currency(ctx, ("budget", pool)) == ctx.budgets[pool].currency
    assert budget._subject_currency(ctx, ("campaign", m1)) == ctx.account_currency(m1)
    assert budget._outcome_label(ctx, ()) == "outcomes"
    assert budget._outcome_label(ctx, (m1,)) == "conversions"
    assert budget._depends_on(ctx, (m1, m1), (("budget", pool),)) == (
        DecisionSubject("budget", pool),
        DecisionSubject("campaign", m1),
    )
    assert not budget._subject_active(ctx, ("campaign", 999_999))


def test_day_boundary_falls_back_to_the_account_zone_then_utc() -> None:
    """AAP 0.5.4.1: evidence names the boundary its dates were read on, even off-window."""
    _, ctx = _built("shared.poor_member.does_not_fire")
    timezone = next(acct.timezone for acct in ctx.accounts.values() if acct.source == "google")
    assert budget._day_boundary(ctx, "google") == timezone
    assert budget._day_boundary(ctx, "linkedin") == "UTC"
    google_days = tuple(item for item in ctx.window.source_days if item.source == "google")
    linkedin_only = replace(
        ctx.window,
        source_days=tuple(replace(item, source="linkedin", boundary="UTC") for item in google_days),
    )
    shifted = _fresh(ctx, window=linkedin_only)
    assert budget._day_boundary(shifted, "google") == timezone
    no_zone = _fresh(
        shifted,
        accounts={key: replace(acct, timezone=None) for key, acct in ctx.accounts.items()},
    )
    assert budget._day_boundary(no_zone, "google") == "UTC"
