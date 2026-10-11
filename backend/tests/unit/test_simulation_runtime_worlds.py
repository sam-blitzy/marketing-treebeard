"""The runtime worlds ``MOCK_SCENARIO`` selects, and the generator capabilities they rest on.

QA findings AAP1-F11 and UX1-F14: the running product could only simulate ``DEFAULT_SPEC``, so
a C2 Hold, C1b, a C0 untracked campaign with "Record intended state", unavailable MQL/QL, CR3
"not estimable", the attribution floors, unknown CRM evidence, pipeline unavailable and low
search-term coverage were unreachable with ``DATA_MODE=simulated``. This module proves, in
memory and with no database:

* **Selection**: ``RUNTIME_SPECS`` is keyed exactly by ``RUNTIME_SCENARIOS``;
  ``SimulatedWorld.from_settings`` builds the ``MOCK_SCENARIO`` world (or an explicit one) with
  the given clock and epoch; the default world keeps every default.
* **Capabilities**: a campaign's own CRM funnel (``LinkedInCampaignSpec.funnel``); the world's
  epoch and ``PlatformChange.reappear_after_epoch``; ``WorldSpec.observed_history_days``;
  ``SimulatedWorld.last_platform_change``; ``Fault.person_creatives``, resolved by
  ``SimulatedWorld.fault_targets`` identically on the wire (``payloads.render``) and in the
  scenario converter; the controller's world-identity check (``world_mismatch``).
* **States**: every state the catalogue's ``runtime.<world>.case`` entries promise holds in
  both runtime worlds for several seeds and instants, including 00:00-05:00 UTC, when the New
  York day lags the UTC day, and the US daylight-saving changeovers.

The database half (seeded budget history, the untracked campaign raised by a later entity sync,
the world switch that turns earlier runs historical, 409 ``subject_retired``) is
``integration/test_end_to_end_simulated.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cache
from typing import Any, Final, cast

import httpx
import pytest

from ads_engine.analysis import (
    budget,
    campaigns,
    creative,
    discrepancy,
    keywords,
    precedence,
    white_papers,
)
from ads_engine.analysis.attribution import attribution_for
from ads_engine.analysis.context import (
    B1,
    C0,
    C1B,
    C2,
    CAMPAIGNS,
    CR3,
    FLAG_CREATIVE_FLOOR,
    FLAG_DEAL_FLOOR,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_SEARCH_TERM_COVERAGE,
    FLAG_SIMULATED,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    KEYWORDS,
    SECTION_ORDER,
    WHITE_PAPERS,
    AnalysisContext,
    Candidate,
)
from ads_engine.analysis.precedence import PrecedenceOutput
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.db.models.ops import SourceDataset
from ads_engine.settings import Settings
from ads_engine.simulation import DEFAULT_SCENARIO, HUBSPOT_CONTACT_IDS, RUNTIME_SCENARIOS, payloads
from ads_engine.simulation.generator import (
    CRM_UNAVAILABLE_SPEC,
    DEFAULT_SPEC,
    EDGE_STATES_SPEC,
    RUNTIME_SPECS,
    Fault,
    FunnelSpec,
    LinkedInCampaignSpec,
    LinkedInSpec,
    PlatformChange,
    SimulatedWorld,
    WorldSpec,
    person_contact_id,
)
from ads_engine.simulation.scenarios import SCENARIOS, ContextSpec, Scenario, context_from_world
from ads_engine.sync.mode import SourceModeController

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

NOW: Final = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
"""A mid-afternoon UTC instant, the runtime worlds' anchor date in the probes reported."""

EPOCH_LEAD: Final = timedelta(hours=6)
"""How long before the evaluated instant each world's epoch lies: past the untracked campaign's
reappearance, as on a stack that has run for a few hours."""

SEEDS: Final[tuple[int, ...]] = (42, 7)
"""``MOCK_SEED`` values the states are checked at: the default and another."""

INSTANTS: Final[tuple[datetime, ...]] = (
    NOW,
    # US daylight saving ends on 2026-11-01; at 04:30 UTC the New York day still lags UTC.
    datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
    # US daylight saving starts on 2027-03-14; 06:00 UTC is 01:00 in New York.
    datetime(2027, 3, 14, 6, 0, tzinfo=UTC),
)
"""Clock instants on different dates and times of day."""

