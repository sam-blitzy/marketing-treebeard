"""Contract tests of the scenario catalogue in ``ads_engine.simulation.scenarios``.

AAP 0.11.2 ("Acceptance tests"): every acceptance test runs on scenario fixtures built so that
the condition fires in one case and, with a single input moved across its threshold, does not
fire in the other. AAP 0.5.1 (Simulation): ``SCENARIOS`` is the catalogue that makes each rule
fire and not fire. These tests hold the catalogue to that contract (names, twins, single-input
differences, the module partition and rule coverage), build every scenario's context without a
database, exercise every validation branch of the declarative specs, the configuration
overrides and the CRM binding resolution, and cross-check the expectations the rule code that
already exists can evaluate (C0 discrepancies and the pipeline veto).

No database, network or file writes: the shipped ``config/`` files are only read.
"""

import dataclasses
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from ads_engine.analysis import discrepancy
from ads_engine.analysis.attribution import build_attribution
from ads_engine.analysis.context import (
    B1,
    C0,
    C1B,
    C2,
    CR3,
    FINDING_PIPELINE_UNAVAILABLE,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_STALE,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    G1,
    K1,
    RULE_IDS,
    AnalysisContext,
    DatasetRef,
    DecisionInfo,
    DiscrepancyInfo,
    FeedbackInfo,
    search_term_external_id,
)
from ads_engine.analysis.thresholds import (
    CONTENT_ASSETS_FILE,
    FORM_URN_PREFIX,
    THRESHOLDS_FILE,
    ConfigError,
    CrmBindings,
    StageBinding,
    Thresholds,
)
from ads_engine.db.models.ops import DATASETS, SOURCES
from ads_engine.simulation import (
    DEFAULT_SCENARIO,
    DEFAULT_SEED,
    GOOGLE_CAMPAIGN_IDS,
    HUBSPOT_STAGE_IDS,
    LINKEDIN_CAMPAIGN_IDS,
    scenarios,
)
from ads_engine.simulation.generator import (
    GoogleSearchTermSpec,
    PropertyRecord,
    SimulatedWorld,
    WorldSpec,
)
from ads_engine.simulation.scenarios import (
    FIXED_ANCHOR,
    FIXED_CLOCK,
    FIXED_NOW,
    MODULE_CAMPAIGNS,
    MODULE_END_TO_END,
    MODULE_KEYWORDS,
    MODULES,
    REPLACED_DATASET_OFFSET,
    SCENARIOS,
    ContextSpec,
    CrmPatch,
    DecisionSpec,
    DiscrepancySpec,
    EntityIndex,
    Expectation,
    FeedbackSpec,
    FreshnessOverride,
    Scenario,
    SubjectRef,
    context_from_world,
    for_module,
    input_differences,
    pairs,
    resolve_bindings,
    thresholds_with,
    validate_catalogue,
)

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
MIDNIGHT = datetime.combine(FIXED_ANCHOR, time(0), tzinfo=UTC)
CONFLICT = "wp.override_conflict.case"
BUILDABLE = tuple(name for name in SCENARIOS if name != CONFLICT)
PAIRS = tuple(pairs())
# B1 is emitted exactly with C2's "Scale" outcome and never under a veto "Hold" (its not-firing
# half is the C2 Hold case), and CR3 always proposes a next test, "exploratory" when no stage
# is weak. Neither rule has a not-firing condition of its own, so the catalogue pins them with
# cases instead of a fires / does_not_fire twin.
NO_OWN_TWIN = frozenset({B1, CR3})


def _scenario(name: str) -> Scenario:
    return SCENARIOS[name]


def _with_context(scenario: Scenario, **changes: Any) -> Scenario:
    """The scenario with its context spec changed (validated again on construction)."""
    return dataclasses.replace(scenario, context=dataclasses.replace(scenario.context, **changes))


# ---------------------------------------------------------------------------
# Fixed time and module paths
# ---------------------------------------------------------------------------


def test_fixed_clock_reads_noon_utc_on_the_anchor() -> None:
    assert FIXED_CLOCK() == FIXED_NOW == datetime(2026, 10, 2, 12, tzinfo=UTC)
    assert FIXED_NOW.date() == FIXED_ANCHOR
    assert len(set(MODULES)) == len(MODULES) == 12
    assert all(re.fullmatch(r"(acceptance|integration)/test_[a-z0-9_]+\.py", m) for m in MODULES)


# ---------------------------------------------------------------------------
# Catalogue structure: validate_catalogue and _catalogue_problems
# ---------------------------------------------------------------------------


def test_shipped_catalogue_has_no_structural_problem() -> None:
    assert scenarios._catalogue_problems(SCENARIOS) == []
    validate_catalogue()
    validate_catalogue(SCENARIOS)
    assert isinstance(SCENARIOS, MappingProxyType)
    assert next(iter(SCENARIOS)) == DEFAULT_SCENARIO
    assert all(name == scenario.name for name, scenario in SCENARIOS.items())


def _without(*names: str) -> dict[str, Scenario]:
    catalogue = dict(SCENARIOS)
    for name in names:
        del catalogue[name]
    return catalogue


def _replacing(name: str, **changes: Any) -> dict[str, Scenario]:
    catalogue = dict(SCENARIOS)
    catalogue[name] = dataclasses.replace(SCENARIOS[name], **changes)
    return catalogue


def _renamed(name: str, new_name: str) -> dict[str, Scenario]:
    catalogue = _without(name)
    catalogue[new_name] = dataclasses.replace(SCENARIOS[name], name=new_name)
    return catalogue


def _keyed_under(key: str, name: str) -> dict[str, Scenario]:
    catalogue = dict(SCENARIOS)
    catalogue[key] = SCENARIOS[name]
    return catalogue


@pytest.mark.parametrize(
    ("catalogue", "problems"),
    [
        pytest.param(
            _without("k1.gate.does_not_fire"),
            ["k1.gate.fires: pair 'k1.gate.does_not_fire' does not resolve"],
            id="missing-twin",
        ),
        pytest.param(
            _renamed("c0.removed.case", "c0.removed.standalone"),
            ["c0.removed.standalone: the name must end in its variant 'case'"],
            id="name-without-variant",
        ),
        pytest.param(
            _keyed_under("c0.alias.case", "c0.removed.case"),
            ["'c0.alias.case' is keyed under another name than 'c0.removed.case'"],
            id="mis-keyed",
        ),
        pytest.param(
            _replacing("c0.removed.case", variant="maybe"),
            [
                "c0.removed.case: unknown variant 'maybe'",
                "c0.removed.case: the name must end in its variant 'maybe'",
                "c0.removed.case: pair None does not resolve",
            ],
            id="unknown-variant",
        ),
        pytest.param(
            _replacing("c0.removed.case", module="acceptance/test_nowhere.py"),
            ["c0.removed.case: unknown module 'acceptance/test_nowhere.py'"],
            id="unknown-module",
        ),
        pytest.param(
            _replacing("c0.removed.case", pair="c0.status.fires"),
            ["c0.removed.case: a case has no pair"],
            id="case-with-pair",
        ),
        pytest.param(
            _replacing("k1.gate.does_not_fire", module=MODULE_CAMPAIGNS),
            [
                "k1.gate.fires: pair 'k1.gate.does_not_fire' runs in another module",
                "k1.gate.does_not_fire: pair 'k1.gate.fires' runs in another module",
            ],
            id="twin-in-another-module",
        ),
        pytest.param(
            _replacing("k1.gate.does_not_fire", pair="k1.low_rate_1pct.fires"),
            [
                "k1.gate.fires: pair 'k1.gate.does_not_fire' is not its does_not_fire twin",
                "k1.gate.does_not_fire: pair 'k1.low_rate_1pct.fires' is not its fires twin",
            ],
            id="pair-not-pointing-back",
        ),
    ],
)
def test_catalogue_problems_name_each_defect(
    catalogue: dict[str, Scenario], problems: list[str]
) -> None:
    assert scenarios._catalogue_problems(catalogue) == problems
    with pytest.raises(ValueError, match=r"^invalid scenario catalogue: ") as raised:
        validate_catalogue(catalogue)
    assert str(raised.value) == "invalid scenario catalogue: " + "; ".join(problems)


