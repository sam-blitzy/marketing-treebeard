"""Acceptance tests for precedence and the read-time overlay (AAP 0.5.4.2, 0.3.2, 0.5.3).

AAP 0.11.2 row ``acceptance/test_precedence.py``. Each test names the guarantee it proves and
runs against the scenario catalogue (``ads_engine.simulation.scenarios.SCENARIOS``): the
guarantee holds in one case and, with a single input moved across its threshold, does not in
its twin.

* **P1 discrepancies first**: every item depending on a discrepant campaign or budget (the
  campaign itself, its budget, its keywords and creatives, a shared budget containing it, a
  white-paper row whose spend comes from it) is held with "Resolve discrepancy first"; every
  other item is flagged "Unverified while N discrepancies are open"; C0 items are never held
  and are pinned first in Campaigns, oldest first. A discrepancy detected after a run holds
  its items at read time through the overlay, with the banner count updated at once, while
  the stored run stays byte-identical.
* **P2 to P8**: absent conversion tracking suppresses the zero-outcome rules of its source;
  ``data_status`` follows the modes of the datasets an item read; ``stale`` and
  ``incomplete`` derive from each item's own required datasets; the short-window flag and
  banner use the configured trusted minimum, and sustained rules are withheld below
  ``window.sustained_min_days``; withheld patterns carry their sample sizes and never become
  items; a pair that is not distinguishable names no winner; attribution floors pass through.
* **Feedback**: dismiss, snooze and done hide a rule and entity until ``until``
  (``feedback.*_days``), C0 exempt; the counts are stored in ``run_suppression`` and served
  from storage.
* **Historical-only items**: after a LinkedIn dataset activation, items computed from the old
  dataset are historical with every action refused (409 ``subject_retired``), an older custom
  run stays historical indefinitely, and frozen provenance keeps its simulated mode while
  ``data_status_now`` reports the live source.
* **Ranking**: ties break by entity name, then key, whatever the input order.

Pure tests build the context with ``make_context``, evaluate every rule module in the runner's
order and pass the ``RuleResult`` values to ``precedence.apply_precedence``. Database tests
store the scenario's world through ``sync.mode.bootstrap_sources`` (the simulator serving every
source on an ``httpx.MockTransport``), persist runs with ``runner.run_analysis`` and read them
back through ``GET /api/v1/recommendations`` and ``analysis.overlay.overlay``. Worlds, syncs
and runs share the catalogue's fixed clock. The API reads the time only through its request
clock (``ads_engine.api.deps.get_now``): a test whose API assertions depend on the request
time (feedback ``until`` and ``hidden_by``) fixes that clock through
``api_app.dependency_overrides[get_now]`` before logging in; other read-time checks
(``data_status_now``, the short-window banner) call the overlay directly at :data:`READ_AT`;
every other API assertion is on clock-independent state (holds, historical marking, counts,
refusals). Database tests need the PostgreSQL 18 test database, which fails the test when
unreachable, never skips it. No test reaches a real network.
"""

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine import decisions
from ads_engine.analysis import (
    budget,
    campaigns,
    creative,
    discrepancy,
    keywords,
    overlay,
    precedence,
    runner,
    white_papers,
)
from ads_engine.analysis.context import (
    B1,
    B2,
    B3,
    B4,
    BANNER_INCOMPLETE_DATASETS,
    BANNER_OPEN_DISCREPANCIES,
    BANNER_SHORT_WINDOW,
    BANNER_STALE_DATASETS,
    BANNER_TRACKING_ABSENT,
    BUDGET,
    C0,
    C1,
    C1B,
    C2,
    CAMPAIGNS,
    CR1,
    CREATIVE,
    DATA_LIVE,
    DATA_MIXED,
    DATA_SIMULATED,
    DATA_STALE,
    ENTITY_BUDGET,
    ENTITY_CAMPAIGN,
    ENTITY_CONTENT_ASSET,
    ENTITY_CREATIVE_PAIR,
    ENTITY_KEYWORD,
    ENTITY_SEARCH_TERM,
    ENTITY_SEGMENT,
    ENTITY_TERM_CLUSTER,
    FEEDBACK_SUPPRESSION,
    FINDING_CONVERSION_TRACKING_ABSENT,
    FINDING_INCOMPLETE_DATASET,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_CREATIVE_FLOOR,
    FLAG_DEAL_FLOOR,
    FLAG_INCOMPLETE,
    FLAG_NOT_DISTINGUISHABLE,
    FLAG_SHORT_WINDOW,
    FLAG_SIMULATED,
    FLAG_STALE,
    FLAG_UNVERIFIED_DISCREPANCIES,
    GROUP_CHANGES,
    GROUP_PAUSE,
    HELD_DISCREPANCY_REASON,
    HISTORICAL_REASON,
    K1,
    K3,
    KEYWORDS,
    SECTION_ORDER,
    SUPPRESS_TRACKING_ABSENT,
    W2,
    WHITE_PAPERS,
    AnalysisContext,
    Candidate,
    DayFact,
    DecisionShape,
    DiscrepancyInfo,
    EntityRef,
    FeedbackInfo,
    FeedbackKind,
    OpenDiscrepancyState,
    RuleResult,
    Source,
    SubjectKey,
    Withheld,
    cohort_group,
    unverified_message,
)
from ads_engine.analysis.precedence import TRACKING_ABSENT_MESSAGE, PrecedenceOutput
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.api.deps import get_now
from ads_engine.api.security import CSRF_HEADER, CSRF_VALUE
from ads_engine.db.models.analysis import (
    AnalysisRun,
    Recommendation,
    RecommendationFeedback,
    RunSuppression,
)
from ads_engine.db.models.decisions import Discrepancy
from ads_engine.db.models.ops import AppUser, SourceDataset
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.scenarios import (
    FIXED_ANCHOR,
    FIXED_CLOCK,
    FIXED_NOW,
    MODULE_PRECEDENCE,
    SCENARIOS,
    FreshnessOverride,
    Scenario,
)
from ads_engine.sync import freshness
from ads_engine.sync.mode import SourceModeController, bootstrap_sources

pytestmark = pytest.mark.anyio

type ContextFactory = Callable[..., AnalysisContext]
"""The ``make_context`` fixture: ``make_context(scenario, *, thresholds=None, now=None)``."""
type WorldFactory = Callable[..., SimulatedWorld]
"""The ``make_world`` fixture: ``make_world(scenario)`` on the scenario's fixed clock."""
type ThresholdsFactory = Callable[..., Thresholds]
"""The ``make_thresholds`` fixture: an overridden copy of ``config/`` loaded."""
type EnvSetter = Callable[..., None]
"""The ``set_env`` fixture."""
type CredentialSetter = Callable[[Source], Mapping[str, str]]
"""The ``live_credentials`` fixture: fake credentials making one source live."""
type TransportFactory = Callable[..., httpx.MockTransport]
"""The ``mock_transport_for`` fixture: the simulator acting as one live platform."""
type LoginFunction = Callable[..., Awaitable[httpx.Response]]
"""The ``login`` fixture: ``login(client)`` signs the test admin in on the app's clock."""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RULE_MODULES = (discrepancy, keywords, campaigns, budget, creative, white_papers)
"""The rule modules in the runner's fixed evaluation order (``runner.EVALUATION_ORDER``)."""

READ_AT = FIXED_NOW + timedelta(hours=1)
"""The read instant of overlay checks: one hour after every scenario's run and sync instant."""

ANCHOR_MIDNIGHT = datetime.combine(FIXED_ANCHOR, time(0), tzinfo=UTC)
"""UTC midnight of the catalogue anchor, from which scenario hour offsets are counted."""

SUSTAINED_RULES = (K3, C1, C1B, C2, B1, B2, B3, B4)
"""The sustained-pattern rules gated by ``window.sustained_min_days`` (AAP 0.5.4.1)."""

SUSTAINED_ONLY_RULES = (C2, B1, B2, B3, B4)
"""Sustained rules whose own window minimum is the sustained minimum itself."""

FLOOR_FLAGS = (FLAG_ATTRIBUTION_FLOOR, FLAG_CREATIVE_FLOOR, FLAG_DEAL_FLOOR)
"""The attribution floor flags the white-paper rules set (P8)."""

WINNER_WORDS = frozenset(
    {"winner", "winners", "winning", "wins", "won", "better", "best", "beat", "beats"}
)
"""Words that call one creative variant better than another (P7), with ``outperform*``."""

FEEDBACK_DAYS: dict[FeedbackKind, int] = {"dismiss": 30, "snooze": 7, "done": 30}
"""AAP 0.5.2 ``recommendation_feedback`` durations; ``feedback.*_days`` must configure them."""

ALLOCATION_SCENARIO = "b3.allocation.case"
"""Two B3 donors free 300 and 200 against one receiver's B1 increase of 400 (AAP 0.11.2)."""

STALE_AFTER_KEY = "sources.stale_after_hours"
"""The threshold P4 judges each required dataset's age against (AAP 0.5.4.2 P4)."""

TRUSTED_MIN_DAYS_KEY = "window.trusted_min_days"
"""The threshold P5 judges the window's length against (AAP 0.5.4.1, 0.5.4.2 P5)."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _evaluate(ctx: AnalysisContext) -> list[RuleResult]:
    """Every rule module's result on ``ctx``, in the runner's order."""
    return [module.evaluate(ctx) for module in RULE_MODULES]


def _precede(ctx: AnalysisContext, extra: Iterable[RuleResult] = ()) -> PrecedenceOutput:
    """Evaluate every rule on ``ctx`` and apply precedence to the results plus ``extra``."""
    return precedence.apply_precedence([*_evaluate(ctx), *extra], ctx)


def _candidates(
    out: PrecedenceOutput, section: str | None = None, rule_id: str | None = None
) -> list[Candidate]:
    """The ranked candidates of one section (every section when ``None``), of one rule."""
    sections = SECTION_ORDER if section is None else (section,)
    return [
        item.candidate
        for name in sections
        for item in out.items[name]
        if rule_id is None or item.candidate.rule_id == rule_id
    ]


def _flag_message(candidate: Candidate, code: str) -> str | None:
    """The message of the candidate's flag ``code``, or ``None`` when it has none."""
    return next((flag.message for flag in candidate.flags if flag.code == code), None)


def _scenario(name: str) -> Scenario:
    """The precedence scenario ``name``; fails when the catalogue files it under another module."""
    scenario = SCENARIOS[name]
    assert scenario.module == MODULE_PRECEDENCE, f"{name} belongs to {scenario.module}"
    return scenario


def _variant(name: str, **changes: Any) -> Scenario:
    """The catalogue scenario ``name`` with its context changed: one input moved."""
    base = SCENARIOS[name]
    return replace(base, context=replace(base.context, **changes))


def _ranking(out: PrecedenceOutput) -> list[tuple[str, int, str, str]]:
    """Every ranked item as ``(section, rank, rule, entity key)``, in output order."""
    return [
        (section, item.rank, item.candidate.rule_id, item.candidate.entity_ref.key)
        for section in SECTION_ORDER
        for item in out.items[section]
    ]


def _campaigns_behind(ctx: AnalysisContext, candidate: Candidate) -> frozenset[int]:
    """The campaigns an item's entity rests on, derived from the context alone.

    Independent of ``precedence``'s own expansion, so the P1 test checks holds against what
    the item is about: a campaign is itself, a keyword or search term belongs to the campaign
    of its ad group, a creative pair to its creatives' campaign, a white paper to the
    campaigns of its per-creative rows, a term cluster to the campaigns it read, a segment to
    its campaign and a shared budget to its member campaigns.
    """
    ref = candidate.entity_ref
    extra = candidate.evidence.extra
    found: set[int] = set()
    if ref.type in (ENTITY_CAMPAIGN, ENTITY_SEGMENT):
        external = ref.external_id.split("|", 1)[0]
        found.update(
            campaign.id
            for campaign in ctx.campaigns.values()
            if campaign.source == ref.source and campaign.external_id == external
        )
    elif ref.type == ENTITY_KEYWORD and ref.id is not None:
        found.add(ctx.keywords[ref.id].campaign_id)
    elif ref.type == ENTITY_SEARCH_TERM:
        ad_group = ref.external_id.split("|", 1)[0]
        found.update(
            group.campaign_id for group in ctx.ad_groups.values() if group.external_id == ad_group
        )
    elif ref.type == ENTITY_CREATIVE_PAIR:
        urns = set(ref.external_id.split("|"))
        found.update(item.campaign_id for item in ctx.creatives.values() if item.urn in urns)
    elif ref.type == ENTITY_CONTENT_ASSET:
        for row in extra.get("rows", ()):
            creative_id = row.get("creative_id")
            if creative_id in ctx.creatives:
                found.add(ctx.creatives[creative_id].campaign_id)
    elif ref.type == ENTITY_TERM_CLUSTER:
        found.update(int(campaign_id) for campaign_id in extra.get("campaign_ids", ()))
    elif ref.type == ENTITY_BUDGET:
        found.update(c.id for c in ctx.campaigns.values() if c.budget_id == ref.id)
    return frozenset(found)