RUNTIME_WORLDS: Final[tuple[str, ...]] = RUNTIME_SCENARIOS[1:]
"""The seeded edge worlds beside ``default``."""

MODULES: Final = (discrepancy, keywords, campaigns, budget, creative, white_papers)
"""Every rule module, in the runner's evaluation order."""

ASSOCIATIONS_URL: Final = (
    "https://api.hubapi.com/crm/associations/2026-09/contacts/deals/batch/read"
)
"""The simulated HubSpot contact-to-deal association batch read."""

SCALE_CREATIVE: Final = "cr_edge_scale"
"""The lead creative of "Benchmarks - Retargeting (NA)", whose people's deal reads fail."""


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _runtime_scenario(world: str) -> Scenario:
    """The catalogue entry of a runtime world."""
    return SCENARIOS[f"runtime.{world}.case"]


def _world(name: str, seed: int, now: datetime) -> SimulatedWorld:
    """A runtime world on a fixed clock, its epoch :data:`EPOCH_LEAD` before ``now``."""
    return SimulatedWorld(seed, RUNTIME_SPECS[name], clock=lambda: now, epoch=now - EPOCH_LEAD)


@cache
def _outcome(name: str, seed: int, now: datetime) -> tuple[AnalysisContext, PrecedenceOutput]:
    """The default-window context of a runtime world and its precedence output (memoised)."""
    ctx = context_from_world(_world(name, seed, now), ContextSpec(), Thresholds.load(), now=now)
    return ctx, precedence.apply_precedence([module.evaluate(ctx) for module in MODULES], ctx)


def _candidates(out: PrecedenceOutput, section: str | None = None) -> list[Candidate]:
    """The ranked candidates of one section, or of every section."""
    sections = SECTION_ORDER if section is None else (section,)
    return [item.candidate for name in sections for item in out.items[name]]


def _named(out: PrecedenceOutput, rule_id: str, name: str) -> list[Candidate]:
    """The ranked candidates of ``rule_id`` on the entity called ``name``."""
    return [c for c in _candidates(out) if c.rule_id == rule_id and c.entity_ref.name == name]


def _metric(candidate: Candidate, key: str) -> object:
    """A metric value of a candidate's evidence.

    Raises:
        AssertionError: When the evidence has no such metric.
    """
    for metric in candidate.evidence.metrics:
        if metric.key == key:
            return metric.value
    raise AssertionError(f"{candidate.rule_id} {candidate.entity_ref.name}: no metric {key!r}")


def _flags(candidate: Candidate) -> set[str]:
    """A candidate's flag codes."""
    return {flag.code for flag in candidate.flags}


def _only(found: Sequence[Candidate], what: str) -> Candidate:
    """The single candidate of ``found``.

    Raises:
        AssertionError: When there is not exactly one, naming ``what``.
    """
    assert len(found) == 1, f"{what}: {len(found)} candidates"
    return found[0]


def _position(spec: WorldSpec, creative_key: str) -> int:
    """The index of a creative in the spec's LinkedIn creatives (its person-id position)."""
    linkedin = cast(LinkedInSpec, spec.linkedin)
    return next(i for i, item in enumerate(linkedin.creatives) if item.key == creative_key)


def _associations(world: SimulatedWorld, contact_ids: Sequence[str]) -> Mapping[str, Any]:
    """Render one contact-to-deal association batch read from ``world``'s portal."""
    request = httpx.Request(
        "POST",
        ASSOCIATIONS_URL,
        headers={"Authorization": "Bearer <redacted>", "Content-Type": "application/json"},
        content=json.dumps({"inputs": [{"id": i} for i in contact_ids]}).encode(),
    )
    rendered = payloads.render("hubspot", request, world)
    assert rendered.status == 207 or rendered.status == 200, rendered.body
    return cast(Mapping[str, Any], rendered.body)


def _error_ids(body: Mapping[str, Any], category: str) -> set[str]:
    """The ``fromObjectId`` of every batch error of ``category``."""
    return {
        identifier
        for error in body.get("errors", [])
        if error["category"] == category
        for identifier in error["context"]["fromObjectId"]
    }