def test_catalogue_problems_report_uncovered_modules_and_rules() -> None:
    keywords_only = {name: s for name, s in SCENARIOS.items() if s.module == MODULE_KEYWORDS}
    problems = scenarios._catalogue_problems(keywords_only)
    covered = {s.expect.rule_id for s in keywords_only.values()}
    assert [p for p in problems if p.startswith("module ")] == [
        f"module {module} has no scenario" for module in MODULES if module != MODULE_KEYWORDS
    ]
    assert [p for p in problems if p.startswith("rule ")] == [
        f"rule {rule} appears in no expectation"
        for rule in scenarios._REQUIRED_RULES
        if rule not in covered
    ]
    assert "rule C0 appears in no expectation" in problems
    assert len(problems) == len(MODULES) - 1 + len(set(scenarios._REQUIRED_RULES) - covered)
    with pytest.raises(ValueError, match=f"module {re.escape(MODULE_CAMPAIGNS)} has no scenario"):
        validate_catalogue(keywords_only)


def test_build_catalogue_is_deterministic_and_rejects_duplicate_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuilt = scenarios._build_catalogue()
    assert list(rebuilt) == list(SCENARIOS)
    assert all(rebuilt[name] == SCENARIOS[name] for name in SCENARIOS)
    duplicate = SCENARIOS["k1.gate.fires"]

    def twice() -> Iterator[Scenario]:
        yield duplicate
        yield duplicate

    monkeypatch.setattr(scenarios, "_integration_scenarios", twice)
    with pytest.raises(ValueError, match=r"^duplicate scenario names: k1\.gate\.fires$"):
        scenarios._build_catalogue()


# ---------------------------------------------------------------------------
# Naming and the twin contract
# ---------------------------------------------------------------------------


def test_names_are_topic_case_variant() -> None:
    for name, scenario in SCENARIOS.items():
        assert scenario.module in MODULES, name
        assert scenario.guarantee.strip(), name
        if name == DEFAULT_SCENARIO:
            assert (scenario.variant, scenario.pair, scenario.module) == (
                "case",
                None,
                MODULE_END_TO_END,
            )
            continue
        topic, case, variant = name.split(".")
        assert re.fullmatch(r"[a-z0-9_]+", topic), name
        assert re.fullmatch(r"[a-z0-9_]+", case), name
        assert variant == scenario.variant, name
        if variant == "case":
            assert scenario.pair is None, name


def test_twins_name_each_other_in_one_module() -> None:
    twins = [s for s in SCENARIOS.values() if s.variant != "case"]
    assert len(twins) == 2 * len(PAIRS)
    for scenario in twins:
        assert scenario.pair is not None
        twin = SCENARIOS[scenario.pair]
        assert twin.pair == scenario.name
        assert {scenario.variant, twin.variant} == {"fires", "does_not_fire"}
        assert twin.module == scenario.module
        assert twin.guarantee == scenario.guarantee
        assert scenario.name.removesuffix(scenario.variant) == twin.name.removesuffix(twin.variant)


def test_pairs_yields_every_twin_once_in_name_order() -> None:
    fires = sorted(name for name, s in SCENARIOS.items() if s.variant == "fires")
    assert [first.name for first, _ in PAIRS] == fires
    for first, second in PAIRS:
        assert (first.variant, second.variant) == ("fires", "does_not_fire")
        assert (first.pair, second.pair) == (second.name, first.name)
    seen = Counter(scenario.name for pair in PAIRS for scenario in pair)
    assert set(seen) == {name for name, s in SCENARIOS.items() if s.variant != "case"}
    assert set(seen.values()) == {1}
    assert list(pairs()) == list(PAIRS)


@pytest.mark.parametrize(
    ("fires", "twin"), PAIRS, ids=[first.name.removesuffix(".fires") for first, _ in PAIRS]
)
def test_twins_move_exactly_one_input(fires: Scenario, twin: Scenario) -> None:
    moved = input_differences(fires, twin)
    assert len(moved) == 1, moved
    assert moved[0].startswith(("spec.", "context.")), moved
    assert input_differences(twin, fires) == moved
    assert input_differences(fires, fires) == ()
    # The moved input changes what the rule output must show.
    assert fires.expect != twin.expect


def test_input_differences_paths() -> None:
    fires = _scenario("k1.gate.fires")
    assert input_differences(fires, _scenario("k1.gate.does_not_fire")) == (
        "spec.google.search_terms[1].profile",
    )
    renamed = dataclasses.replace(fires, spec=fires.spec.with_(name="k1.renamed"))
    assert input_differences(fires, renamed) == ()
    overridden = _with_context(fires, threshold_overrides={"window.trusted_min_days": 21})
    assert input_differences(fires, overridden) == (
        "context.threshold_overrides['window.trusted_min_days']",
    )
    lowered = _with_context(fires, threshold_overrides={"window.trusted_min_days": 20})
    assert input_differences(overridden, lowered) == (
        "context.threshold_overrides['window.trusted_min_days']",
    )
    seven = _with_context(fires, window=(-7, -1))
    assert input_differences(fires, seven) == ("context.window",)
    fourteen = _with_context(fires, window=(-14, -1))
    assert input_differences(seven, fourteen) == ("context.window[0]",)
    decided = _with_context(
        fires, decisions=(DecisionSpec(SubjectRef("google", "campaign", "search"), 0),)
    )
    assert input_differences(fires, decided) == ("context.decisions",)
    stale = _with_context(fires, freshness={"google.entities": FreshnessOverride("complete")})
    unknown = _with_context(fires, freshness={"google.entities": FreshnessOverride("unknown")})
    assert input_differences(stale, unknown) == ("context.freshness['google.entities']",)


# ---------------------------------------------------------------------------
# Modules and rule coverage
# ---------------------------------------------------------------------------


def test_for_module_partitions_the_catalogue() -> None:
    seen: list[str] = []
    for module in MODULES:
        chosen = for_module(module)
        assert chosen, module
        names = [scenario.name for scenario in chosen]
        assert names == sorted(names)
        assert all(scenario.module == module for scenario in chosen)
        seen.extend(names)
    assert sorted(seen) == sorted(SCENARIOS)
    assert len(seen) == len(set(seen))


def test_for_module_rejects_an_unknown_module() -> None:
    with pytest.raises(
        ValueError, match=r"^unknown test module 'unit/test_x\.py'; expected one of"
    ):
        for_module("unit/test_x.py")


def test_every_required_rule_has_twins_or_its_own_cases() -> None:
    required = scenarios._REQUIRED_RULES
    assert set(required) <= set(RULE_IDS)
    assert G1 in required
    variants = {
        rule: {s.variant for s in SCENARIOS.values() if s.expect.rule_id == rule}
        for rule in required
    }
    assert all(variants.values()), variants
    lacking = {rule for rule, found in variants.items() if not {"fires", "does_not_fire"} <= found}
    assert lacking <= NO_OWN_TWIN
    # B1's two halves are C2's outcomes: Scale carries the B1 item, the veto Hold has none.
    assert _scenario("c2.outcome_scale.case").expect.values["b1"] is True
    assert _scenario("c2.outcome_hold.case").expect.values["b1"] is False
    assert any(s.expect.rule_id == B1 and s.expect.fires for s in SCENARIOS.values())
    # CR3 always proposes a test; its guarantees are the variable per stage and the sample.
    cr3 = [s for s in SCENARIOS.values() if s.expect.rule_id == CR3]
    assert {s.expect.values["variable"] for s in cr3 if s.expect.fires} == {
        "headline",
        "cta_label",
        "form_questions",
    }
    assert {s.expect.values["sample_needed"] for s in cr3 if not s.expect.fires} == {
        "not estimable"
    }


# ---------------------------------------------------------------------------
# Every scenario's context
# ---------------------------------------------------------------------------


def _check_window(scenario: Scenario, ctx: AnalysisContext) -> None:
    window = ctx.window
    spec = scenario.context
    if spec.custom is not None:
        assert (window.start, window.end) == spec.custom
    elif spec.window is not None:
        assert window.start == FIXED_ANCHOR + timedelta(days=spec.window[0])
        assert window.end == FIXED_ANCHOR + timedelta(days=spec.window[1])
    else:
        assert window.end == window.default_end
        assert (window.end - window.start).days + 1 == ctx.thresholds.default_days
        if scenario.fixed_time:
            assert (window.start, window.end) == (date(2026, 9, 4), date(2026, 10, 1))