def _subjects_behind(ctx: AnalysisContext, candidate: Candidate) -> frozenset[SubjectKey]:
    """The decision subjects an item rests on: its campaigns, their budgets, its own budget.

    A B1 or B3 card also rests on the allocation partners its ``extra.allocations`` names
    (each donor funding a B1, each receiver a B3 funds): a campaign partner with its budget,
    a budget partner with its member campaigns.
    """
    subjects: set[SubjectKey] = set()
    for campaign_id in _campaigns_behind(ctx, candidate):
        subjects.add(("campaign", campaign_id))
        budget_id = ctx.campaigns[campaign_id].budget_id
        if budget_id is not None:
            subjects.add(("budget", budget_id))
    ref = candidate.entity_ref
    if ref.type == ENTITY_BUDGET and ref.id is not None:
        subjects.add(("budget", ref.id))
    role = {B1: "donor", B3: "receiver"}.get(candidate.rule_id)
    for entry in candidate.evidence.extra.get("allocations", ()) if role else ():
        partner_type, partner_id = entry[role]["type"], entry[role]["id"]
        subjects.add((partner_type, partner_id))
        if partner_type == "budget":
            subjects.update(
                ("campaign", c.id) for c in ctx.campaigns.values() if c.budget_id == partner_id
            )
        elif (partner_budget := ctx.campaigns[partner_id].budget_id) is not None:
            subjects.add(("budget", partner_budget))
    return frozenset(subjects)


def _campaign_shape(ctx: AnalysisContext, campaign_id: int) -> DecisionShape:
    """A campaign's decision shape: a member of an explicitly shared budget or its own."""
    budget_id = ctx.campaigns[campaign_id].budget_id
    shared = budget_id is not None and ctx.budgets[budget_id].explicitly_shared
    return "shared_member" if shared else "own_budget"


def _status_discrepancy(
    ctx: AnalysisContext,
    campaign_id: int,
    *,
    number: int,
    detected_at: datetime,
    state: OpenDiscrepancyState,
) -> DiscrepancyInfo:
    """An unresolved ``status`` discrepancy on a campaign: decided active, platform paused."""
    campaign = ctx.campaigns[campaign_id]
    return DiscrepancyInfo(
        id=number,
        subject_type="campaign",
        subject_id=campaign_id,
        subject_name=campaign.name,
        source=campaign.source,
        shape=_campaign_shape(ctx, campaign_id),
        kind="status",
        decided_value={"status": "active"},
        platform_value={"status": "paused"},
        detected_at=detected_at,
        state=state,
    )


def _withheld_lines(out: PrecedenceOutput, rule_id: str) -> list[Withheld]:
    """Every withheld line of ``rule_id``, across the sections."""
    return [
        line
        for section in SECTION_ORDER
        for line in out.withheld[section]
        if line.rule_id == rule_id
    ]


def _window_lines(out: PrecedenceOutput, rule_id: str) -> list[Withheld]:
    """The withheld lines of ``rule_id`` that name a window shortfall in days."""
    return [
        line
        for line in _withheld_lines(out, rule_id)
        if line.unit == "days" and line.reason.startswith("window ")
    ]


def _assert_policy_thresholds_frozen(
    ctx: AnalysisContext, out: PrecedenceOutput, *, stale_after_hours: int, trusted_min_days: int
) -> None:
    """Every item's evidence froze the P4 and P5 thresholds it was judged against (0.5.4.8).

    Each item keeps exactly the thresholds its rule recorded, with their values and order,
    followed by ``sources.stale_after_hours`` (every item here reads a dataset) and
    ``window.trusted_min_days``, with the values given: "the thresholds applied", whether or
    not the item came out stale or short.
    """
    recorded = {
        (candidate.rule_id, candidate.entity_ref.type, candidate.entity_ref.key): (
            candidate.evidence.thresholds
        )
        for result in _evaluate(ctx)
        for candidate in result.candidates
    }
    items = _candidates(out)
    assert items
    for candidate in items:
        what = f"{candidate.rule_id} {candidate.entity_ref.key}"
        assert candidate.required_datasets, what
        own = recorded[(candidate.rule_id, candidate.entity_ref.type, candidate.entity_ref.key)]
        assert not {STALE_AFTER_KEY, TRUSTED_MIN_DAYS_KEY} & set(own), what
        assert tuple(candidate.evidence.thresholds.items()) == (
            *own.items(),
            (STALE_AFTER_KEY, stale_after_hours),
            (TRUSTED_MIN_DAYS_KEY, trusted_min_days),
        ), what
        rendered = candidate.evidence.to_json()["thresholds"]
        assert rendered[STALE_AFTER_KEY] == stale_after_hours, what
        assert rendered[TRUSTED_MIN_DAYS_KEY] == trusted_min_days, what


def _all_sources_scenario() -> Scenario:
    """The historical-only scenario's world with a Google part added beside LinkedIn.

    The ``historical.*`` worlds hold LinkedIn and HubSpot only, while the historical-only
    guarantee also covers Google-only items staying current and the read-time P4 test needs
    a Google and a LinkedIn item side by side. The world is composed with the Google part of
    ``p3.simulated`` (the K1 world) instead of adding a catalogue entry, which the scenario
    catalogue's owner maintains; every other input of the scenario is unchanged.
    """
    historical = _scenario("historical.activation.case")
    google = SCENARIOS["p3.simulated.case"].spec.google
    return replace(historical, spec=historical.spec.with_(google=google))


def _allocation_parties() -> tuple[int, int, int]:
    """``(donor freeing 300, donor freeing 200, receiver)`` of :data:`ALLOCATION_SCENARIO`."""
    ids = SCENARIOS[ALLOCATION_SCENARIO].ids
    return (
        ids.id("linkedin", "campaign", "donor-a"),
        ids.id("linkedin", "campaign", "donor-b"),
        ids.id("linkedin", "campaign", "receiver"),
    )


def _funding_cards(out: PrecedenceOutput) -> dict[tuple[str, int], Candidate]:
    """The B1 and B3 cards of a run, keyed by ``(rule, campaign id)``."""
    return {
        (candidate.rule_id, candidate.entity_ref.id): candidate
        for candidate in _candidates(out, BUDGET)
        if candidate.rule_id in (B1, B3) and candidate.entity_ref.id is not None
    }


def _frozen_subjects(candidate: Candidate) -> list[SubjectKey]:
    """The subjects frozen in a candidate's ``evidence.extra.depends_on``, in stored order."""
    return [(entry["type"], entry["id"]) for entry in candidate.evidence.extra["depends_on"]]


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


class _Unthrottled:
    """Grants every HubSpot request start at once, in place of the 4 requests/second throttle.

    The live HubSpot clients of these tests talk to the simulator's in-process
    ``httpx.MockTransport``, so pacing protects no platform quota, while the shared throttle
    would keep one live HubSpot backfill asleep for minutes. ``HubSpotClient`` awaits only
    ``acquire()`` before each attempt, so this stand-in implements exactly that contract
    instead of importing the connector's limiter, which is outside this module's dependencies.
    """

    async def acquire(self) -> None:
        """Grant the request start immediately."""
        return None


def _controller(
    world: SimulatedWorld, transports: Mapping[Source, httpx.AsyncBaseTransport] | None = None
) -> SourceModeController:
    """A controller on the scenario clock, its world and ``transports`` for live sources.

    Settings are the current environment's (``set_env``, ``live_credentials``), read by the
    controller itself.
    """
    return SourceModeController(
        world=world,
        clock=FIXED_CLOCK,
        transports=transports,
        # Duck-typed throttle: see _Unthrottled for why the connector's limiter is not imported.
        hubspot_limiter=_Unthrottled(),  # type: ignore[arg-type]
    )


async def _bootstrap(
    world: SimulatedWorld,
    *,
    force_simulated: bool,
    transports: Mapping[Source, httpx.AsyncBaseTransport] | None = None,
) -> SourceModeController:
    """Give every source of ``world`` an active dataset, built and backfilled as at first start.

    Raises:
        AssertionError: When any source's bootstrap failed.
    """
    controller = _controller(world, transports)
    report = await bootstrap_sources(
        controller.settings, force_simulated=force_simulated, controller=controller
    )
    assert not report.failures, report.failures
    return controller


async def _default_run(now: datetime = FIXED_NOW) -> AnalysisRun:
    """Compute and store the default-window run at ``now``."""
    window = await runner.default_window(now=now)
    return await runner.run_analysis(window, "default", "manual", now=now)


async def _custom_run(
    days: int, *, now: datetime = FIXED_NOW, thresholds: Thresholds | None = None
) -> AnalysisRun:
    """Compute and store a ``days``-day custom run ending on the default window end."""
    end = (await runner.default_window(now=now, thresholds=thresholds)).end
    start = end - timedelta(days=days - 1)
    window = await runner.custom_window(start, end, now=now, thresholds=thresholds)
    return await runner.run_analysis(window, "custom", "manual", thresholds=thresholds, now=now)


async def _page(client: httpx.AsyncClient, run_id: int | None = None) -> dict[str, Any]:
    """``GET /api/v1/recommendations`` for ``run_id`` (the latest default run when ``None``)."""
    params = {} if run_id is None else {"run_id": run_id}
    response = await client.get("/api/v1/recommendations", params=params)
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


def _page_items(page: Mapping[str, Any], rule_id: str | None = None) -> list[dict[str, Any]]:
    """Every item of a recommendations page (of one rule), section by section."""
    return [
        item
        for section in page["sections"]
        for group in section["groups"]
        for item in group["items"]
        if rule_id is None or item["rule_id"] == rule_id
    ]


def _page_section(page: Mapping[str, Any], key: str) -> dict[str, Any]:
    """One section of a recommendations page."""
    section: dict[str, Any] = next(s for s in page["sections"] if s["key"] == key)
    return section


def _banner(page: Mapping[str, Any], code: str) -> dict[str, Any] | None:
    """The page's banner ``code``, or ``None`` when it is not shown."""
    return next((banner for banner in page["banners"] if banner["code"] == code), None)


def _provenance_sources(item: Mapping[str, Any]) -> set[str]:
    """The sources an API item's frozen ``evidence.provenance`` names."""
    return {entry["source"] for entry in item["evidence"]["provenance"]}


def _frozen(value: object) -> str:
    """A canonical JSON rendering, for byte-identical comparisons of stored values."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


async def _stored(sessions: async_sessionmaker[AsyncSession], run_id: int) -> list[Recommendation]:
    """The stored recommendations of one run, by id, read in a fresh session."""
    async with sessions() as session:
        rows = await session.scalars(
            select(Recommendation)
            .where(Recommendation.run_id == run_id)
            .order_by(Recommendation.id)
        )
        return list(rows)


async def _overlay_at(
    sessions: async_sessionmaker[AsyncSession], run: AnalysisRun, now: datetime, config: Thresholds
) -> tuple[list[Recommendation], overlay.OverlayResult]:
    """The stored items of ``run`` and their current-state overlay read at ``now``."""
    async with sessions() as session:
        stored = await session.get(AnalysisRun, run.id)
        assert stored is not None
        rows = list(
            await session.scalars(
                select(Recommendation)
                .where(Recommendation.run_id == run.id)
                .order_by(Recommendation.id)
            )
        )
        result = await overlay.overlay(session, stored, rows, now, thresholds=config)
    return rows, result


def _forbid_rule_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every rule module, precedence and the runner raise if anything calls them.

    A read that still answers then served its sections from storage alone (AAP 0.5.3:
    stored-only reads return the frozen outputs without re-running rules).
    """

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the read path re-ran analysis instead of reading stored outputs")

    for module in RULE_MODULES:
        monkeypatch.setattr(module, "evaluate", refuse)
    monkeypatch.setattr(precedence, "apply_precedence", refuse)
    monkeypatch.setattr(runner, "run_analysis", refuse)
    monkeypatch.setattr(runner, "_evaluate", refuse)


# ---------------------------------------------------------------------------
# P1 discrepancies first (AAP 0.5.4.2 P1)
# ---------------------------------------------------------------------------


def test_p1_items_depending_on_a_discrepant_campaign_are_held_and_others_unverified(
    make_context: ContextFactory,
) -> None:
    """P1: a discrepant campaign holds every item resting on it; every other item is flagged.

    The default world gives every section items. One Google campaign (with keyword and search
    term items) and one LinkedIn campaign (with campaign, creative-pair and white-paper items)
    get an unresolved ``status`` discrepancy each. Every non-C0 item whose entity rests on
    either campaign or its budget is held with "Resolve discrepancy first" and lists the
    blocking discrepancy; every other non-C0 item stays unheld; every non-C0 item carries
    "Unverified while 2 discrepancies are open". The C0 items are never held nor flagged and
    are pinned first in Campaigns, oldest ``detected_at`` first (not id order). The partner
    without discrepancies holds nothing and flags nothing.
    """
    base = make_context("default", now=FIXED_NOW)
    partner = _precede(base)
    assert not base.discrepancies
    assert not _candidates(partner, CAMPAIGNS, C0)
    for item in partner.all_items():
        assert not item.candidate.held, item.candidate.entity_ref.key
        assert not item.candidate.has_flag(FLAG_UNVERIFIED_DISCREPANCIES)

    types_by_campaign: dict[int, set[str]] = {}
    for candidate in _candidates(partner):
        for campaign_id in _campaigns_behind(base, candidate):
            types_by_campaign.setdefault(campaign_id, set()).add(candidate.entity_ref.type)
    google = min(
        campaign_id
        for campaign_id, types in types_by_campaign.items()
        if base.campaigns[campaign_id].source == "google"
        and {ENTITY_KEYWORD, ENTITY_SEARCH_TERM} <= types
    )
    linkedin = min(
        campaign_id
        for campaign_id, types in types_by_campaign.items()
        if base.campaigns[campaign_id].source == "linkedin"
        and {ENTITY_CAMPAIGN, ENTITY_CREATIVE_PAIR, ENTITY_CONTENT_ASSET} <= types
    )
    # Ids run against detection order, so pinning must follow detected_at, never the id.
    newer = _status_discrepancy(
        base, google, number=1, detected_at=base.now - timedelta(days=1), state="open"
    )
    older = _status_discrepancy(
        base,
        linkedin,
        number=2,
        detected_at=base.now - timedelta(days=3),
        state="awaiting_platform",
    )
    ctx = replace(base, discrepancies=(newer, older), cache={})
    out = _precede(ctx)
    by_subject = {newer.key: newer.id, older.key: older.id}

    held_types: set[str] = set()
    unheld = 0
    for candidate in _candidates(out):
        if candidate.rule_id == C0:
            continue
        blocking = sorted(
            by_subject[key] for key in _subjects_behind(ctx, candidate) if key in by_subject
        )
        name = candidate.entity_ref.key
        assert _flag_message(candidate, FLAG_UNVERIFIED_DISCREPANCIES) == unverified_message(2)
        if blocking:
            assert candidate.held, name
            assert candidate.held_reason == HELD_DISCREPANCY_REASON, name
            assert list(candidate.evidence.extra["blocking_discrepancies"]) == blocking, name
            held_types.add(candidate.entity_ref.type)
        else:
            assert not candidate.held, name
            assert candidate.held_reason is None, name
            unheld += 1
    # The campaign itself, its keywords and search terms, its creatives and its white paper.
    assert {
        ENTITY_CAMPAIGN,
        ENTITY_KEYWORD,
        ENTITY_SEARCH_TERM,
        ENTITY_CREATIVE_PAIR,
        ENTITY_CONTENT_ASSET,
    } <= held_types
    assert unheld > 0

    pinned = out.items[CAMPAIGNS][:2]
    assert [item.rank for item in pinned] == [1, 2]
    assert [item.candidate.rule_id for item in pinned] == [C0, C0]
    assert [item.candidate.evidence.extra["discrepancy_id"] for item in pinned] == [
        older.id,
        newer.id,
    ]
    for item in pinned:
        assert item.candidate.pinned
        assert not item.candidate.held
        assert not item.candidate.has_flag(FLAG_UNVERIFIED_DISCREPANCIES)
    assert len(_candidates(out, CAMPAIGNS, C0)) == 2