# ---------------------------------------------------------------------------------------------
# Selection: the registry, from_settings and the default world
# ---------------------------------------------------------------------------------------------


def test_runtime_specs_are_keyed_by_the_runtime_scenario_names() -> None:
    assert tuple(RUNTIME_SPECS) == RUNTIME_SCENARIOS
    assert RUNTIME_SPECS[DEFAULT_SCENARIO] is DEFAULT_SPEC
    assert RUNTIME_SPECS["edge_states"] is EDGE_STATES_SPEC
    assert RUNTIME_SPECS["crm_unavailable"] is CRM_UNAVAILABLE_SPEC
    assert all(spec.name == name for name, spec in RUNTIME_SPECS.items())
    assert all(spec.anchor is None for spec in RUNTIME_SPECS.values())


@pytest.mark.parametrize("name", RUNTIME_SCENARIOS)
def test_from_settings_builds_the_mock_scenario_world(name: str) -> None:
    epoch = NOW - timedelta(hours=1)
    world = SimulatedWorld.from_settings(
        Settings(mock_scenario=name, mock_seed=7), clock=lambda: NOW, epoch=epoch
    )
    assert world.spec is RUNTIME_SPECS[name]
    assert (world.name, world.seed, world.now(), world.epoch) == (name, 7, NOW, epoch)


def test_from_settings_takes_an_explicit_world_over_mock_scenario() -> None:
    settings = Settings(mock_scenario="edge_states")
    world = SimulatedWorld.from_settings(settings, DEFAULT_SPEC, clock=lambda: NOW)
    assert world.spec is DEFAULT_SPEC
    assert world.epoch == NOW


def test_the_default_world_keeps_every_default() -> None:
    assert Settings().mock_scenario == DEFAULT_SCENARIO
    assert SimulatedWorld.from_settings(Settings()).spec is DEFAULT_SPEC
    assert DEFAULT_SPEC.observed_history_days == 0
    assert all(change.reappear_after_epoch is None for change in DEFAULT_SPEC.platform_changes)
    assert all(not fault.person_creatives for fault in DEFAULT_SPEC.faults)
    linkedin = cast(LinkedInSpec, DEFAULT_SPEC.linkedin)
    assert all(campaign.funnel is None for campaign in linkedin.campaigns)
    world = SimulatedWorld(42, DEFAULT_SPEC, clock=lambda: NOW)
    assert (world.observed_history_days, world.epoch) == (0, NOW)
    assert world.last_platform_change("google", NOW) is None
    assert world.last_platform_change("linkedin", NOW) is None


def test_edge_worlds_append_to_the_default_entities() -> None:
    """Every default entity keeps its position, so every default id is unchanged."""
    for spec in (EDGE_STATES_SPEC, CRM_UNAVAILABLE_SPEC):
        for source in ("google", "linkedin"):
            default_part = getattr(DEFAULT_SPEC, source)
            edge_part = getattr(spec, source)
            for field in ("campaigns", "budgets", "keywords", "creatives", "accounts"):
                if not hasattr(default_part, field):
                    continue
                before = getattr(default_part, field)
                assert getattr(edge_part, field)[: len(before)] == before, (spec.name, field)
    without_stages = EDGE_STATES_SPEC.with_(
        name="crm_unavailable",
        hubspot=cast(Any, EDGE_STATES_SPEC.hubspot).with_(
            removed_stage_labels=("First meeting booked", "Qualified Lead", "Sales Qualified Lead")
        ),
    )
    assert without_stages == CRM_UNAVAILABLE_SPEC


# ---------------------------------------------------------------------------------------------
# Capabilities: funnel, epoch, observed history, last platform change, person faults
# ---------------------------------------------------------------------------------------------


def test_a_campaign_funnel_must_be_a_funnel_spec() -> None:
    linkedin = cast(LinkedInSpec, EDGE_STATES_SPEC.linkedin)
    campaign = next(item for item in linkedin.campaigns if item.funnel is not None)
    assert isinstance(campaign.funnel, FunnelSpec)
    with pytest.raises(ValueError, match="funnel must be a FunnelSpec"):
        replace(campaign, funnel=cast(Any, "portal"))
    assert isinstance(replace(campaign, funnel=None), LinkedInCampaignSpec)