def _check_datasets(scenario: Scenario, ctx: AnalysisContext) -> None:
    present = {source for source in SOURCES if getattr(scenario.spec, source) is not None}
    assert set(ctx.datasets) == present
    for source, ref in ctx.datasets.items():
        assert (ref.source, ref.mode, ref.role) == (
            source,
            scenario.context.mode(source),
            "primary",
        )
    now = scenario.datasets_now()
    linkedin_simulated = "linkedin" in present and scenario.context.mode("linkedin") == "simulated"
    companion = DatasetRef(4, "hubspot", "simulated", "companion")
    if scenario.context.provenance_replaced:
        replaced = ctx.datasets["linkedin"].dataset_id + REPLACED_DATASET_OFFSET
        assert now["linkedin"] == DatasetRef(replaced, "linkedin", "live")
        assert "companion" not in now
    else:
        assert {key: ref for key, ref in now.items() if key != "companion"} == dict(ctx.datasets)
        if linkedin_simulated and "hubspot" in present:
            assert now["companion"] == companion
        else:
            assert "companion" not in now
    if {"linkedin", "hubspot"} <= present:
        assert ctx.linkedin_crm is not None
        expected = companion if linkedin_simulated else ctx.datasets["hubspot"]
        assert ctx.linkedin_crm.dataset == expected
    if "hubspot" in present:
        assert ctx.google_crm is not None
        assert ctx.google_crm.dataset == ctx.datasets["hubspot"]


def _check_freshness(scenario: Scenario, ctx: AnalysisContext) -> None:
    for (dataset_id, name), row in ctx.freshness.items():
        assert (row.dataset_id, row.dataset) == (dataset_id, name)
    for source, ref in ctx.datasets.items():
        names = {name for dataset_id, name in ctx.freshness if dataset_id == ref.dataset_id}
        assert names == {name for name in DATASETS if name.startswith(f"{source}.")}
    for name, override in scenario.context.freshness.items():
        rows = [row for (_, dataset), row in ctx.freshness.items() if dataset == name]
        if name.split(".", 1)[0] in ctx.datasets:
            assert rows, name
        for row in rows:
            assert (row.completeness, row.reason) == (override.completeness, override.reason)


def _check_load_context_order(ctx: AnalysisContext) -> None:
    """The context lists its collections in ``load_context``'s order (code-point text keys)."""
    # Ad group, matched criterion present first, then criterion and term.
    terms = [
        (
            row.ad_group_id,
            row.matched_criterion_id is None,
            row.matched_criterion_id or "",
            row.search_term,
        )
        for row in ctx.search_terms
    ]
    assert terms == sorted(terms)
    segments = [(row.campaign_id, row.facet, row.value) for row in ctx.segments]
    assert segments == sorted(segments)
    for crm in (ctx.linkedin_crm, ctx.google_crm):
        if crm is None:
            continue
        for mapping in (crm.contacts, crm.meetings, crm.deals, crm.associations, crm.fetches):
            assert list(mapping) == sorted(mapping)
        assert all(list(ids) == sorted(ids) for ids in crm.associations.values())
        created = [(crm.deals[hs_id].created_at, hs_id) for hs_id in crm.window_deal_ids]
        assert created == sorted(created)
        touched = [(contact.google_touch_at, contact.hs_id) for contact in crm.google_contacts]
        assert touched == sorted(touched)


@pytest.mark.parametrize("name", BUILDABLE)
def test_every_scenario_builds_its_context(name: str) -> None:
    scenario = SCENARIOS[name]
    before = datetime.now(UTC)
    ctx = scenario.build_context()
    after = datetime.now(UTC)
    assert isinstance(ctx, AnalysisContext)
    if scenario.fixed_time:
        assert scenario.spec.anchor == FIXED_ANCHOR
        assert ctx.now == FIXED_NOW
    else:
        assert name == DEFAULT_SCENARIO
        assert before <= ctx.now <= after
    assert ctx.run_kind == scenario.context.kind
    assert ctx.thresholds.config_hash == scenario.thresholds().config_hash
    for key, value in scenario.context.threshold_overrides.items():
        assert ctx.thresholds.value(key) == value
    _check_window(scenario, ctx)
    _check_datasets(scenario, ctx)
    _check_freshness(scenario, ctx)
    _check_load_context_order(ctx)
    assert all(decision.decided_at <= ctx.now for decision in ctx.decisions.values())
    assert all(item.active_at(ctx.now) for item in ctx.feedback)
    detected = [(item.detected_at, item.id) for item in ctx.discrepancies]
    assert detected == sorted(detected)


@pytest.mark.parametrize("name", ["k1.gate.fires", "c0.status.fires", "veto.cohort.case"])
def test_contexts_are_reproducible(name: str) -> None:
    scenario = SCENARIOS[name]
    assert scenario.build_context() == scenario.build_context()


def test_default_scenario_runs_on_the_given_instant_and_seed() -> None:
    scenario = _scenario(DEFAULT_SCENARIO)
    assert not scenario.fixed_time
    ctx = scenario.build_context(now=FIXED_NOW)
    assert ctx.now == FIXED_NOW
    assert (ctx.window.start, ctx.window.end) == (date(2026, 9, 4), date(2026, 10, 1))
    assert scenario.build_context(seed=7, now=FIXED_NOW) != ctx


def test_build_context_normalises_the_instant_to_utc() -> None:
    eastern = FIXED_NOW.astimezone(ZoneInfo("America/New_York"))
    ctx = _scenario("k1.gate.fires").build_context(now=eastern)
    assert ctx.now == FIXED_NOW
    assert ctx.now.tzinfo is UTC


def test_override_conflict_case_fails_config_load() -> None:
    scenario = _scenario(CONFLICT)
    with pytest.raises(ConfigError, match="is listed under two assets") as raised:
        scenario.build_context()
    assert raised.value.file == CONTENT_ASSETS_FILE
    with pytest.raises(ConfigError, match="is listed under two assets"):
        scenario.thresholds()


def test_context_from_world_needs_an_aware_instant() -> None:
    scenario = _scenario("k1.gate.fires")
    with pytest.raises(ValueError, match="context_from_world needs an aware instant"):
        context_from_world(
            scenario.world(),
            scenario.context,
            scenario.thresholds(),
            now=FIXED_NOW.replace(tzinfo=None),
        )


def test_context_from_world_reads_the_world_clock_by_default() -> None:
    scenario = _scenario("k1.gate.fires")
    ctx = context_from_world(scenario.world(), scenario.context, scenario.thresholds())
    assert ctx.now == FIXED_NOW
    assert ctx == scenario.build_context()


def test_context_from_world_reports_an_entity_the_world_lacks() -> None:
    scenario = _scenario("k1.gate.fires")
    spec = dataclasses.replace(
        scenario.context,
        decisions=(DecisionSpec(SubjectRef("google", "campaign", "nowhere"), 0, status="paused"),),
    )
    with pytest.raises(ValueError, match=r"^the world has no google campaign 'nowhere'$"):
        context_from_world(scenario.world(), spec, scenario.thresholds(), now=FIXED_NOW)


# ---------------------------------------------------------------------------
# Scenario accessors: world, thresholds, ids, datasets_now, read_time_discrepancies
# ---------------------------------------------------------------------------


def test_world_runs_on_the_fixed_clock_with_the_scenario_registry() -> None:
    scenario = _scenario("k1.gate.fires")
    world = scenario.world()
    assert (world.seed, world.now(), world.anchor) == (DEFAULT_SEED, FIXED_NOW, FIXED_ANCHOR)
    assert world.spec is scenario.spec
    assert world.content_assets == scenario.context.content_assets == ()
    later = FIXED_NOW + timedelta(days=1)
    other = scenario.world(7, clock=lambda: later)
    assert (other.seed, other.now()) == (7, later)
    papers = _scenario("wp.costs.fires")
    assert papers.context.content_assets
    assert papers.world().content_assets == papers.context.content_assets


def test_default_world_uses_the_real_clock_and_the_shipped_registry() -> None:
    scenario = _scenario(DEFAULT_SCENARIO)
    before = datetime.now(UTC)
    world = scenario.world()
    assert before <= world.now() <= datetime.now(UTC)
    assert scenario.context.content_assets is None
    assert world.content_assets == Thresholds.load().content_assets


def test_scenario_thresholds_apply_overrides_and_registry() -> None:
    shipped = Thresholds.load()
    assert _scenario(DEFAULT_SCENARIO).thresholds().config_hash == shipped.config_hash
    configured = _scenario("p5.configured_min.case").thresholds()
    assert configured.trusted_min_days == 21 != shipped.trusted_min_days
    assert configured.config_hash != shipped.config_hash
    empty = _scenario("k1.gate.fires").thresholds()
    assert shipped.content_assets
    assert empty.content_assets == ()
    assert empty.config_hash != shipped.config_hash