@pytest.mark.parametrize("subject", ["member campaign", "shared budget"])
def test_p1_a_shared_budget_is_held_by_a_discrepancy_on_it_or_on_a_member(
    subject: str, make_context: ContextFactory
) -> None:
    """P1: the shared budget containing a discrepant campaign, or itself discrepant, is held.

    ``shared.poor_member.does_not_fire`` proposes a budget-level Scale and B1 on an explicitly
    shared budget. A ``status`` discrepancy on one member campaign, or a ``budget``
    discrepancy on the shared budget itself, holds those items; the partner without a
    discrepancy holds none.
    """
    base = make_context("shared.poor_member.does_not_fire")
    shared = next(item for item in base.budgets.values() if item.explicitly_shared)
    member = min(c.id for c in base.campaigns.values() if c.budget_id == shared.id)

    def budget_items(out: PrecedenceOutput) -> list[Candidate]:
        return [
            candidate
            for candidate in _candidates(out)
            if candidate.rule_id != C0
            and candidate.entity_ref.type == ENTITY_BUDGET
            and candidate.entity_ref.id == shared.id
        ]

    partner = budget_items(_precede(base))
    assert B1 in {candidate.rule_id for candidate in partner}
    assert not any(candidate.held for candidate in partner)

    if subject == "member campaign":
        info = _status_discrepancy(
            base, member, number=7, detected_at=base.now - timedelta(hours=5), state="open"
        )
    else:
        info = DiscrepancyInfo(
            id=7,
            subject_type="budget",
            subject_id=shared.id,
            subject_name=shared.name,
            source="google",
            shape="budget",
            kind="budget",
            decided_value={"daily_budget": "1200.00"},
            platform_value={"daily_budget": "1000.00"},
            detected_at=base.now - timedelta(hours=5),
            state="open",
        )
    out = _precede(replace(base, discrepancies=(info,), cache={}))
    held = budget_items(out)
    assert sorted(c.rule_id for c in held) == sorted(c.rule_id for c in partner)
    for candidate in held:
        assert candidate.held
        assert candidate.held_reason == HELD_DISCREPANCY_REASON
        assert list(candidate.evidence.extra["blocking_discrepancies"]) == [info.id]
    (c0,) = _candidates(out, CAMPAIGNS, C0)
    assert c0.evidence.extra["discrepancy_id"] == info.id
    assert not c0.held


def test_p1_funding_cards_freeze_their_allocation_partners_for_the_overlay(
    make_context: ContextFactory,
) -> None:
    """P1: a B3 card depends on the receiver it funds and a B1 on every donor funding it.

    ``evidence.extra.depends_on`` is what the read-time overlay holds an item on, so each
    donor's frozen list names the receiver its money moves to and the receiver's names both
    donors, after the card's own campaign. The relationship is not transitive: a donor does
    not depend on the other donor of the same receiver.
    """
    donor_a, donor_b, receiver = _allocation_parties()
    out = _precede(make_context(ALLOCATION_SCENARIO))
    assert [(a.donor, a.receiver) for a in out.allocations] == [
        (("campaign", donor_a), ("campaign", receiver)),
        (("campaign", donor_b), ("campaign", receiver)),
    ]
    cards = _funding_cards(out)
    assert set(cards) == {(B1, receiver), (B3, donor_a), (B3, donor_b)}
    for card in cards.values():
        assert not card.held
        assert _frozen_subjects(card) == [subject.key for subject in card.depends_on]

    b1 = _frozen_subjects(cards[(B1, receiver)])
    assert b1[0] == ("campaign", receiver)
    assert {("campaign", donor_a), ("campaign", donor_b)} <= set(b1)
    for donor, other in ((donor_a, donor_b), (donor_b, donor_a)):
        frozen = _frozen_subjects(cards[(B3, donor)])
        assert frozen[0] == ("campaign", donor)
        assert ("campaign", receiver) in frozen
        assert ("campaign", other) not in frozen


@pytest.mark.parametrize("discrepant", ["receiver", "donor"])
def test_p1_a_discrepancy_on_an_allocation_partner_holds_the_cards_it_funds_or_draws_on(
    discrepant: str, make_context: ContextFactory
) -> None:
    """P1: a discrepant receiver holds both donor cards; a discrepant donor holds the B1.

    In ``b3.allocation.case`` a ``status`` discrepancy on the receiver holds its B1 and both
    B3 cards telling the user to move money to it; one on the 300 donor holds that donor's
    card and the B1 it funds, while the other donor, which depends only on the receiver,
    stays actionable. Each held card is held whole, with "Resolve discrepancy first" and the
    blocking discrepancy, and keeps the allocation, amounts and wording it has without the
    discrepancy: the allocation precedes the holds.
    """
    donor_a, donor_b, receiver = _allocation_parties()
    base = make_context(ALLOCATION_SCENARIO)
    partner = _precede(base)
    unheld = _funding_cards(partner)
    assert not any(card.held for card in unheld.values())

    target = receiver if discrepant == "receiver" else donor_a
    info = _status_discrepancy(
        base, target, number=7, detected_at=base.now - timedelta(hours=5), state="open"
    )
    out = _precede(replace(base, discrepancies=(info,), cache={}))
    assert out.allocations == partner.allocations
    held = {(B1, receiver), (B3, donor_a)}
    if discrepant == "receiver":
        held.add((B3, donor_b))
    cards = _funding_cards(out)
    assert set(cards) == set(unheld)
    for key, card in cards.items():
        if key in held:
            assert card.held, key
            assert card.held_reason == HELD_DISCREPANCY_REASON, key
            assert list(card.evidence.extra["blocking_discrepancies"]) == [info.id], key
        else:
            assert not card.held, key
            assert card.held_reason is None, key
            assert "blocking_discrepancies" not in card.evidence.extra, key
        assert card.reason == unheld[key].reason, key
        assert card.evidence.extra["allocations"] == unheld[key].evidence.extra["allocations"]
        assert _frozen_subjects(card) == _frozen_subjects(unheld[key]), key


def test_p1_scenario_holds_the_dependent_item_and_pins_the_exempt_discrepancy(
    make_context: ContextFactory,
) -> None:
    """P1: ``p1.holds`` holds its K1 item, while ``p1.c0_exempt`` keeps C0 unheld and pinned.

    Both run on the world in which the campaign was paused after the window, so the entity
    sync raised a ``status`` discrepancy against the decided state.
    """
    holds = _scenario("p1.holds.case")
    ctx = make_context(holds)
    assert ctx.discrepancies
    out = _precede(ctx)
    k1 = _candidates(out, KEYWORDS, K1)
    assert k1
    for candidate in k1:
        assert candidate.held is holds.expect.values["held"]
        assert candidate.held_reason == holds.expect.values["held_reason"]
        assert _flag_message(candidate, FLAG_UNVERIFIED_DISCREPANCIES) == unverified_message(
            len(ctx.discrepancies)
        )

    exempt = _scenario("p1.c0_exempt.case")
    out = _precede(make_context(exempt))
    first = out.items[CAMPAIGNS][0].candidate
    assert first.rule_id == exempt.expect.rule_id == C0
    assert first.held is exempt.expect.values["held"]
    assert first.pinned is exempt.expect.values["pinned"]
    assert first.evidence.extra["kind"] == exempt.expect.values["kind"]


async def test_p1_a_discrepancy_detected_after_the_run_holds_items_at_read_time(
    db: async_sessionmaker[AsyncSession],
    set_env: EnvSetter,
    make_world: WorldFactory,
    authed_client: httpx.AsyncClient,
) -> None:
    """P1 at read time: a discrepancy raised after the run holds its items through the overlay.

    The run is stored first; then the scenario's post-run ``status`` discrepancy is raised on
    the campaign the K1 item rests on, exactly as the entity sync's detection would. At once,
    without a rerun, ``GET /recommendations`` shows the item ``held_now`` with "Resolve
    discrepancy first" and the live "Unverified while 1 discrepancies are open" flag, the
    overlay counts one open discrepancy and the discrepancy banner leads with that count. The
    stored item is unchanged: ``held`` stays false, and evidence and flags are byte-identical.
    """
    scenario = _scenario("p1.post_run_discrepancy.case")
    expected = scenario.expect.values
    set_env(DATA_MODE="simulated")
    await _bootstrap(make_world(scenario), force_simulated=True)
    run = await _default_run()

    before = await _page(authed_client, run.id)
    (k1,) = _page_items(before, K1)
    assert k1["held"] is False
    assert k1["current"]["held_now"] is False
    assert before["overlay"]["open_discrepancies"] == 0
    assert _banner(before, BANNER_OPEN_DISCREPANCIES) is None
    stored_before = {row.id: _frozen(row.evidence) for row in await _stored(db, run.id)}

    (spec,) = scenario.context.post_run_discrepancies
    detected_at = ANCHOR_MIDNIGHT + timedelta(hours=spec.detected_at_offset_hours)
    assert detected_at > run.started_at
    campaign_id = next(
        subject["id"]
        for subject in k1["evidence"]["extra"]["depends_on"]
        if subject["type"] == "campaign"
    )
    assert spec.decided_value is not None
    assert spec.platform_value is not None
    async with db() as session:
        ref = decisions.SubjectRef("campaign", campaign_id)
        state = await decisions.load_subject_state(session, ref, lock=True)
        assert state.latest_snapshot is not None
        assert state.current_decision is not None
        await decisions.raise_discrepancy(
            session,
            ref,
            spec.kind,
            decided_value=dict(spec.decided_value),
            platform_value=dict(spec.platform_value),
            snapshot_id=state.latest_snapshot.id,
            decision_id=state.current_decision.id,
            detected_at=detected_at,
            state=state,
            locked=True,
        )
        await session.commit()

    after = await _page(authed_client, run.id)
    assert after["run"]["id"] == run.id
    (held,) = _page_items(after, K1)
    assert held["id"] == k1["id"]
    assert held["held"] is expected["held"]
    assert held["current"]["held_now"] is expected["held_now"]
    assert held["current"]["held_reason_now"] == expected["held_reason_now"]
    assert held["current"]["historical"] is False
    flags_now = {flag["code"]: flag["message"] for flag in held["current"]["flags_now"]}
    assert flags_now[FLAG_UNVERIFIED_DISCREPANCIES] == unverified_message(1)
    assert after["overlay"]["open_discrepancies"] == expected["open_discrepancies"]
    banner = after["banners"][0]
    assert banner["code"] == BANNER_OPEN_DISCREPANCIES
    assert banner["count"] == expected["open_discrepancies"]
    assert _frozen(held["evidence"]) == _frozen(k1["evidence"])
    assert held["flags"] == k1["flags"]
    stored_after = {row.id: _frozen(row.evidence) for row in await _stored(db, run.id)}
    assert stored_after == stored_before
    assert all(not row.held for row in await _stored(db, run.id))