def test_campaign_funnels_shape_their_own_people_only() -> None:
    """C1b and Hold people never reach QL; people of portal-funnel campaigns do."""
    ctx, _out = _outcome("edge_states", 42, NOW)
    veto = attribution_for(ctx).veto
    by_name = {ctx.campaigns[campaign_id].name: entry for campaign_id, entry in veto.items()}
    c1b = by_name["Analyst Brief - Lead gen (NA)"]
    hold = by_name["ROI Calculator - Lead gen (NA)"]
    assert (c1b.ql, c1b.meetings) == (0, 0)
    assert (c1b.mql or 0) > 0
    assert hold.ql == 0
    assert (hold.meetings or 0) > 0
    for entry in (c1b, hold):
        assert entry.matured_leads == entry.eligible_contacts >= 20
        assert entry.unresolved_share in (None, Decimal(0))
    assert by_name["Pipeline Report - Lead gen (NA)"].ql, "the portal funnel reaches QL"


def test_the_epoch_defaults_to_the_clock_and_must_be_aware() -> None:
    clock = [NOW]
    world = SimulatedWorld(42, EDGE_STATES_SPEC, clock=lambda: clock[0])
    clock[0] = NOW + timedelta(hours=1)
    assert world.epoch == NOW, "the epoch is the clock's reading at construction"
    with pytest.raises(ValueError, match="epoch"):
        SimulatedWorld(42, EDGE_STATES_SPEC, clock=lambda: NOW, epoch=NOW.replace(tzinfo=None))


def test_a_campaign_reappears_a_set_time_after_the_epoch() -> None:
    epoch = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)
    world = SimulatedWorld(42, EDGE_STATES_SPEC, clock=lambda: epoch, epoch=epoch)
    after = _runtime_scenario("edge_states").expect.values["c0_untracked_after_epoch_minutes"]
    name = _runtime_scenario("edge_states").expect.values["c0_untracked"]

    def listed(at: datetime) -> bool:
        return name in {campaign.name for campaign in world.google_campaigns(at)}

    assert not listed(epoch)
    assert not listed(epoch + timedelta(minutes=cast(int, after)) - timedelta(seconds=1))
    assert listed(epoch + timedelta(minutes=cast(int, after)))
    change = next(c for c in EDGE_STATES_SPEC.platform_changes if c.reappear_after_epoch)
    assert change.reappear_instant(world.anchor, epoch) == epoch + timedelta(minutes=15)
    with pytest.raises(ValueError, match="needs the world's epoch"):
        change.reappear_instant(world.anchor)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"disappear": False, "field": "status", "value": "PAUSED"},
            "only a disappearance can reappear",
        ),
        ({"reappear_at_offset_hours": 1}, "reappear_after_epoch excludes reappear_at"),
        ({"reappear_after_epoch": 15}, "reappear_after_epoch must be a timedelta"),
    ],
)
def test_reappear_after_epoch_is_validated(changes: dict[str, Any], message: str) -> None:
    change = next(c for c in EDGE_STATES_SPEC.platform_changes if c.reappear_after_epoch)
    assert isinstance(change, PlatformChange)
    with pytest.raises(ValueError, match=message):
        replace(change, **changes)