_INDEX_TABLES: Mapping[str, tuple[str, str, str | None]] = MappingProxyType(
    {
        "account": ("accounts", "external_id", "name"),
        # The budget table stores no name, so a context's budget names are placeholders; the
        # index carries the platform's name (checked against the world below).
        "budget": ("budgets", "external_id", None),
        "campaign": ("campaigns", "external_id", "name"),
        "ad_group": ("ad_groups", "external_id", "name"),
        "keyword": ("keywords", "criterion_id", "text"),
        "conversion_action": ("conversion_actions", "external_id", "name"),
        "campaign_group": ("campaign_groups", "external_id", "name"),
        "creative": ("creatives", "urn", "headline"),
    }
)


def _spec_entities(spec: WorldSpec) -> Iterator[tuple[str, str, str]]:
    google = spec.google
    if google is not None:
        yield ("google", "account", google.customer_id)
        yield from (("google", "budget", item.key) for item in google.budgets)
        yield from (("google", "campaign", item.key) for item in google.campaigns)
        yield from (("google", "ad_group", item.key) for item in google.ad_groups)
        yield from (("google", "keyword", item.key) for item in google.keywords)
        yield from (("google", "conversion_action", item.key) for item in google.conversion_actions)
    linkedin = spec.linkedin
    if linkedin is not None:
        yield from (("linkedin", "account", str(item.id)) for item in linkedin.accounts)
        yield from (("linkedin", "campaign_group", item.key) for item in linkedin.campaign_groups)
        yield from (("linkedin", "campaign", item.key) for item in linkedin.campaigns)
        yield from (("linkedin", "creative", item.key) for item in linkedin.creatives)
        yield from (("linkedin", "lead_form", str(item.id)) for item in linkedin.lead_forms)


@pytest.mark.parametrize(
    "name",
    ["k1.gate.fires", "c0.member_moved.case", "wp.credit_latest_touch.case", "cr3.click.case"],
)
def test_entity_index_matches_the_rows_of_the_context(name: str) -> None:
    scenario = SCENARIOS[name]
    ids = scenario.ids
    ctx = scenario.build_context()
    entities = list(_spec_entities(scenario.spec))
    assert entities
    for source, entity, key in entities:
        assert ids.has(source, entity, key)
        external = ids.external_id(source, entity, key)
        assert ids.entity_key(source, entity, key) == f"{source}:{entity}:{external}"
        if entity == "lead_form":
            assert external == f"{FORM_URN_PREFIX}{key}"
            continue
        table, external_field, name_field = _INDEX_TABLES[entity]
        row = getattr(ctx, table)[ids.id(source, entity, key)]
        assert getattr(row, external_field) == external, (source, entity, key)
        if name_field is not None:
            assert getattr(row, name_field) == ids.name(source, entity, key)
    budgets = scenario.world().google_budgets(FIXED_NOW) if scenario.spec.google else ()
    for record in budgets:
        assert ids.name("google", "budget", record.key) == record.name
        assert ids.external_id("google", "budget", record.key) == str(record.id)


def test_entity_index_numbers_google_campaigns_before_linkedin_ones() -> None:
    spec = _scenario(DEFAULT_SCENARIO).spec
    assert spec.google is not None
    assert spec.linkedin is not None
    ids = EntityIndex.of(spec)
    first_google = spec.google.campaigns[0].key
    first_linkedin = spec.linkedin.campaigns[0].key
    assert ids.id("google", "campaign", first_google) == 1
    assert ids.external_id("google", "campaign", first_google) == str(GOOGLE_CAMPAIGN_IDS.at(0))
    assert ids.id("linkedin", "campaign", first_linkedin) == len(spec.google.campaigns) + 1
    assert ids.external_id("linkedin", "campaign", first_linkedin) == str(
        LINKEDIN_CAMPAIGN_IDS.at(0)
    )
    assert ids.id("google", "account", spec.google.customer_id) == 1
    assert ids.id("linkedin", "account", str(spec.linkedin.accounts[0].id)) == 2
    reference = SubjectRef("google", "campaign", first_google)
    assert ids.subject(reference) == ("campaign", 1)
    budget = spec.google.budgets[0]
    assert ids.subject(SubjectRef("google", "budget", budget.key)) == ("budget", 1)
    explicit = [creative for creative in spec.linkedin.creatives if creative.id is not None]
    assert explicit
    assert ids.external_id("linkedin", "creative", explicit[0].key).endswith(str(explicit[0].id))


@pytest.mark.parametrize("method", ["id", "external_id", "name"])
def test_entity_index_rejects_an_unknown_entity(method: str) -> None:
    ids = _scenario("k1.gate.fires").ids
    assert not ids.has("google", "campaign", "nowhere")
    lookup = cast(Callable[[str, str, str], object], getattr(ids, method))
    with pytest.raises(KeyError, match="the world has no google campaign 'nowhere'"):
        lookup("google", "campaign", "nowhere")
    with pytest.raises(KeyError, match="the world has no linkedin campaign 'search'"):
        ids.subject(SubjectRef("linkedin", "campaign", "search"))


def test_datasets_now_adds_the_companion_while_linkedin_is_simulated() -> None:
    simulated = _scenario("c1b.eligible.fires").datasets_now()
    assert isinstance(simulated, MappingProxyType)
    assert simulated["companion"] == DatasetRef(4, "hubspot", "simulated", "companion")
    assert simulated["linkedin"] == DatasetRef(2, "linkedin", "simulated")
    live = _scenario("fallback.isolation.case")
    assert live.context.mode("linkedin") == "live"
    assert "companion" not in live.datasets_now()
    assert live.datasets_now()["linkedin"] == DatasetRef(2, "linkedin", "live")
    assert set(_scenario("k1.gate.fires").datasets_now()) == {"google"}


def test_datasets_now_after_a_live_linkedin_replacement() -> None:
    scenario = _scenario("historical.activation.case")
    assert scenario.context.provenance_replaced
    now = scenario.datasets_now()
    assert now["linkedin"] == DatasetRef(2 + REPLACED_DATASET_OFFSET, "linkedin", "live")
    assert "companion" not in now
    ctx = scenario.build_context()
    assert ctx.datasets["linkedin"] == DatasetRef(2, "linkedin", "simulated")
    assert ctx.linkedin_crm is not None
    assert ctx.linkedin_crm.dataset.role == "companion"


def test_read_time_discrepancies_resolve_post_run_specs() -> None:
    scenario = _scenario("p1.post_run_discrepancy.case")
    specs = scenario.context.post_run_discrepancies
    found = scenario.read_time_discrepancies()
    assert len(found) == len(specs) >= 1
    for position, (info, item) in enumerate(zip(found, specs, strict=True)):
        ref = item.subject
        assert info.id == 1_000 + position
        assert (info.subject_type, info.subject_id) == scenario.ids.subject(ref)
        assert info.subject_name == scenario.ids.name(ref.source, ref.entity, ref.key)
        assert (info.source, info.kind, info.state) == (ref.source, item.kind, item.state)
        assert info.detected_at == MIDNIGHT + timedelta(hours=item.detected_at_offset_hours)
        assert info.decided_value == item.decided_value
        assert info.platform_value == item.platform_value
    stored = scenario.build_context().discrepancies
    assert not {(d.key, d.kind) for d in stored} & {(d.key, d.kind) for d in found}
    assert _scenario("k1.gate.fires").read_time_discrepancies() == ()


def test_read_time_discrepancies_carry_each_subject_shape() -> None:
    # "pool" is an explicitly shared budget with members m1 and m2; "solo" has its own budget.
    base = _scenario("shared.poor_member.fires")
    subjects = (
        (SubjectRef("google", "budget", "pool"), "budget", "budget"),
        (SubjectRef("google", "campaign", "m1"), "status", "shared_member"),
        (SubjectRef("google", "campaign", "solo"), "status", "own_budget"),
    )
    scenario = _with_context(
        base,
        post_run_discrepancies=tuple(
            DiscrepancySpec(ref, cast(Any, kind), 6) for ref, kind, _ in subjects
        ),
    )
    found = scenario.read_time_discrepancies()
    assert [info.shape for info in found] == [shape for _, _, shape in subjects]
    # Budgets carry no stored name; campaigns carry theirs.
    assert [info.subject_name for info in found] == [
        "",
        base.ids.name("google", "campaign", "m1"),
        base.ids.name("google", "campaign", "solo"),
    ]
    linkedin = _with_context(
        _scenario("c1b.eligible.fires"),
        post_run_discrepancies=(
            DiscrepancySpec(
                SubjectRef("linkedin", "campaign", _first_linkedin_campaign("c1b.eligible.fires")),
                "status",
                6,
            ),
        ),
    )
    assert [info.shape for info in linkedin.read_time_discrepancies()] == ["own_budget"]