@pytest.mark.parametrize("discrepant", ["receiver", "donor"])
async def test_p1_a_discrepancy_on_an_allocation_partner_after_the_run_holds_at_read_time(
    discrepant: str, db: async_sessionmaker[AsyncSession], make_context: ContextFactory
) -> None:
    """P1 at read time: a post-run discrepancy on an allocation partner holds the partner card.

    A world bootstrapped from scratch has no budget history yet, so every budget rule is
    withheld in its first run. The stored run is therefore ``b3.allocation.case``'s
    precedence output, computed with no discrepancy and written row by row as the runner
    writes it, on active datasets carrying the ids its provenance names. A ``status``
    discrepancy raised afterwards on the receiver makes its B1 and both B3 cards funding it
    ``held_now``; one on the 300 donor makes that card and the B1 it funds ``held_now``,
    while the other donor stays actionable. The overlay counts the discrepancy at once, and
    every stored card stays unheld with byte-identical evidence.
    """
    ctx = make_context(ALLOCATION_SCENARIO)
    out = _precede(ctx)
    funding = [item for item in out.items[BUDGET] if item.candidate.rule_id in (B1, B3)]
    assert not any(item.candidate.held for item in funding)
    donor_a, donor_b, receiver = _allocation_parties()
    target = receiver if discrepant == "receiver" else donor_a
    held = {(B1, receiver), (B3, donor_a)}
    if discrepant == "receiver":
        held.add((B3, donor_b))
    provenance = {
        (entry.source, entry.dataset_id, entry.mode, entry.role)
        for item in funding
        for entry in item.candidate.evidence.provenance
    }
    assert {source for source, *_ in provenance} == {"linkedin"}

    async with db() as session, session.begin():
        for source, dataset_id, mode, role in sorted(provenance):
            session.add(
                SourceDataset(
                    id=dataset_id,
                    source=source,
                    mode=mode,
                    state="active",
                    role=role,
                    activated_at=ctx.now - timedelta(days=1),
                )
            )
        await session.flush()
        run = AnalysisRun(
            kind="default",
            window_start=ctx.window.start,
            window_end=ctx.window.end,
            source_day_boundaries=[],
            trigger="manual",
            data_modes={},
            dataset_ids={},
            config_hash=ctx.thresholds.config_hash,
            status="succeeded",
            started_at=ctx.now,
            finished_at=ctx.now + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        rows = [Recommendation(**runner._recommendation_row(run.id, item)) for item in funding]
        session.add_all(rows)
        await session.flush()
        subjects = {row.id: (row.rule_id, row.decision_subject["id"]) for row in rows}
    assert set(subjects.values()) == {(B1, receiver), (B3, donor_a), (B3, donor_b)}
    stored_before = {row.id: _frozen(row.evidence) for row in await _stored(db, run.id)}

    _, before = await _overlay_at(db, run, READ_AT, ctx.thresholds)
    assert before.overlay.open_discrepancies == 0
    for item_id in subjects:
        assert before.items[item_id].held_now is False, subjects[item_id]
        assert before.items[item_id].historical is False, subjects[item_id]

    (dataset_id,) = {dataset_id for _, dataset_id, *_ in provenance}
    async with db() as session, session.begin():
        session.add(
            Discrepancy(
                source="linkedin",
                dataset_id=dataset_id,
                subject_type="campaign",
                subject_id=target,
                kind="status",
                decided_value={"status": "active"},
                platform_value={"status": "paused"},
                detected_at=ctx.now + timedelta(minutes=30),
                state="open",
            )
        )

    _, after = await _overlay_at(db, run, READ_AT, ctx.thresholds)
    assert after.overlay.open_discrepancies == 1
    for item_id, key in subjects.items():
        view = after.items[item_id]
        assert view.historical is False, key
        if key in held:
            assert view.held_now is True, key
            assert view.held_reason_now == HELD_DISCREPANCY_REASON, key
        else:
            assert view.held_now is False, key
            assert view.held_reason_now is None, key
    stored_after = await _stored(db, run.id)
    assert {row.id: _frozen(row.evidence) for row in stored_after} == stored_before
    assert all(not row.held for row in stored_after)


# ---------------------------------------------------------------------------
# P2 conversion tracking absent (AAP 0.5.4.2 P2)
# ---------------------------------------------------------------------------


def _with_outcome(ctx: AnalysisContext, source: str, **outcome: Any) -> AnalysisContext:
    """``ctx`` with the given outcome fields set on the first spending window day of ``source``.

    Moves exactly one input: the outcomes recorded on one day of the lowest-id spending
    campaign of the source.
    """
    window = ctx.window
    campaign_id = min(
        campaign.id
        for campaign in ctx.campaigns.values()
        if campaign.source == source
        and any(
            window.start <= day.date <= window.end and day.cost > 0
            for day in ctx.campaign_days.get(campaign.id, ())
        )
    )
    days = list(ctx.campaign_days[campaign_id])
    index = next(
        position
        for position, day in enumerate(days)
        if window.start <= day.date <= window.end and day.cost > 0
    )
    days[index] = replace(days[index], **outcome)
    return replace(ctx, campaign_days={**ctx.campaign_days, campaign_id: tuple(days)}, cache={})


def _without_outcomes(ctx: AnalysisContext, source: str) -> AnalysisContext:
    """``ctx`` with every outcome of ``source``'s campaigns zeroed, spend unchanged."""
    zeroed: dict[int, tuple[DayFact, ...]] = dict(ctx.campaign_days)
    for campaign in ctx.campaigns.values():
        if campaign.source != source:
            continue
        zeroed[campaign.id] = tuple(
            replace(
                day,
                leads=None if day.leads is None else 0,
                website_conversions=None if day.website_conversions is None else 0,
                conversions=None if day.conversions is None else Decimal(0),
                reported_floors=(),
            )
            for day in ctx.campaign_days.get(campaign.id, ())
        )
    return replace(ctx, campaign_days=zeroed, cache={})


def test_p2_absent_tracking_records_its_finding_and_suppresses_the_zero_outcome_rules(
    make_context: ContextFactory,
) -> None:
    """P2: spend with zero outcomes suppresses K1, C1 and B2's zero-outcome branch of Google.

    ``p2.tracking_absent.fires`` spends with no conversion in the window: Keywords, Campaigns
    and Budget each record ``conversion_tracking_absent`` with the verbatim message, naming
    Google. The twin records conversions, so it emits a K1 card; under the absent-tracking
    context that card, and copies of it as C1 and as B2's zero-outcome branch, are suppressed
    and counted ``tracking_absent`` in their sections, while a B2 of the poor-efficiency
    branch stays. The same context with one conversion recorded is not absent: no finding,
    nothing suppressed.
    """
    fires = _scenario("p2.tracking_absent.fires")
    twin = _scenario("p2.tracking_absent.does_not_fire")
    source = str(fires.expect.values["source"])
    ctx = make_context(fires)
    out = _precede(ctx)
    for section in (KEYWORDS, CAMPAIGNS, BUDGET):
        found = [f for f in out.findings[section] if f.code == fires.expect.finding]
        assert len(found) == 1, section
        (finding,) = found
        assert finding.code == FINDING_CONVERSION_TRACKING_ABSENT
        assert finding.message == TRACKING_ABSENT_MESSAGE
        assert finding.message == (
            "No conversions or leads recorded — verify tracking before acting on zero-outcome rules"
        )
        assert finding.evidence["source"] == source
        assert finding.evidence["outcomes"] == 0
        assert all(amount > 0 for amount in finding.evidence["spend"].values())
    for section in (CREATIVE, WHITE_PAPERS):
        assert all(f.code != FINDING_CONVERSION_TRACKING_ABSENT for f in out.findings[section])
    assert set(cast("tuple[str, ...]", fires.expect.values["suppressed_rules"])) == {K1, C1, B2, W2}
    assert not _candidates(out, KEYWORDS, K1)

    twin_out = _precede(make_context(twin))
    assert all(
        f.code != FINDING_CONVERSION_TRACKING_ABSENT
        for section in SECTION_ORDER
        for f in twin_out.findings[section]
    )
    # Decision: no world can make K1, C1 or B2's zero-outcome branch fire while its source
    # records no outcome, because each sizes its gate from recorded outcomes (K1's baseline
    # rate, C1's and B2's expected outcomes). The twin's genuine K1 card, and copies of it
    # under the other suppressed rule ids, therefore probe the P2 step itself.
    (k1,) = [c for c in keywords.evaluate(make_context(twin)).candidates if c.rule_id == K1]
    c1 = replace(k1, rule_id=C1, section=CAMPAIGNS, group=GROUP_PAUSE)
    b2_zero = replace(
        k1,
        rule_id=B2,
        section=BUDGET,
        group=GROUP_CHANGES,
        evidence=k1.evidence.with_extra(branch="zero_outcomes"),
    )
    b2_poor = replace(b2_zero, evidence=k1.evidence.with_extra(branch="poor_efficiency"))
    probes = RuleResult(candidates=(k1, c1, b2_zero, b2_poor))

    suppressed = _precede(ctx, (probes,))
    for section in (KEYWORDS, CAMPAIGNS, BUDGET):
        assert suppressed.suppressed[section].get(SUPPRESS_TRACKING_ABSENT) == 1, section
    assert k1.entity_ref.key not in {c.entity_ref.key for c in _candidates(suppressed, KEYWORDS)}
    assert not _candidates(suppressed, CAMPAIGNS, C1)
    kept_b2 = _candidates(suppressed, BUDGET, B2)
    assert [c.evidence.extra["branch"] for c in kept_b2] == ["poor_efficiency"]
    keywords_finding = next(
        f for f in suppressed.findings[KEYWORDS] if f.code == FINDING_CONVERSION_TRACKING_ABSENT
    )
    assert keywords_finding.evidence["suppressed_items"] == 1

    recorded = _with_outcome(ctx, source, conversions=Decimal(1))
    one = _precede(recorded, (probes,))
    assert all(
        f.code != FINDING_CONVERSION_TRACKING_ABSENT
        for section in SECTION_ORDER
        for f in one.findings[section]
    )
    assert all(SUPPRESS_TRACKING_ABSENT not in counts for counts in one.suppressed.values())
    assert k1.entity_ref.key in {c.entity_ref.key for c in _candidates(one, KEYWORDS, K1)}
    assert _candidates(one, CAMPAIGNS, C1)


def test_p2_absent_lead_tracking_suppresses_w2_of_that_source_only(
    make_context: ContextFactory,
) -> None:
    """P2: a LinkedIn source spending with no leads suppresses its W2; Google is untouched.

    ``fallback.recovery_all_pass`` holds every source. With LinkedIn's outcomes zeroed (spend
    kept), Campaigns, Budget and White papers record ``conversion_tracking_absent`` naming
    LinkedIn, and a W2 retire card on a LinkedIn white paper is suppressed; Google, which
    still records conversions, keeps its K1 card and records no finding. With LinkedIn's
    outcomes intact nothing is suppressed.
    """
    # The catalogue world holding all three sources; its LinkedIn outcomes are zeroed in the
    # context only. Without leads no contact reaches Qualified Lead, so every white-paper
    # cohort shows "Insufficient data" and W2 cannot fire: a LinkedIn card copied as W2
    # probes the P2 step instead, as in the Google test above.
    intact = make_context("fallback.recovery_all_pass.case")
    absent = _without_outcomes(intact, "linkedin")
    linkedin_card = next(
        candidate
        for result in _evaluate(intact)
        for candidate in result.candidates
        if candidate.entity_ref.source == "linkedin"
    )
    w2 = replace(
        linkedin_card,
        rule_id=W2,
        section=WHITE_PAPERS,
        group=cohort_group("USD"),
        entity_ref=EntityRef("linkedin", ENTITY_CONTENT_ASSET, None, "guide|USD", "Guide"),
        increases_spend_on=(),
        decision_subject=None,
    )
    probes = RuleResult(candidates=(w2,))

    out = _precede(absent, (probes,))
    for section in (CAMPAIGNS, BUDGET, WHITE_PAPERS):
        found = [f for f in out.findings[section] if f.code == FINDING_CONVERSION_TRACKING_ABSENT]
        assert [f.evidence["source"] for f in found] == ["linkedin"], section
    assert all(f.code != FINDING_CONVERSION_TRACKING_ABSENT for f in out.findings[KEYWORDS])
    assert out.suppressed[WHITE_PAPERS] == {SUPPRESS_TRACKING_ABSENT: 1}
    assert not _candidates(out, WHITE_PAPERS, W2)
    assert _candidates(out, KEYWORDS, K1)

    kept = _precede(intact, (probes,))
    assert [c.entity_ref.key for c in _candidates(kept, WHITE_PAPERS, W2)] == [w2.entity_ref.key]
    assert all(SUPPRESS_TRACKING_ABSENT not in counts for counts in kept.suppressed.values())


TRACKING_SECTIONS: dict[str, tuple[str, ...]] = {
    "google": (KEYWORDS, CAMPAIGNS, BUDGET),
    "linkedin": (CAMPAIGNS, BUDGET, WHITE_PAPERS),
}
"""The sections a source's ``conversion_tracking_absent`` lands in: those of its K1, C1, B2, W2."""


def _unreported_outcomes(ctx: AnalysisContext, source: str) -> AnalysisContext:
    """``ctx`` with every outcome of ``source``'s campaigns unreported (``None``), spend unchanged.

    No day keeps a reported floor either, so nothing about the source's outcomes was recorded.
    """
    unknown: dict[int, tuple[DayFact, ...]] = dict(ctx.campaign_days)
    for campaign in ctx.campaigns.values():
        if campaign.source != source:
            continue
        unknown[campaign.id] = tuple(
            replace(day, leads=None, website_conversions=None, conversions=None, reported_floors=())
            for day in ctx.campaign_days.get(campaign.id, ())
        )
    return replace(ctx, campaign_days=unknown, cache={})


def _zero_outcome_probes(intact: AnalysisContext, source: str) -> RuleResult:
    """A genuine card of ``source`` copied under each zero-outcome rule P2 suppresses for it.

    Google's is its K1 card, kept as it is and copied as C1 and as B2's zero-outcome branch;
    LinkedIn's is its first card, copied as C1, as B2's zero-outcome branch and as a W2 retire
    card on a white paper. As in the tests above, no world makes these rules fire while their
    source records no outcome, so the copies probe the P2 step itself.
    """
    card = next(
        candidate
        for result in _evaluate(intact)
        for candidate in result.candidates
        if candidate.entity_ref.source == source
    )
    plain = replace(card, increases_spend_on=())
    probes = [
        replace(plain, rule_id=C1, section=CAMPAIGNS, group=GROUP_PAUSE),
        replace(
            plain,
            rule_id=B2,
            section=BUDGET,
            group=GROUP_CHANGES,
            evidence=card.evidence.with_extra(branch="zero_outcomes"),
        ),
    ]
    if source == "google":
        assert card.rule_id == K1
        probes.append(card)
    else:
        probes.append(
            replace(
                plain,
                rule_id=W2,
                section=WHITE_PAPERS,
                group=cohort_group("USD"),
                entity_ref=EntityRef("linkedin", ENTITY_CONTENT_ASSET, None, "guide|USD", "Guide"),
                decision_subject=None,
            )
        )
    return RuleResult(candidates=tuple(probes))


def _zero_outcome_candidates(results: Iterable[RuleResult], source: str) -> list[Candidate]:
    """The K1, C1, zero-outcome B2 and W2 candidates of ``source`` that the rules emitted."""
    return [
        candidate
        for result in results
        for candidate in result.candidates
        if candidate.entity_ref.source == source
        and (
            candidate.rule_id in (K1, C1, W2)
            or (
                candidate.rule_id == B2
                and candidate.evidence.extra.get("branch") == "zero_outcomes"
            )
        )
    ]


@pytest.mark.parametrize("source", ["google", "linkedin"])
def test_p2_unreported_outcomes_record_nothing_yet_never_feed_a_zero_outcome_rule(
    make_context: ContextFactory, source: str
) -> None:
    """P2 and unknown outcomes: nothing recorded is absent tracking; it is never a zero count.

    ``fallback.recovery_all_pass.case`` holds every source, each recording outcomes. With one
    source's outcomes unreported (``None``) on every day and no partial count kept, its spend
    stands with no outcome recorded, so, exactly as with a known 0 on every day, that source
    records ``conversion_tracking_absent`` in each section it affects and its K1, C1,
    zero-outcome B2 and W2 cards are suppressed there, while the other source records nothing.
    Unknown still differs from zero before P2: no rule turns the unreported days into a K1, C1,
    zero-outcome B2 or W2 candidate, and Google's account-wide K1 line says its conversions are
    unavailable on every day, where a known 0 says "0 conversions".
    """
    intact = make_context("fallback.recovery_all_pass.case")
    probes = _zero_outcome_probes(intact, source)
    days = intact.window.days
    k1_lines = {
        "unreported": (
            "account cost/conversion unavailable: conversions unavailable on "
            f"{days} of {days} days in the {days}-day window"
        ),
        "known_zero": (
            f"account cost/conversion unavailable: 0 conversions in the {days}-day window"
        ),
    }
    for case, ctx in (
        ("unreported", _unreported_outcomes(intact, source)),
        ("known_zero", _without_outcomes(intact, source)),
    ):
        results = _evaluate(ctx)
        assert _zero_outcome_candidates(results, source) == [], case
        out = precedence.apply_precedence([*results, probes], ctx)
        for section in SECTION_ORDER:
            found = [
                f for f in out.findings[section] if f.code == FINDING_CONVERSION_TRACKING_ABSENT
            ]
            if section not in TRACKING_SECTIONS[source]:
                assert found == [], (case, section)
                continue
            (finding,) = found
            assert finding.message == TRACKING_ABSENT_MESSAGE
            assert (finding.evidence["source"], finding.evidence["outcomes"]) == (source, 0)
            assert all(amount > 0 for amount in finding.evidence["spend"].values())
            assert out.suppressed[section].get(SUPPRESS_TRACKING_ABSENT) == 1, (case, section)
        for probe in probes.candidates:
            kept = [c.entity_ref.key for c in _candidates(out, probe.section, probe.rule_id)]
            assert probe.entity_ref.key not in kept, (case, probe.rule_id)
        if source == "google":
            (line,) = _withheld_lines(out, K1)
            assert line.entity is not None
            assert line.reason == k1_lines[case], case


def test_p2_a_partially_reported_day_records_its_floor_and_suppresses_nothing(
    make_context: ContextFactory,
) -> None:
    """P2 and partial reports: a day's reported floor is a recorded outcome, never a zero.

    LinkedIn sums its creatives' rows per campaign day; when one row of a day reports 1 lead and
    another reports none, the day's leads stay unavailable for every count, rate and efficiency,
    and the reporting rows' 1 lead is kept as the day's floor. With LinkedIn otherwise unreported
    (the context of the test above), that one floor means a lead was recorded: no
    ``conversion_tracking_absent`` finding, and the C1, zero-outcome B2 and W2 probes all
    stand. The day itself still counts as unreported, so no rule reads it as a zero either.
    """
    intact = make_context("fallback.recovery_all_pass.case")
    probes = _zero_outcome_probes(intact, "linkedin")
    unknown = _unreported_outcomes(intact, "linkedin")
    ctx = _with_outcome(unknown, "linkedin", reported_floors=(("leads", 1),))
    floored = [
        (campaign, day)
        for campaign in ctx.campaigns.values()
        if campaign.source == "linkedin"
        for day in ctx.campaign_days.get(campaign.id, ())
        if day.reported_floors
    ]
    ((campaign, day),) = floored
    assert day.outcome(campaign.objective_group) is None
    assert day.recorded_outcome(campaign.objective_group) == 1

    results = _evaluate(ctx)
    assert _zero_outcome_candidates(results, "linkedin") == []
    out = precedence.apply_precedence([*results, probes], ctx)
    assert all(
        f.code != FINDING_CONVERSION_TRACKING_ABSENT
        for section in SECTION_ORDER
        for f in out.findings[section]
    )
    assert all(SUPPRESS_TRACKING_ABSENT not in counts for counts in out.suppressed.values())
    for probe in probes.candidates:
        kept = [c.entity_ref.key for c in _candidates(out, probe.section, probe.rule_id)]
        assert kept == [probe.entity_ref.key], (probe.rule_id, kept)


@pytest.mark.parametrize(
    ("name", "absent"),
    [("p2.tracking_absent.fires", True), ("p2.tracking_absent.does_not_fire", False)],
)
async def test_p2_the_tracking_finding_is_stored_and_shown_as_a_banner_at_read_time(
    name: str,
    absent: bool,
    set_env: EnvSetter,
    make_world: WorldFactory,
    authed_client: httpx.AsyncClient,
) -> None:
    """P2 at read time: the stored tracking finding leads its sections and names its source.

    The stored run of ``p2.tracking_absent.fires`` returns ``conversion_tracking_absent``
    first in Keywords, Campaigns and Budget, and the page shows the tracking banner naming
    Google with the verbatim advice. The twin's run, which records conversions, shows
    neither.
    """
    set_env(DATA_MODE="simulated")
    await _bootstrap(make_world(_scenario(name)), force_simulated=True)
    page = await _page(authed_client, (await _default_run()).id)
    banner = _banner(page, BANNER_TRACKING_ABSENT)
    if not absent:
        assert banner is None
        assert all(
            finding["code"] != FINDING_CONVERSION_TRACKING_ABSENT
            for section in page["sections"]
            for finding in section["findings"]
        )
        return
    for key in (KEYWORDS, CAMPAIGNS, BUDGET):
        codes = [finding["code"] for finding in _page_section(page, key)["findings"]]
        assert codes[0] == FINDING_CONVERSION_TRACKING_ABSENT, key
    assert banner is not None
    assert banner["message"] == (
        "No conversions or leads recorded for Google — verify tracking before acting on "
        "zero-outcome rules"
    )
    assert banner["count"] == 1


# ---------------------------------------------------------------------------
# P3 simulated data (AAP 0.5.4.2 P3)
# ---------------------------------------------------------------------------


def test_p3_data_status_follows_the_modes_of_the_datasets_an_item_read(
    make_context: ContextFactory,
) -> None:
    """P3: simulated datasets give ``simulated``, a live/simulated join ``mixed``, live ``live``.

    ``p3.simulated`` reads simulated Google data: K1 is ``simulated`` with the "Simulated
    data (Google)" flag. ``p3.mixed`` joins live LinkedIn with simulated HubSpot: C1b is
    ``mixed`` and its flag says so. The same world with HubSpot live as well is ``live`` and
    carries no ``simulated`` flag.
    """
    simulated = _scenario("p3.simulated.case")
    out = _precede(make_context(simulated))
    k1 = _candidates(out, KEYWORDS, K1)
    assert k1
    for candidate in k1:
        assert candidate.data_status == simulated.expect.values["data_status"] == DATA_SIMULATED
        assert set(simulated.expect.flags) <= set(candidate.flag_codes)
        assert _flag_message(candidate, FLAG_SIMULATED) == "Simulated data (Google)"

    mixed = _scenario("p3.mixed.case")
    out = _precede(make_context(mixed))
    c1b = _candidates(out, CAMPAIGNS, C1B)
    assert c1b
    for candidate in c1b:
        assert candidate.data_status == mixed.expect.values["data_status"] == DATA_MIXED
        modes = {entry.mode for entry in candidate.evidence.provenance}
        assert modes == {"live", "simulated"}
        message = _flag_message(candidate, FLAG_SIMULATED)
        assert message == "Joins live and simulated data (HubSpot simulated)"

    live = _variant("p3.mixed.case", modes={"linkedin": "live", "hubspot": "live"})
    out = _precede(make_context(live))
    assert _candidates(out, CAMPAIGNS, C1B)
    for item in out.all_items():
        assert item.candidate.data_status == DATA_LIVE, item.candidate.entity_ref.key
        assert not item.candidate.has_flag(FLAG_SIMULATED)
        assert {entry.mode for entry in item.candidate.evidence.provenance} == {"live"}


# ---------------------------------------------------------------------------
# P4 stale or incomplete data (AAP 0.5.4.2 P4, 0.3.2 freshness per dataset)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["p4.stale.case", "p4.entity_sync_does_not_clear_metrics.case"])
def test_p4_a_stale_required_dataset_flags_exactly_the_items_that_read_it(
    name: str, make_context: ContextFactory
) -> None:
    """P4: an item is stale when a dataset it reads last succeeded too long ago.

    In both cases ``google.metrics_daily`` last succeeded 30 hours ago, beyond
    ``sources.stale_after_hours``. Every item reading it carries ``stale`` ("Data as of …");
    an item that does not read it carries none. In ``p4.entity_sync_does_not_clear_metrics``
    the entity sync succeeded an hour ago, and the metrics dataset stays stale all the same.
    """
    scenario = _scenario(name)
    stale_dataset = str(scenario.expect.values["stale_dataset"])
    ctx = make_context(scenario)
    stale_after = timedelta(hours=ctx.thresholds.int("sources.stale_after_hours"))
    google = ctx.datasets["google"].dataset_id
    metrics_row = ctx.freshness_for(stale_dataset, google)
    assert metrics_row is not None
    assert metrics_row.last_success_at is not None
    assert ctx.now - metrics_row.last_success_at > stale_after
    entities_row = ctx.freshness_for("google.entities", google)
    assert entities_row is not None
    assert entities_row.last_success_at is not None
    assert ctx.now - entities_row.last_success_at <= stale_after

    out = _precede(ctx)
    assert _candidates(out, KEYWORDS, K1)
    readers = 0
    for candidate in _candidates(out):
        message = _flag_message(candidate, FLAG_STALE)
        if stale_dataset in candidate.required_datasets:
            readers += 1
            assert message is not None, candidate.entity_ref.key
            assert message.startswith("Data as of "), message
        else:
            assert message is None, candidate.entity_ref.key
    assert readers >= 1
    for candidate in _candidates(out, KEYWORDS, K1):
        assert set(scenario.expect.flags) <= set(candidate.flag_codes)


def test_p4_staleness_uses_the_configured_hours_the_window_end_and_own_datasets(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """P4: "more than ``sources.stale_after_hours`` ago" or coverage short of the window end.

    On the K1 world, whose K1 reads ``google.search_terms`` and ``google.metrics_daily`` and
    whose B3 reads ``google.entities`` and ``google.metrics_daily``:

    * search terms last succeeding one hour beyond the limit make K1 stale and leave B3
      fresh, because B3 does not read them;
    * exactly at the limit nothing is stale ("more than", not "at least");
    * metrics covering through the day before the window end make both stale, with the
      shortfall named;
    * with ``sources.stale_after_hours`` raised by 12, search terms one hour beyond the
      default limit are fresh again.

    In every case each item's frozen evidence records the configured hours it was judged
    against (24 by default, the raised value under the edited configuration) and the
    ``window.trusted_min_days`` (14), stale or not (AAP 0.5.4.8 "the thresholds applied").
    """
    hours = make_context("p4.stale.case").thresholds.int(STALE_AFTER_KEY)
    assert hours == 24
    cases: tuple[tuple[dict[str, FreshnessOverride], set[str]], ...] = (
        (
            {
                "google.search_terms": FreshnessOverride(
                    "complete", last_success_offset_hours=-(hours + 1)
                )
            },
            {K1},
        ),
        (
            {
                "google.search_terms": FreshnessOverride(
                    "complete", last_success_offset_hours=-hours
                )
            },
            set(),
        ),
        (
            {"google.metrics_daily": FreshnessOverride("complete", covered_through_offset=-2)},
            {K1, B3},
        ),
    )
    for overrides, expected in cases:
        ctx = make_context(_variant("p4.stale.case", freshness=overrides))
        out = _precede(ctx)
        rules = {candidate.rule_id for candidate in _candidates(out)}
        assert {K1, B3} <= rules, overrides
        stale = {c.rule_id for c in _candidates(out) if c.has_flag(FLAG_STALE)}
        assert stale == expected, overrides
        assert ctx.window.trusted_min_days == 14
        _assert_policy_thresholds_frozen(ctx, out, stale_after_hours=hours, trusted_min_days=14)
        if "google.metrics_daily" in overrides:
            for candidate in _candidates(out):
                message = _flag_message(candidate, FLAG_STALE)
                assert message is not None
                end = ctx.window.end - timedelta(days=1)
                assert f"google.metrics_daily covers through {end.isoformat()}" in message

    raised = make_thresholds({STALE_AFTER_KEY: hours + 12})
    assert raised.int(STALE_AFTER_KEY) == hours + 12
    late = FreshnessOverride("complete", last_success_offset_hours=-(hours + 1))
    world = _variant("p4.stale.case", freshness={"google.search_terms": late})
    ctx = make_context(world, thresholds=raised)
    out = _precede(ctx)
    assert {K1, B3} <= {candidate.rule_id for candidate in _candidates(out)}
    assert not any(candidate.has_flag(FLAG_STALE) for candidate in _candidates(out))
    _assert_policy_thresholds_frozen(ctx, out, stale_after_hours=hours + 12, trusted_min_days=14)


def test_p4_an_incomplete_required_dataset_flags_exactly_the_items_that_read_it(
    make_context: ContextFactory,
) -> None:
    """P4: ``incomplete`` when a required dataset is not complete over the window.

    ``p4.incomplete`` marks ``google.search_terms`` incomplete ("result limit reached at
    finest partition"): K1, which reads it, carries ``incomplete`` naming the dataset and its
    reason, and Keywords records an ``incomplete_dataset`` finding; B3, which does not read
    it, carries none. The twin with the dataset complete flags nothing incomplete.
    """
    scenario = _scenario("p4.incomplete.case")
    dataset = str(scenario.expect.values["incomplete_dataset"])
    out = _precede(make_context(scenario))
    assert _candidates(out, BUDGET, B3)
    for candidate in _candidates(out):
        message = _flag_message(candidate, FLAG_INCOMPLETE)
        if dataset in candidate.required_datasets:
            assert message is not None, candidate.entity_ref.key
            assert dataset in message
            assert "result limit reached at finest partition" in message
        else:
            assert message is None, candidate.entity_ref.key
    assert _candidates(out, KEYWORDS, K1)
    findings = [f for f in out.findings[KEYWORDS] if f.code == FINDING_INCOMPLETE_DATASET]
    assert [f.evidence["dataset"] for f in findings] == [dataset]

    complete = _variant("p4.incomplete.case", freshness={dataset: FreshnessOverride("complete")})
    out = _precede(make_context(complete))
    assert _candidates(out, KEYWORDS, K1)
    assert not any(c.has_flag(FLAG_INCOMPLETE) for c in _candidates(out))
    assert all(
        f.code != FINDING_INCOMPLETE_DATASET
        for section in SECTION_ORDER
        for f in out.findings[section]
    )


def test_p4_a_live_item_reading_a_stale_dataset_has_data_status_stale(
    make_context: ContextFactory,
) -> None:
    """P4 with P3: a live item becomes ``stale`` when a dataset it reads is stale.

    The all-live LinkedIn and HubSpot world: C1b is ``live`` while ``linkedin.leads`` is
    fresh, and ``stale`` with the ``stale`` flag once it last succeeded beyond the limit.
    """
    modes = {"linkedin": "live", "hubspot": "live"}
    fresh = _precede(make_context(_variant("p3.mixed.case", modes=modes)))
    assert [c.data_status for c in _candidates(fresh, CAMPAIGNS, C1B)] == [DATA_LIVE]
    hours = make_context("p3.mixed.case").thresholds.int("sources.stale_after_hours")
    late = FreshnessOverride("complete", last_success_offset_hours=-(hours + 1))
    stale_world = _variant("p3.mixed.case", modes=modes, freshness={"linkedin.leads": late})
    stale = _precede(make_context(stale_world))
    (c1b,) = _candidates(stale, CAMPAIGNS, C1B)
    assert c1b.data_status == DATA_STALE
    assert c1b.has_flag(FLAG_STALE)


async def test_p4_read_time_flags_follow_current_freshness_of_each_items_own_datasets(
    db: async_sessionmaker[AsyncSession],
    set_env: EnvSetter,
    make_world: WorldFactory,
    thresholds: Thresholds,
) -> None:
    """P4 at read time: current freshness flags exactly the items reading the dataset.

    After the run every dataset refreshes again, ``google.entities`` included, except
    ``google.metrics_daily``, whose sync stopped; ``google.search_terms`` refreshes
    incomplete. Read beyond ``sources.stale_after_hours`` of the metrics' last success, the K1
    card (search terms and metrics) is ``stale`` and ``incomplete``, while the LinkedIn C1b
    card, which reads neither, is neither: the fresh entity sync does not clear the stale
    metrics, and the page lists exactly the metrics stale and the search terms incomplete.
    The stored flags never change. Read an hour after the run, before any of this, nothing
    is stale or incomplete.
    """
    set_env(DATA_MODE="simulated")
    controller = await _bootstrap(make_world(_all_sources_scenario()), force_simulated=True)
    run = await _default_run()
    rows, early = await _overlay_at(db, run, READ_AT, thresholds)
    k1 = next(row for row in rows if row.rule_id == K1)
    c1b = next(row for row in rows if row.rule_id == C1B)
    assert not early.overlay.stale_datasets
    assert not early.overlay.incomplete_datasets
    for row in (k1, c1b):
        codes = {flag.code for flag in early.items[row.id].flags_now}
        assert not codes & {FLAG_STALE, FLAG_INCOMPLETE}, row.rule_id

    stale_after = timedelta(hours=thresholds.int("sources.stale_after_hours"))
    later = FIXED_NOW + stale_after + timedelta(hours=1)
    refreshed_at = later - timedelta(hours=1)
    active = await controller.active_datasets()
    dataset_ids = {ref.dataset_id for ref in active.values() if ref is not None}
    dataset_ids.update(ref.companion_id for ref in active.values() if ref and ref.companion_id)
    # Every dataset refreshes again shortly before the read except Google's daily metrics,
    # whose sync stopped; the search-terms refresh hit the result limit.
    async with db() as session:
        for dataset_id in sorted(dataset_ids):
            rows_now = await freshness.load_freshness(session, dataset_id)
            for name, current in sorted(rows_now.items()):
                if name == "google.metrics_daily":
                    continue
                incomplete = name == "google.search_terms"
                await freshness.record_freshness(
                    session,
                    dataset_id,
                    name,
                    freshness.FreshnessOutcome.success(
                        refreshed_at,
                        covered_from=current.covered_from,
                        covered_through=current.covered_through,
                        complete=not incomplete,
                        reason="result limit reached at finest partition" if incomplete else None,
                    ),
                )
        await session.commit()

    rows, late = await _overlay_at(db, run, later, thresholds)
    k1_now = {flag.code: flag.message for flag in late.items[k1.id].flags_now}
    assert FLAG_STALE in k1_now
    assert k1_now[FLAG_STALE].startswith("Data as of ")
    assert "google.search_terms" in k1_now[FLAG_INCOMPLETE]
    c1b_now = {flag.code for flag in late.items[c1b.id].flags_now}
    assert not c1b_now & {FLAG_STALE, FLAG_INCOMPLETE}
    assert late.items[k1.id].data_status_now == DATA_SIMULATED
    assert late.overlay.stale_datasets == ("google.metrics_daily",)
    assert late.overlay.incomplete_datasets == ("google.search_terms",)
    banners = {banner.code for banner in late.banners}
    assert {BANNER_STALE_DATASETS, BANNER_INCOMPLETE_DATASETS} <= banners
    assert [row.flags for row in rows] == [row.flags for row in await _stored(db, run.id)]
    assert FLAG_STALE not in k1.flags
    assert FLAG_INCOMPLETE not in k1.flags


# ---------------------------------------------------------------------------
# P5 short window and sustained minimum (AAP 0.5.4.1, 0.5.4.2 P5)
# ---------------------------------------------------------------------------


def test_p5_the_short_window_flag_uses_the_configured_trusted_minimum(
    make_context: ContextFactory, make_thresholds: ThresholdsFactory
) -> None:
    """P5: with ``window.trusted_min_days: 21`` a 20-day window is short and a 21-day one is not.

    Every item of the 20-day run carries ``short_window`` with the configured values in its
    message, never literals; the 21-day twin under the same configuration flags nothing. Both
    runs freeze the applied minimum, 21, into every item's evidence, short or not, beside the
    default ``sources.stale_after_hours`` (24) of P4 (AAP 0.5.4.8 "the thresholds applied").
    """
    scenario = _scenario("p5.configured_min.case")
    values = scenario.expect.values
    config = make_thresholds({"window.trusted_min_days": 21})
    assert config.trusted_min_days == values["trusted_min_days"] == 21

    short = make_context(scenario, thresholds=config)
    assert short.window.days == values["window_days"] == 20
    assert short.window.short_window
    out = _precede(short)
    assert _candidates(out, KEYWORDS, K1)
    for item in out.all_items():
        assert _flag_message(item.candidate, FLAG_SHORT_WINDOW) == (
            "Window of 20 days is below the 21-day trusted minimum"
        )
    assert config.int(STALE_AFTER_KEY) == 24
    _assert_policy_thresholds_frozen(short, out, stale_after_hours=24, trusted_min_days=21)

    trusted = make_context(_variant(scenario.name, window=(-21, -1)), thresholds=config)
    assert trusted.window.days == 21
    assert not trusted.window.short_window
    out = _precede(trusted)
    assert _candidates(out, KEYWORDS, K1)
    assert not any(item.candidate.has_flag(FLAG_SHORT_WINDOW) for item in out.all_items())
    _assert_policy_thresholds_frozen(trusted, out, stale_after_hours=24, trusted_min_days=21)


async def test_p5_the_short_window_banner_uses_the_configured_trusted_minimum(
    db: async_sessionmaker[AsyncSession],
    set_env: EnvSetter,
    make_world: WorldFactory,
    make_thresholds: ThresholdsFactory,
) -> None:
    """P5 banner: a stored 20-day run under ``trusted_min_days: 21`` shows the short-window banner.

    The banner is built at read time from the run and the configured minimum, never a
    literal: "Window of 20 days is below the 21-day trusted minimum", and every stored item of
    that run carries the ``short_window`` flag. The 21-day run under the same configuration
    has neither banner nor flag. Both runs store the applied minimum, 21, and P4's
    ``sources.stale_after_hours`` (24) in every item's frozen evidence.
    """
    scenario = _scenario("p5.configured_min.case")
    config = make_thresholds({"window.trusted_min_days": 21})
    set_env(DATA_MODE="simulated")
    await _bootstrap(make_world(scenario), force_simulated=True)
    for days, short in ((20, True), (21, False)):
        run = await _custom_run(days, thresholds=config)
        rows, result = await _overlay_at(db, run, READ_AT, config)
        assert [row.rule_id for row in rows if row.rule_id == K1], days
        for row in rows:
            stored = row.evidence["thresholds"]
            assert stored[TRUSTED_MIN_DAYS_KEY] == config.trusted_min_days == 21, row.rule_id
            assert stored[STALE_AFTER_KEY] == config.int(STALE_AFTER_KEY) == 24, row.rule_id
        banner = next((b for b in result.banners if b.code == BANNER_SHORT_WINDOW), None)
        flagged = [FLAG_SHORT_WINDOW in row.flags for row in rows]
        if short:
            assert banner is not None
            assert banner.message == (
                f"Window of {days} days is below the {config.trusted_min_days}-day trusted minimum"
            )
            assert all(flagged)
        else:
            assert banner is None
            assert not any(flagged)


def test_p5_sustained_rules_evaluate_at_the_sustained_minimum_and_are_withheld_below(
    make_context: ContextFactory,
) -> None:
    """P5: C2 evaluates on a 7-day window and is withheld on a 6-day one, with the days shown.

    ``p5.sustained_min.fires`` caps the budget on each of 7 days: C2 fires (with the
    short-window flag, 7 being below the trusted minimum). The 6-day twin withholds it with
    "window 6 days of 7" and the observed and required days.
    """
    fires = _scenario("p5.sustained_min.fires")
    twin = _scenario("p5.sustained_min.does_not_fire")
    ctx = make_context(fires)
    sustained = ctx.thresholds.int("window.sustained_min_days")
    assert ctx.window.days == fires.expect.values["window_days"] == sustained
    out = _precede(ctx)
    scale = _candidates(out, CAMPAIGNS, C2)
    assert scale
    assert all(set(fires.expect.flags) <= set(c.flag_codes) for c in scale)
    assert not _window_lines(out, C2)

    short = make_context(twin)
    assert short.window.days == twin.expect.values["window_days"] == sustained - 1
    out = _precede(short)
    assert not _candidates(out, CAMPAIGNS, C2)
    lines = _window_lines(out, C2)
    assert lines
    assert twin.expect.withheld_reason is not None
    for line in lines:
        assert line.reason.startswith(twin.expect.withheld_reason)
        assert (line.observed, line.required, line.unit) == (
            sustained - 1,
            twin.expect.values["required"],
            "days",
        )


def test_p5_every_sustained_rule_is_gated_by_the_sustained_minimum(
    make_context: ContextFactory,
) -> None:
    """P5: K3, C1, C1b, C2/B1, B2, B3 and B4 are all withheld below the sustained minimum.

    On the default world with a window one day below ``window.sustained_min_days``, each
    sustained rule lists a withheld "window N days of M" line in days, with M at least the
    sustained minimum, and emits no item. At the minimum the rules whose window minimum is
    the sustained one (C2/B1, B2, B3, B4) are evaluated, so no window line remains for them;
    K3, C1 and C1b stay withheld only by their own longer minimums.
    """
    sustained = make_context("p3.simulated.case").thresholds.int("window.sustained_min_days")

    below = make_context(
        _variant("default", window=(-(sustained - 1), -1), kind="custom"), now=FIXED_NOW
    )
    assert below.window.days == sustained - 1
    out = _precede(below)
    for rule_id in SUSTAINED_RULES:
        lines = _window_lines(out, rule_id)
        assert lines, rule_id
        for line in lines:
            assert line.observed == sustained - 1, rule_id
            assert line.required is not None
            assert line.required >= sustained, rule_id
            assert line.reason.startswith(f"window {sustained - 1} days of {line.required}")
        assert not _candidates(out, rule_id=rule_id), rule_id

    at = make_context(_variant("default", window=(-sustained, -1), kind="custom"), now=FIXED_NOW)
    assert at.window.days == sustained
    out = _precede(at)
    for rule_id in SUSTAINED_ONLY_RULES:
        assert not _window_lines(out, rule_id), rule_id
    assert {C2, B1, B2, B3} <= {c.rule_id for c in _candidates(out)}
    for rule_id in (K3, C1, C1B):
        for line in _window_lines(out, rule_id):
            assert line.observed == sustained
            assert line.required is not None
            assert line.required > sustained, rule_id


# ---------------------------------------------------------------------------
# P6 sample-size withholding, P7 no premature winners, P8 floors (AAP 0.5.4.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["p6.withheld_sizes.case", "p5.sustained_min.does_not_fire", "default"]
)
def test_p6_withheld_patterns_carry_their_sample_and_never_become_items(
    name: str, make_context: ContextFactory
) -> None:
    """P6: every withheld line carries ``sample.observed``, ``sample.required`` and ``unit``.

    Lines withheld for sample size state both sizes; lines withheld for validity alone (a
    segment window that does not exist, an unavailable pipeline) state neither, never one
    without the other. No withheld pattern is also an item. In ``p6.withheld_sizes`` the K1
    term with 60 of the 61 required clicks is withheld with exactly those numbers.
    """
    scenario = SCENARIOS[name]
    out = _precede(make_context(scenario, now=FIXED_NOW))
    items = {(c.rule_id, c.entity_ref.key) for c in _candidates(out)}
    lines = [line for section in SECTION_ORDER for line in out.withheld[section]]
    assert lines
    for section in SECTION_ORDER:
        for line in out.withheld[section]:
            assert line.section == section
            sample = line.to_json()["sample"]
            assert set(sample) == {"observed", "required", "unit"}
            assert sample["unit"].strip()
            assert (sample["observed"] is None) == (sample["required"] is None), line.reason
            if line.entity is not None:
                assert (line.rule_id, line.entity.key) not in items, line.reason

    if name == "p6.withheld_sizes.case":
        values = scenario.expect.values
        assert not _candidates(out, KEYWORDS, K1)
        sized = [line for line in _withheld_lines(out, K1) if line.entity is not None]
        assert [(line.observed, line.required, line.unit) for line in sized] == [
            (values["observed"], values["required"], values["unit"])
        ]
        assert scenario.expect.withheld_reason is not None
        assert sized[0].reason.startswith(scenario.expect.withheld_reason)


def test_p7_a_pair_that_is_not_yet_distinguishable_names_no_winner(
    make_context: ContextFactory,
) -> None:
    """P7: a CR1 pair that CR1 has not called distinguishable carries the flag and no winner.

    ``p7.no_winner_wording`` compares two variants 1.00% against 0.90% on 20,000 impressions
    each: not distinguishable. The pair carries ``not_distinguishable`` and neither its action
    nor its reason (variant and campaign names removed) uses winner wording.
    """
    scenario = _scenario("p7.no_winner_wording.case")
    assert scenario.expect.values["winner_wording"] is False
    out = _precede(make_context(scenario))
    pairs = _candidates(out, CREATIVE, CR1)
    assert pairs
    for pair in pairs:
        assert pair.evidence.extra["distinguishable"] is False
        assert set(scenario.expect.flags) <= set(pair.flag_codes)
        assert pair.has_flag(FLAG_NOT_DISTINGUISHABLE)
        text = f"{pair.action} {pair.reason}"
        names = [pair.entity_ref.name, *pair.evidence.extra.get("names", ())]
        campaign = pair.evidence.extra.get("campaign")
        if isinstance(campaign, Mapping):
            names.append(str(campaign.get("name", "")))
        for name in sorted(filter(None, names), key=len, reverse=True):
            text = text.replace(name, " ")
        words = {word.strip(".,;:()!?").lower() for word in text.split()}
        calls = sorted(w for w in words if w in WINNER_WORDS or w.startswith("outperform"))
        assert not calls, text


@pytest.mark.parametrize(
    "name", ["wp.contact_floor.fires", "wp.creative_floor.case", "wp.orphan_share.case"]
)
def test_p8_attribution_floor_flags_pass_through_precedence_unchanged(
    name: str, make_context: ContextFactory
) -> None:
    """P8: the contact, creative and deal floor flags of white-paper rows survive precedence.

    Each floor flag the white-paper rules set, code and message, is still on the ranked row,
    and precedence adds no floor flag of its own.
    """
    scenario = SCENARIOS[name]
    ctx = make_context(scenario)

    def floors(candidates: Iterable[Candidate]) -> dict[str, list[tuple[str, str]]]:
        return {
            candidate.entity_ref.key: [
                (flag.code, flag.message) for flag in candidate.flags if flag.code in FLOOR_FLAGS
            ]
            for candidate in candidates
        }

    before = floors(white_papers.evaluate(ctx).candidates)
    after = floors(_candidates(_precede(ctx), WHITE_PAPERS))
    assert after == before
    present = {code for flags in after.values() for code, _ in flags}
    assert set(scenario.expect.flags) <= present


# ---------------------------------------------------------------------------
# Feedback suppression (AAP 0.5.4.2, 0.5.2 recommendation_feedback)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(FEEDBACK_DAYS))
def test_feedback_hides_its_rule_and_entity_until_it_expires(
    kind: FeedbackKind, make_context: ContextFactory
) -> None:
    """Feedback: dismiss (30 d), snooze (7 d) and done (30 d) hide the item while in force.

    The scenario's feedback was written at the anchor's midnight for ``feedback.{kind}_days``:
    the K1 card on that term is dropped and counted under its suppression reason. Feedback
    written exactly that many days before the run has expired and the card reappears; one
    written a second later is still in force.
    """
    scenario = _scenario(f"feedback.{kind}.case")
    ctx = make_context(scenario)
    days = ctx.thresholds.int(f"feedback.{kind}_days")
    assert days == FEEDBACK_DAYS[kind]
    (entry,) = ctx.feedback
    assert entry.kind == kind
    assert entry.until == ANCHOR_MIDNIGHT + timedelta(days=days)
    reason = FEEDBACK_SUPPRESSION[kind]

    def cards(out: PrecedenceOutput) -> list[Candidate]:
        return [c for c in _candidates(out, KEYWORDS, K1) if c.feedback_key == entry.key]

    hidden = _precede(ctx)
    assert not cards(hidden)
    counts = cast("Mapping[str, int]", scenario.expect.values["suppressed"])
    assert dict(hidden.suppressed[KEYWORDS]) == {reason: counts[kind]}

    written = ctx.now - timedelta(days=days)
    expired = replace(entry, until=written + timedelta(days=days))
    back = _precede(replace(ctx, feedback=(expired,), cache={}))
    assert len(cards(back)) == 1
    assert not any(back.suppressed[section] for section in SECTION_ORDER)

    in_force = replace(entry, until=written + timedelta(days=days, seconds=1))
    still = _precede(replace(ctx, feedback=(in_force,), cache={}))
    assert not cards(still)
    assert dict(still.suppressed[KEYWORDS]) == {reason: 1}


def test_feedback_never_hides_a_c0_discrepancy(make_context: ContextFactory) -> None:
    """Feedback: C0 is exempt; dismiss, snooze and done on a C0 item change nothing."""
    ctx = make_context(_scenario("p1.c0_exempt.case"))
    (c0,) = _candidates(_precede(ctx), CAMPAIGNS, C0)
    entries = tuple(
        FeedbackInfo(
            rule_id=C0,
            entity_type=c0.entity_ref.type,
            entity_key=c0.entity_ref.key,
            kind=kind,
            until=ctx.now + timedelta(days=days),
        )
        for kind, days in FEEDBACK_DAYS.items()
    )
    out = _precede(replace(ctx, feedback=entries, cache={}))
    first = out.items[CAMPAIGNS][0]
    assert (first.rank, first.candidate.rule_id) == (1, C0)
    assert first.candidate.entity_ref.key == c0.entity_ref.key
    assert not any(out.suppressed[section] for section in SECTION_ORDER)


@pytest.mark.parametrize("kind", list(FEEDBACK_DAYS))
async def test_feedback_counts_are_stored_with_the_next_run_and_served_from_storage(
    kind: FeedbackKind,
    db: async_sessionmaker[AsyncSession],
    set_env: EnvSetter,
    make_world: WorldFactory,
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    admin_user: AppUser,
    login: LoginFunction,
    thresholds: Thresholds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feedback: a POSTed dismiss, snooze or done suppresses until ``until``, served stored.

    One controlled clock drives the request, every run and the overlay: the API's
    ``get_now`` is fixed before logging in, so nothing depends on the host's date.
    ``POST /recommendations/{id}/feedback`` on the K1 card at :data:`READ_AT` records the
    item's rule and entity until exactly ``feedback.{kind}_days`` later. The next default run
    drops the card and stores the count in ``run_suppression`` for Keywords. Feedback is in
    force only while ``until`` is after the run or read instant: a run over the first run's
    window one second before ``until`` still drops and counts the card, and one at ``until``
    keeps it with no suppression row. ``GET /recommendations`` returns each run's stored
    count with every rule module, precedence and the runner disabled, so nothing is re-run
    on read; the first run's card shows ``hidden_by`` at once and one second before
    ``until``, and none at ``until``.
    """
    del admin_user  # Requested so the admin row exists before logging in.
    clock = [READ_AT]
    api_app.dependency_overrides[get_now] = lambda: clock[0]

    async def sign_in(at: datetime) -> None:
        """Move the request clock to ``at`` and log in afresh: a session lasts 12 h on it."""
        clock[0] = at
        api_client.cookies.clear()
        response = await login(api_client)
        assert response.status_code == 200, response.text

    async def window_run(base: AnalysisRun, now: datetime) -> AnalysisRun:
        """A custom run at ``now`` over exactly the window dates of ``base``."""
        window = await runner.custom_window(base.window_start, base.window_end, now=now)
        return await runner.run_analysis(window, "custom", "manual", now=now)

    async def suppressions(run: AnalysisRun) -> list[tuple[str, str, int]]:
        """The ``run_suppression`` rows stored for ``run``, in write order."""
        async with db() as session:
            rows = await session.scalars(
                select(RunSuppression)
                .where(RunSuppression.run_id == run.id)
                .order_by(RunSuppression.id)
            )
            return [(row.section, row.reason, row.count) for row in rows]

    await sign_in(READ_AT)
    api_client.headers[CSRF_HEADER] = CSRF_VALUE
    scenario = _scenario(f"feedback.{kind}.case")
    set_env(DATA_MODE="simulated")
    await _bootstrap(make_world(scenario), force_simulated=True)
    first = await _default_run()
    (card,) = _page_items(await _page(api_client, first.id), K1)
    (stored_card,) = [row for row in await _stored(db, first.id) if row.rule_id == K1]

    response = await api_client.post(
        f"/api/v1/recommendations/{card['id']}/feedback", json={"kind": kind}
    )
    assert response.status_code == 201, response.text
    feedback = response.json()
    assert (feedback["recommendation_id"], feedback["rule_id"], feedback["kind"]) == (
        card["id"],
        K1,
        kind,
    )
    days = timedelta(days=thresholds.int(f"feedback.{kind}_days"))
    assert days == timedelta(days=FEEDBACK_DAYS[kind])
    until = READ_AT + days
    created = datetime.fromisoformat(feedback["created_at"])
    assert created == READ_AT
    assert datetime.fromisoformat(feedback["until"]) == created + days == until
    async with db() as session:
        (stored_feedback,) = (await session.scalars(select(RecommendationFeedback))).all()
    assert (stored_feedback.rule_id, stored_feedback.entity_type, stored_feedback.entity_id) == (
        K1,
        stored_card.entity_type,
        stored_card.entity_id,
    )
    assert (stored_feedback.created_at, stored_feedback.until) == (READ_AT, until)

    second = await _default_run(READ_AT + timedelta(hours=1))
    in_force = await window_run(first, until - timedelta(seconds=1))
    expired = await window_run(first, until)
    reason = FEEDBACK_SUPPRESSION[kind]
    for run in (second, in_force):
        assert await suppressions(run) == [(KEYWORDS, reason, 1)], run.id
        assert not [row for row in await _stored(db, run.id) if row.rule_id == K1], run.id
    assert await suppressions(expired) == []
    (back,) = [row for row in await _stored(db, expired.id) if row.rule_id == K1]
    assert (back.entity_type, back.entity_id) == (stored_card.entity_type, stored_card.entity_id)

    _forbid_rule_evaluation(monkeypatch)
    page = await _page(api_client)
    assert page["run"]["id"] == second.id
    assert _page_section(page, KEYWORDS)["suppressed"] == {reason: 1}
    assert not _page_items(page, K1)
    for key in SECTION_ORDER:
        if key != KEYWORDS:
            assert _page_section(page, key)["suppressed"] == {}, key
    (old,) = _page_items(await _page(api_client, first.id), K1)
    assert old["current"]["hidden_by"] == kind

    for at, run, hidden_by in (
        (until - timedelta(seconds=1), in_force, kind),
        (until, expired, None),
    ):
        await sign_in(at)
        page = await _page(api_client, run.id)
        assert page["run"]["id"] == run.id
        keyword_items = _page_items(page, K1)
        if hidden_by is None:
            assert _page_section(page, KEYWORDS)["suppressed"] == {}
            (shown,) = keyword_items
            assert shown["id"] == back.id
            assert shown["current"]["hidden_by"] is None
        else:
            assert _page_section(page, KEYWORDS)["suppressed"] == {reason: 1}
            assert not keyword_items
        for key in SECTION_ORDER:
            if key != KEYWORDS:
                assert _page_section(page, key)["suppressed"] == {}, (at, key)
        (old,) = _page_items(await _page(api_client, first.id), K1)
        assert old["current"]["hidden_by"] == hidden_by, at


# ---------------------------------------------------------------------------
# Historical-only items (AAP 0.3.2 "Items from replaced data are historical-only")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Replaced:
    """A stored world whose simulated LinkedIn dataset was replaced by a live one.

    Attributes:
        default_run: The default run computed before the activation.
        custom_run: A custom run over the same window, computed before the activation.
        retired: The LinkedIn dataset and its companion CRM dataset the activation retired.
        live_dataset_id: The live LinkedIn dataset now active.
    """

    default_run: AnalysisRun
    custom_run: AnalysisRun
    retired: frozenset[int]
    live_dataset_id: int


async def _replace_linkedin(
    make_world: WorldFactory,
    live_credentials: CredentialSetter,
    mock_transport_for: TransportFactory,
    sessions: async_sessionmaker[AsyncSession],
) -> _Replaced:
    """Store the historical world, run, then activate a live LinkedIn dataset before any rerun.

    HubSpot runs live from the start (the simulator acting as the live portal), Google and
    LinkedIn simulated for lack of credentials, so LinkedIn-attributed CRM evidence reads the
    simulated companion. Both runs are stored; then LinkedIn credentials are added and the
    controller's first live build probes, backfills and activates a live LinkedIn dataset,
    retiring the simulated one with its companion in one transaction (AAP 0.3.3).
    """
    world = make_world(_all_sources_scenario())
    live_credentials("hubspot")
    await _bootstrap(
        world, force_simulated=False, transports={"hubspot": mock_transport_for("hubspot", world)}
    )
    default_run = await _default_run()
    # A custom run over exactly the default window: still a custom run (AAP 0.5.3 run kinds).
    custom_run = await _custom_run((default_run.window_end - default_run.window_start).days + 1)
    assert default_run.data_modes == {
        "google": "simulated",
        "linkedin": "simulated",
        "hubspot": "live",
    }
    old_linkedin = default_run.dataset_ids["linkedin"]
    companion = default_run.dataset_ids["hubspot_linkedin_effective"]
    assert companion != default_run.dataset_ids["hubspot"]

    live_credentials("linkedin")
    transports: dict[Source, httpx.AsyncBaseTransport] = {
        "hubspot": mock_transport_for("hubspot", world),
        "linkedin": mock_transport_for("linkedin", world),
    }
    # Credentials added to a simulated source: the controller probes the full LinkedIn
    # capability set and, every check passing, builds, backfills and activates a live dataset,
    # the same readiness path recovery takes (``recover`` serves only a source in automatic
    # fallback after a denial, which this source never was).
    outcome = await _controller(world, transports).ensure_dataset("linkedin")
    assert outcome is not None
    assert outcome.activated
    assert outcome.mode == "live"
    assert outcome.dataset_id is not None
    assert {old_linkedin, companion} <= set(outcome.retired)
    async with sessions() as session:
        states = {
            row.id: row.state
            for row in await session.scalars(
                select(SourceDataset).where(
                    SourceDataset.id.in_((old_linkedin, companion, outcome.dataset_id))
                )
            )
        }
    assert states == {old_linkedin: "retired", companion: "retired", outcome.dataset_id: "active"}
    return _Replaced(
        default_run=default_run,
        custom_run=custom_run,
        retired=frozenset({old_linkedin, companion}),
        live_dataset_id=outcome.dataset_id,
    )


async def test_historical_items_from_a_replaced_linkedin_dataset_are_held_google_stays_current(
    db: async_sessionmaker[AsyncSession],
    make_world: WorldFactory,
    live_credentials: CredentialSetter,
    mock_transport_for: TransportFactory,
    authed_client: httpx.AsyncClient,
) -> None:
    """Historical-only: after the activation and before the rerun, old LinkedIn items are held.

    The latest default run is still the one computed before the activation. Each of its
    items whose frozen provenance names the retired LinkedIn or companion dataset is
    ``historical`` and ``held_now`` with "Computed from data that has since been replaced";
    the stored rows are unchanged. Google-only items stay current, exactly as
    ``overlay.is_current`` judges their provenance against the current dataset ids.
    """
    scenario = _scenario("historical.activation.case")
    expected = scenario.expect.values
    replaced = await _replace_linkedin(make_world, live_credentials, mock_transport_for, db)
    run = replaced.default_run
    page = await _page(authed_client)
    assert page["run"]["id"] == run.id
    async with db() as session:
        current_ids = await overlay.current_dataset_ids(session)
    assert replaced.live_dataset_id in current_ids
    assert not replaced.retired & current_ids

    items = _page_items(page)
    assert {item["rule_id"] for item in items} >= {scenario.expect.rule_id, K1}
    linkedin_items = [item for item in items if "linkedin" in _provenance_sources(item)]
    google_items = [item for item in items if _provenance_sources(item) == {"google"}]
    assert any(item["rule_id"] == C1B for item in linkedin_items)
    assert google_items
    for item in linkedin_items:
        current = item["current"]
        assert current["historical"] is expected["historical"], item["rule_id"]
        assert current["held_now"] is expected["held_now"], item["rule_id"]
        assert current["held_reason_now"] == expected["held_reason_now"] == HISTORICAL_REASON
        assert item["held"] is False
        assert not overlay.is_current(item["evidence"]["provenance"], current_ids)
    for item in google_items:
        assert item["current"]["historical"] is False, item["rule_id"]
        assert item["current"]["held_now"] is False, item["rule_id"]
        assert overlay.is_current(item["evidence"]["provenance"], current_ids)
    assert all(not row.held for row in await _stored(db, run.id))


async def test_historical_an_older_custom_run_stays_historical_indefinitely(
    db: async_sessionmaker[AsyncSession],
    make_world: WorldFactory,
    live_credentials: CredentialSetter,
    mock_transport_for: TransportFactory,
    authed_client: httpx.AsyncClient,
    thresholds: Thresholds,
) -> None:
    """Historical-only: a custom run from the replaced dataset stays historical after reruns.

    Once the queued default rerun completes on the live LinkedIn dataset, its own items are
    all current; the custom run computed before the activation keeps its LinkedIn items
    historical, through the API and through the overlay read a year later.
    """
    scenario = _scenario("historical.custom_run.case")
    replaced = await _replace_linkedin(make_world, live_credentials, mock_transport_for, db)
    custom = replaced.custom_run
    assert custom.kind == scenario.expect.values["run_kind"] == "custom"

    rerun = await _default_run()
    latest = await _page(authed_client)
    assert latest["run"]["id"] == rerun.id
    assert rerun.dataset_ids["linkedin"] == replaced.live_dataset_id
    assert _page_items(latest)
    assert all(item["current"]["historical"] is False for item in _page_items(latest))

    page = await _page(authed_client, custom.id)
    assert page["run"]["kind"] == "custom"
    linkedin_items = [i for i in _page_items(page) if "linkedin" in _provenance_sources(i)]
    assert any(item["rule_id"] == scenario.expect.rule_id for item in linkedin_items)
    for item in linkedin_items:
        assert item["current"]["historical"] is scenario.expect.values["historical"]
        assert item["current"]["held_reason_now"] == HISTORICAL_REASON

    later = READ_AT + timedelta(days=365)
    rows, result = await _overlay_at(db, custom, later, thresholds)
    linkedin_rows = [
        row for row in rows if any(e["source"] == "linkedin" for e in row.evidence["provenance"])
    ]
    assert linkedin_rows
    for row in linkedin_rows:
        assert result.items[row.id].historical
        assert result.items[row.id].held_reason_now == HISTORICAL_REASON
        assert not result.items[row.id].actions_enabled


async def test_historical_feedback_on_a_historical_item_is_refused_with_409(
    db: async_sessionmaker[AsyncSession],
    make_world: WorldFactory,
    live_credentials: CredentialSetter,
    mock_transport_for: TransportFactory,
    authed_client: httpx.AsyncClient,
) -> None:
    """Historical-only: feedback on a historical item is 409 ``subject_retired``, nothing stored.

    Every action on a historical item is disabled; the API enforces it for dismiss, snooze
    and done alike. The current Google item of the same run still accepts feedback.
    """
    scenario = _scenario("historical.feedback_409.case")
    replaced = await _replace_linkedin(make_world, live_credentials, mock_transport_for, db)
    items = _page_items(await _page(authed_client, replaced.default_run.id))
    target = next(item for item in items if item["rule_id"] == scenario.expect.rule_id)
    assert target["current"]["historical"] is True
    for kind in FEEDBACK_DAYS:
        response = await authed_client.post(
            f"/api/v1/recommendations/{target['id']}/feedback", json={"kind": kind}
        )
        assert response.status_code == scenario.expect.values["feedback_status"] == 409, kind
        assert response.json()["error"]["code"] == scenario.expect.values["error"]
    async with db() as session:
        stored = await session.scalar(select(func.count()).select_from(RecommendationFeedback))
    assert stored == 0

    google = next(item for item in items if _provenance_sources(item) == {"google"})
    accepted = await authed_client.post(
        f"/api/v1/recommendations/{google['id']}/feedback", json={"kind": "snooze"}
    )
    assert accepted.status_code == 201, accepted.text


async def test_historical_provenance_stays_simulated_while_data_status_now_reports_live(
    db: async_sessionmaker[AsyncSession],
    make_world: WorldFactory,
    live_credentials: CredentialSetter,
    mock_transport_for: TransportFactory,
    authed_client: httpx.AsyncClient,
    thresholds: Thresholds,
) -> None:
    """Historical-only: frozen provenance keeps its simulated mode after LinkedIn goes live.

    The C1b card was computed from the simulated LinkedIn dataset and its simulated
    companion: its ``evidence.provenance`` still names both, simulated, and its stored
    ``data_status`` stays ``simulated``, while the overlay, read an hour after the sync,
    reports ``data_status_now`` ``live`` because LinkedIn and the CRM evidence now read live
    datasets.
    """
    scenario = _scenario("historical.provenance_frozen.case")
    expected = scenario.expect.values
    replaced = await _replace_linkedin(make_world, live_credentials, mock_transport_for, db)
    run = replaced.default_run

    (card,) = _page_items(await _page(authed_client, run.id), scenario.expect.rule_id)
    provenance = card["evidence"]["provenance"]
    assert {entry["dataset_id"] for entry in provenance} == replaced.retired
    assert {entry["mode"] for entry in provenance} == {expected["provenance_mode"]}
    assert card["data_status"] == DATA_SIMULATED

    rows, result = await _overlay_at(db, run, READ_AT, thresholds)
    (row,) = [row for row in rows if row.id == card["id"]]
    assert _frozen(row.evidence["provenance"]) == _frozen(provenance)
    assert row.data_status == DATA_SIMULATED
    view = result.items[row.id]
    assert view.historical
    assert view.data_status_now == expected["data_status_now"] == DATA_LIVE
    assert not any(flag.code == FLAG_SIMULATED for flag in view.flags_now)
    assert result.overlay.source_modes["linkedin"] == "live"


# ---------------------------------------------------------------------------
# Ranking (AAP 0.5.4.1 "Ranking ties", 0.5.4.2)
# ---------------------------------------------------------------------------


def test_ranking_breaks_ties_by_entity_name_then_key_whatever_the_input_order(
    make_context: ContextFactory,
) -> None:
    """Ranking: equal rank keys order by entity name, then key, for any input order.

    ``ranking.deterministic`` gives two K1 terms the same wasted spend: "free crm templates"
    ranks before "free crm tools". Applying precedence twice, with the rule results reversed
    or rotated and each result's candidates reversed, yields identical ranks in every
    section. Two candidates with the same rank key and the same name order by entity key.
    """
    scenario = _scenario("ranking.deterministic.case")
    ctx = make_context(scenario)
    results = _evaluate(ctx)
    out = precedence.apply_precedence(results, ctx)
    first, second = _candidates(out, KEYWORDS, K1)[:2]
    assert (first.entity_ref.name, second.entity_ref.name) == scenario.expect.values["order"]
    assert first.rank_key == second.rank_key
    assert first.evidence.metric("min_clicks").value == scenario.expect.values["n_star"]

    baseline = _ranking(out)
    assert _ranking(precedence.apply_precedence(results, ctx)) == baseline
    orders = [
        results[::-1],
        *(results[shift:] + results[:shift] for shift in range(1, len(results))),
    ]
    for order in orders:
        shuffled = [replace(result, candidates=result.candidates[::-1]) for result in order]
        assert _ranking(precedence.apply_precedence(shuffled, ctx)) == baseline

    raw = [c for c in keywords.evaluate(ctx).candidates if c.rule_id == K1]
    templates = next(c for c in raw if c.entity_ref.name == first.entity_ref.name)
    tools = next(c for c in raw if c.entity_ref.name == second.entity_ref.name)
    renamed = replace(tools, entity_ref=replace(tools.entity_ref, name=templates.entity_ref.name))
    expected_keys = sorted([templates.entity_ref.key, renamed.entity_ref.key])
    for pair in ((templates, renamed), (renamed, templates)):
        ranked = precedence.apply_precedence(RuleResult(candidates=pair), ctx)
        assert [c.entity_ref.key for c in _candidates(ranked, KEYWORDS)] == expected_keys
        assert [item.rank for item in ranked.items[KEYWORDS]] == [1, 2]