@pytest.mark.parametrize(
    ("days", "message"),
    [
        (-1, "observed_history_days"),
        (EDGE_STATES_SPEC.history_days + 1, "observed_history_days must be at most history_days"),
    ],
)
def test_observed_history_days_is_bounded(days: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        EDGE_STATES_SPEC.with_(observed_history_days=days)


def test_the_edge_worlds_record_observed_budget_history() -> None:
    for name in RUNTIME_WORLDS:
        world = _world(name, 42, NOW)
        days = _runtime_scenario(name).expect.values["observed_history_days"]
        assert world.observed_history_days == days == 35


def test_last_platform_change_follows_the_epoch() -> None:
    epoch = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)
    world = SimulatedWorld(42, EDGE_STATES_SPEC, clock=lambda: epoch, epoch=epoch)
    disappeared = world.last_platform_change("google", epoch)
    assert disappeared is not None
    assert disappeared < epoch - timedelta(days=EDGE_STATES_SPEC.history_days)
    assert world.last_platform_change("google", epoch + timedelta(minutes=14)) == disappeared
    reappeared = epoch + timedelta(minutes=15)
    assert world.last_platform_change("google", reappeared) == reappeared
    assert world.last_platform_change("google", epoch + timedelta(days=3)) == reappeared
    assert world.last_platform_change("linkedin", epoch + timedelta(days=3)) is None
    with pytest.raises(ValueError, match="at"):
        world.last_platform_change("google", epoch.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"ids": ("9900000000000001",)}, "a fault takes ids or person_creatives, not both"),
        ({"source": "linkedin", "surface": "lead_form_responses"}, "person_creatives applies"),
        ({"kind": "continuation_failure"}, "person_creatives applies"),
        ({"person_creatives": ()}, "a batch_errors fault needs ids"),
    ],
)
def test_person_creative_faults_are_validated(changes: dict[str, Any], message: str) -> None:
    (fault,) = EDGE_STATES_SPEC.faults
    with pytest.raises(ValueError, match=message):
        replace(fault, **changes)


def test_a_person_creative_fault_must_name_a_lead_creative() -> None:
    (fault,) = EDGE_STATES_SPEC.faults
    spec = EDGE_STATES_SPEC.with_(faults=(replace(fault, person_creatives=("cr_missing",)),))
    with pytest.raises(ValueError, match="names unknown lead creative 'cr_missing'"):
        SimulatedWorld(42, spec, clock=lambda: NOW)


def test_fault_targets_reads_the_creative_from_the_person_contact_id() -> None:
    world = _world("edge_states", 42, NOW)
    (fault,) = world.spec.faults
    position = _position(world.spec, SCALE_CREATIVE)
    day = date(2026, 10, 1)
    assert world.fault_targets(fault, person_contact_id(position, day, 0))
    assert world.fault_targets(fault, person_contact_id(position, day + timedelta(days=9), 7))
    assert not world.fault_targets(fault, person_contact_id(position - 1, day, 0))
    assert not world.fault_targets(fault, person_contact_id(0, day, 3))
    # Outside the person block, and not a contact id at all.
    assert not world.fault_targets(fault, str(HUBSPOT_CONTACT_IDS.start - 1))
    assert not world.fault_targets(fault, str(HUBSPOT_CONTACT_IDS.end + 1))
    assert not world.fault_targets(fault, "not-a-contact")
    explicit = Fault(
        "hubspot",
        "associations_batch_read",
        "batch_errors",
        ids=("9900000000000001",),
        category="VALIDATION_ERROR",
    )
    assert world.fault_targets(explicit, "9900000000000001")
    assert not world.fault_targets(explicit, person_contact_id(position, day, 0))


def test_the_wire_fails_the_deal_reads_of_the_faulted_creatives_people() -> None:
    """``payloads.render`` resolves ``person_creatives`` to the request's matching ids."""
    world = _world("edge_states", 42, NOW)
    ctx, _out = _outcome("edge_states", 42, NOW)
    leads = sorted(ctx.leads, key=lambda lead: (lead.submitted_at, lead.id))
    retargeting = _campaign_id(ctx, "Benchmarks - Retargeting (NA)")
    (scale_creative,) = (c.id for c in ctx.creatives.values() if c.campaign_id == retargeting)
    faulted = sorted(
        {
            lead.contact_hs_id
            for lead in leads
            if lead.creative_id == scale_creative and lead.contact_hs_id is not None
        }
    )[:5]
    others = sorted(
        {
            lead.contact_hs_id
            for lead in leads
            if lead.creative_id not in (None, scale_creative) and lead.contact_hs_id is not None
        }
    )[:5]
    assert faulted
    assert others
    (fault,) = world.spec.faults
    assert all(world.fault_targets(fault, identifier) for identifier in faulted)
    assert not any(world.fault_targets(fault, identifier) for identifier in others)
    body = _associations(world, [*faulted, *others])
    assert _error_ids(body, "VALIDATION_ERROR") == set(faulted)
    answered = {result["from"]["id"] for result in body.get("results", [])}
    assert answered | _error_ids(body, "OBJECT_NOT_FOUND") == set(others)