def _first_linkedin_campaign(name: str) -> str:
    linkedin = SCENARIOS[name].spec.linkedin
    assert linkedin is not None
    return linkedin.campaigns[0].key


def test_read_time_discrepancies_of_the_default_world_use_today() -> None:
    base = _scenario(DEFAULT_SCENARIO)
    assert base.spec.google is not None
    key = base.spec.google.campaigns[0].key
    scenario = _with_context(
        base,
        post_run_discrepancies=(
            DiscrepancySpec(SubjectRef("google", "campaign", key), "status", 6),
        ),
    )
    before = datetime.now(UTC).date()
    (info,) = scenario.read_time_discrepancies()
    after = datetime.now(UTC).date()
    days = {datetime.combine(day, time(6), tzinfo=UTC) for day in (before, after)}
    assert info.detected_at in days
    assert info.subject_name == base.ids.name("google", "campaign", key)


# ---------------------------------------------------------------------------
# Declarative specs: validation and freezing
# ---------------------------------------------------------------------------

_REF = SubjectRef("google", "campaign", "search")


@pytest.mark.parametrize(
    ("build", "message"),
    [
        pytest.param(
            lambda: SubjectRef(cast(Any, "bing"), "campaign", "x"),
            "SubjectRef.source must be google or linkedin, found 'bing'",
            id="subject-source",
        ),
        pytest.param(
            lambda: SubjectRef("google", cast(Any, "keyword"), "x"),
            "SubjectRef.entity must be campaign or budget, found 'keyword'",
            id="subject-entity",
        ),
        pytest.param(
            lambda: SubjectRef("linkedin", "budget", "x"),
            "budget subjects are Google budgets",
            id="subject-linkedin-budget",
        ),
        pytest.param(
            lambda: SubjectRef("google", "campaign", "  "),
            "SubjectRef.key must be a non-blank spec key",
            id="subject-blank-key",
        ),
        pytest.param(
            lambda: SubjectRef("google", "campaign", cast(Any, 7)),
            "SubjectRef.key must be a non-blank spec key",
            id="subject-non-text-key",
        ),
        pytest.param(
            lambda: DecisionSpec(_REF, 0, status="PAUSED"),
            "DecisionSpec.status must be normalised, found 'PAUSED'",
            id="decision-status",
        ),
        pytest.param(
            lambda: DecisionSpec(_REF, 0, origin=cast(Any, "system")),
            "DecisionSpec.origin cannot be 'system'",
            id="decision-origin",
        ),
        pytest.param(
            lambda: DiscrepancySpec(_REF, cast(Any, "price"), 0),
            "DiscrepancySpec.kind cannot be 'price'",
            id="discrepancy-kind",
        ),
        pytest.param(
            lambda: DiscrepancySpec(_REF, "status", 0, state=cast(Any, "resolved")),
            "DiscrepancySpec.state cannot be 'resolved'",
            id="discrepancy-state",
        ),
        pytest.param(
            lambda: FeedbackSpec("K9", "dismiss", "google", "campaign", "search"),
            "FeedbackSpec.rule_id cannot be 'K9'",
            id="feedback-rule",
        ),
        pytest.param(
            lambda: FeedbackSpec(K1, cast(Any, "hide"), "google", "campaign", "search"),
            "FeedbackSpec.kind cannot be 'hide'",
            id="feedback-kind",
        ),
        pytest.param(
            lambda: CrmPatch(ql_after_touch_hours=24, clear_ql=True),
            "a CrmPatch cannot both set and clear QL",
            id="crm-patch",
        ),
        pytest.param(
            lambda: ContextSpec(window=(-7, -1), custom=(date(2026, 9, 1), date(2026, 9, 7))),
            "ContextSpec takes window offsets or custom dates, not both",
            id="context-window-and-custom",
        ),
        pytest.param(
            lambda: ContextSpec(window=(-1, -7)),
            "ContextSpec.window must be (start, end) offsets with end >= start",
            id="context-window-inverted",
        ),
        pytest.param(
            lambda: ContextSpec(window=cast(Any, (-7, -1, 0))),
            "ContextSpec.window must be (start, end) offsets with end >= start",
            id="context-window-length",
        ),
        pytest.param(
            lambda: ContextSpec(custom=(date(2026, 9, 7), date(2026, 9, 1))),
            "ContextSpec.custom must be (start, end) dates with end >= start",
            id="context-custom-inverted",
        ),
        pytest.param(
            lambda: ContextSpec(kind=cast(Any, "weekly")),
            "ContextSpec.kind cannot be 'weekly'",
            id="context-kind",
        ),
        pytest.param(
            lambda: ContextSpec(observation_cadence_hours=0),
            "ContextSpec.observation_cadence_hours must be positive",
            id="context-cadence",
        ),
        pytest.param(
            lambda: ContextSpec(observation_gaps=((-57, -64),)),
            "observation gap (-57, -64) must end after it starts",
            id="context-gap",
        ),
        pytest.param(
            lambda: ContextSpec(modes=cast(Any, {"bing": "live"})),
            "ContextSpec.modes cannot map 'bing' to 'live'",
            id="context-mode-source",
        ),
        pytest.param(
            lambda: ContextSpec(modes=cast(Any, {"google": "mock"})),
            "ContextSpec.modes cannot map 'google' to 'mock'",
            id="context-mode-value",
        ),
        pytest.param(
            lambda: ContextSpec(freshness={"google.keywords": FreshnessOverride("complete")}),
            "ContextSpec.freshness names unknown dataset 'google.keywords'",
            id="context-freshness-dataset",
        ),
        pytest.param(
            lambda: ContextSpec(threshold_overrides={"trusted_min_days": 21}),
            "threshold override 'trusted_min_days' must be a dotted key",
            id="context-override-undotted",
        ),
        pytest.param(
            lambda: ContextSpec(threshold_overrides=cast(Any, {21: 21})),
            "threshold override 21 must be a dotted key",
            id="context-override-non-text",
        ),
        pytest.param(
            lambda: Expectation("K9", True),
            "Expectation.rule_id cannot be 'K9'",
            id="expectation-rule",
        ),
        pytest.param(
            lambda: Expectation(K1, True, finding="no_such_finding"),
            "Expectation.finding cannot be 'no_such_finding'",
            id="expectation-finding",
        ),
        pytest.param(
            lambda: Expectation(K1, True, flags=("no_such_flag",)),
            "Expectation flag 'no_such_flag' is not a flag code",
            id="expectation-flag",
        ),
    ],
)
def test_specs_reject_invalid_input(build: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(message)}"):
        build()


def test_specs_accept_their_boundaries() -> None:
    assert ContextSpec(window=(-1, -1)).window == (-1, -1)
    assert ContextSpec(custom=(date(2026, 9, 1), date(2026, 9, 1))).custom is not None
    assert ContextSpec(observation_cadence_hours=None).observation_cadence_hours is None
    assert ContextSpec().observation_cadence_hours == 3
    assert ContextSpec(observation_gaps=((-64, -57),)).observation_gaps == ((-64, -57),)
    assert SubjectRef("google", "budget", "pool").entity == "budget"
    assert DecisionSpec(_REF, 0, status="ended", origin="resolution").status == "ended"
    assert Expectation(None, True).rule_id is None


def test_specs_freeze_their_mappings() -> None:
    overrides: dict[str, object] = {"window.trusted_min_days": 21}
    assets = _scenario("wp.costs.fires").context.content_assets
    assert assets
    spec = ContextSpec(
        threshold_overrides=overrides,
        modes={"google": "live"},
        freshness={"google.entities": FreshnessOverride("incomplete", reason="cut short")},
        content_assets=cast(Any, list(assets)),
    )
    overrides["window.trusted_min_days"] = 7
    assert spec.threshold_overrides == {"window.trusted_min_days": 21}
    for mapping in (spec.threshold_overrides, spec.modes, spec.freshness):
        assert isinstance(mapping, MappingProxyType)
        with pytest.raises(TypeError):
            cast(Any, mapping)["window.max_days"] = 1
    assert spec.content_assets == assets
    assert isinstance(spec.content_assets, tuple)
    assert (spec.mode("google"), spec.mode("linkedin"), spec.mode("hubspot")) == (
        "live",
        "simulated",
        "simulated",
    )
    expect = Expectation(K1, True, {"n_star": 61}, flags=cast(Any, [FLAG_STALE]))
    assert isinstance(expect.values, MappingProxyType)
    assert expect.flags == (FLAG_STALE,)
    stored = DiscrepancySpec(_REF, "status", 0, decided_value={"status": "active"})
    assert isinstance(stored.decided_value, MappingProxyType)
    assert stored.decided_value == {"status": "active"}
    assert stored.platform_value is None


def test_crm_patch_selects_leads_by_creative_and_submission_day() -> None:
    patch = CrmPatch(creative_key="cr-a", submitted_from_offset=-20, submitted_to_offset=-10)
    assert patch.selects("cr-a", -20)
    assert patch.selects("cr-a", -15)
    assert patch.selects("cr-a", -10)
    assert not patch.selects("cr-b", -15)
    assert not patch.selects("cr-a", -21)
    assert not patch.selects("cr-a", -9)
    every = CrmPatch(clear_ql=True)
    assert every.selects("any", -400)
    assert every.selects("other", 0)


# ---------------------------------------------------------------------------
# Configuration with overrides
# ---------------------------------------------------------------------------


def test_thresholds_with_nothing_is_the_shipped_configuration() -> None:
    first = thresholds_with({})
    assert first.config_hash == Thresholds.load().config_hash
    assert thresholds_with({}) is first


def test_thresholds_with_replaces_existing_dotted_keys() -> None:
    shipped = Thresholds.load()
    changed = thresholds_with(
        {
            "window.default_days": Decimal("21"),
            "window.presets": (14, 21, 28, 56, 90),
            "window.trusted_min_days": 21,
            "budget.capped_day_ratio": Decimal("0.90"),
        }
    )
    assert (changed.default_days, changed.trusted_min_days) == (21, 21)
    assert changed.presets == (14, 21, 28, 56, 90)
    assert changed.decimal("budget.capped_day_ratio") == Decimal("0.90")
    assert changed.config_hash != shipped.config_hash
    # Memoised on the YAML value each override stands for.
    same = thresholds_with(
        {
            "budget.capped_day_ratio": 0.9,
            "window.trusted_min_days": 21,
            "window.presets": [14, 21, 28, 56, 90],
            "window.default_days": 21,
        }
    )
    assert same is changed


def test_thresholds_with_overrides_the_merged_crm_lists() -> None:
    changed = thresholds_with({"crm.booking_sources": ("MEETINGS_PUBLIC", "CRM_UI")})
    assert changed.strings("crm.booking_sources") == ("MEETINGS_PUBLIC", "CRM_UI")
    assert changed.mapping.booking_sources == ("MEETINGS_PUBLIC", "CRM_UI")
    assert changed.config_hash != Thresholds.load().config_hash


@pytest.mark.parametrize(
    "key",
    [
        "window.nonexistent",
        "nowhere.deeper.key",
        "window.default_days.extra",
        "window.default_days.extra.more",
    ],
)
def test_thresholds_with_rejects_a_missing_key(key: str) -> None:
    with pytest.raises(ConfigError, match="threshold override names a missing key") as raised:
        thresholds_with({key: 1})
    assert (raised.value.file, raised.value.key) == (THRESHOLDS_FILE, key)


def test_thresholds_with_validates_the_result() -> None:
    with pytest.raises(ConfigError, match=r"window\.presets"):
        thresholds_with({"window.default_days": 21})


def test_thresholds_with_replaces_the_registry() -> None:
    assets = _scenario("wp.costs.fires").context.content_assets
    assert assets
    loaded = thresholds_with({}, assets)
    assert loaded.content_assets == assets
    for asset in assets:
        for form in asset.lead_form_urns:
            assert loaded.asset_for_form(form) == asset
    assert thresholds_with({}, list(assets)) is loaded
    assert thresholds_with({}, ()).content_assets == ()


def test_config_documents_report_unreadable_and_non_mapping_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        ConfigError, match=r"cannot read /nonexistent-config/thresholds\.yaml"
    ) as err:
        scenarios._documents("/nonexistent-config")
    assert (err.value.file, err.value.key) == (THRESHOLDS_FILE, None)
    monkeypatch.setattr(scenarios.yaml, "safe_load", lambda handle: ["not", "a", "mapping"])
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        scenarios._documents(f"{CONFIG_DIR}/.")


# ---------------------------------------------------------------------------
# CRM bindings resolved against the simulated portal
# ---------------------------------------------------------------------------


class _HiddenPortal(SimulatedWorld):
    """A world whose portal answers some property definitions differently (failure paths)."""

    def __init__(
        self, spec: WorldSpec, answers: Mapping[str, PropertyRecord | None], **kwargs: Any
    ) -> None:
        super().__init__(DEFAULT_SEED, spec, clock=FIXED_CLOCK, content_assets=(), **kwargs)
        self._answers = dict(answers)

    def hubspot_property(self, object_type: str, name: str) -> PropertyRecord | None:
        if name in self._answers:
            return self._answers[name]
        return super().hubspot_property(object_type, name)


def _portal(name: str = "c1b.eligible.fires", **kwargs: Any) -> SimulatedWorld:
    return SimulatedWorld(
        DEFAULT_SEED, SCENARIOS[name].spec, clock=FIXED_CLOCK, content_assets=(), **kwargs
    )


def _mapped(**bindings: StageBinding) -> Thresholds:
    shipped = Thresholds.load()
    return dataclasses.replace(shipped, mapping=dataclasses.replace(shipped.mapping, **bindings))


_MQL_ID = str(HUBSPOT_STAGE_IDS.at(0))
_QL_ID = str(HUBSPOT_STAGE_IDS.at(1))
_BOOKED = "date_first_meeting_was_booked"


def test_resolve_bindings_without_a_portal_verifies_nothing() -> None:
    world = _scenario("k1.gate.fires").world()
    bindings = resolve_bindings(world, Thresholds.load(), FIXED_NOW)
    assert bindings == CrmBindings.unavailable("CRM stage bindings not verified", at=FIXED_NOW)
    assert not bindings.mql.available
    assert not bindings.ql.available


def test_resolve_bindings_binds_the_shipped_stage_labels() -> None:
    bindings = resolve_bindings(_portal(), Thresholds.load(), FIXED_NOW)
    assert bindings.verified_at == FIXED_NOW
    assert (bindings.mql.available, bindings.mql.stage_id, bindings.mql.label) == (
        True,
        _MQL_ID,
        "First meeting booked",
    )
    assert bindings.mql.property == f"hs_v2_date_entered_{_MQL_ID}"
    assert (bindings.ql.available, bindings.ql.property, bindings.ql.label) == (
        True,
        f"hs_v2_date_entered_{_QL_ID}",
        "Qualified Lead",
    )
    assert not bindings.google.available
    assert bindings.google.reason == (
        "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY and HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not configured"
    )
    ctx = _scenario("c1b.eligible.fires").build_context()
    assert ctx.linkedin_crm is not None
    assert ctx.linkedin_crm.bindings == bindings


def test_resolve_bindings_matches_labels_case_insensitively_in_order() -> None:
    thresholds = _mapped(
        mql=StageBinding("stage_label", ("FIRST MEETING BOOKED",), None),
        ql=StageBinding("stage_label", ("Sales Accepted", "qualified lead"), None),
    )
    bindings = resolve_bindings(_portal(), thresholds, FIXED_NOW)
    assert (bindings.mql.available, bindings.mql.label) == (True, "First meeting booked")
    assert (bindings.ql.available, bindings.ql.stage_id) == (True, _QL_ID)


def test_resolve_bindings_reports_unresolved_stage_labels() -> None:
    unresolved = _scenario("c2.qlstage_unresolved.case")
    ql = resolve_bindings(unresolved.world(), unresolved.thresholds(), FIXED_NOW).ql
    assert not ql.available
    assert ql.reason == (
        "stages 'Qualified Lead', 'Sales Qualified Lead' not found in lifecyclestage options"
    )
    spec = _scenario("c1b.eligible.fires").spec
    assert spec.hubspot is not None
    removed = spec.with_(hubspot=spec.hubspot.with_(removed_stage_labels=("First meeting booked",)))
    world = SimulatedWorld(DEFAULT_SEED, removed, clock=FIXED_CLOCK, content_assets=())
    mql = resolve_bindings(world, Thresholds.load(), FIXED_NOW).mql
    assert not mql.available
    assert mql.reason == "stage 'First meeting booked' not found in lifecyclestage options"


def test_resolve_bindings_reports_a_stage_without_its_date_property() -> None:
    spec = _scenario("c1b.eligible.fires").spec
    world = _HiddenPortal(spec, {f"hs_v2_date_entered_{_MQL_ID}": None})
    mql = resolve_bindings(world, Thresholds.load(), FIXED_NOW).mql
    assert (mql.available, mql.stage_id, mql.label) == (False, _MQL_ID, "First meeting booked")
    assert mql.reason == (
        f"stage 'First meeting booked' resolved to id '{_MQL_ID}', but property "
        f"'hs_v2_date_entered_{_MQL_ID}' not found"
    )