def _campaign_id(ctx: AnalysisContext, name: str) -> int:
    """The id of the context's campaign called ``name``."""
    (found,) = (key for key, campaign in ctx.campaigns.items() if campaign.name == name)
    return found


def test_the_converter_fails_the_same_people_as_the_wire() -> None:
    """In memory, exactly the faulted campaign's contacts have unknown CRM evidence."""
    ctx, _out = _outcome("edge_states", 42, NOW)
    veto = attribution_for(ctx).veto
    for campaign_id, entry in veto.items():
        name = ctx.campaigns[campaign_id].name
        if name == "Benchmarks - Retargeting (NA)":
            assert entry.eligible_contacts > 0
            assert entry.unknown_evidence_contacts == entry.eligible_contacts
            assert entry.available is False
        else:
            assert not entry.unknown_evidence_contacts, name


# ---------------------------------------------------------------------------------------------
# World identity: the controller never syncs a dataset of another world
# ---------------------------------------------------------------------------------------------


def _dataset(mode: str, world: str | None, seed: int | None) -> SourceDataset:
    """A transient ``source_dataset`` row (never flushed)."""
    return SourceDataset(
        id=1,
        source="google",
        mode=mode,
        state="active",
        role="primary",
        simulated_world=world,
        simulated_seed=seed,
    )


def test_world_mismatch_names_both_worlds() -> None:
    controller = SourceModeController(Settings(mock_scenario="crm_unavailable", mock_seed=42))
    assert controller.configured_world is not None
    assert controller.configured_world.describe() == "crm_unavailable (seed 42)"
    reason = controller.world_mismatch(_dataset("simulated", "edge_states", 42))
    assert reason == (
        "simulated dataset not synced: generated from world edge_states (seed 42), "
        "configured crm_unavailable (seed 42)"
    )
    assert controller.world_mismatch(_dataset("simulated", "crm_unavailable", 7)) is not None
    assert controller.world_mismatch(_dataset("simulated", "crm_unavailable", 42)) is None
    assert controller.world_mismatch(_dataset("simulated", None, None)) is None
    assert controller.world_mismatch(_dataset("live", None, None)) is None


def test_an_injected_world_records_and_checks_no_world() -> None:
    world = SimulatedWorld(42, EDGE_STATES_SPEC, clock=lambda: NOW)
    controller = SourceModeController(Settings(mock_scenario="default"), world=world)
    assert controller.configured_world is None
    assert controller.world is world
    assert controller.world_mismatch(_dataset("simulated", "edge_states", 7)) is None


# ---------------------------------------------------------------------------------------------
# States: every promised state, both worlds, several seeds and instants
# ---------------------------------------------------------------------------------------------

CASES: Final = [
    pytest.param(name, seed, now, id=f"{name}-{seed}-{now:%Y%m%dT%H%M}")
    for name in RUNTIME_WORLDS
    for seed in SEEDS
    for now in INSTANTS
]


def test_the_catalogue_promises_the_runtime_states() -> None:
    for name in RUNTIME_WORLDS:
        scenario = _runtime_scenario(name)
        assert scenario.spec is RUNTIME_SPECS[name]
        assert FLAG_SIMULATED in scenario.expect.flags
        assert tuple(cast(Sequence[str], scenario.expect.values["populated_sections"])) == (
            SECTION_ORDER
        )