def test_resolve_bindings_in_property_mode() -> None:
    labelled = StageBinding("property", ("First meeting booked",), _BOOKED)
    thresholds = _mapped(mql=labelled, ql=StageBinding("property", (), _BOOKED))
    bound = resolve_bindings(_portal(mapping=thresholds.mapping), thresholds, FIXED_NOW)
    assert (bound.mql.available, bound.mql.property, bound.mql.label) == (
        True,
        _BOOKED,
        "First meeting booked",
    )
    assert bound.mql.stage_id is None
    assert (bound.ql.available, bound.ql.label) == (True, None)
    missing = resolve_bindings(_portal(), thresholds, FIXED_NOW).mql
    assert not missing.available
    assert missing.label == "First meeting booked"
    assert missing.reason == f"property '{_BOOKED}' not found on contacts"
    text = _mapped(mql=StageBinding("property", (), "email"))
    wrong = resolve_bindings(_portal(), text, FIXED_NOW).mql
    assert not wrong.available
    assert wrong.reason == "property 'email' has type 'string', not a date or datetime property"


def test_resolve_bindings_verifies_the_google_link() -> None:
    properties = ("google_campaign_id", "google_ads_touch_at")
    linked = resolve_bindings(
        _portal(google_crm_properties=properties), Thresholds.load(), FIXED_NOW
    )
    assert linked.google.available
    assert (linked.google.campaign_property, linked.google.touch_date_property) == properties
    spec = _scenario("c1b.eligible.fires").spec
    hidden = _HiddenPortal(spec, dict.fromkeys(properties), google_crm_properties=properties)
    broken = resolve_bindings(hidden, Thresholds.load(), FIXED_NOW).google
    assert not broken.available
    assert broken.campaign_property == "google_campaign_id"
    assert broken.reason == (
        "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY property 'google_campaign_id' not found; "
        "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY property 'google_ads_touch_at' not usable"
    )
    text_touch = PropertyRecord("contacts", "google_ads_touch_at", "Touch", "string", "text")
    typed = _HiddenPortal(
        spec, {"google_ads_touch_at": text_touch}, google_crm_properties=properties
    )
    assert resolve_bindings(typed, Thresholds.load(), FIXED_NOW).google.reason == (
        "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY property 'google_ads_touch_at' not usable"
    )


def test_context_takes_verified_bindings_from_the_spec() -> None:
    bindings = CrmBindings.unavailable("portal not verified yet", at=FIXED_NOW)
    ctx = _with_context(_scenario("c1b.eligible.fires"), crm_bindings=bindings).build_context()
    assert ctx.linkedin_crm is not None
    assert ctx.google_crm is not None
    assert ctx.linkedin_crm.bindings == bindings
    assert ctx.google_crm.bindings == bindings


# ---------------------------------------------------------------------------
# Stored rows a context spec may carry instead of specs
# ---------------------------------------------------------------------------


def test_custom_dates_build_a_custom_window() -> None:
    start, end = date(2026, 9, 10), date(2026, 9, 30)
    ctx = _with_context(
        _scenario("k1.gate.fires"), custom=(start, end), kind="custom"
    ).build_context()
    assert (ctx.window.start, ctx.window.end, ctx.run_kind) == (start, end, "custom")


def test_stored_decisions_and_discrepancies_pass_through() -> None:
    base = _scenario("c0.status.fires")
    plain = base.build_context()
    key = next(iter(plain.decisions))
    current = plain.decisions[key]
    later = dataclasses.replace(
        current, status="paused", decided_at=FIXED_NOW - timedelta(hours=1), origin="user"
    )
    future = dataclasses.replace(current, status="ended", decided_at=FIXED_NOW + timedelta(hours=1))
    stored = DiscrepancyInfo(
        id=77,
        subject_type=key[0],
        subject_id=key[1],
        subject_name="Stored",
        source=plain.campaigns[key[1]].source if key[0] == "campaign" else "google",
        shape=current.shape,
        kind="untracked",
        decided_value=None,
        platform_value={"status": "active"},
        detected_at=FIXED_NOW - timedelta(hours=2),
        state="open",
    )
    ctx = _with_context(
        base, decisions=(*base.context.decisions, later, future), discrepancies=(stored,)
    ).build_context()
    assert ctx.decisions[key] == later
    assert isinstance(ctx.decisions[key], DecisionInfo)
    assert stored in ctx.discrepancies


def test_feedback_specs_and_rows_key_entities_as_items_do() -> None:
    base = _scenario("k1.gate.fires")
    google = base.spec.google
    assert google is not None
    term = google.search_terms[0]
    assert term.matched_keyword_key is not None
    # A term no keyword matched (the search-terms report leaves its criterion empty).
    unmatched = GoogleSearchTermSpec("crm pricing", term.ad_group_key, None)
    spec = base.spec.with_(google=google.with_(search_terms=(*google.search_terms, unmatched)))
    row = FeedbackInfo(
        K1, "search_term", "google:search_term:stored", "snooze", FIXED_NOW + timedelta(days=1)
    )
    expired = FeedbackInfo(
        K1, "campaign", "google:campaign:1", "dismiss", FIXED_NOW - timedelta(days=1)
    )
    feedback: tuple[FeedbackSpec | FeedbackInfo, ...] = (
        FeedbackSpec(K1, "dismiss", "google", "campaign", "search"),
        FeedbackSpec(K1, "done", "google", "search_term", f"{term.ad_group_key}|{term.term}"),
        FeedbackSpec(K1, "done", "google", "search_term", f"{term.ad_group_key}|crm pricing"),
        FeedbackSpec(K1, "dismiss", "linkedin", "segment", "seniority:Director"),
        FeedbackSpec(K1, "snooze", "google", "campaign", "search", until_offset_hours=-1),
        row,
        expired,
    )
    scenario = _with_context(dataclasses.replace(base, spec=spec), feedback=feedback)
    ctx = scenario.build_context()
    ids = scenario.ids
    criterion = ids.external_id("google", "keyword", term.matched_keyword_key)
    group = ids.external_id("google", "ad_group", term.ad_group_key)
    assert [item.entity_key for item in ctx.feedback] == [
        ids.entity_key("google", "campaign", "search"),
        f"google:search_term:{search_term_external_id(group, criterion, term.term)}",
        f"google:search_term:{search_term_external_id(group, None, 'crm pricing')}",
        "linkedin:segment:seniority:Director",
        row.entity_key,
    ]
    assert all(item.active_at(FIXED_NOW) for item in ctx.feedback)
    assert expired not in ctx.feedback


def test_an_incomplete_entity_enumeration_infers_no_removal() -> None:
    fires = _scenario("c0.missing.fires")
    assert {item.kind for item in fires.build_context().discrepancies} == {"missing"}
    deferred = _with_context(
        fires,
        freshness={"google.entities": FreshnessOverride("incomplete", reason="page deferred")},
    )
    ctx = deferred.build_context()
    assert all(item.kind != "missing" for item in ctx.discrepancies)
    rows = [row for (_, name), row in ctx.freshness.items() if name == "google.entities"]
    assert [(row.completeness, row.reason) for row in rows] == [("incomplete", "page deferred")]


def test_no_snapshot_cadence_reads_no_snapshot_history() -> None:
    base = _scenario("k1.gate.fires")
    assert base.build_context().observations
    ctx = _with_context(base, observation_cadence_hours=None).build_context()
    assert dict(ctx.observations) == {}
    assert dict(ctx.decisions) == {}
    assert ctx.discrepancies == ()


def test_decision_patches_keep_the_fields_of_each_shape() -> None:
    base = _scenario("shared.poor_member.fires")
    ids = base.ids
    at = MIDNIGHT - timedelta(hours=24)
    decisions = (
        DecisionSpec(
            SubjectRef("google", "campaign", "m1"), -24, "paused", daily_budget=Decimal("50.00")
        ),
        DecisionSpec(
            SubjectRef("google", "budget", "pool"), -24, "paused", daily_budget=Decimal("600.00")
        ),
        DecisionSpec(SubjectRef("google", "campaign", "solo"), -24, daily_budget=Decimal("80.00")),
    )
    baseline = base.build_context().decisions
    ctx = _with_context(base, decisions=(*base.context.decisions, *decisions)).build_context()
    member = ctx.decisions[("campaign", ids.id("google", "campaign", "m1"))]
    assert (member.shape, member.status, member.daily_budget, member.total_budget) == (
        "shared_member",
        "paused",
        None,
        None,
    )
    assert member.budget_id == ids.id("google", "budget", "pool")
    assert member.decided_at == at
    pool = ctx.decisions[("budget", ids.id("google", "budget", "pool"))]
    assert (pool.shape, pool.status, pool.budget_id, pool.daily_budget) == (
        "budget",
        None,
        None,
        Decimal("600.00"),
    )
    solo_key = ("campaign", ids.id("google", "campaign", "solo"))
    solo = ctx.decisions[solo_key]
    assert (solo.shape, solo.daily_budget, solo.budget_id) == ("own_budget", Decimal("80.00"), None)
    # A budget-only patch keeps the decided status (AAP 0.5.2 patch model).
    assert solo.status == baseline[solo_key].status