@pytest.mark.parametrize(("name", "seed", "now"), CASES)
def test_every_shared_state_holds(name: str, seed: int, now: datetime) -> None:
    values = _runtime_scenario(name).expect.values
    _ctx, out = _outcome(name, seed, now)
    assert [section for section in SECTION_ORDER if not out.items[section]] == []

    untracked = _only(_named(out, C0, cast(str, values["c0_untracked"])), "C0 untracked")
    extra = untracked.evidence.extra
    assert (extra["kind"], extra["shape"]) == ("untracked", "own_budget")
    assert tuple(extra["resolutions"]) == ("adopt_platform", "record_intended")

    scale_name = cast(str, values["c2_scale_unknown_crm_evidence"])
    scale = _only(_named(out, C2, scale_name), "C2 Scale")
    assert scale.action.startswith("Scale")
    assert {FLAG_PIPELINE_UNAVAILABLE, FLAG_UNKNOWN_CRM_EVIDENCE} <= _flags(scale)
    assert FLAG_PIPELINE_UNAVAILABLE in _flags(_only(_named(out, B1, scale_name), "its B1"))

    cr3 = _only(_named(out, CR3, cast(str, values["cr3_not_estimable"])), "CR3")
    assert _metric(cr3, "sample_per_variant") == "not estimable"
    next_test = cr3.evidence.extra["next_test"]
    assert next_test["not_estimable_reason"] == "target above 100%"
    assert Decimal(str(next_test["baseline"])) > Decimal(1) / Decimal("1.2")

    rows = _candidates(out, WHITE_PAPERS)
    assert all(FLAG_DEAL_FLOOR in _flags(row) for row in rows)
    benchmarks = _only(
        [row for row in rows if row.entity_ref.name == "Paid Media Benchmarks 2026"], "W row"
    )
    wanted = set(cast(Sequence[str], values["white_paper_flags"]))
    assert wanted == {FLAG_CREATIVE_FLOOR, FLAG_DEAL_FLOOR, FLAG_UNKNOWN_CRM_EVIDENCE}
    assert wanted <= _flags(benchmarks)

    coverage = [f for f in out.findings[KEYWORDS] if f.code == values["low_search_term_coverage"]]
    assert len(coverage) == 1, coverage
    assert all(FLAG_SEARCH_TERM_COVERAGE in _flags(c) for c in _candidates(out, KEYWORDS))


@pytest.mark.parametrize(
    ("seed", "now"), [(seed, now) for seed in SEEDS for now in INSTANTS], ids=str
)
def test_edge_states_reach_a_c2_hold_and_c1b(seed: int, now: datetime) -> None:
    values = _runtime_scenario("edge_states").expect.values
    _ctx, out = _outcome("edge_states", seed, now)

    hold_name = cast(str, values["c2_hold"])
    hold = _only(_named(out, C2, hold_name), "C2 Hold")
    assert hold.action.startswith("Hold")
    assert _metric(hold, "ql") == 0
    for key in ("matured_leads", "matched_leads", "eligible_contacts", "mql", "meetings"):
        assert cast(int, _metric(hold, key)) > 0, key
    assert _metric(hold, "unresolved") == 0
    assert _named(out, B1, hold_name) == [], "a vetoed campaign gets no B1"

    c1b = _only(_named(out, C1B, cast(str, values["c1b"])), "C1b")
    assert (_metric(c1b, "ql"), _metric(c1b, "meetings")) == (0, 0)
    assert cast(int, _metric(c1b, "eligible_contacts")) >= 20


@pytest.mark.parametrize(
    ("seed", "now"), [(seed, now) for seed in SEEDS for now in INSTANTS], ids=str
)
def test_crm_unavailable_makes_mql_and_ql_unavailable(seed: int, now: datetime) -> None:
    values = _runtime_scenario("crm_unavailable").expect.values
    _ctx, out = _outcome("crm_unavailable", seed, now)

    for name in cast(Sequence[str], values["c2_scale_pipeline_unavailable"]):
        scale = _only(_named(out, C2, name), f"C2 {name}")
        assert scale.action.startswith("Scale"), name
        assert FLAG_PIPELINE_UNAVAILABLE in _flags(scale), name

    c1b_name = cast(str, values["c1b_withheld"])
    assert _named(out, C1B, c1b_name) == []
    withheld = [
        line
        for line in out.withheld[CAMPAIGNS]
        if line.rule_id == C1B and line.entity is not None and line.entity.name == c1b_name
    ]
    assert len(withheld) == 1
    assert withheld[0].reason.startswith("pipeline unavailable")

    rows = _candidates(out, WHITE_PAPERS)
    assert rows
    for row in rows:
        for count in cast(Sequence[str], values["unavailable_counts"]):
            assert _metric(row, count) is None, (row.entity_ref.name, count)
        assert FLAG_PIPELINE_UNAVAILABLE in _flags(row)
    finding = _runtime_scenario("crm_unavailable").expect.finding
    assert any(f.code == finding for f in out.findings[WHITE_PAPERS])