def test_clearing_ql_on_the_contacts_turns_scale_into_a_veto() -> None:
    base = _scenario("c2.outcome_scale.case")
    (before,) = build_attribution(base.build_context()).veto.values()
    assert before.ql
    assert not before.veto_fires
    cleared = _with_context(base, crm_patches=(*base.context.crm_patches, CrmPatch(clear_ql=True)))
    (after,) = build_attribution(cleared.build_context()).veto.values()
    assert (after.ql, after.veto_fires) == (0, True)
    assert after.eligible_contacts == before.eligible_contacts


def test_a_date_property_binding_reads_stage_dates_at_utc_midnight() -> None:
    base = _scenario("wp.costs.fires")
    assert base.context.crm_patches == ()
    shipped = base.thresholds()
    mapping = dataclasses.replace(shipped.mapping, ql=StageBinding("property", (), _BOOKED))
    thresholds = dataclasses.replace(shipped, mapping=mapping)
    world = SimulatedWorld(
        DEFAULT_SEED,
        base.spec,
        clock=FIXED_CLOCK,
        content_assets=base.context.content_assets,
        mapping=mapping,
    )
    ctx = context_from_world(world, base.context, thresholds, now=FIXED_NOW)
    assert ctx.linkedin_crm is not None
    assert ctx.linkedin_crm.bindings.ql.property == _BOOKED
    contacts = ctx.linkedin_crm.contacts.values()
    entered = [contact.ql_at for contact in contacts if contact.ql_at is not None]
    assert len(entered) == len(contacts) == 10
    assert all(instant.tzinfo is UTC and instant.time() == time(0) for instant in entered)
    staged = base.build_context().linkedin_crm
    assert staged is not None
    assert {
        hs_id: cast(datetime, contact.ql_at).date() for hs_id, contact in staged.contacts.items()
    } == {
        hs_id: cast(datetime, contact.ql_at).date()
        for hs_id, contact in ctx.linkedin_crm.contacts.items()
    }


def test_catalogue_helpers_keep_exact_totals() -> None:
    fractional = scenarios._split_decimal(Decimal("0.3"), 7)
    assert fractional[:-1] == [Decimal("0.042857")] * 6
    assert sum(fractional) == Decimal("0.3")
    assert scenarios._split_decimal(Decimal("10"), 4) == [2, 2, 3, 3]
    assert scenarios._split_money(Decimal("100.00"), 3) == [
        Decimal("33.33"),
        Decimal("33.33"),
        Decimal("33.34"),
    ]
    day = scenarios._metrics("1.00", 10, 1)
    merged = scenarios._combine(scenarios._daily((-3, -2), day), scenarios._daily((-1,), day))
    assert sorted(merged.days) == [-3, -2, -1]
    with pytest.raises(ValueError, match=r"^profiles overlap on day offset -3$"):
        scenarios._combine(scenarios._daily((-3, -2), day), scenarios._daily((-3,), day))


# ---------------------------------------------------------------------------
# Cross-checks against the rule code that already exists
# ---------------------------------------------------------------------------

C0_SCENARIOS = tuple(name for name, s in SCENARIOS.items() if s.expect.rule_id == C0)


@pytest.mark.parametrize("name", C0_SCENARIOS)
def test_c0_expectations_hold_on_the_built_context(name: str) -> None:
    scenario = SCENARIOS[name]
    ctx = scenario.build_context()
    result = discrepancy.evaluate(ctx)
    values = scenario.expect.values
    assert bool(result.candidates) is scenario.expect.fires
    assert len(result.candidates) == len(ctx.discrepancies)
    if values.get("open") == 0:
        assert ctx.discrepancies == ()
    if "kind" in values:
        assert {item.kind for item in ctx.discrepancies} == {values["kind"]}
    if "state" in values:
        assert {item.state for item in ctx.discrepancies} == {values["state"]}
    if "subject_type" in values:
        assert values["subject_type"] in {item.subject_type for item in ctx.discrepancies}
    if "shape" in values:
        assert values["shape"] in {item.shape for item in ctx.discrepancies}
    if "source" in values:
        assert {item.source for item in ctx.discrepancies} == {values["source"]}
    for dataset in ("google.entities", "linkedin.entities"):
        if dataset in values:
            rows = [row for (_, n), row in ctx.freshness.items() if n == dataset]
            assert {row.completeness for row in rows} == {values[dataset]}


VETO_SCENARIOS = tuple(
    name
    for name, s in SCENARIOS.items()
    if (s.expect.rule_id == C2 and "outcome" in s.expect.values)
    or name.startswith(("veto.", "c1b."))
)
_VETO_COUNTS: Mapping[str, str] = MappingProxyType(
    {
        "matured_leads": "matured_leads",
        "matched_leads": "matched_leads",
        "unresolved": "unresolved_leads",
        "eligible_contacts": "eligible_contacts",
        "ql": "ql",
        "matured_ql": "ql",
        "meetings": "meetings",
    }
)


@pytest.mark.parametrize("name", VETO_SCENARIOS)
def test_pipeline_veto_expectations_hold_on_the_built_context(name: str) -> None:
    scenario = SCENARIOS[name]
    ctx = scenario.build_context()
    (population,) = build_attribution(ctx).veto.values()
    expect = scenario.expect
    for key, attribute in _VETO_COUNTS.items():
        if key in expect.values:
            assert getattr(population, attribute) == expect.values[key], key
    if "unresolved_share" in expect.values:
        wanted = Decimal(cast(str, expect.values["unresolved_share"]))
        assert population.unresolved_share is not None
        assert population.unresolved_share.quantize(wanted) == wanted
    if "unknown_evidence_share" in expect.values:
        share = Decimal(population.unknown_evidence_contacts) / population.eligible_contacts
        assert share == Decimal(cast(str, expect.values["unknown_evidence_share"]))
    unavailable = FLAG_PIPELINE_UNAVAILABLE in expect.flags or (
        expect.finding == FINDING_PIPELINE_UNAVAILABLE
    )
    if unavailable:
        assert not population.available
    if FLAG_ATTRIBUTION_FLOOR in expect.flags:
        assert population.attribution_floor
    if FLAG_UNKNOWN_CRM_EVIDENCE in expect.flags:
        assert population.unknown_evidence
    outcome = expect.values.get("outcome")
    if outcome == "Hold":
        assert population.veto_fires
    elif outcome == "Scale":
        assert not population.veto_fires
        assert population.available is not unavailable
    if expect.rule_id == C1B:
        assert expect.fires is (population.veto_fires and population.meetings == 0)


_ROW_COUNTS = ("mql", "ql", "meetings_booked", "other_meetings")
_ROW_COSTS = ("cost_per_ql", "cost_per_meeting")
WHITE_PAPER_SCENARIOS = tuple(
    name
    for name, s in SCENARIOS.items()
    if name.startswith("wp.")
    and name != CONFLICT
    and any(key in s.expect.values for key in (*_ROW_COUNTS, *_ROW_COSTS, "spend", "mql_label"))
)


@pytest.mark.parametrize("name", WHITE_PAPER_SCENARIOS)
def test_white_paper_expectations_hold_on_the_built_context(name: str) -> None:
    scenario = SCENARIOS[name]
    values = scenario.expect.values
    rows = build_attribution(scenario.build_context()).rows()
    if "asset" in values:
        rows = tuple(row for row in rows if row.slug == values["asset"])
    (row,) = rows
    for key in _ROW_COUNTS:
        if key in values:
            assert getattr(row, key).render() == str(values[key]), key
    for key in _ROW_COSTS:
        if key in values:
            assert getattr(row, key).render() == values[key], key
    if "spend" in values:
        assert row.spend == Decimal(cast(str, values["spend"]))
    if "mql_label" in values:
        assert row.mql_label == values["mql_label"]
