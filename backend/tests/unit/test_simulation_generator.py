"""Behavioural tests of the seeded simulated world in ``ads_engine.simulation.generator``.

AAP 0.3.3 ("Contract fidelity", "Cross-source joins with simulation"), FR-11 and the NFR
"simulated data is seeded, so tests and demos reproduce exactly": the world is deterministic,
keeps the platform invariants its module docstring promises, reveals records only once their
instants have passed and validates every spec it is built from.

No database, network or wall clock: every world takes a fixed :class:`Clock`, explicit content
assets and an explicit HubSpot mapping, except where a test exercises the config-file path.
"""

import hashlib
import os
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from pydantic import AliasChoices

from ads_engine.analysis.thresholds import (
    CONFIG_FILES,
    CONTENT_ASSETS_FILE,
    ContentAsset,
    HubSpotMapping,
    StageBinding,
    Thresholds,
)
from ads_engine.settings import Settings
from ads_engine.simulation import (
    GOOGLE_AD_GROUP_IDS,
    GOOGLE_BUDGET_IDS,
    GOOGLE_CAMPAIGN_IDS,
    GOOGLE_CONVERSION_ACTION_IDS,
    GOOGLE_CRITERION_IDS,
    HUBSPOT_CONTACT_IDS,
    HUBSPOT_DEAL_IDS,
    HUBSPOT_HASHED_CONTACT_IDS,
    HUBSPOT_MEETING_IDS,
    HUBSPOT_STAGE_IDS,
    LINKEDIN_ACCOUNT_IDS,
    LINKEDIN_CAMPAIGN_GROUP_IDS,
    LINKEDIN_CAMPAIGN_IDS,
    LINKEDIN_CREATIVE_IDS,
    LINKEDIN_FORM_IDS,
    LINKEDIN_POST_IDS,
    SIM_GOOGLE_CUSTOMER_ID,
    is_simulated_email,
    is_simulated_id,
)
from ads_engine.simulation.generator import (
    DEFAULT_HISTORY_DAYS,
    DEFAULT_LIFECYCLE_STAGES,
    DEFAULT_SPEC,
    FAULT_KINDS,
    FAULT_SURFACES,
    LEADS_PER_CREATIVE_DAY,
    PERSON_CREATIVE_POSITIONS,
    SEGMENT_PIVOTS,
    BulkDealSpec,
    ContactRecord,
    DailyProfile,
    DayMetrics,
    DealRecord,
    ExplicitDays,
    Fault,
    FormVersionSpec,
    FunnelSpec,
    GoogleAdGroupSpec,
    GoogleBudgetSpec,
    GoogleBulkSpec,
    GoogleCampaignDay,
    GoogleCampaignSpec,
    GoogleConversionActionSpec,
    GoogleCustomerRecord,
    GoogleKeywordSpec,
    GoogleSearchTermSpec,
    GoogleSpec,
    HubSpotSpec,
    LinkedInAccountSpec,
    LinkedInCampaignGroupSpec,
    LinkedInCampaignSpec,
    LinkedInCreativeSpec,
    LinkedInLeadFormSpec,
    LinkedInLeadResponse,
    LinkedInSegmentSpec,
    LinkedInSpec,
    MeetingRecord,
    PlatformChange,
    QualityScoreProfile,
    QuestionSpec,
    SearchFilter,
    SegmentValueSpec,
    SimulatedWorld,
    StageSpec,
    WorldSpec,
    person_contact_id,
    person_object_id,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

ANCHOR = date(2026, 10, 2)
# 12:00 UTC is 08:00 in America/New_York: a third of the Google day and half of the LinkedIn
# (UTC) day have elapsed.
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SPEC = DEFAULT_SPEC.with_(anchor=ANCHOR)
NA, EMEA = 990_000_001, 990_000_002
PIPELINE_FORM, EMEA_FORM, BENCHMARKS_FORM, PLAYBOOK_FORM = (
    990_000_101,
    990_000_102,
    990_000_201,
    990_000_301,
)
LANDING_CREATIVE = 990_001_301
MQL_STAGE = str(HUBSPOT_STAGE_IDS.at(0))
QL_STAGE = str(HUBSPOT_STAGE_IDS.at(1))
CENT = Decimal("0.01")

# The registry and mapping of the shipped config files, passed explicitly so the world under
# test never depends on the working directory.
CONTENT_ASSETS = (
    ContentAsset(
        "b2b-pipeline-report-2026",
        "The 2026 B2B Pipeline Report",
        ("urn:li:leadGenForm:990000101", "urn:li:leadGenForm:990000102"),
    ),
    ContentAsset(
        "paid-media-benchmarks-2026",
        "Paid Media Benchmarks 2026",
        ("urn:li:leadGenForm:990000201",),
    ),
    ContentAsset(
        "attribution-playbook",
        "The Attribution Playbook",
        ("urn:li:leadGenForm:990000301",),
        ("urn:li:sponsoredCreative:990001301",),
    ),
)
MAPPING = HubSpotMapping(
    mql=StageBinding("stage_label", ("First meeting booked",), None),
    ql=StageBinding("stage_label", ("Qualified Lead", "Sales Qualified Lead"), None),
    booking_sources=("MEETINGS_PUBLIC",),
    meeting_outcome_exclusions=("CANCELED",),
)


@dataclass
class Clock:
    """A settable clock: the only source of the current instant a world reads."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True)
class NamedScenario:
    """A minimal ``ScenarioLike``: a name and the spec it runs on."""

    name: str
    spec: WorldSpec


def build(
    spec: WorldSpec = SPEC,
    *,
    seed: int = 42,
    now: datetime = NOW,
    mapping: HubSpotMapping = MAPPING,
    content_assets: Sequence[ContentAsset] = CONTENT_ASSETS,
    google_crm_properties: tuple[str, str] | None = None,
) -> SimulatedWorld:
    """Build a world with a fixed clock and the explicit registry and mapping."""
    return SimulatedWorld(
        seed,
        spec,
        clock=Clock(now),
        content_assets=content_assets,
        mapping=mapping,
        google_crm_properties=google_crm_properties,
    )


def ms(instant: datetime) -> int:
    """Epoch milliseconds of an aware instant, as the LinkedIn lead finder takes them."""
    return (instant - EPOCH) // timedelta(milliseconds=1)


def midnight(day: date) -> datetime:
    """The UTC instant at which ``day`` starts."""
    return datetime.combine(day, time(0), tzinfo=UTC)


def sha256(text: str) -> str:
    """The SHA-256 hex digest lead ingestion stores for a normalised email."""
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()


def google_part(spec: WorldSpec = SPEC) -> GoogleSpec:
    """The Google part of a spec that has one."""
    assert spec.google is not None
    return spec.google


def linkedin_part(spec: WorldSpec = SPEC) -> LinkedInSpec:
    """The LinkedIn part of a spec that has one."""
    assert spec.linkedin is not None
    return spec.linkedin


def hubspot_part(spec: WorldSpec = SPEC) -> HubSpotSpec:
    """The HubSpot part of a spec that has one."""
    assert spec.hubspot is not None
    return spec.hubspot


def campaign_spec(key: str) -> GoogleCampaignSpec:
    """The default spec's Google campaign ``key``."""
    return next(campaign for campaign in google_part().campaigns if campaign.key == key)


def daily_profile(profile: object) -> DailyProfile:
    """Narrow a spec profile to the :class:`DailyProfile` the default spec uses."""
    assert isinstance(profile, DailyProfile)
    return profile


def google_ids(world: SimulatedWorld) -> dict[str, int]:
    """Google campaign id by spec key."""
    return {campaign.key: campaign.id for campaign in world.google_campaigns()}


def linkedin_ids(world: SimulatedWorld, account: int = NA) -> dict[str, int]:
    """LinkedIn campaign id by spec key, for one account."""
    return {campaign.key: campaign.id for campaign in world.linkedin_campaigns(account)}


def all_responses(world: SimulatedWorld, account: int) -> tuple[LinkedInLeadResponse, ...]:
    """Every lead response of an account submitted over the world's history, by now."""
    return world.linkedin_lead_responses(
        account, ms(midnight(world.history_start)), ms(world.now() + timedelta(days=1))
    )


def full_days(days: int) -> tuple[date, date]:
    """The ``days`` complete native days before the anchor (the anchor itself is partial)."""
    return ANCHOR - timedelta(days=days), ANCHOR - timedelta(days=1)


@pytest.fixture(scope="module")
def world() -> SimulatedWorld:
    """The default world at :data:`NOW`, shared by every read-only test of this module."""
    return build()


# ---------------------------------------------------------------------------
# Construction, identity and invariants
# ---------------------------------------------------------------------------


def test_default_world_satisfies_every_invariant_it_checks(world: SimulatedWorld) -> None:
    assert world.self_check() == ()
    assert world.self_check(days=120) == ()


def test_self_check_reports_a_day_spending_over_twice_its_daily_budget() -> None:
    # Explicit days are authoritative and never capped, so a scenario can break the rule.
    overspent = ExplicitDays({-2: DayMetrics(cost=Decimal("400.00"), impressions=2000, clicks=100)})
    spec = SPEC.with_(
        google=google_part().with_campaign("g_competitor", status="PAUSED", profile=overspent)
    )
    overspending = build(spec)
    competitor = google_ids(overspending)["g_competitor"]
    assert overspending.self_check() == (
        f"Google campaign {competitor} on {ANCHOR - timedelta(days=2)}: spend 400.00 exceeds "
        "twice its daily budget 150",
    )


def test_self_check_needs_a_positive_window(world: SimulatedWorld) -> None:
    with pytest.raises(ValueError, match="days must be >= 1"):
        world.self_check(days=0)


def test_identity_comes_from_the_seed_spec_and_clock(world: SimulatedWorld) -> None:
    assert world.seed == 42
    assert world.spec is SPEC
    assert world.name == "default"
    assert world.anchor == ANCHOR
    assert world.history_start == ANCHOR - timedelta(days=DEFAULT_HISTORY_DAYS)
    assert world.now() == NOW
    assert world.content_assets == CONTENT_ASSETS
    assert world.mapping is MAPPING
    assert world.google_crm_properties is None


def test_anchor_defaults_to_the_clock_utc_date() -> None:
    assert DEFAULT_SPEC.anchor is None
    # 22:30 in New York on 2 October is already 3 October in UTC.
    evening = datetime(2026, 10, 2, 22, 30, tzinfo=ZoneInfo("America/New_York"))
    unanchored = build(DEFAULT_SPEC, now=evening)
    assert unanchored.anchor == date(2026, 10, 3)
    assert unanchored.now() == evening.astimezone(UTC)
    assert unanchored.now().tzinfo is UTC


def test_a_scenario_like_object_supplies_its_spec() -> None:
    scenario_world = SimulatedWorld(
        42,
        NamedScenario("first-start", SPEC),
        clock=Clock(NOW),
        content_assets=CONTENT_ASSETS,
        mapping=MAPPING,
    )
    assert scenario_world.spec is SPEC
    assert scenario_world.name == "default"


def test_a_scenario_without_a_world_spec_is_rejected() -> None:
    with pytest.raises(ValueError, match="scenario must be a WorldSpec"):
        SimulatedWorld(42, NamedScenario("broken", cast(WorldSpec, "not a spec")))


@pytest.mark.parametrize("seed", ["42", True, 4.2])
def test_the_seed_must_be_an_integer(seed: object) -> None:
    with pytest.raises(ValueError, match="seed must be an integer"):
        build(seed=cast(int, seed))


def test_naive_instants_are_rejected(world: SimulatedWorld) -> None:
    naive_clock_world = build(now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match=re.escape("clock() must be timezone-aware")):
        naive_clock_world.now()
    with pytest.raises(ValueError, match="at must be timezone-aware"):
        world.google_campaigns(NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="state_at instant must be timezone-aware"):
        world.state_at(NOW.replace(tzinfo=None))


# ---------------------------------------------------------------------------
# Determinism (AAP NFR: simulated data is seeded)
# ---------------------------------------------------------------------------


def test_same_seed_spec_and_clock_reproduce_every_read() -> None:
    first, second = build(), build()
    start, end = ANCHOR - timedelta(days=28), ANCHOR
    assert first.google_campaign_daily(start, end) == second.google_campaign_daily(start, end)
    assert first.google_keyword_daily(start, end) == second.google_keyword_daily(start, end)
    assert first.google_search_term_daily(start, end) == second.google_search_term_daily(start, end)
    for account in (NA, EMEA):
        assert first.linkedin_creative_daily(account, start, end) == (
            second.linkedin_creative_daily(account, start, end)
        )
        assert all_responses(first, account) == all_responses(second, account)
        assert first.linkedin_segments(account, "MEMBER_SENIORITY", start, end) == (
            second.linkedin_segments(account, "MEMBER_SENIORITY", start, end)
        )
    for object_type in ("contacts", "meetings", "deals"):
        assert first.hubspot_search(object_type, ()) == second.hubspot_search(object_type, ())


def test_another_seed_changes_the_numbers_but_not_the_entities(world: SimulatedWorld) -> None:
    other = build(seed=43)
    start, end = full_days(28)
    assert other.google_campaigns() == world.google_campaigns()
    assert other.linkedin_creatives(NA) == world.linkedin_creatives(NA)
    assert other.google_campaign_daily(start, end) != world.google_campaign_daily(start, end)
    assert other.linkedin_creative_daily(NA, start, end) != world.linkedin_creative_daily(
        NA, start, end
    )
    assert {r.id for r in all_responses(other, NA)}.isdisjoint(
        {r.id for r in all_responses(world, NA)}
    )


def test_reads_do_not_depend_on_call_order_or_range_splits() -> None:
    forward, backward = build(), build()
    early = (ANCHOR - timedelta(days=90), ANCHOR - timedelta(days=60))
    late = (ANCHOR - timedelta(days=10), ANCHOR)
    forward_rows = (forward.google_campaign_daily(*early), forward.google_campaign_daily(*late))
    backward_late = backward.google_campaign_daily(*late)
    backward_early = backward.google_campaign_daily(*early)
    assert forward_rows == (backward_early, backward_late)

    whole = forward.google_keyword_daily(early[0], late[1])
    middle = ANCHOR - timedelta(days=30)
    assert whole == (
        forward.google_keyword_daily(early[0], middle)
        + forward.google_keyword_daily(middle + timedelta(days=1), late[1])
    )

    # People are assigned to lead responses chronologically, whichever window is read first.
    late_window = (ms(midnight(late[0])), ms(NOW))
    early_window = (ms(midnight(early[0])), ms(midnight(early[1])))
    forward_leads = (
        forward.linkedin_lead_responses(NA, *early_window),
        forward.linkedin_lead_responses(NA, *late_window),
    )
    backward_late_leads = backward.linkedin_lead_responses(NA, *late_window)
    backward_early_leads = backward.linkedin_lead_responses(NA, *early_window)
    assert forward_leads == (backward_early_leads, backward_late_leads)


# ---------------------------------------------------------------------------
# Time: history, the partial current day and the future
# ---------------------------------------------------------------------------


def test_daily_reads_cover_only_days_that_exist(world: SimulatedWorld) -> None:
    rows = world.google_campaign_daily(ANCHOR - timedelta(days=3), ANCHOR + timedelta(days=5))
    assert max(row.date for row in rows) == ANCHOR
    assert world.google_campaign_daily(ANCHOR + timedelta(days=1), ANCHOR + timedelta(days=9)) == ()
    creative_days = world.linkedin_creative_daily(NA, ANCHOR, ANCHOR + timedelta(days=5))
    assert {row.date for row in creative_days} == {ANCHOR}

    history = world.google_campaign_daily(
        world.history_start - timedelta(days=30), world.history_start + timedelta(days=1)
    )
    assert min(row.date for row in history) == world.history_start
    assert (
        world.google_campaign_daily(
            world.history_start - timedelta(days=30), world.history_start - timedelta(days=1)
        )
        == ()
    )


def test_a_range_must_not_end_before_it_starts(world: SimulatedWorld) -> None:
    with pytest.raises(ValueError, match="is after end"):
        world.google_campaign_daily(ANCHOR, ANCHOR - timedelta(days=1))


def test_the_current_native_day_is_partial_in_proportion_to_elapsed_time(
    world: SimulatedWorld,
) -> None:
    tomorrow = build(now=NOW + timedelta(days=1))
    partial = {row.campaign_id: row for row in world.google_campaign_daily(ANCHOR, ANCHOR)}
    full = {row.campaign_id: row for row in tomorrow.google_campaign_daily(ANCHOR, ANCHOR)}
    assert partial.keys() <= full.keys()
    assert partial
    for campaign_id, row in partial.items():
        whole = full[campaign_id]
        # A third of the New York day has elapsed; partial values are floored.
        assert whole.cost / 3 - CENT <= row.cost <= whole.cost / 3
        assert abs(row.impressions - whole.impressions / 3) <= 1
        assert row.clicks <= whole.clicks
        assert row.cost_micros == int(row.cost * 1_000_000)
    yesterday = ANCHOR - timedelta(days=1)
    assert world.google_campaign_daily(yesterday, yesterday) == tomorrow.google_campaign_daily(
        yesterday, yesterday
    )

    linkedin_partial = world.linkedin_creative_daily(NA, ANCHOR, ANCHOR)
    linkedin_full = {
        row.creative_id: row for row in tomorrow.linkedin_creative_daily(NA, ANCHOR, ANCHOR)
    }
    for row in linkedin_partial:
        assert row.cost <= linkedin_full[row.creative_id].cost
        assert row.leads <= linkedin_full[row.creative_id].leads


# ---------------------------------------------------------------------------
# Google Ads
# ---------------------------------------------------------------------------


def test_google_customer_is_the_reserved_simulated_customer(world: SimulatedWorld) -> None:
    assert world.google_customer() == GoogleCustomerRecord(
        customer_id=SIM_GOOGLE_CUSTOMER_ID,
        descriptive_name="Simulated Ads - Google",
        currency_code="USD",
        time_zone="America/New_York",
    )
    assert world.google_customer_id == SIM_GOOGLE_CUSTOMER_ID
    assert is_simulated_id("google", world.google_customer_id)
    assert world.currency_for("google") == "USD"


def test_google_entities_carry_reserved_ids_and_every_status(world: SimulatedWorld) -> None:
    campaigns = world.google_campaigns()
    assert [campaign.key for campaign in campaigns] == [c.key for c in google_part().campaigns]
    assert [campaign.id for campaign in campaigns] == sorted(c.id for c in campaigns)
    assert {campaign.status for campaign in campaigns} == {"ENABLED", "PAUSED", "REMOVED"}
    by_key = {campaign.key: campaign for campaign in campaigns}
    for campaign in campaigns:
        assert GOOGLE_CAMPAIGN_IDS.contains(campaign.id)
        assert GOOGLE_BUDGET_IDS.contains(campaign.budget_id)
        assert is_simulated_id("google", campaign.id)
    ad_groups = world.google_ad_groups()
    assert len(ad_groups) == len(google_part().ad_groups)
    for group in ad_groups:
        assert GOOGLE_AD_GROUP_IDS.contains(group.id)
        assert group.campaign_id == by_key[group.campaign_key].id
    groups = {group.id: group for group in ad_groups}
    keywords = world.google_keywords()
    assert len(keywords) == len(google_part().keywords)
    for keyword in keywords:
        assert GOOGLE_CRITERION_IDS.contains(keyword.criterion_id)
        assert groups[keyword.ad_group_id].campaign_id == keyword.campaign_id
    actions = world.google_conversion_actions()
    assert all(GOOGLE_CONVERSION_ACTION_IDS.contains(action.id) for action in actions)
    assert {action.status for action in actions} == {"ENABLED", "REMOVED"}
    assert [(a.key, a.counting_type, a.include_in_conversions_metric) for a in actions] == [
        ("ca_demo", "ONE_PER_CLICK", True),
        ("ca_newsletter", "MANY_PER_CLICK", False),
        ("ca_legacy", "ONE_PER_CLICK", True),
    ]


def test_google_budgets_follow_their_period_and_membership(world: SimulatedWorld) -> None:
    budgets = {budget.key: budget for budget in world.google_budgets()}
    campaigns = {campaign.key: campaign for campaign in world.google_campaigns()}
    shared = budgets["b_shared"]
    assert shared.explicitly_shared
    assert shared.campaign_ids == tuple(
        sorted((campaigns["g_product_analytics"].id, campaigns["g_product_reporting"].id))
    )
    lifetime = budgets["b_webinar"]
    assert (lifetime.period, lifetime.amount, lifetime.amount_micros) == (
        "CUSTOM_PERIOD",
        None,
        None,
    )
    assert (lifetime.total_amount, lifetime.total_amount_micros) == (Decimal(9000), 9_000_000_000)
    daily = budgets["b_brand"]
    assert (daily.amount, daily.amount_micros, daily.total_amount) == (
        Decimal(200),
        200_000_000,
        None,
    )
    webinar = campaigns["g_webinar"]
    assert (webinar.budget_amount, webinar.budget_total_amount) == (None, Decimal(9000))
    members = Counter(member for budget in budgets.values() for member in budget.campaign_ids)
    assert members == Counter(campaign.id for campaign in campaigns.values())
    for campaign in campaigns.values():
        assert campaign.id in budgets[campaign.budget_key].campaign_ids
        assert campaign.budget_name == budgets[campaign.budget_key].name


def test_keyword_quality_score_follows_its_profile(world: SimulatedWorld) -> None:
    keywords = {keyword.key: keyword for keyword in world.google_keywords()}
    assert keywords["kw_mta"].quality_score == 5
    assert keywords["kw_brand"].quality_score == 9
    assert keywords["kw_attr_reporting"].quality_score is None
    before = {keyword.key: keyword for keyword in world.google_keywords(NOW - timedelta(days=20))}
    assert before["kw_mta"].quality_score == 7

    mta = keywords["kw_mta"].criterion_id
    rows = [row for row in world.google_keyword_daily(*full_days(40)) if row.criterion_id == mta]
    assert rows
    for row in rows:
        expected = 5 if (row.date - ANCHOR).days >= -14 else 7
        assert row.historical_quality_score == expected


def test_keyword_rows_report_cost_micros_and_average_cpc(world: SimulatedWorld) -> None:
    rows = world.google_keyword_daily(*full_days(28))
    assert rows
    for row in rows:
        assert row.cost_micros == int(row.cost * 1_000_000)
        assert row.clicks <= row.impressions
        if row.clicks:
            assert row.average_cpc_micros == row.cost_micros // row.clicks
            assert row.average_cpc == Decimal(row.cost_micros // row.clicks) / 1_000_000


def test_a_cpc_trend_raises_cost_per_click_from_its_offset(world: SimulatedWorld) -> None:
    keyword = next(k for k in world.google_keywords() if k.key == "kw_platform_cost")
    rows = [
        row
        for row in world.google_keyword_daily(ANCHOR - timedelta(days=50), ANCHOR)
        if row.criterion_id == keyword.criterion_id
    ]

    def cpc(first: int, last: int) -> float:
        window = [row for row in rows if first <= (row.date - ANCHOR).days <= last]
        return float(sum(row.cost for row in window)) / sum(row.clicks for row in window)

    # +4% of the base CPC per day from offset -30: flat before, about twice as high now.
    assert cpc(-10, -1) > 1.5 * cpc(-50, -31)


def test_google_spend_nests_search_terms_in_keywords_in_campaigns(world: SimulatedWorld) -> None:
    start, end = ANCHOR - timedelta(days=35), ANCHOR
    campaign_cost = {
        (row.date, row.campaign_id): row.cost for row in world.google_campaign_daily(start, end)
    }
    keyword_rows = world.google_keyword_daily(start, end)
    keyword_by_group: defaultdict[tuple[date, int], Decimal] = defaultdict(Decimal)
    keyword_by_campaign: defaultdict[tuple[date, int], Decimal] = defaultdict(Decimal)
    for row in keyword_rows:
        keyword_by_group[(row.date, row.ad_group_id)] += row.cost
        keyword_by_campaign[(row.date, row.campaign_id)] += row.cost
    term_rows = world.google_search_term_daily(start, end)
    term_by_group: defaultdict[tuple[date, int], Decimal] = defaultdict(Decimal)
    for row in term_rows:
        if row.criterion_id is not None:
            term_by_group[(row.date, row.ad_group_id)] += row.cost
    assert term_by_group
    for key, cost in term_by_group.items():
        assert cost <= keyword_by_group[key]
    for key, cost in keyword_by_campaign.items():
        assert cost <= campaign_cost[key]

    keyword_text = {keyword.criterion_id: keyword for keyword in world.google_keywords()}
    for row in term_rows:
        assert row.criterion_id is not None
        assert row.keyword_text == keyword_text[row.criterion_id].text
        assert row.match_type == keyword_text[row.criterion_id].match_type
        assert row.cost_micros == int(row.cost * 1_000_000)
    assert {row.status for row in term_rows} == {"NONE", "ADDED", "EXCLUDED", "ADDED_EXCLUDED"}


def test_conversions_are_whole_numbers_under_last_click_counting(world: SimulatedWorld) -> None:
    rows = world.google_campaign_daily(ANCHOR - timedelta(days=28), ANCHOR)
    assert sum(row.conversions for row in rows) > 0
    for row in rows:
        assert row.conversions == row.conversions.to_integral_value()
        assert row.clicks <= row.impressions


def test_budget_capped_campaigns_spend_their_budget_and_lose_impression_share(
    world: SimulatedWorld,
) -> None:
    campaigns = {campaign.id: campaign for campaign in world.google_campaigns()}
    rows = world.google_campaign_daily(*full_days(60))
    for key in ("g_brand", "g_retargeting"):
        capped = [row for row in rows if campaigns[row.campaign_id].key == key]
        budget = cast(Decimal, campaigns[capped[0].campaign_id].budget_amount)
        ratios = [row.cost / budget for row in capped]
        assert len(capped) == 60
        # AAP budget.capped_day_ratio (0.95) and the 10% budget-lost share both hold daily.
        assert min(ratios) >= Decimal("0.95")
        assert all(
            cast(Decimal, row.search_budget_lost_impression_share) >= Decimal("0.10")
            for row in capped
        )
        # Google never spends more than twice a daily budget; capped days centre on it.
        assert max(ratios) <= 2
        assert Decimal("0.975") <= statistics.mean(ratios) <= Decimal("1.015")
    generic = [row for row in rows if campaigns[row.campaign_id].key == "g_generic"]
    assert all(row.cost < Decimal("0.95") * 400 for row in generic)
    assert all(
        cast(Decimal, row.search_budget_lost_impression_share) < Decimal("0.10") for row in generic
    )
    display = [row for row in rows if campaigns[row.campaign_id].key == "g_display"]
    assert display
    assert all(row.search_budget_lost_impression_share is None for row in display)
    assert all(row.search_impression_share is None for row in display)
    shared_ids = {c.id for c in campaigns.values() if c.budget_explicitly_shared}
    shared_spend: defaultdict[date, Decimal] = defaultdict(Decimal)
    for row in rows:
        if row.campaign_id in shared_ids:
            shared_spend[row.date] += row.cost
    assert max(shared_spend.values()) <= Decimal(250)


def test_a_shared_budget_caps_its_members_jointly() -> None:
    tight = build(SPEC.with_(google=google_part().with_budget("b_shared", amount=Decimal(100))))
    campaigns = {campaign.id: campaign for campaign in tight.google_campaigns()}
    members = {c.id for c in campaigns.values() if c.budget_key == "b_shared"}
    spend: defaultdict[date, Decimal] = defaultdict(Decimal)
    for row in tight.google_campaign_daily(*full_days(60)):
        if row.campaign_id in members:
            spend[row.date] += row.cost
            assert cast(Decimal, row.search_budget_lost_impression_share) >= Decimal("0.10")
    utilisation = [total / 100 for total in spend.values()]
    assert len(utilisation) == 60
    assert min(utilisation) >= Decimal("0.95")
    assert Decimal("0.975") <= statistics.mean(utilisation) <= Decimal("1.015")
    assert tight.self_check() == ()


def test_id_range_filters_select_exactly_the_matching_rows(world: SimulatedWorld) -> None:
    start, end = full_days(14)
    ids = google_ids(world)
    brand = ids["g_brand"]
    every_row = world.google_campaign_daily(start, end)
    assert world.google_campaign_daily(start, end, campaign_ids=(brand, brand)) == tuple(
        row for row in every_row if row.campaign_id == brand
    )
    core = next(group.id for group in world.google_ad_groups() if group.key == "ag_core")
    keywords = world.google_keyword_daily(start, end)
    assert world.google_keyword_daily(start, end, ad_group_ids=(core, core)) == tuple(
        row for row in keywords if row.ad_group_id == core
    )
    generic = ids["g_generic"]
    terms = world.google_search_term_daily(start, end)
    assert world.google_search_term_daily(start, end, campaign_ids=(generic, generic)) == tuple(
        row for row in terms if row.campaign_id == generic
    )


def test_stopped_campaigns_deliver_only_until_their_profile_ends(world: SimulatedWorld) -> None:
    ids = google_ids(world)
    rows = world.google_campaign_daily(world.history_start, ANCHOR)
    last = {
        key: max(row.date for row in rows if row.campaign_id == ids[key])
        for key in ("g_seasonal", "g_legacy", "g_brand")
    }
    assert last == {
        "g_seasonal": ANCHOR - timedelta(days=45),
        "g_legacy": ANCHOR - timedelta(days=150),
        "g_brand": ANCHOR,
    }


def test_daily_profiles_shape_spend_days_and_weekend_outcomes(world: SimulatedWorld) -> None:
    start, end = full_days(100)
    rival = [
        row
        for row in world.google_search_term_daily(start, end)
        if row.search_term == "rival attribution suite alternative"
    ]
    # spend_day_share 0.85 through golden-ratio sampling: close to 85 of 100 days.
    assert 82 <= len(rival) <= 88

    brand = google_ids(world)["g_brand"]
    brand_rows = [
        row for row in world.google_campaign_daily(start, end) if row.campaign_id == brand
    ]

    def rate(rows: list[GoogleCampaignDay]) -> float:
        return float(sum(row.conversions for row in rows)) / sum(row.clicks for row in rows)

    weekend = [row for row in brand_rows if row.date.weekday() >= 5]
    weekday = [row for row in brand_rows if row.date.weekday() < 5]
    assert rate(weekend) < 0.75 * rate(weekday)

    profile = daily_profile(campaign_spec("g_display").profile)
    fractional = build(
        SPEC.with_(
            google=google_part().with_campaign(
                "g_display", profile=profile.with_(fractional_conversions=True)
            )
        )
    )
    display = google_ids(fractional)["g_display"]
    conversions = [
        row.conversions
        for row in fractional.google_campaign_daily(start, end)
        if row.campaign_id == display
    ]
    assert any(value != value.to_integral_value() for value in conversions)
    assert all(-cast(int, value.as_tuple().exponent) <= 6 for value in conversions)
    assert fractional.self_check() == ()


def test_conversion_action_models_change_at_the_start_of_their_day() -> None:
    changed = build(
        SPEC.with_(
            google=google_part().with_conversion_action(
                "ca_demo", model_changes=((-10, "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"),)
            )
        )
    )
    before = changed.google_conversion_actions(NOW - timedelta(days=20))[0]
    after = changed.google_conversion_actions()[0]
    assert (before.key, before.attribution_model) == ("ca_demo", "GOOGLE_ADS_LAST_CLICK")
    assert after.attribution_model == "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"


def test_explicit_days_are_delivered_exactly_and_never_capped() -> None:
    explicit = ExplicitDays(
        {
            -2: DayMetrics(cost=Decimal("10.00"), impressions=50, clicks=5),
            -3: DayMetrics(
                cost=Decimal("290.00"),
                impressions=1000,
                clicks=100,
                conversions=Decimal(4),
                conversions_value=Decimal("400.00"),
                budget_lost_is=Decimal("0.2500"),
                impression_share=Decimal("0.5000"),
            ),
        }
    )
    assert list(explicit.days) == [-3, -2]
    assert explicit.at(-1) == DayMetrics()
    paused = build(
        SPEC.with_(
            google=google_part().with_campaign("g_competitor", status="PAUSED", profile=explicit)
        )
    )
    competitor = google_ids(paused)["g_competitor"]
    rows = [
        row
        for row in paused.google_campaign_daily(ANCHOR - timedelta(days=30), ANCHOR)
        if row.campaign_id == competitor
    ]
    assert [(row.date, row.cost, row.clicks) for row in rows] == [
        (ANCHOR - timedelta(days=3), Decimal("290.00"), 100),
        (ANCHOR - timedelta(days=2), Decimal("10.00"), 5),
    ]
    big, small = rows
    # 290 is far above the 150 budget: explicit days are authoritative.
    assert (big.conversions, big.conversions_value) == (Decimal(4), Decimal("400.00"))
    assert (big.search_budget_lost_impression_share, big.search_impression_share) == (
        Decimal("0.2500"),
        Decimal("0.5000"),
    )
    assert cast(Decimal, small.search_budget_lost_impression_share) < Decimal("0.10")
    assert paused.self_check() == ()


def test_bulk_filler_extends_every_id_range_after_the_explicit_entities() -> None:
    bulk = GoogleBulkSpec(
        campaigns=3,
        ad_groups_per_campaign=2,
        keywords_in_one_ad_group=4,
        search_term_rows_per_day=6,
    )
    assert bulk.has_keyword_group
    filled = build(SPEC.with_google(bulk=bulk))
    explicit = len(google_part().campaigns)
    campaigns = filled.google_campaigns()
    assert len(campaigns) == explicit + 4
    extra = campaigns[explicit:]
    assert [c.id for c in extra] == [GOOGLE_CAMPAIGN_IDS.at(explicit + i) for i in range(4)]
    assert [c.key for c in extra] == [
        "bulk-campaign-0",
        "bulk-campaign-1",
        "bulk-campaign-2",
        "bulk-keywords",
    ]
    assert [c.budget_amount for c in extra] == [Decimal(10)] * 4
    assert len(filled.google_ad_groups()) == len(google_part().ad_groups) + 3 * 2 + 1
    keywords = filled.google_keywords()[len(google_part().keywords) :]
    group = filled.google_ad_groups()[-1]
    assert group.key == "bulk-keywords"
    assert [(k.ad_group_id, k.campaign_id, k.quality_score) for k in keywords] == [
        (group.id, extra[-1].id, 5)
    ] * 4

    day = ANCHOR - timedelta(days=1)
    bulk_rows = filled.google_campaign_daily(day, day, campaign_ids=(extra[0].id, extra[-1].id))
    assert [(row.campaign_id, row.cost, row.clicks) for row in bulk_rows] == [
        (extra[0].id, Decimal("0.50"), 1),
        (extra[1].id, Decimal("0.50"), 1),
        (extra[2].id, Decimal("0.50"), 1),
        (extra[3].id, Decimal("1.60"), 4),
    ]
    second = filled.google_campaign_daily(day, day, campaign_ids=(extra[1].id, extra[1].id))
    assert [row.campaign_id for row in second] == [extra[1].id]
    keyword_rows = filled.google_keyword_daily(day, day, ad_group_ids=(group.id, group.id))
    assert [(row.criterion_id, row.cost, row.historical_quality_score) for row in keyword_rows] == [
        (keyword.criterion_id, Decimal("0.40"), 5) for keyword in keywords
    ]
    term_rows = filled.google_search_term_daily(day, day, ad_group_ids=(group.id, group.id))
    assert len(term_rows) == 6
    assert [row.criterion_id for row in term_rows] == [
        keywords[index % 4].criterion_id for index in range(6)
    ]
    assert filled.self_check() == ()


def test_bulk_search_terms_without_keywords_are_unmatched_traffic() -> None:
    filled = build(SPEC.with_google(bulk=GoogleBulkSpec(search_term_rows_per_day=3)))
    campaign = filled.google_campaigns()[-1]
    assert (campaign.key, campaign.budget_amount) == ("bulk-keywords", Decimal(10))
    day = ANCHOR - timedelta(days=1)
    terms = filled.google_search_term_daily(day, day, campaign_ids=(campaign.id, campaign.id))
    assert [(row.criterion_id, row.keyword_text, row.impressions) for row in terms] == [
        (None, None, 1)
    ] * 3
    rows = filled.google_campaign_daily(day, day, campaign_ids=(campaign.id, campaign.id))
    assert [(row.cost, row.impressions) for row in rows] == [(Decimal(0), 3)]
    assert filled.self_check() == ()


def test_google_platform_changes_apply_from_their_instant(world: SimulatedWorld) -> None:
    pause = datetime(2026, 9, 20, 15, tzinfo=UTC)
    changes = (
        PlatformChange(
            "google", "budget", "b_brand", field="amount", value=Decimal(260), at_offset_hours=-36
        ),
        PlatformChange(
            "google",
            "campaign",
            "g_product_reporting",
            field="budget_key",
            value="b_generic",
            at_offset_hours=-72,
        ),
        PlatformChange("google", "campaign", "g_brand", field="status", value="PAUSED", at=pause),
        PlatformChange(
            "google",
            "keyword",
            "kw_rival",
            disappear=True,
            at_offset_hours=-24 * 10,
            reappear_at_offset_hours=-24 * 5,
        ),
        PlatformChange(
            "google", "ad_group", "ag_display", field="status", value="PAUSED", at_offset_hours=-72
        ),
    )
    changed = build(SPEC.adding(platform_changes=changes))
    before, after = NOW - timedelta(days=4), NOW

    budgets_before = {b.key: b for b in changed.google_budgets(before)}
    budgets_after = {b.key: b for b in changed.google_budgets(after)}
    assert budgets_before["b_brand"].amount == Decimal(200)
    assert (budgets_after["b_brand"].amount, budgets_after["b_brand"].amount_micros) == (
        Decimal(260),
        260_000_000,
    )
    reporting = google_ids(changed)["g_product_reporting"]
    assert reporting in budgets_before["b_shared"].campaign_ids
    assert reporting in budgets_after["b_generic"].campaign_ids
    assert reporting not in budgets_after["b_shared"].campaign_ids
    moved = next(c for c in changed.google_campaigns() if c.key == "g_product_reporting")
    assert (moved.budget_key, moved.budget_explicitly_shared) == ("b_generic", False)

    state = changed.state_at(after)
    assert state.at == after
    assert state.changed("google", "budget", "b_brand", "amount")
    assert state.value("google", "budget", "b_brand", "amount", None) == Decimal(260)
    assert not changed.state_at(before).changed("google", "budget", "b_brand", "amount")
    assert changed.state_at(before).value("google", "budget", "b_brand", "amount", "spec") == "spec"
    assert changed.state_at(after) is state

    brand = next(c for c in changed.google_campaigns() if c.key == "g_brand")
    assert brand.status == "PAUSED"
    brand_rows = [
        row for row in changed.google_campaign_daily(*full_days(30)) if row.campaign_id == brand.id
    ]
    # Paused at 11:00 New York time: the morning of 20 September still served.
    assert max(row.date for row in brand_rows) == date(2026, 9, 20)

    absent_from, absent_until = date(2026, 9, 22), date(2026, 9, 25)
    assert not changed.state_at(NOW - timedelta(days=7)).present("google", "keyword", "kw_rival")
    assert "kw_rival" not in {k.key for k in changed.google_keywords(NOW - timedelta(days=7))}
    assert "kw_rival" in {k.key for k in changed.google_keywords(NOW - timedelta(days=12))}
    assert "kw_rival" in {k.key for k in changed.google_keywords()}
    rival = next(k.criterion_id for k in changed.google_keywords() if k.key == "kw_rival")

    def rival_days(source: SimulatedWorld) -> set[date]:
        return {
            row.date
            for row in source.google_keyword_daily(*full_days(30))
            if row.criterion_id == rival
        }

    absence = {absent_from + timedelta(days=step) for step in range(4)}
    assert max(absence) == absent_until
    assert rival_days(world) & absence
    assert not rival_days(changed) & absence
    assert rival_days(changed) == rival_days(world) - absence

    display_group = next(g for g in changed.google_ad_groups() if g.key == "ag_display")
    display_before = next(g for g in changed.google_ad_groups(before) if g.key == "ag_display")
    assert (display_before.status, display_group.status) == ("ENABLED", "PAUSED")

    # Changing one entity never shifts another entity's numbers.
    generic = google_ids(world)["g_generic"]
    window = full_days(30)
    assert changed.google_campaign_daily(*window, campaign_ids=(generic, generic)) == (
        world.google_campaign_daily(*window, campaign_ids=(generic, generic))
    )
    assert changed.self_check() == ()


def test_a_disappeared_campaign_leaves_every_listing() -> None:
    gone = PlatformChange("google", "campaign", "g_display", disappear=True, at_offset_hours=-48)
    changed = build(SPEC.adding(platform_changes=(gone,)))
    assert "g_display" not in {c.key for c in changed.google_campaigns()}
    assert "ag_display" not in {g.key for g in changed.google_ad_groups()}
    assert next(b for b in changed.google_budgets() if b.key == "b_display").campaign_ids == ()
    earlier = changed.google_campaigns(NOW - timedelta(days=3))
    assert "g_display" in {c.key for c in earlier}


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------


def test_linkedin_accounts_and_their_currencies(world: SimulatedWorld) -> None:
    accounts = world.linkedin_accounts()
    assert [(a.id, a.currency, a.status, a.type) for a in accounts] == [
        (NA, "USD", "ACTIVE", "BUSINESS"),
        (EMEA, "EUR", "ACTIVE", "BUSINESS"),
    ]
    assert accounts[0].urn == "urn:li:sponsoredAccount:990000001"
    assert all(LINKEDIN_ACCOUNT_IDS.contains(account.id) for account in accounts)
    assert world.linkedin_account_ids == ("990000001", "990000002")
    assert world.currency_for("linkedin") == "USD"
    assert world.currency_for("linkedin", "urn:li:sponsoredAccount:990000002") == "EUR"
    assert world.currency_for("linkedin", 990_000_099) is None
    assert world.currency_for("hubspot") == "USD"


def test_linkedin_account_selectors_accept_urns_and_reject_unknown_ids(
    world: SimulatedWorld,
) -> None:
    assert world.linkedin_campaigns("urn:li:sponsoredAccount:990000002") == (
        world.linkedin_campaigns(EMEA)
    )
    assert world.linkedin_campaigns("990000002") == world.linkedin_campaigns(EMEA)
    with pytest.raises(KeyError, match="no simulated LinkedIn account 990000050"):
        world.linkedin_campaigns(990_000_050)
    with pytest.raises(ValueError, match="account_id must be an id"):
        world.linkedin_campaigns("urn:li:organization:12")


def test_linkedin_groups_and_campaigns_list_every_status(world: SimulatedWorld) -> None:
    groups = [*world.linkedin_campaign_groups(NA), *world.linkedin_campaign_groups(EMEA)]
    assert {group.status for group in groups} == {
        "ACTIVE",
        "PAUSED",
        "ARCHIVED",
        "DRAFT",
        "CANCELED",
    }
    by_key = {group.key: group for group in groups}
    assert by_key["grp_na_webinars"].run_end == midnight(ANCHOR - timedelta(days=90))
    assert by_key["grp_na_planning"].run_start == midnight(ANCHOR + timedelta(days=60))
    assert by_key["grp_na_leadgen"].run_start == midnight(world.history_start)
    assert by_key["grp_na_leadgen"].run_end is None
    assert by_key["grp_emea_leadgen"].currency == "EUR"
    for group in groups:
        assert LINKEDIN_CAMPAIGN_GROUP_IDS.contains(group.id)
        assert group.urn == f"urn:li:sponsoredCampaignGroup:{group.id}"
        assert group.last_modified_at == group.created_at

    campaigns = [*world.linkedin_campaigns(NA), *world.linkedin_campaigns(EMEA)]
    assert {c.status for c in campaigns} == {"ACTIVE", "PAUSED", "ARCHIVED", "COMPLETED"}
    group_accounts = {group.id: group.account_id for group in groups}
    for campaign in campaigns:
        assert LINKEDIN_CAMPAIGN_IDS.contains(campaign.id)
        assert group_accounts[campaign.group_id] == campaign.account_id
        assert campaign.urn == f"urn:li:sponsoredCampaign:{campaign.id}"
        assert campaign.currency == ("USD" if campaign.account_id == NA else "EUR")
        assert (campaign.version_tag, campaign.last_modified_at) == ("1", campaign.created_at)
    completed = next(c for c in campaigns if c.status == "COMPLETED")
    assert (completed.daily_budget, completed.total_budget) == (None, Decimal("6000.00"))


def test_linkedin_creatives_reference_their_posts_and_lead_forms(world: SimulatedWorld) -> None:
    creatives = world.linkedin_creatives(NA)
    assert [c.id for c in creatives] == sorted(c.id for c in creatives)
    for creative in creatives:
        assert LINKEDIN_CREATIVE_IDS.contains(creative.id)
        assert LINKEDIN_POST_IDS.contains(creative.post_id)
        assert creative.urn == f"urn:li:sponsoredCreative:{creative.id}"
        if creative.lead_form_id is None:
            assert creative.lead_form_urn is None
            assert creative.landing_page_url is not None
        else:
            assert creative.lead_form_urn == f"urn:li:leadGenForm:{creative.lead_form_id}"
    landing = next(c for c in creatives if c.id == LANDING_CREATIVE)
    assert (landing.lead_form_id, landing.cta_label) == (None, "LEARN_MORE")

    posts = world.linkedin_posts([creative.post_urn for creative in creatives])
    assert [post.id for post in posts] == [creative.post_id for creative in creatives]
    for post, creative in zip(posts, creatives, strict=True):
        assert (post.headline, post.commentary, post.account_id) == (
            creative.headline,
            creative.intro_text,
            NA,
        )
        assert post.urn == creative.post_urn
    known = creatives[0].post_urn
    noisy = [known, known, "urn:li:share:1", "urn:li:share:abc", "urn:li:ugcPost:5"]
    assert world.linkedin_posts(noisy) == (posts[0],)

    pipeline = linkedin_ids(world)["li_pipeline_na"]
    of_pipeline = tuple(c for c in creatives if c.campaign_id == pipeline)
    assert len(of_pipeline) == 2
    assert world.linkedin_creatives(NA, [pipeline]) == of_pipeline
    assert world.linkedin_creatives(NA, [f"urn:li:sponsoredCampaign:{pipeline}"]) == of_pipeline
    with pytest.raises(ValueError, match="campaign id must be an id"):
        world.linkedin_creatives(NA, ["pipeline"])


def test_linkedin_daily_leads_equal_the_non_test_lead_responses(world: SimulatedWorld) -> None:
    start = ANCHOR - timedelta(days=20)
    for account in (NA, EMEA):
        rows = world.linkedin_creative_daily(account, start, ANCHOR)
        responses = world.linkedin_lead_responses(
            account, ms(midnight(start)), ms(midnight(ANCHOR + timedelta(days=1)))
        )
        expected = Counter(
            (response.source_creative_id, response.submitted_at.date())
            for response in responses
            if not response.test_lead
        )
        reported = {(row.creative_id, row.date): row.leads for row in rows if row.leads}
        assert reported == dict(expected)
        for row in rows:
            assert row.leads <= row.form_opens <= row.clicks <= row.impressions
            assert row.account_id == account
    landing = [
        row
        for row in world.linkedin_creative_daily(NA, start, ANCHOR)
        if row.creative_id == LANDING_CREATIVE
    ]
    assert landing
    assert all(row.leads == row.form_opens == 0 for row in landing)
    assert sum(row.landing_page_clicks for row in landing) > 0


def test_lead_responses_are_simulated_people_on_the_form_version_in_force(
    world: SimulatedWorld,
) -> None:
    responses = all_responses(world, NA)
    assert len(responses) > 500
    assert [r.submitted_at for r in responses] == sorted(r.submitted_at for r in responses)
    assert all(r.submitted_at <= NOW and r.account_id == NA for r in responses)
    assert all(is_simulated_email(r.email) and r.email.endswith("@sim.invalid") for r in responses)
    tests = [r for r in responses if r.test_lead]
    hidden = [r for r in responses if r.creative_id is None]
    # 2% test leads and 2% without an associated creative (unknown creative row).
    assert 0.01 * len(responses) <= len(tests) <= 0.035 * len(responses)
    assert 0.01 * len(responses) <= len(hidden) <= 0.035 * len(responses)
    assert all(r.person_index is None and r.email.startswith("test-lead-") for r in tests)
    assert all(not r.test_lead and r.source_creative_id for r in hidden)

    switch = midnight(ANCHOR - timedelta(days=60))
    pipeline = [r for r in responses if r.form_id == PIPELINE_FORM]
    assert {r.form_version for r in pipeline if r.submitted_at < switch} == {1}
    assert {r.form_version for r in pipeline if r.submitted_at >= switch} == {2}
    for response in pipeline:
        answers = {answer.question_id: answer.answer for answer in response.answers}
        assert response.versioned_form_urn == (
            f"urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{PIPELINE_FORM},"
            f"{response.form_version})"
        )
        if response.form_version == 1:
            assert answers[1003] == response.email
        else:
            # Version 2 reuses question id 1003 for the job title; the email moved to 1005.
            assert answers[1005] == response.email
            assert answers[1003] != response.email

    people = [r for r in responses if not r.test_lead]
    repeats = [
        index for index, count in Counter(r.person_index for r in people).items() if count > 1
    ]
    assert repeats
    emails_by_person = defaultdict(set)
    for response in people:
        emails_by_person[response.person_index].add(response.email)
    assert all(len(emails) == 1 for emails in emails_by_person.values())
    assert {r.form_id for r in all_responses(world, EMEA)} == {EMEA_FORM}


def test_lead_response_lookup_respects_the_submission_instant(world: SimulatedWorld) -> None:
    later = build(now=NOW + timedelta(days=1))
    pending = later.linkedin_lead_responses(
        NA, ms(NOW) + 1, ms(midnight(ANCHOR + timedelta(days=1)))
    )
    assert pending
    first = pending[0]
    assert first.submitted_at > NOW
    assert world.linkedin_lead_response(first.id) is None
    assert later.linkedin_lead_response(first.id) == first
    assert world.linkedin_lead_response("no-such-response") is None
    earlier = all_responses(world, NA)[-1]
    assert world.linkedin_lead_response(earlier.id) == earlier
    assert world.linkedin_lead_responses(NA, ms(NOW) + 1, ms(NOW + timedelta(days=2))) == ()
    assert world.linkedin_lead_responses(NA, ms(NOW), ms(NOW - timedelta(hours=1))) == ()


def test_lead_forms_expose_their_versions(world: SimulatedWorld) -> None:
    forms = world.linkedin_lead_forms(NA)
    assert [form.id for form in forms] == [PIPELINE_FORM, BENCHMARKS_FORM, PLAYBOOK_FORM]
    assert all(LINKEDIN_FORM_IDS.contains(form.id) for form in forms)
    pipeline = forms[0]
    assert (pipeline.urn, pipeline.state, pipeline.current_version) == (
        "urn:li:leadGenForm:990000101",
        "PUBLISHED",
        2,
    )
    assert [q.question_id for q in pipeline.questions] == [1001, 1002, 1003, 1005, 1004]
    assert pipeline.last_modified_at == midnight(ANCHOR - timedelta(days=60))
    earlier = world.linkedin_lead_forms(NA, NOW - timedelta(days=61))[0]
    assert earlier.current_version == 1
    assert [form.id for form in world.linkedin_lead_forms(EMEA)] == [EMEA_FORM]

    first = world.linkedin_form_version(PIPELINE_FORM, 1)
    second = world.linkedin_form_version("urn:li:leadGenForm:990000101", 2)
    assert first is not None
    assert second is not None
    assert (first.email_question_ids, first.active_from) == ((1003,), midnight(world.history_start))
    assert (second.email_question_ids, second.active_from) == (
        (1005,),
        midnight(ANCHOR - timedelta(days=60)),
    )
    assert [q.text for q in first.questions] == [
        "First name",
        "Last name",
        "Email address",
        "Company name",
    ]
    assert world.linkedin_form_version(PIPELINE_FORM, 3) is None
    assert world.linkedin_form_version(990_000_999, 1) is None


def test_segments_are_thresholded_aggregates_for_the_exact_range(world: SimulatedWorld) -> None:
    start, end = full_days(28)
    pipeline = linkedin_ids(world)["li_pipeline_na"]
    seniority = world.linkedin_segments(NA, "MEMBER_SENIORITY", start, end)
    assert {row.pivot for row in seniority} == {"MEMBER_SENIORITY"}
    rows = [row for row in seniority if row.campaign_id == pipeline]
    assert [row.impressions for row in rows] == sorted(
        (cast(int, row.impressions) for row in rows), reverse=True
    )
    for row in seniority:
        assert row.value.startswith("urn:li:seniority:")
        assert cast(int, row.impressions) % 10 == 0
        assert cast(int, row.impressions) >= 100
        assert row.leads is not None

    delivered = [
        row for row in world.linkedin_creative_daily(NA, start, end) if row.campaign_id == pipeline
    ]
    impressions = sum(row.impressions for row in delivered)
    director = next(row for row in rows if row.value == "urn:li:seniority:6")
    assert director.impressions == round(impressions * 0.10, -1)
    leads = sum(cast(int, row.leads) for row in rows)
    cost = sum(cast(Decimal, row.cost) for row in rows)
    # Directors convert at half the campaign's cost per lead (C3a's proven-good segment).
    director_rate = cast(int, director.leads) / cast(Decimal, director.cost)
    assert director_rate > Decimal("1.5") * leads / cost

    industry = world.linkedin_segments(
        NA, "MEMBER_INDUSTRY", start, end, campaign_ids=[f"urn:li:sponsoredCampaign:{pipeline}"]
    )
    assert industry
    assert {row.campaign_id for row in industry} == {pipeline}
    assert all(row.leads is None and row.website_conversions is None for row in industry)
    other = world.linkedin_segments(NA, "MEMBER_INDUSTRY", start, end)
    assert any(row.leads is not None for row in other if row.campaign_id != pipeline)
    functions = world.linkedin_segments(NA, "MEMBER_JOB_FUNCTION", start, end)
    assert all(row.value.startswith("urn:li:function:") for row in functions)
    with pytest.raises(ValueError, match="pivot must be one of"):
        world.linkedin_segments(NA, "MEMBER_COUNTRY", start, end)


def test_segment_privacy_settings_limit_the_values_returned() -> None:
    start, end = full_days(28)
    top_two = build(SPEC.with_linkedin(segment_top_values=2))
    rows = top_two.linkedin_segments(NA, "MEMBER_JOB_FUNCTION", start, end)
    assert rows
    assert max(Counter(row.campaign_id for row in rows).values()) == 2
    private = build(SPEC.with_linkedin(segment_min_impressions=10**9))
    assert private.linkedin_segments(NA, "MEMBER_SENIORITY", start, end) == ()


def test_campaign_delivery_is_the_total_the_segments_split(world: SimulatedWorld) -> None:
    """The segment fallback's discovery view: per-campaign impressions over the exact range.

    Each delivering campaign of the account appears once, in id order, with the impressions of
    its creatives' days in the range; every campaign with segment rows is among them, and a
    campaign without delivery, or of another account, is not.
    """
    start, end = full_days(28)
    delivery = world.linkedin_campaign_delivery(NA, start, end)
    assert delivery
    assert [record.campaign_id for record in delivery] == sorted(
        {record.campaign_id for record in delivery}
    )
    assert all(record.account_id == NA and record.impressions > 0 for record in delivery)
    daily: Counter[int] = Counter()
    for row in world.linkedin_creative_daily(NA, start, end):
        daily[row.campaign_id] += row.impressions
    assert {record.campaign_id: record.impressions for record in delivery} == {
        campaign: impressions for campaign, impressions in daily.items() if impressions
    }
    segmented = {
        row.campaign_id for row in world.linkedin_segments(NA, "MEMBER_SENIORITY", start, end)
    }
    assert segmented <= {record.campaign_id for record in delivery}
    emea = {record.campaign_id for record in world.linkedin_campaign_delivery(EMEA, start, end)}
    assert emea.isdisjoint(record.campaign_id for record in delivery)
    future = end + timedelta(days=400)
    assert world.linkedin_campaign_delivery(NA, future, future) == ()


def test_a_linkedin_daily_budget_scales_down_the_campaign_demand(world: SimulatedWorld) -> None:
    capped = build(
        SPEC.with_(
            linkedin=linkedin_part().with_campaign("li_pipeline_na", daily_budget=Decimal("100.00"))
        )
    )
    pipeline = linkedin_ids(world)["li_pipeline_na"]

    def spend(source: SimulatedWorld) -> dict[date, Decimal]:
        totals: defaultdict[date, Decimal] = defaultdict(Decimal)
        for row in source.linkedin_creative_daily(NA, *full_days(60)):
            if row.campaign_id == pipeline:
                totals[row.date] += row.cost
        return dict(totals)

    capped_spend, demand = spend(capped), spend(world)
    assert capped_spend.keys() == demand.keys()
    assert all(capped_spend[day] < demand[day] for day in demand)
    utilisation = [total / 100 for total in capped_spend.values()]
    assert Decimal("0.96") <= statistics.mean(utilisation) <= 1
    # Leads still equal the lead responses of the scaled-down delivery.
    assert capped.self_check(days=60) == ()


@pytest.mark.parametrize("seed", [42, 7])
def test_every_complete_capped_linkedin_day_meets_the_capped_day_ratio(seed: int) -> None:
    # LinkedIn reports no budget-lost impression share, so the analysis sees a capped LinkedIn
    # day only through spend / daily budget >= budget.capped_day_ratio.
    floor = Thresholds.load(CONFIG_DIR).ratio("budget.capped_day_ratio")
    budget = Decimal("100.00")
    spec = SPEC.with_(linkedin=linkedin_part().with_campaign("li_pipeline_na", daily_budget=budget))
    capped = build(spec, seed=seed)
    pipeline = linkedin_ids(capped)["li_pipeline_na"]
    spend: defaultdict[date, Decimal] = defaultdict(Decimal)
    for row in capped.linkedin_creative_daily(NA, capped.history_start, ANCHOR - timedelta(days=1)):
        if row.campaign_id == pipeline:
            spend[row.date] += row.cost
    # The campaign's demand (about $144 a day) exceeds $100 on every day of the history.
    utilisation = [total / budget for total in spend.values()]
    assert len(utilisation) >= 200
    assert min(utilisation) >= floor
    assert max(utilisation) <= 1
    assert Decimal("0.96") <= statistics.mean(utilisation) <= 1
    assert capped.self_check(days=len(utilisation) + 1) == ()

    # The floor binds complete days only: today's partial row is the elapsed half, floored.
    whole = {
        row.creative_id: row
        for row in build(spec, seed=seed, now=NOW + timedelta(days=1)).linkedin_creative_daily(
            NA, ANCHOR, ANCHOR
        )
        if row.campaign_id == pipeline
    }
    partial = [
        row
        for row in capped.linkedin_creative_daily(NA, ANCHOR, ANCHOR)
        if row.campaign_id == pipeline
    ]
    assert partial
    for row in partial:
        assert whole[row.creative_id].cost / 2 - CENT <= row.cost <= whole[row.creative_id].cost / 2


def test_a_capped_linkedin_day_delivers_explicit_creatives_exactly_and_prices_the_rest() -> None:
    explicit = ExplicitDays(
        {
            -2: DayMetrics(
                cost=Decimal("40.00"), impressions=2000, clicks=30, form_opens=12, leads=5
            ),
            -3: DayMetrics(
                cost=Decimal("120.00"), impressions=4000, clicks=40, form_opens=10, leads=3
            ),
        }
    )
    capped = build(
        SPEC.with_(
            linkedin=linkedin_part()
            .with_campaign("li_pipeline_na", daily_budget=Decimal("100.00"))
            .with_creative("cr_pipeline_question", profile=explicit)
        )
    )
    creatives = {c.key: c.id for c in capped.linkedin_creatives(NA)}
    rows = {
        (row.date, row.creative_id): row
        for row in capped.linkedin_creative_daily(NA, *full_days(5))
    }
    question, stat = creatives["cr_pipeline_question"], creatives["cr_pipeline_stat"]
    two, three = ANCHOR - timedelta(days=2), ANCHOR - timedelta(days=3)
    for day, stated in ((two, explicit.days[-2]), (three, explicit.days[-3])):
        row = rows[(day, question)]
        assert (row.cost, row.impressions, row.clicks, row.form_opens, row.leads) == (
            stated.cost,
            stated.impressions,
            stated.clicks,
            stated.form_opens,
            stated.leads,
        )
    # $40 stated plus about $86 of demand caps the day: the profile creative takes the rest.
    assert Decimal("0.96") <= (rows[(two, question)].cost + rows[(two, stat)].cost) / 100 <= 1
    # Stated spend alone above the budget leaves the profile creative nothing to deliver.
    assert (three, stat) not in rows
    assert capped.self_check() == ()


def test_explicit_creative_days_drive_leads_and_responses() -> None:
    explicit = ExplicitDays(
        {-2: DayMetrics(cost=Decimal("40.00"), impressions=2000, clicks=30, form_opens=12, leads=5)}
    )
    exact = build(
        SPEC.with_(linkedin=linkedin_part().with_creative("cr_pipeline_question", profile=explicit))
    )
    question = next(c.id for c in exact.linkedin_creatives(NA) if c.key == "cr_pipeline_question")
    rows = [
        row
        for row in exact.linkedin_creative_daily(NA, *full_days(30))
        if row.creative_id == question
    ]
    assert [(r.date, r.cost, r.impressions, r.clicks, r.form_opens, r.leads) for r in rows] == [
        (ANCHOR - timedelta(days=2), Decimal("40.00"), 2000, 30, 12, 5)
    ]
    responses = [
        r for r in all_responses(exact, NA) if r.source_creative_id == question and not r.test_lead
    ]
    assert len(responses) == 5
    assert {r.submitted_at.date() for r in responses} == {ANCHOR - timedelta(days=2)}
    assert exact.self_check() == ()


def test_linkedin_platform_changes_version_entities_and_stop_delivery(
    world: SimulatedWorld,
) -> None:
    pause = datetime(2026, 9, 25, 18, tzinfo=UTC)
    changes = (
        PlatformChange(
            "linkedin", "campaign", "li_benchmarks_na", field="status", value="PAUSED", at=pause
        ),
        PlatformChange(
            "linkedin",
            "campaign",
            "li_pipeline_na",
            field="daily_budget",
            value=None,
            at_offset_hours=-24,
        ),
        PlatformChange(
            "linkedin",
            "campaign_group",
            "grp_na_leadgen",
            field="total_budget",
            value=Decimal("90000.00"),
            at_offset_hours=-48,
        ),
        PlatformChange(
            "linkedin",
            "creative",
            "cr_playbook_checklist",
            field="status",
            value="PAUSED",
            at_offset_hours=-24 * 7,
        ),
        PlatformChange(
            "linkedin", "creative", "cr_playbook_guide", disappear=True, at_offset_hours=-72
        ),
    )
    changed = build(SPEC.adding(platform_changes=changes))
    campaigns = {campaign.key: campaign for campaign in changed.linkedin_campaigns(NA)}
    benchmarks = campaigns["li_benchmarks_na"]
    assert (benchmarks.status, benchmarks.version_tag, benchmarks.last_modified_at) == (
        "PAUSED",
        "2",
        pause,
    )
    assert campaigns["li_pipeline_na"].daily_budget is None
    before = {c.key: c for c in changed.linkedin_campaigns(NA, pause - timedelta(seconds=1))}
    assert (before["li_benchmarks_na"].status, before["li_benchmarks_na"].version_tag) == (
        "ACTIVE",
        "1",
    )
    assert before["li_pipeline_na"].daily_budget == Decimal("250.00")
    group = next(g for g in changed.linkedin_campaign_groups(NA) if g.key == "grp_na_leadgen")
    assert (group.total_budget, group.last_modified_at) == (
        Decimal("90000.00"),
        midnight(ANCHOR) - timedelta(hours=48),
    )
    creatives = {creative.key: creative for creative in changed.linkedin_creatives(NA)}
    assert creatives["cr_playbook_checklist"].intended_status == "PAUSED"
    assert "cr_playbook_guide" not in creatives
    assert "cr_playbook_guide" in {
        c.key for c in changed.linkedin_creatives(NA, at=NOW - timedelta(days=4))
    }

    ids = {c.key: c.id for c in changed.linkedin_creatives(NA, at=NOW - timedelta(days=30))}
    benchmark_creatives = {ids["cr_benchmarks_cpl"], ids["cr_benchmarks_ctr"]}

    def benchmark_days(source: SimulatedWorld) -> set[date]:
        return {
            row.date
            for row in source.linkedin_creative_daily(NA, *full_days(14))
            if row.creative_id in benchmark_creatives
        }

    # Paused at 18:00 UTC: 25 September still delivered, nothing after it.
    assert max(benchmark_days(changed)) == date(2026, 9, 25)
    assert max(benchmark_days(world)) == ANCHOR - timedelta(days=1)
    assert all(
        r.submitted_at.date() <= date(2026, 9, 25)
        for r in all_responses(changed, NA)
        if r.source_creative_id in benchmark_creatives
    )
    assert changed.self_check() == ()


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------


def test_lifecycle_options_put_the_mapped_custom_stages_after_lead(world: SimulatedWorld) -> None:
    options = world.hubspot_lifecycle_options()
    assert [option.label for option in options] == [
        "Subscriber",
        "Lead",
        "First meeting booked",
        "Qualified Lead",
        "Marketing Qualified Lead",
        "Sales Qualified Lead",
        "Opportunity",
        "Customer",
        "Evangelist",
        "Other",
    ]
    assert [option.display_order for option in options] == list(range(len(options)))
    assert [option.value for option in options[2:4]] == [MQL_STAGE, QL_STAGE]
    defaults = {value for value, _label in DEFAULT_LIFECYCLE_STAGES}
    assert {option.value for option in options} - defaults == {MQL_STAGE, QL_STAGE}

    lifecycle = world.hubspot_property("contacts", "lifecyclestage")
    assert lifecycle is not None
    assert (lifecycle.type, lifecycle.options) == ("enumeration", options)
    for option in options:
        entered = world.hubspot_property("contacts", f"hs_v2_date_entered_{option.value}")
        assert entered is not None
        assert entered.type == "datetime"
    amount = world.hubspot_property("deals", "amount")
    assert amount is not None
    assert amount.type == "number"
    source = world.hubspot_property("meetings", "hs_meeting_source")
    assert source is not None
    assert "MEETINGS_PUBLIC" in {option.value for option in source.options}
    assert world.hubspot_property("contacts", "date_first_meeting_was_booked") is None
    assert world.hubspot_property("tickets", "subject") is None


def test_every_simulated_lead_person_resolves_to_their_contact(world: SimulatedWorld) -> None:
    responses = world.linkedin_lead_responses(NA, ms(NOW - timedelta(days=45)), ms(NOW))
    people = sorted({r.email for r in responses if not r.test_lead})
    found: list[ContactRecord] = []
    for email in people:
        digest = sha256(email)
        contact = world.hubspot_contact_by_email_hash(digest)
        if contact is None:
            continue
        found.append(contact)
        assert (contact.email, contact.email_sha256) == (email, digest)
        assert HUBSPOT_CONTACT_IDS.contains(contact.id)
        assert is_simulated_id("hubspot", contact.id)
        assert contact.created_at <= contact.updated_at <= NOW
        assert world.hubspot_contact_by_email_hash(digest.upper()) == contact
    # 97.5% of people have a contact; a few are created late or not yet.
    assert len(found) >= 0.9 * len(people)
    assert world.hubspot_contacts_by_id([found[1].id, "17", found[0].id]) == (found[1], found[0])
    test_lead = next(r for r in responses if r.test_lead)
    assert world.hubspot_contact_by_email_hash(sha256(test_lead.email)) is None


def lead_ordinal(creative_key: str, day: date, response_id: str) -> int:
    """A response's position among its creative's responses on ``day``, recovered from its id.

    The world seeds response ids from ``{seed}:lead-id:{creative key}:{day}:{position}``.
    """
    for ordinal in range(LEADS_PER_CREATIVE_DAY):
        label = f"42:lead-id:{creative_key}:{day.isoformat()}:{ordinal}"
        digest = hashlib.sha256(label.encode()).hexdigest()
        candidate = (
            f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
            f"-{int(digest[32], 16) % 10}"
        )
        if candidate == response_id:
            return ordinal
    raise AssertionError(f"{response_id} is no response of {creative_key} on {day}")


def first_lead_day(contact_id: str) -> date:
    """The UTC day of a simulated person's first lead, decoded from their contact id."""
    slot = int(contact_id) - HUBSPOT_CONTACT_IDS.start
    return date(1970, 1, 1) + timedelta(
        days=slot // (PERSON_CREATIVE_POSITIONS * LEADS_PER_CREATIVE_DAY)
    )


def is_person(contact_id: str) -> bool:
    """Whether a contact id lies in the simulated people's block of ``HUBSPOT_CONTACT_IDS``."""
    return (
        HUBSPOT_CONTACT_IDS.contains(contact_id)
        and int(contact_id) - HUBSPOT_CONTACT_IDS.start < 10**10
    )


def owner_of(meeting: MeetingRecord) -> str:
    """The attendee whose own meeting ids include ``meeting`` (a shared meeting adds a guest)."""
    (owner,) = (
        contact_id
        for contact_id in meeting.contact_ids
        if meeting.id in {person_object_id("meetings", contact_id, j) for j in (0, 1)}
    )
    return owner


def test_person_crm_ids_derive_from_each_persons_first_lead(world: SimulatedWorld) -> None:
    positions = {creative.key: index for index, creative in enumerate(linkedin_part().creatives)}
    keys = {
        creative.id: creative.key
        for account in (NA, EMEA)
        for creative in world.linkedin_creatives(account)
    }
    first_leads: dict[str, LinkedInLeadResponse] = {}
    for account in (NA, EMEA):
        for response in all_responses(world, account):
            if response.test_lead:
                continue
            known = first_leads.get(response.email)
            order = (response.submitted_at, response.id)
            if known is None or order < (known.submitted_at, known.id):
                first_leads[response.email] = response
    checked = 0
    for email, lead in first_leads.items():
        contact = world.hubspot_contact_by_email_hash(sha256(email))
        if contact is None:
            continue
        key = keys[lead.source_creative_id]
        day = lead.submitted_at.date()
        expected = person_contact_id(positions[key], day, lead_ordinal(key, day, lead.id))
        assert contact.id == expected
        assert first_lead_day(contact.id) == day
        owned = {person_object_id("deals", contact.id, j) for j in (0, 1)}
        assert set(world.hubspot_associations("contacts", "deals", contact.id)) <= owned
        checked += 1
    assert checked > 2000
    meetings = cast(tuple[MeetingRecord, ...], world.hubspot_search("meetings", ()))
    people_meetings = [m for m in meetings if all(is_person(c) for c in m.contact_ids)]
    assert len(people_meetings) > 1000
    # Every meeting of the people is owned by exactly one attendee, whose meeting ids name it.
    assert all(owner_of(meeting) in meeting.contact_ids for meeting in people_meetings)


def test_person_ids_are_injective_and_bounded_by_construction() -> None:
    first_day, last_day = date(1970, 1, 1), date(2106, 11, 23)
    per_day = PERSON_CREATIVE_POSITIONS * LEADS_PER_CREATIVE_DAY
    last = (PERSON_CREATIVE_POSITIONS - 1, last_day, LEADS_PER_CREATIVE_DAY - 1)
    corners = {
        (0, first_day, 0): 0,
        (0, first_day, LEADS_PER_CREATIVE_DAY - 1): LEADS_PER_CREATIVE_DAY - 1,
        (1, first_day, 0): LEADS_PER_CREATIVE_DAY,
        (PERSON_CREATIVE_POSITIONS - 1, first_day, LEADS_PER_CREATIVE_DAY - 1): per_day - 1,
        (0, first_day + timedelta(days=1), 0): per_day,
        last: 5 * 10**9 - 1,
    }
    for coordinates, offset in corners.items():
        contact_id = person_contact_id(*coordinates)
        assert contact_id == str(HUBSPOT_CONTACT_IDS.at(offset))
        assert person_object_id("meetings", contact_id, 0) == str(
            HUBSPOT_MEETING_IDS.at(2 * offset)
        )
        assert person_object_id("deals", contact_id, 1) == str(HUBSPOT_DEAL_IDS.at(2 * offset + 1))
    # The last person slot's second deal ends just below the first Google-touch object.
    assert person_object_id("deals", person_contact_id(*last), 1) == str(
        HUBSPOT_DEAL_IDS.at(10**10 - 1)
    )
    sample = [
        (position, day, ordinal)
        for position in (0, 1, PERSON_CREATIVE_POSITIONS - 1)
        for day in (first_day, first_day + timedelta(days=1), ANCHOR, last_day)
        for ordinal in (0, 1, LEADS_PER_CREATIVE_DAY - 1)
    ]
    assert len({person_contact_id(*coordinates) for coordinates in sample}) == len(sample)
    rejected: list[tuple[tuple[Any, ...], str]] = [
        ((PERSON_CREATIVE_POSITIONS, ANCHOR, 0), "creative position 200 exceeds"),
        ((-1, ANCHOR, 0), "creative_position must be >= 0"),
        ((True, ANCHOR, 0), "creative_position must be an integer"),
        ((0, ANCHOR, LEADS_PER_CREATIVE_DAY), "lead ordinal 500 exceeds"),
        ((0, ANCHOR, -1), "ordinal must be >= 0"),
        ((0, date(1969, 12, 31), 0), "lies outside the 50000 UTC days"),
        ((0, last_day + timedelta(days=1), 0), "lies outside the 50000 UTC days"),
        ((0, NOW, 0), "day must be a date"),
    ]
    for arguments, message in rejected:
        with pytest.raises(ValueError, match=message):
            person_contact_id(*arguments)
    contact_id = person_contact_id(0, ANCHOR, 0)
    with pytest.raises(ValueError, match="object_type must be meetings or deals"):
        person_object_id(cast(Any, "companies"), contact_id, 0)
    with pytest.raises(ValueError, match="owns 2 deals, found index 2"):
        person_object_id("deals", contact_id, 2)
    with pytest.raises(ValueError, match="index must be >= 0"):
        person_object_id("deals", contact_id, -1)
    for other in (
        str(HUBSPOT_CONTACT_IDS.at(10**10)),
        str(HUBSPOT_HASHED_CONTACT_IDS.start),
        "not-an-id",
    ):
        with pytest.raises(ValueError, match="is not a simulated person contact id"):
            person_object_id("meetings", other, 0)


def person_layout_spec(
    position: int, profile: DailyProfile | ExplicitDays, *, hubspot: bool = True
) -> WorldSpec:
    """A world whose only lead creative sits at ``position`` of ``linkedin.creatives``.

    Landing-page creatives with no delivery fill the positions before it.
    """
    fillers = tuple(
        LinkedInCreativeSpec(
            f"filler_{index}",
            "cmp",
            f"Filler {index}",
            "Filler intro text",
            landing_page_url="https://www.sim.invalid/offer",
        )
        for index in range(position)
    )
    lead = LinkedInCreativeSpec(
        "cr_lead",
        "cmp",
        "Lead headline",
        "Lead intro text",
        lead_form_id=PIPELINE_FORM,
        profile=profile,
    )
    return WorldSpec(
        "person-layout",
        anchor=ANCHOR,
        linkedin=_li(creatives=(*fillers, lead)),
        hubspot=HubSpotSpec() if hubspot else None,
    )


def test_a_world_rejects_lead_creatives_its_person_ids_cannot_cover_when_built() -> None:
    daily = _P.with_(form_open_rate=Decimal("0.05"), form_completion_rate=Decimal("0.5"))
    with pytest.raises(
        ValueError,
        match=re.escape(
            "lead creative cr_lead is at position 200 of linkedin.creatives; simulated person "
            "ids cover only lead creatives below position PERSON_CREATIVE_POSITIONS (200)"
        ),
    ):
        build(person_layout_spec(PERSON_CREATIVE_POSITIONS, daily), content_assets=())
    full = DayMetrics(
        cost=Decimal("50.00"),
        impressions=20_000,
        clicks=1_000,
        form_opens=LEADS_PER_CREATIVE_DAY,
        leads=LEADS_PER_CREATIVE_DAY,
    )
    below = full.with_(leads=LEADS_PER_CREATIVE_DAY - 1)
    with pytest.raises(
        ValueError,
        match=re.escape(
            "lead creative cr_lead states 500 leads on ExplicitDays offset -2; each day must "
            "state fewer than LEADS_PER_CREATIVE_DAY (500)"
        ),
    ):
        build(person_layout_spec(0, ExplicitDays({-3: below, -2: full})), content_assets=())
    # At both limits less one the world builds, and its people take the layout's last slots.
    edge = build(
        person_layout_spec(PERSON_CREATIVE_POSITIONS - 1, ExplicitDays({-2: below})),
        content_assets=(),
    )
    day = ANCHOR - timedelta(days=2)
    slots = {
        person_contact_id(PERSON_CREATIVE_POSITIONS - 1, day, ordinal)
        for ordinal in range(LEADS_PER_CREATIVE_DAY - 1)
    }
    contacts = cast(tuple[ContactRecord, ...], edge.hubspot_search("contacts", ()))
    assert len(contacts) > 300
    assert {contact.id for contact in contacts} <= slots
    assert edge.self_check() == ()
    # Without a HubSpot part no person takes a contact id, so no position limit applies.
    linkedin_only = build(
        person_layout_spec(PERSON_CREATIVE_POSITIONS, daily, hubspot=False), content_assets=()
    )
    assert all_responses(linkedin_only, NA)


def test_a_restart_on_a_later_date_keeps_every_simulated_crm_identity() -> None:
    # The default spec has no anchor: a worker restarted a day later rebuilds the world with
    # its anchor and history start a day later. Ids must still name the same people and events.
    first = build(DEFAULT_SPEC, now=NOW)
    later = build(DEFAULT_SPEC, now=NOW + timedelta(days=1))
    assert later.history_start == first.history_start + timedelta(days=1)
    first_contacts = {
        c.id: c for c in cast(tuple[ContactRecord, ...], first.hubspot_search("contacts", ()))
    }
    later_contacts = {
        c.id: c for c in cast(tuple[ContactRecord, ...], later.hubspot_search("contacts", ()))
    }
    first_ids = {contact.email_sha256: contact.id for contact in first_contacts.values()}
    later_ids = {contact.email_sha256: contact.id for contact in later_contacts.values()}
    shared_hashes = first_ids.keys() & later_ids.keys()
    assert len(shared_hashes) >= 0.99 * len(first_ids)
    assert all(first_ids[digest] == later_ids[digest] for digest in shared_hashes)
    shared_ids = first_contacts.keys() & later_contacts.keys()
    assert all(first_contacts[i].email_sha256 == later_contacts[i].email_sha256 for i in shared_ids)
    # The people the later world lacks are those whose first lead precedes its history.
    assert all(first_lead_day(i) < later.history_start for i in first_contacts.keys() - shared_ids)
    # A by-id refresh, as lead matching runs it, returns the same person with the same history.
    refreshed = later.hubspot_contacts_by_id(sorted(first_contacts, key=int))
    assert {contact.id for contact in refreshed} == shared_ids
    for contact in refreshed:
        before = first_contacts[contact.id]
        assert contact.email_sha256 == before.email_sha256
        assert contact.created_at == before.created_at
        assert before.stage_entered.items() <= contact.stage_entered.items()
    first_meetings = {
        m.id: m for m in cast(tuple[MeetingRecord, ...], first.hubspot_search("meetings", ()))
    }
    later_meetings = {
        m.id: m for m in cast(tuple[MeetingRecord, ...], later.hubspot_search("meetings", ()))
    }
    shared_meetings = first_meetings.keys() & later_meetings.keys()
    assert len(shared_meetings) >= 0.98 * len(first_meetings)
    guests_changed = 0
    for meeting_id in shared_meetings:
        before, after = first_meetings[meeting_id], later_meetings[meeting_id]
        assert (before.title, before.created_at, before.start_at, before.source) == (
            after.title,
            after.created_at,
            after.start_at,
            after.source,
        )
        assert owner_of(before) == owner_of(after)
        if not set(before.contact_ids) <= set(after.contact_ids):
            guests_changed += 1
    # A shared meeting's guest is one of the most recent people before its owner, so only an
    # owner among the later world's first people (or next to a lead only one world has, where
    # a delivery trend counts from the history start) can get another guest.
    assert guests_changed <= 2
    first_deals = {d.id: d for d in cast(tuple[DealRecord, ...], first.hubspot_search("deals", ()))}
    later_deals = {d.id: d for d in cast(tuple[DealRecord, ...], later.hubspot_search("deals", ()))}
    shared_deals = first_deals.keys() & later_deals.keys()
    assert len(shared_deals) >= 0.98 * len(first_deals)
    for deal_id in shared_deals:
        before_deal, after_deal = first_deals[deal_id], later_deals[deal_id]
        assert (before_deal.name, before_deal.stage, before_deal.amount) == (
            after_deal.name,
            after_deal.stage,
            after_deal.amount,
        )
        assert before_deal.contact_ids == after_deal.contact_ids
        if before_deal.contact_ids:
            assert before_deal.created_at == after_deal.created_at
        else:
            # Orphan deals are drawn over the 90 days before the anchor, so they move with it.
            assert after_deal.created_at - before_deal.created_at == timedelta(days=1)
    # A repeat lead reuses one of the most recent people before it, so, likewise, only a few
    # repeat leads name another person (a choice among every earlier person moved about 90).
    first_emails = {r.id: r.email for account in (NA, EMEA) for r in all_responses(first, account)}
    later_emails = {r.id: r.email for account in (NA, EMEA) for r in all_responses(later, account)}
    shared_leads = first_emails.keys() & later_emails.keys()
    assert len(shared_leads) >= 0.98 * len(first_emails)
    assert sum(1 for lead in shared_leads if first_emails[lead] != later_emails[lead]) <= 3


def test_contact_stages_record_when_each_bound_stage_was_entered(world: SimulatedWorld) -> None:
    contacts = world.hubspot_search("contacts", ())
    assert len(contacts) > 1000
    order = {option.value: option.display_order for option in world.hubspot_lifecycle_options()}
    stages = Counter(contact.lifecycle_stage for contact in contacts)
    assert stages[MQL_STAGE] > 0
    assert stages[QL_STAGE] > 0
    assert stages["lead"] > 0
    skipped = 0
    for contact in contacts:
        assert contact.stage_entered["lead"] == contact.created_at
        assert set(contact.stage_entered) <= {"lead", MQL_STAGE, QL_STAGE}
        assert contact.lifecycle_stage == max(contact.stage_entered, key=order.__getitem__)
        assert all(instant <= NOW for instant in contact.stage_entered.values())
        assert contact.updated_at == max(contact.stage_entered.values())
        assert contact.bound_dates == {}
        assert contact.email is not None
        assert is_simulated_email(contact.email)
        if QL_STAGE in contact.stage_entered and MQL_STAGE not in contact.stage_entered:
            skipped += 1
    # MQL and QL are drawn independently, and a skipped stage leaves MQL empty.
    assert skipped > 0


def test_hubspot_records_exist_only_once_their_instants_have_passed(
    world: SimulatedWorld,
) -> None:
    early_now = NOW - timedelta(days=30)
    early = build(now=early_now)
    for object_type in ("contacts", "meetings", "deals"):
        early_records = early.hubspot_search(object_type, ())
        late_records = world.hubspot_search(object_type, ())
        early_ids = {record.id for record in early_records}
        assert early_ids <= {record.id for record in late_records}
        assert all(record.created_at <= early_now for record in early_records)
        recent = {record.id for record in late_records if record.created_at > early_now}
        assert recent
        assert recent.isdisjoint(early_ids)
    for contact in cast(tuple[ContactRecord, ...], early.hubspot_search("contacts", ())):
        assert all(instant <= early_now for instant in contact.stage_entered.values())
    late_meetings = cast(tuple[MeetingRecord, ...], world.hubspot_search("meetings", ()))
    visible = early.hubspot_meetings(meeting.id for meeting in late_meetings)
    assert visible
    assert all(meeting.created_at <= early_now for meeting in visible)
    late_by_id = {meeting.id: meeting for meeting in late_meetings}
    assert any(
        meeting.outcome == "SCHEDULED" and late_by_id[meeting.id].outcome == "COMPLETED"
        for meeting in visible
    )


def test_meetings_follow_their_start_time_and_associations_are_symmetric(
    world: SimulatedWorld,
) -> None:
    meetings = cast(tuple[MeetingRecord, ...], world.hubspot_search("meetings", ()))
    assert len(meetings) > 100
    sources = {source for source, _weight in hubspot_part().funnel.meeting_sources}
    for meeting in meetings:
        assert HUBSPOT_MEETING_IDS.contains(meeting.id)
        assert meeting.source in sources
        assert meeting.end_at == meeting.start_at + timedelta(minutes=30)
        assert meeting.timestamp == meeting.start_at
        assert meeting.start_at.minute % 15 == 0
        if meeting.outcome != "CANCELED":
            assert meeting.outcome == ("COMPLETED" if meeting.start_at <= NOW else "SCHEDULED")
        assert world.hubspot_associations("meetings", "contacts", meeting.id) == tuple(
            sorted(meeting.contact_ids, key=int)
        )
        for contact_id in meeting.contact_ids:
            assert meeting.id in world.hubspot_associations("contacts", "meetings", contact_id)
    assert {meeting.outcome for meeting in meetings} == {"CANCELED", "COMPLETED", "SCHEDULED"}
    # Associations are many-to-many: some meetings involve two contacts.
    assert any(len(meeting.contact_ids) == 2 for meeting in meetings)
    assert world.hubspot_meetings([meetings[1].id, "1", meetings[0].id]) == (
        meetings[1],
        meetings[0],
    )


def test_deals_link_to_their_contacts_and_orphans_to_none(world: SimulatedWorld) -> None:
    deals = cast(tuple[DealRecord, ...], world.hubspot_search("deals", ()))
    orphans = [deal for deal in deals if not deal.contact_ids]
    assert len(orphans) == hubspot_part().orphan_deals == 6
    assert all(deal.name.startswith("Inbound deal") for deal in orphans)
    assert all(
        midnight(ANCHOR) - timedelta(days=90) <= deal.created_at <= midnight(ANCHOR)
        for deal in orphans
    )
    for deal in deals:
        assert HUBSPOT_DEAL_IDS.contains(deal.id)
        assert (deal.pipeline, deal.currency) == ("default", "USD")
        assert deal.amount is not None
        assert deal.amount > 0
        assert world.hubspot_associations("deals", "contacts", deal.id) == deal.contact_ids
        for contact_id in deal.contact_ids:
            assert deal.id in world.hubspot_associations("contacts", "deals", contact_id)
    assert world.hubspot_deals([orphans[1].id, "123", orphans[0].id]) == (orphans[1], orphans[0])
    orphan_block = HUBSPOT_DEAL_IDS.at(2 * 10**10 + 6)
    assert world.hubspot_deals([str(orphan_block), str(HUBSPOT_DEAL_IDS.start - 1)]) == ()


def test_object_ids_decode_only_inside_their_own_block(world: SimulatedWorld) -> None:
    # The last ids of the Google, orphan and bulk blocks, and the never-allocated block above
    # the seeded contacts' objects, hold nothing in the default world.
    for offset in (2 * 10**10 - 1, 3 * 10**10 - 1, 5 * 10**10 - 1, 9 * 10**10, 10**11 - 1):
        assert world.hubspot_deals([str(HUBSPOT_DEAL_IDS.at(offset))]) == ()
        assert world.hubspot_meetings([str(HUBSPOT_MEETING_IDS.at(offset))]) == ()
    deals = cast(tuple[DealRecord, ...], world.hubspot_search("deals", ()))
    deal = next(deal for deal in deals if deal.contact_ids and is_person(deal.contact_ids[0]))
    assert world.hubspot_deals([deal.id]) == (deal,)
    assert deal.id in {person_object_id("deals", deal.contact_ids[0], j) for j in (0, 1)}


def test_associations_without_targets_are_empty_and_unknown_pairs_fail(
    world: SimulatedWorld,
) -> None:
    contacts = world.hubspot_search("contacts", ())
    lonely = next(
        contact
        for contact in contacts
        if not world.hubspot_associations("contacts", "meetings", contact.id)
    )
    assert world.hubspot_associations("contacts", "meetings", lonely.id) == ()
    assert world.hubspot_associations("contacts", "deals", "9900099999999999") == ()
    assert world.hubspot_associations("meetings", "contacts", "1") == ()
    assert world.hubspot_associations("deals", "contacts", "1") == ()
    with pytest.raises(ValueError, match="unsupported association contacts -> companies"):
        world.hubspot_associations("contacts", "companies", lonely.id)


def test_properties_render_every_record_field_by_hubspot_name(world: SimulatedWorld) -> None:
    contact = next(
        c
        for c in cast(tuple[ContactRecord, ...], world.hubspot_search("contacts", ()))
        if MQL_STAGE in c.stage_entered
    )
    values = world.hubspot_properties(contact)
    assert (values["hs_object_id"], values["email"], values["lifecyclestage"]) == (
        contact.id,
        contact.email,
        contact.lifecycle_stage,
    )
    assert (values["firstname"], values["lastname"]) == (contact.first_name, contact.last_name)
    assert (values["createdate"], values["lastmodifieddate"]) == (
        contact.created_at,
        contact.updated_at,
    )
    for option in world.hubspot_lifecycle_options():
        assert values[f"hs_v2_date_entered_{option.value}"] == contact.stage_entered.get(
            option.value
        )

    meeting = cast(MeetingRecord, world.hubspot_search("meetings", ())[0])
    meeting_values = world.hubspot_properties(meeting)
    assert (
        meeting_values["hs_meeting_start_time"],
        meeting_values["hs_meeting_end_time"],
        meeting_values["hs_meeting_outcome"],
        meeting_values["hs_meeting_source"],
        meeting_values["hs_createdate"],
    ) == (meeting.start_at, meeting.end_at, meeting.outcome, meeting.source, meeting.created_at)

    deal = cast(DealRecord, world.hubspot_search("deals", ())[0])
    deal_values = world.hubspot_properties(deal)
    assert (
        deal_values["dealname"],
        deal_values["amount"],
        deal_values["dealstage"],
        deal_values["closedate"],
        deal_values["deal_currency_code"],
    ) == (deal.name, deal.amount, deal.stage, None, "USD")


def test_crm_search_applies_every_operator(world: SimulatedWorld) -> None:
    contacts = cast(tuple[ContactRecord, ...], world.hubspot_search("contacts", ()))
    assert [int(c.id) for c in contacts] == sorted(int(c.id) for c in contacts)

    def search(*filters: SearchFilter) -> set[str]:
        return {record.id for record in world.hubspot_search("contacts", filters)}

    every = {contact.id for contact in contacts}
    qualified = {c.id for c in contacts if c.lifecycle_stage == QL_STAGE}
    assert qualified
    assert search(SearchFilter("lifecyclestage", "EQ", QL_STAGE)) == qualified
    assert search(SearchFilter("lifecyclestage", "NEQ", QL_STAGE)) == every - qualified
    assert search(SearchFilter("lifecyclestage", "IN", values=(QL_STAGE, MQL_STAGE))) == {
        c.id for c in contacts if c.lifecycle_stage in (QL_STAGE, MQL_STAGE)
    }
    entered = f"hs_v2_date_entered_{QL_STAGE}"
    reached = {c.id for c in contacts if QL_STAGE in c.stage_entered}
    assert search(SearchFilter(entered, "HAS_PROPERTY")) == reached
    assert search(SearchFilter(entered, "NOT_HAS_PROPERTY")) == every - reached

    cutoff = NOW - timedelta(days=30)
    newer = {c.id for c in contacts if c.created_at >= cutoff}
    assert newer
    assert search(SearchFilter("createdate", "GTE", ms(cutoff))) == newer
    assert search(SearchFilter("createdate", "LT", ms(cutoff))) == every - newer
    assert search(SearchFilter("createdate", "GT", str(ms(cutoff)))) <= newer
    assert search(SearchFilter("createdate", "LTE", ms(cutoff))) >= every - newer
    low, high = NOW - timedelta(days=40), NOW - timedelta(days=20)
    assert search(SearchFilter("createdate", "BETWEEN", ms(low), ms(high))) == {
        c.id for c in contacts if ms(low) <= ms(c.created_at) <= ms(high)
    }
    # A combined AND group, and a value that does not parse as the property's type.
    assert (
        search(
            SearchFilter("createdate", "GTE", ms(cutoff)),
            SearchFilter("lifecyclestage", "EQ", QL_STAGE),
        )
        == newer & qualified
    )
    assert search(SearchFilter("createdate", "EQ", "not-a-number")) == set()
    assert search(SearchFilter("email", "EQ", cast(str, contacts[0].email))) == {contacts[0].id}
    assert search(SearchFilter("google_campaign_id", "EQ", "1")) == set()


def test_crm_search_sorts_by_a_property_with_missing_values_last(world: SimulatedWorld) -> None:
    deals = cast(
        tuple[DealRecord, ...],
        world.hubspot_search("deals", (), sort_property="amount", descending=True),
    )
    amounts = [cast(Decimal, deal.amount) for deal in deals]
    assert amounts == sorted(amounts, reverse=True)
    rich = world.hubspot_search("deals", [SearchFilter("amount", "GT", Decimal(30000))])
    assert rich
    assert all(cast(Decimal, cast(DealRecord, d).amount) > 30000 for d in rich)
    oldest_first = world.hubspot_search("meetings", (), sort_property="hs_createdate")
    created = [record.created_at for record in oldest_first]
    assert created == sorted(created)
    by_id = world.hubspot_search("deals", ())
    assert world.hubspot_search("deals", (), sort_property="closedate") == by_id
    with pytest.raises(ValueError, match="unknown CRM object type 'companies'"):
        world.hubspot_search("companies", ())


def test_seeded_contacts_answer_a_deterministic_share_of_real_lead_hashes() -> None:
    # Its own world: the seeded contacts it materialises become searchable, and the shared
    # fixture's contact searches must keep returning only simulated people.
    world = build()
    digests = [sha256(f"real.person.{index}@example.com") for index in range(400)]
    answered = {digest: world.hashed_contact_for(digest) for digest in digests}
    hits = {digest: contact for digest, contact in answered.items() if contact is not None}
    # live_lead_match_share is 0.6.
    assert 0.5 * len(digests) <= len(hits) <= 0.7 * len(digests)
    again = build()
    searchable = {contact.id for contact in world.hubspot_search("contacts", ())}
    with_meetings: list[str] = []
    with_deals: list[str] = []
    for digest, contact in hits.items():
        assert HUBSPOT_HASHED_CONTACT_IDS.contains(contact.id)
        assert (contact.email, contact.email_sha256) == (None, digest)
        assert world.hashed_contact_for(digest.upper()) == contact
        assert again.hashed_contact_for(digest) == contact
        assert world.hubspot_contact_by_email_hash(digest) is None
        assert contact.id in searchable
        (by_id,) = world.hubspot_contacts_by_id([contact.id])
        assert (by_id.id, by_id.email_sha256) == (contact.id, None)
        if world.hubspot_associations("contacts", "meetings", contact.id):
            with_meetings.append(contact.id)
        if world.hubspot_associations("contacts", "deals", contact.id):
            with_deals.append(contact.id)
    assert with_meetings
    assert with_deals
    meeting_ids = world.hubspot_associations("contacts", "meetings", with_meetings[0])
    meetings = world.hubspot_meetings(meeting_ids)
    assert [meeting.id for meeting in meetings] == list(meeting_ids)
    assert all(meeting.contact_ids == (with_meetings[0],) for meeting in meetings)
    assert world.hubspot_associations("meetings", "contacts", meeting_ids[0]) == (with_meetings[0],)
    deal_ids = world.hubspot_associations("contacts", "deals", with_deals[0])
    assert [deal.contact_ids for deal in world.hubspot_deals(deal_ids)] == [(with_deals[0],)] * len(
        deal_ids
    )
    assert all(HUBSPOT_MEETING_IDS.contains(meeting_id) for meeting_id in meeting_ids)
    assert all(HUBSPOT_DEAL_IDS.contains(deal_id) for deal_id in deal_ids)
    beyond = str(HUBSPOT_HASHED_CONTACT_IDS.at(2 * 10**10))
    assert world.hubspot_contacts_by_id([beyond]) == ()
    with pytest.raises(ValueError, match="SHA-256 hex digest"):
        world.hashed_contact_for("not-a-digest")
    never = build(SPEC.with_hubspot(live_lead_match_share=Decimal(0)))
    assert all(never.hashed_contact_for(digest) is None for digest in digests[:50])


def test_crm_search_finds_materialised_seeded_records_once() -> None:
    world = build()
    object_types = ("contacts", "meetings", "deals")
    simulated_only = {kind: world.hubspot_search(kind, ()) for kind in object_types}
    assert not any(
        HUBSPOT_HASHED_CONTACT_IDS.contains(contact.id) for contact in simulated_only["contacts"]
    )

    # Real-looking lead emails until one is answered by a seeded contact with both objects.
    found: tuple[str, ContactRecord, tuple[str, ...], tuple[str, ...]] | None = None
    for index in range(1000):
        digest = sha256(f"real.buyer.{index}@example.com")
        contact = world.hashed_contact_for(digest)
        if contact is None:
            continue
        deal_ids = world.hubspot_associations("contacts", "deals", contact.id)
        meeting_ids = world.hubspot_associations("contacts", "meetings", contact.id)
        if deal_ids and meeting_ids:
            found = (digest, contact, deal_ids, meeting_ids)
            break
    assert found is not None, "no seeded contact with a deal and a meeting"
    digest, seeded, deal_ids, meeting_ids = found
    (deal,) = world.hubspot_deals(deal_ids[:1])
    (by_id,) = world.hubspot_contacts_by_id([seeded.id])

    def ids(kind: str, *filters: SearchFilter) -> list[str]:
        return [record.id for record in world.hubspot_search(kind, filters)]

    # Search agrees with batch reads and associations for every object type.
    assert deal in world.hubspot_search("deals", ())
    assert by_id in world.hubspot_search("contacts", ())
    assert set(meeting_ids) <= set(ids("meetings"))
    created = ms(deal.created_at)
    assert deal.id in ids("deals", SearchFilter("createdate", "BETWEEN", created - 1, created + 1))
    window = (
        SearchFilter("createdate", "GTE", created),
        SearchFilter("createdate", "LT", created + 1),
    )
    assert ids("deals", *window) == [
        record.id
        for record in world.hubspot_search("deals", ())
        if ms(record.created_at) == created
    ]
    assert deal.id in ids("deals", *window)
    by_creation = cast(
        tuple[DealRecord, ...], world.hubspot_search("deals", (), sort_property="createdate")
    )
    assert [record.created_at for record in by_creation] == sorted(
        record.created_at for record in by_creation
    )
    # The materialised records join the simulated people's, and only seeded ones are added.
    searched = {kind: world.hubspot_search(kind, ()) for kind in object_types}
    for kind in object_types:
        before = {record.id for record in simulated_only[kind]}
        added = [record for record in searched[kind] if record.id not in before]
        assert before <= {record.id for record in searched[kind]}
        assert added
        for record in added:
            owners = (record.id,) if isinstance(record, ContactRecord) else record.contact_ids
            assert owners
            assert all(HUBSPOT_HASHED_CONTACT_IDS.contains(owner) for owner in owners)

    # Repeated lookups through every surface never duplicate a record.
    assert world.hashed_contact_for(digest) == seeded
    assert world.hashed_contact_for(digest.upper()) == seeded
    assert [record.id for record in world.hubspot_deals(deal_ids)] == list(deal_ids)
    assert [record.id for record in world.hubspot_meetings(meeting_ids)] == list(meeting_ids)
    assert world.hubspot_contacts_by_id([seeded.id]) == (by_id,)
    assert world.hubspot_associations("deals", "contacts", deal.id) == (seeded.id,)
    assert world.hubspot_associations("meetings", "contacts", meeting_ids[0]) == (seeded.id,)
    for kind in object_types:
        again = ids(kind)
        assert len(again) == len(set(again))
        assert world.hubspot_search(kind, ()) == searched[kind]
    assert ids("deals", *window).count(deal.id) == 1

    # A record materialised before its instant stays out of search until then. A deal is
    # created at least an hour after its contact, so the contact already exists.
    early = build(now=deal.created_at - timedelta(seconds=1))
    early_contact = early.hashed_contact_for(digest)
    assert early_contact is not None
    assert early_contact.id == seeded.id
    assert early.hubspot_deals([deal.id]) == ()
    assert deal.id not in {record.id for record in early.hubspot_search("deals", ())}
    assert seeded.id in {record.id for record in early.hubspot_search("contacts", ())}


def test_crm_search_is_duplicate_free_while_other_threads_materialise_records() -> None:
    # A fresh world: the first searches build the simulated people while lookups on other
    # threads materialise seeded contacts, so both grow the stores that search snapshots.
    world = build()
    object_types = ("contacts", "meetings", "deals")
    digests = [sha256(f"real.concurrent.{index}@example.com") for index in range(240)]

    def look_up(chunk: Sequence[str]) -> list[str]:
        answered = (world.hashed_contact_for(digest) for digest in chunk)
        return [contact.id for contact in answered if contact is not None]

    def search(kind: str) -> list[str]:
        return [record.id for record in world.hubspot_search(kind, ())]

    with ThreadPoolExecutor(max_workers=8) as pool:
        lookups = [pool.submit(look_up, digests[start::4]) for start in range(4)]
        searches = [pool.submit(search, kind) for _round in range(4) for kind in object_types]
        materialised = {contact_id for future in lookups for contact_id in future.result()}
        results = [future.result() for future in searches]

    assert materialised
    for ids in results:
        assert len(ids) == len(set(ids))
    assert materialised <= set(search("contacts"))


def test_a_property_bound_stage_is_recorded_as_a_contact_instant() -> None:
    bound = HubSpotMapping(
        mql=StageBinding("property", (), "date_first_meeting_was_booked"),
        ql=MAPPING.ql,
        booking_sources=MAPPING.booking_sources,
        meeting_outcome_exclusions=MAPPING.meeting_outcome_exclusions,
    )
    property_world = build(mapping=bound)
    stage_world = build()
    definition = property_world.hubspot_property("contacts", "date_first_meeting_was_booked")
    assert definition is not None
    # A property-mode binding names a datetime property (AAP 0.5.4.7).
    assert (definition.type, definition.field_type) == ("datetime", "date")
    contacts = cast(
        tuple[ContactRecord, ...],
        property_world.hubspot_search(
            "contacts", [SearchFilter("date_first_meeting_was_booked", "HAS_PROPERTY")]
        ),
    )
    assert contacts
    for contact in contacts:
        booked = contact.bound_dates["date_first_meeting_was_booked"]
        assert booked.tzinfo is UTC
        assert property_world.hubspot_properties(contact)["date_first_meeting_was_booked"] == booked
        assert MQL_STAGE not in contact.stage_entered
        assert contact.updated_at >= booked
        # The same event the stage-label world records as its MQL stage entry, to the instant.
        (staged,) = stage_world.hubspot_contacts_by_id([contact.id])
        assert staged.stage_entered[MQL_STAGE] == booked
    # Events keep their time of day: they are instants, not calendar dates.
    assert any(c.bound_dates["date_first_meeting_was_booked"].time() != time(0) for c in contacts)
    pivot = sorted(c.bound_dates["date_first_meeting_was_booked"] for c in contacts)[
        len(contacts) // 2
    ]
    for after in (ms(pivot), ms(pivot) + 1):
        recent = property_world.hubspot_search(
            "contacts", [SearchFilter("date_first_meeting_was_booked", "GTE", after)]
        )
        assert {c.id for c in recent} == {
            c.id for c in contacts if ms(c.bound_dates["date_first_meeting_was_booked"]) >= after
        }


def test_a_removed_stage_label_leaves_its_binding_unresolved() -> None:
    removed = build(SPEC.with_hubspot(removed_stage_labels=("First meeting booked",)))
    labels = [option.label for option in removed.hubspot_lifecycle_options()]
    assert "First meeting booked" not in labels
    assert "Qualified Lead" in labels
    contacts = cast(tuple[ContactRecord, ...], removed.hubspot_search("contacts", ()))
    assert not any(MQL_STAGE in contact.stage_entered for contact in contacts)
    assert any(QL_STAGE in contact.stage_entered for contact in contacts)
    with pytest.raises(ValueError, match="neither a portal stage nor a mapping label"):
        build(SPEC.with_hubspot(removed_stage_labels=("Never heard of it",)))


def test_a_mapping_label_the_portal_lacks_becomes_a_custom_stage() -> None:
    accepted = HubSpotMapping(
        mql=MAPPING.mql,
        ql=StageBinding("stage_label", ("Sales Accepted Lead",), None),
        booking_sources=MAPPING.booking_sources,
        meeting_outcome_exclusions=MAPPING.meeting_outcome_exclusions,
    )
    added_world = build(mapping=accepted)
    options = added_world.hubspot_lifecycle_options()
    added = options[-2]
    assert (added.label, added.value) == ("Sales Accepted Lead", str(HUBSPOT_STAGE_IDS.at(2)))
    assert options[-1].label == "Other"
    contacts = cast(tuple[ContactRecord, ...], added_world.hubspot_search("contacts", ()))
    assert any(added.value in contact.stage_entered for contact in contacts)
    assert added_world.self_check() == ()


def test_a_configured_google_crm_link_adds_google_touch_contacts() -> None:
    properties = ("google_campaign_id", "google_touch_date")
    linked = build(SPEC.with_hubspot(google_touch_contacts=12), google_crm_properties=properties)
    assert linked.google_crm_properties == properties
    campaign_property = linked.hubspot_property("contacts", "google_campaign_id")
    touch_property = linked.hubspot_property("contacts", "google_touch_date")
    assert campaign_property is not None
    assert touch_property is not None
    assert (campaign_property.type, touch_property.type) == ("string", "datetime")
    search_campaigns = {str(c.id) for c in linked.google_campaigns() if c.channel == "SEARCH"}
    google_contacts = cast(
        tuple[ContactRecord, ...],
        linked.hubspot_search("contacts", [SearchFilter("google_campaign_id", "HAS_PROPERTY")]),
    )
    assert 8 <= len(google_contacts) <= 12
    first_google_id = HUBSPOT_CONTACT_IDS.at(10**10)
    for contact in google_contacts:
        assert contact.google_campaign_ref in search_campaigns
        assert contact.google_touch_at is not None
        assert first_google_id <= int(contact.id) < first_google_id + 12
        assert cast(str, contact.email).startswith("google-contact-")
        values = linked.hubspot_properties(contact)
        assert (values["google_campaign_id"], values["google_touch_date"]) == (
            contact.google_campaign_ref,
            contact.google_touch_at,
        )
    plain = linked.hubspot_search(
        "contacts", [SearchFilter("google_campaign_id", "NOT_HAS_PROPERTY")]
    )
    assert plain
    assert linked.self_check() == ()
    from_spec = build(SPEC.with_hubspot(google_crm_properties=properties, google_touch_contacts=3))
    assert from_spec.google_crm_properties == properties


def test_bulk_deals_pile_into_one_utc_day_and_its_peak_hour() -> None:
    bulk = BulkDealSpec(day_offset=-1, count=60, peak_hour=12, peak_hour_count=40)
    piled = build(SPEC.with_hubspot(bulk_deals=bulk))
    day = midnight(ANCHOR - timedelta(days=1))

    def created(first: datetime, last: datetime) -> list[DealRecord]:
        found = piled.hubspot_search(
            "deals", [SearchFilter("createdate", "BETWEEN", ms(first), ms(last))]
        )
        return [cast(DealRecord, deal) for deal in found if deal.name.startswith("Bulk deal")]

    in_day = created(day, day + timedelta(days=1) - timedelta(milliseconds=1))
    assert len(in_day) == 60
    peak = created(day + timedelta(hours=12), day + timedelta(hours=13) - timedelta(milliseconds=1))
    assert len(peak) == 40
    assert all(not deal.contact_ids and deal.amount == Decimal(1000) for deal in in_day)
    assert piled.hubspot_deals([in_day[5].id]) == (in_day[5],)
    assert piled.hubspot_associations("deals", "contacts", in_day[5].id) == ()
    beyond = str(HUBSPOT_DEAL_IDS.at(3 * 10**10 + 60))
    assert piled.hubspot_deals([beyond]) == ()


def test_funnel_shares_control_contacts_and_their_timing() -> None:
    late = build(
        SPEC.with_(
            hubspot=hubspot_part().with_funnel(
                late_contact_share=Decimal(1),
                pre_touch_share=Decimal(0),
                pre_touch_deal_share=Decimal(0),
            )
        )
    )
    # A repeat lead can reuse a person from either account, so first touches span both.
    responses = sorted(
        (r for account in (NA, EMEA) for r in all_responses(late, account) if not r.test_lead),
        key=lambda response: response.submitted_at,
    )
    first_touch: dict[str, datetime] = {}
    for response in responses:
        first_touch.setdefault(response.email, response.submitted_at)
    checked = 0
    for email, touched in first_touch.items():
        contact = late.hubspot_contact_by_email_hash(sha256(email))
        if touched > NOW - timedelta(days=2):
            # Created two days after the touch: not there yet, found by a later retry.
            assert contact is None
        elif contact is not None:
            assert contact.created_at == touched + timedelta(days=2)
            checked += 1
    assert checked > 100
    retried = build(late.spec, now=NOW + timedelta(days=3))
    recent = [email for email, touched in first_touch.items() if touched > NOW - timedelta(days=2)]
    assert recent
    assert any(retried.hubspot_contact_by_email_hash(sha256(email)) for email in recent)

    unmatched = build(SPEC.with_(hubspot=hubspot_part().with_funnel(match_share=Decimal(0))))
    assert unmatched.hubspot_search("contacts", ()) == ()
    assert unmatched.hubspot_search("meetings", ()) == ()
    assert len(unmatched.hubspot_search("deals", ())) == 6
    assert all_responses(unmatched, NA)

    euro = build(SPEC.with_hubspot(deal_currency="EUR"))
    assert euro.currency_for("hubspot") == "EUR"
    assert {cast(DealRecord, deal).currency for deal in euro.hubspot_search("deals", ())} == {"EUR"}


# ---------------------------------------------------------------------------
# Faults
# ---------------------------------------------------------------------------


def test_faults_apply_their_times_then_stop() -> None:
    limited = Fault("google", "search", "http_status", status=429, retry_after=30, times=2)
    always = Fault("hubspot", "crm_search", "timeout")
    decorated = Fault(
        "linkedin", "lead_form_responses", "decoration_mismatch", ids=("lead-1",), times=1
    )
    faulty = build(SPEC.adding(faults=(limited, always, decorated)))
    assert faulty.faults_for("google", "search") == (limited,)
    assert faulty.consume_fault(limited)
    assert faulty.consume_fault(limited)
    assert not faulty.consume_fault(limited)
    assert faulty.faults_for("google", "search") == ()
    assert all(faulty.consume_fault(always) for _attempt in range(5))
    assert faulty.faults_for("hubspot", "crm_search") == (always,)
    assert faulty.faults_for("linkedin", "ad_campaigns") == ()
    # An equal fault is the spec's fault; counters belong to each world.
    twin = Fault("linkedin", "lead_form_responses", "decoration_mismatch", ids=("lead-1",), times=1)
    assert faulty.consume_fault(twin)
    assert not faulty.consume_fault(decorated)
    assert build(SPEC.adding(faults=(limited,))).consume_fault(limited)
    with pytest.raises(ValueError, match="not part of this world's spec"):
        faulty.consume_fault(Fault("google", "search", "timeout"))


def test_fault_match_predicates_must_all_hold() -> None:
    fault = Fault(
        "hubspot",
        "objects_batch_read",
        "omit_ids",
        ids=cast(tuple[str, ...], (1, 2)),
        match=cast(dict[str, str], {"object_type": "deals", "page": 2}),
    )
    assert fault.ids == ("1", "2")
    assert dict(fault.match) == {"object_type": "deals", "page": "2"}
    assert fault.matches({"object_type": "deals", "page": "2", "account_id": "990000001"})
    assert not fault.matches({"object_type": "deals"})
    assert not fault.matches({"object_type": "contacts", "page": "2"})
    assert Fault("google", "search", "timeout").matches({})
    with pytest.raises(TypeError):
        cast(dict[str, str], fault.match)["page"] = "3"


def test_fault_catalogues_name_every_contract_surface() -> None:
    assert set(FAULT_SURFACES) == {"google", "linkedin", "hubspot"}
    assert FAULT_SURFACES["google"][0] == "search"
    assert "associations_batch_read" in FAULT_SURFACES["hubspot"]
    # The segment fallback's two request forms are surfaces of their own, so a fault can
    # disturb the discovery call or the per-campaign request alone.
    assert FAULT_SURFACES["linkedin"][6:10] == (
        "ad_analytics",
        "ad_analytics_statistics",
        "ad_analytics_campaign_delivery",
        "ad_analytics_member_segments",
    )
    assert "http_status" in FAULT_KINDS
    assert set(SEGMENT_PIVOTS) == {"MEMBER_JOB_FUNCTION", "MEMBER_SENIORITY", "MEMBER_INDUSTRY"}


# ---------------------------------------------------------------------------
# Spec copies, partial worlds and registry validation
# ---------------------------------------------------------------------------


def test_spec_copies_replace_one_keyed_entity_and_revalidate() -> None:
    google = google_part()
    paused = google.with_campaign("g_brand", status="PAUSED")
    assert paused.campaigns[0].status == "PAUSED"
    assert paused.campaigns[1:] == google.campaigns[1:]
    assert google.campaigns[0].status == "ENABLED"
    assert google.with_budget("b_brand", amount=Decimal(1)).budgets[0].amount == 1
    assert google.with_ad_group("ag_brand", status="PAUSED").ad_groups[0].status == "PAUSED"
    assert google.with_keyword("kw_brand", quality_score=3).keywords[0].quality_score == 3
    assert google.with_search_term("acme analytics", status="NONE").search_terms[0].status == "NONE"
    assert (
        google.with_conversion_action("ca_demo", name="Demo").conversion_actions[0].name == "Demo"
    )
    linkedin = linkedin_part()
    assert linkedin.with_campaign("li_pipeline_na", status="PAUSED").campaigns[0].status == "PAUSED"
    assert linkedin.with_campaign_group("grp_na_brand", name="Brand").campaign_groups[2].name == (
        "Brand"
    )
    assert linkedin.with_creative("cr_pipeline_stat", headline="New").creatives[0].headline == "New"
    assert linkedin.with_lead_form(BENCHMARKS_FORM, name="Benchmarks").lead_forms[2].name == (
        "Benchmarks"
    )
    funnel = hubspot_part().with_funnel(match_share=Decimal("0.5")).funnel
    assert (funnel.match_share, funnel.mql_share) == (Decimal("0.5"), FunnelSpec().mql_share)
    assert SPEC.with_google(currency="EUR").google == google.with_(currency="EUR")
    fault = Fault("google", "search", "timeout")
    change = PlatformChange("google", "campaign", "g_brand", field="status", value="PAUSED", at=NOW)
    added = SPEC.adding(faults=[fault], platform_changes=[change])
    assert (added.faults, added.platform_changes) == ((fault,), (change,))
    assert (SPEC.faults, SPEC.platform_changes) == ((), ())

    with pytest.raises(KeyError, match="no item with key='g_missing'"):
        google.with_campaign("g_missing", status="PAUSED")
    with pytest.raises(ValueError, match="names unknown budget 'b_missing'"):
        google.with_campaign("g_brand", budget_key="b_missing")
    with pytest.raises(TypeError):
        google.with_(colour="blue")
    empty = WorldSpec("empty")
    with pytest.raises(ValueError, match="has no Google part"):
        empty.with_google(currency="EUR")
    with pytest.raises(ValueError, match="has no LinkedIn part"):
        empty.with_linkedin(test_lead_share=Decimal(0))
    with pytest.raises(ValueError, match="has no HubSpot part"):
        empty.with_hubspot(orphan_deals=1)


def test_a_world_simulates_only_the_parts_its_spec_has() -> None:
    google_only = build(WorldSpec("google-only", anchor=ANCHOR, google=google_part()))
    assert google_only.self_check() == ()
    assert google_only.linkedin_account_ids == ()
    assert google_only.currency_for("linkedin") is None
    assert google_only.currency_for("hubspot") is None
    with pytest.raises(LookupError, match="has no LinkedIn part"):
        google_only.linkedin_accounts()
    with pytest.raises(LookupError, match="has no HubSpot part"):
        google_only.hubspot_lifecycle_options()

    linkedin_only = build(WorldSpec("linkedin-only", anchor=ANCHOR, linkedin=linkedin_part()))
    assert linkedin_only.self_check() == ()
    assert linkedin_only.currency_for("google") is None
    with pytest.raises(LookupError, match="has no Google part"):
        _ = linkedin_only.google_customer_id
    with pytest.raises(LookupError, match="has no Google part"):
        linkedin_only.google_campaigns()

    crm_only = build(WorldSpec("crm-only", anchor=ANCHOR, hubspot=HubSpotSpec(orphan_deals=2)))
    assert crm_only.self_check() == ()
    assert crm_only.hubspot_search("contacts", ()) == ()
    assert len(crm_only.hubspot_search("deals", ())) == 2


def test_content_assets_must_name_entities_of_the_simulated_world() -> None:
    live_form = ContentAsset("live-paper", "Live paper", ("urn:li:leadGenForm:123456",))
    with pytest.raises(ValueError, match="must use the reserved simulated LinkedIn ids"):
        build(content_assets=(live_form,))
    live_creative = ContentAsset(
        "live-override", "Live override", (), ("urn:li:sponsoredCreative:42",)
    )
    with pytest.raises(ValueError, match="lies outside LINKEDIN_CREATIVE_IDS"):
        build(content_assets=(live_creative,))
    missing_form = ContentAsset("lost-paper", "Lost paper", ("urn:li:leadGenForm:990000999",))
    with pytest.raises(ValueError, match="lacks the registry's LinkedIn entities"):
        build(content_assets=(missing_form,))
    missing_creative = ContentAsset(
        "lost-override", "Lost override", (), ("urn:li:sponsoredCreative:990009999",)
    )
    with pytest.raises(ValueError, match="creative urn:li:sponsoredCreative:990009999"):
        build(content_assets=(missing_creative,))
    assert build(content_assets=()).content_assets == ()


def test_platform_changes_must_name_entities_of_the_world() -> None:
    def changed(*changes: PlatformChange, spec: WorldSpec = SPEC) -> SimulatedWorld:
        return build(spec.adding(platform_changes=changes))

    with pytest.raises(ValueError, match="names unknown google campaign 'g_missing'"):
        changed(
            PlatformChange(
                "google", "campaign", "g_missing", field="status", value="PAUSED", at=NOW
            )
        )
    with pytest.raises(ValueError, match="moves g_brand to unknown budget 'b_missing'"):
        changed(
            PlatformChange(
                "google", "campaign", "g_brand", field="budget_key", value="b_missing", at=NOW
            )
        )
    with pytest.raises(ValueError, match="names unknown linkedin creative 'cr_missing'"):
        changed(
            PlatformChange(
                "linkedin", "creative", "cr_missing", field="status", value="PAUSED", at=NOW
            )
        )
    google_only = WorldSpec("google-only", anchor=ANCHOR, google=google_part())
    with pytest.raises(ValueError, match="names unknown linkedin campaign 'li_pipeline_na'"):
        changed(
            PlatformChange(
                "linkedin", "campaign", "li_pipeline_na", field="status", value="PAUSED", at=NOW
            ),
            spec=google_only,
        )


def test_a_config_registry_naming_live_linkedin_ids_is_set_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for name in CONFIG_FILES:
        (config / name).write_text((CONFIG_DIR / name).read_text(encoding="utf-8"), "utf-8")
    (config / CONTENT_ASSETS_FILE).write_text(
        "assets:\n"
        "  - slug: live-paper\n"
        "    title: Live white paper\n"
        "    lead_forms:\n"
        '      - "urn:li:leadGenForm:123456"\n'
        "    creative_overrides: []\n",
        "utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loaded = SimulatedWorld(42, SPEC, clock=Clock(NOW))
    # Simulating a source must still work when the registry names a live account's forms.
    assert loaded.content_assets == ()
    assert loaded.mapping == Thresholds.load(config).mapping
    assert loaded.self_check() == ()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable ``Settings`` could read, so only explicit arguments count.

    ``monkeypatch`` restores the environment and working directory after the test.
    """
    names = {name.lower() for name in Settings.model_fields}
    for field in Settings.model_fields.values():
        for alias in (field.alias, field.validation_alias):
            if isinstance(alias, str):
                names.add(alias.lower())
            elif isinstance(alias, AliasChoices):
                names.update(choice.lower() for choice in alias.choices if isinstance(choice, str))
    for key in list(os.environ):
        if key.lower() in names:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(REPO_ROOT)


@pytest.mark.usefixtures("isolated_environment")
def test_from_settings_seeds_the_world_and_follows_the_google_crm_link() -> None:
    shipped = Thresholds.load(CONFIG_DIR)
    seeded = SimulatedWorld.from_settings(
        Settings(data_mode="simulated", mock_seed=7), SPEC, clock=Clock(NOW)
    )
    assert seeded.seed == 7
    assert seeded.google_crm_properties is None
    assert seeded.content_assets == shipped.content_assets
    assert seeded.mapping == shipped.mapping
    assert seeded.google_campaign_daily(*full_days(7)) == build(seed=7).google_campaign_daily(
        *full_days(7)
    )
    linked = SimulatedWorld.from_settings(
        Settings(
            data_mode="simulated",
            hubspot_google_campaign_property=" google_campaign ",
            hubspot_google_touch_date_property="google_touch",
        ),
        SPEC,
        clock=Clock(NOW),
    )
    assert (linked.seed, linked.google_crm_properties) == (42, ("google_campaign", "google_touch"))
    half = SimulatedWorld.from_settings(
        Settings(data_mode="simulated", hubspot_google_campaign_property="google_campaign"),
        SPEC,
        clock=Clock(NOW),
    )
    assert half.google_crm_properties is None


# ---------------------------------------------------------------------------
# Spec validation: every invalid value raises ValueError naming the rule
# ---------------------------------------------------------------------------

_P = DailyProfile(impressions_mean=100, ctr=Decimal("0.05"), cpc=Decimal("1.00"))
_V1 = FormVersionSpec(1, (QuestionSpec(1, "email", "EMAIL"),))
_ACCOUNT = LinkedInAccountSpec(NA, "Account")
_GROUP = LinkedInCampaignGroupSpec("grp", NA, "Group")
_CAMPAIGN = LinkedInCampaignSpec("cmp", NA, "grp", "Campaign")
_FORM = LinkedInLeadFormSpec(PIPELINE_FORM, NA, "Form", (_V1,))
_BUDGET = GoogleBudgetSpec("b", Decimal(10))
_G_CAMPAIGN = GoogleCampaignSpec("c", "Campaign", "b")
_AD_GROUP = GoogleAdGroupSpec("ag", "c", "Ad group")


def _li(**changes: Any) -> LinkedInSpec:
    base: dict[str, Any] = {
        "accounts": (_ACCOUNT,),
        "campaign_groups": (_GROUP,),
        "campaigns": (_CAMPAIGN,),
        "lead_forms": (_FORM,),
    }
    return LinkedInSpec(**(base | changes))


def _gs(**changes: Any) -> GoogleSpec:
    base: dict[str, Any] = {
        "budgets": (_BUDGET,),
        "campaigns": (_G_CAMPAIGN,),
        "ad_groups": (_AD_GROUP,),
    }
    return GoogleSpec(**(base | changes))


def _decimal(value: object) -> Decimal:
    return cast(Decimal, value)


INVALID_SPECS: list[Any] = [
    # DayMetrics
    pytest.param(lambda: DayMetrics(impressions=4, clicks=5), "clicks cannot exceed", id="clicks"),
    pytest.param(lambda: DayMetrics(form_opens=1, leads=2), "leads cannot exceed", id="leads"),
    pytest.param(lambda: DayMetrics(cost=Decimal(-1)), "cost must be >= 0", id="negative-cost"),
    pytest.param(lambda: DayMetrics(cost=_decimal(1.5)), "cost must be a Decimal", id="float"),
    pytest.param(
        lambda: DayMetrics(conversions=Decimal("NaN")), "conversions must be finite", id="nan"
    ),
    pytest.param(
        lambda: DayMetrics(conversions=Decimal("0.0000001")),
        "at most 6 decimal places",
        id="seven-decimals",
    ),
    pytest.param(
        lambda: DayMetrics(budget_lost_is=Decimal("1.5")),
        "budget_lost_is must be <= 1",
        id="lost-share",
    ),
    pytest.param(
        lambda: DayMetrics(historical_quality_score=11),
        "historical_quality_score must be <= 10",
        id="qs-high",
    ),
    pytest.param(
        lambda: DayMetrics(historical_quality_score=0),
        "historical_quality_score must be >= 1",
        id="qs-low",
    ),
    pytest.param(
        lambda: DayMetrics(impressions=cast(int, True)),
        "impressions must be an integer",
        id="bool-count",
    ),
    pytest.param(
        lambda: DayMetrics(average_cpc=Decimal("-0.5")), "average_cpc must be >= 0", id="cpc"
    ),
    # DailyProfile, ExplicitDays, QualityScoreProfile
    pytest.param(
        lambda: _P.with_(form_open_rate=Decimal("0.06")),
        "form_open_rate cannot exceed ctr",
        id="opens-above-ctr",
    ),
    pytest.param(lambda: _P.with_(ctr=Decimal(2)), "ctr must be <= 1", id="ctr"),
    pytest.param(lambda: _P.with_(noise=Decimal("0.95")), "noise must be <= 0.9", id="noise"),
    pytest.param(lambda: _P.with_(trend=Decimal(-2)), "trend must be >= -1", id="trend"),
    pytest.param(
        lambda: _P.with_(active_from_offset=5, active_until_offset=1),
        "active_from_offset must not be after active_until_offset",
        id="active-window",
    ),
    pytest.param(
        lambda: _P.with_(trend_from_offset="soon"),
        "trend_from_offset must be an integer",
        id="trend-offset",
    ),
    pytest.param(lambda: _P.with_(impressions_mean=-1), "impressions_mean must be >= 0", id="mean"),
    pytest.param(
        lambda: ExplicitDays({cast(int, "1"): DayMetrics()}),
        "ExplicitDays offset must be an integer",
        id="explicit-key",
    ),
    pytest.param(
        lambda: ExplicitDays({0: cast(DayMetrics, "busy")}),
        "ExplicitDays values must be DayMetrics",
        id="explicit-value",
    ),
    pytest.param(lambda: QualityScoreProfile(11), "score must be <= 10", id="qs-profile"),
    pytest.param(
        lambda: QualityScoreProfile(7, change_offset=-3),
        "change_offset and changed_score must be set together",
        id="qs-pairing",
    ),
    pytest.param(
        lambda: QualityScoreProfile(7, present_share=Decimal(2)),
        "present_share must be <= 1",
        id="qs-share",
    ),
    # Google entity specs
    pytest.param(
        lambda: GoogleBudgetSpec("b", Decimal(0)), "amount must be >= 0.000001", id="budget-zero"
    ),
    pytest.param(
        lambda: GoogleBudgetSpec("b", Decimal(1), period=cast(Any, "MONTHLY")),
        "period must be one of",
        id="budget-period",
    ),
    pytest.param(
        lambda: GoogleBudgetSpec(" ", Decimal(1)),
        "GoogleBudgetSpec.key must be a non-blank string",
        id="budget-key",
    ),
    pytest.param(
        lambda: _G_CAMPAIGN.with_(status="ACTIVE"), "status must be one of", id="campaign-status"
    ),
    pytest.param(
        lambda: _G_CAMPAIGN.with_(channel="RADIO"), "channel must be one of", id="campaign-channel"
    ),
    pytest.param(
        lambda: _G_CAMPAIGN.with_(profile="steady"),
        "profile must be a DailyProfile, ExplicitDays or None",
        id="campaign-profile",
    ),
    pytest.param(
        lambda: _AD_GROUP.with_(name=""), "ad group ag name must be a non-blank", id="ad-group"
    ),
    pytest.param(
        lambda: GoogleKeywordSpec("k", "ag", "text", match_type=cast(Any, "LOOSE")),
        "match_type must be one of",
        id="match-type",
    ),
    pytest.param(
        lambda: GoogleKeywordSpec("k", "ag", "text", quality_score=11),
        "quality_score must be <= 10",
        id="keyword-qs",
    ),
    pytest.param(
        lambda: GoogleKeywordSpec("k", "ag", "text", qs_profile=cast(Any, 7)),
        "qs_profile must be a QualityScoreProfile or None",
        id="keyword-qs-profile",
    ),
    pytest.param(
        lambda: GoogleSearchTermSpec("term", "ag", None, status=cast(Any, "BLOCKED")),
        "status must be one of",
        id="term-status",
    ),
    pytest.param(
        lambda: GoogleConversionActionSpec("a", "A", counting_type=cast(Any, "EVERY")),
        "counting_type must be one of",
        id="counting",
    ),
    pytest.param(
        lambda: GoogleConversionActionSpec("a", "A", attribution_model="LINEAR"),
        "attribution_model must be one of",
        id="attribution",
    ),
    pytest.param(
        lambda: GoogleConversionActionSpec(
            "a", "A", model_changes=((5, "EXTERNAL"), (5, "GOOGLE_ADS_LAST_CLICK"))
        ),
        "model_changes offsets must strictly increase",
        id="model-order",
    ),
    pytest.param(
        lambda: GoogleConversionActionSpec("a", "A", model_changes=((5, "POSITION_BASED"),)),
        "model change must be one of",
        id="model-name",
    ),
    pytest.param(lambda: GoogleBulkSpec(campaigns=-1), "campaigns must be >= 0", id="bulk-count"),
    # GoogleSpec cross-references
    pytest.param(
        lambda: _gs(customer_id="990-000-0001"), "customer_id must be 10 digits", id="customer"
    ),
    pytest.param(lambda: _gs(currency="US"), "currency must be an ISO 4217 code", id="currency"),
    pytest.param(lambda: _gs(time_zone="Mars/Olympus"), "is not an IANA zone", id="time-zone"),
    pytest.param(
        lambda: _gs(budgets=(_BUDGET, _BUDGET)), "budget 'b' is defined twice", id="dup-budget"
    ),
    pytest.param(
        lambda: _gs(campaigns=(_G_CAMPAIGN.with_(budget_key="x"),)),
        "campaign c names unknown budget 'x'",
        id="campaign-budget",
    ),
    pytest.param(
        lambda: _gs(ad_groups=(_AD_GROUP.with_(campaign_key="x"),)),
        "ad group ag names unknown campaign 'x'",
        id="group-campaign",
    ),
    pytest.param(
        lambda: _gs(keywords=(GoogleKeywordSpec("k", "x", "text"),)),
        "keyword k names unknown ad group 'x'",
        id="keyword-group",
    ),
    pytest.param(
        lambda: _gs(search_terms=(GoogleSearchTermSpec("term", "x", None),)),
        "search term 'term' names unknown ad group 'x'",
        id="term-group",
    ),
    pytest.param(
        lambda: _gs(
            ad_groups=(_AD_GROUP, GoogleAdGroupSpec("ag2", "c", "Other")),
            keywords=(GoogleKeywordSpec("k", "ag2", "text"),),
            search_terms=(GoogleSearchTermSpec("term", "ag", "k"),),
        ),
        "must match a keyword of ad group 'ag'",
        id="term-keyword",
    ),
    pytest.param(
        lambda: _gs(bulk=cast(Any, 5)), "bulk must be a GoogleBulkSpec or None", id="bulk-type"
    ),
    # LinkedIn entity specs
    pytest.param(
        lambda: LinkedInAccountSpec(12, "A"), "lies outside LINKEDIN_ACCOUNT_IDS", id="account"
    ),
    pytest.param(
        lambda: _ACCOUNT.with_(currency="EURO"), "currency must be an ISO 4217", id="account-ccy"
    ),
    pytest.param(
        lambda: _GROUP.with_(status="COMPLETED"), "status must be one of", id="group-completed"
    ),
    pytest.param(
        lambda: _GROUP.with_(daily_budget=Decimal("1.005")),
        "at most 2 decimal places",
        id="group-budget",
    ),
    pytest.param(
        lambda: _GROUP.with_(run_start_offset="soon"),
        "run_start_offset must be an integer",
        id="group-schedule",
    ),
    pytest.param(
        lambda: _CAMPAIGN.with_(objective="SALES"), "objective must be one of", id="objective"
    ),
    pytest.param(
        lambda: _CAMPAIGN.with_(total_budget=Decimal("0.001")),
        "at most 2 decimal places",
        id="campaign-budget-places",
    ),
    pytest.param(
        lambda: LinkedInCreativeSpec("cr", "cmp", "H", "I", id=5),
        "creative id 5 lies outside LINKEDIN_CREATIVE_IDS",
        id="creative-id",
    ),
    pytest.param(
        lambda: LinkedInCreativeSpec("cr", "cmp", "H", "I", lead_form_id=5),
        "lead_form_id lies outside LINKEDIN_FORM_IDS",
        id="creative-form",
    ),
    pytest.param(
        lambda: LinkedInCreativeSpec("cr", "cmp", "H", "I", cta_label=cast(Any, "BUY_NOW")),
        "cta_label must be one of",
        id="cta",
    ),
    pytest.param(
        lambda: LinkedInCreativeSpec("cr", "cmp", "H", "I", intended_status=cast(Any, "DONE")),
        "intended_status must be one of",
        id="creative-status",
    ),
    pytest.param(lambda: QuestionSpec(0, "email"), "question_id must be >= 1", id="question"),
    pytest.param(lambda: FormVersionSpec(1, ()), "needs questions", id="version-empty"),
    pytest.param(
        lambda: FormVersionSpec(1, (QuestionSpec(1, "a"), QuestionSpec(1, "b"))),
        "question id 1 is defined twice",
        id="version-dup",
    ),
    pytest.param(
        lambda: LinkedInLeadFormSpec(5, NA, "Form", (_V1,)),
        "lead form id 5 lies outside LINKEDIN_FORM_IDS",
        id="form-id",
    ),
    pytest.param(
        lambda: _FORM.with_(versions=()), "needs at least one version", id="form-no-version"
    ),
    pytest.param(
        lambda: _FORM.with_(versions=(_V1.with_(version=2), _V1.with_(active_from_offset=-5))),
        "versions must strictly increase",
        id="form-order",
    ),
    pytest.param(
        lambda: _FORM.with_(versions=(_V1, _V1.with_(version=2))),
        "version 2 must activate after version 1",
        id="form-activation",
    ),
    pytest.param(
        lambda: SegmentValueSpec("urn:li:seniority:6", Decimal("0.1"), Decimal(0)),
        "cpl_factor must be >= 0.01",
        id="segment-factor",
    ),
    pytest.param(
        lambda: SegmentValueSpec("urn:li:seniority:6", Decimal("1.5")),
        "impression_share must be <= 1",
        id="segment-share",
    ),
    pytest.param(
        lambda: LinkedInSegmentSpec("cmp", cast(Any, "MEMBER_COUNTRY")),
        "pivot must be one of",
        id="segment-pivot",
    ),
    pytest.param(
        lambda: LinkedInSegmentSpec(
            "cmp", "MEMBER_SENIORITY", (SegmentValueSpec("urn:li:function:1", Decimal("0.1")),)
        ),
        "must start with urn:li:seniority:",
        id="segment-prefix",
    ),
    pytest.param(
        lambda: LinkedInSegmentSpec(
            "cmp",
            "MEMBER_SENIORITY",
            (
                SegmentValueSpec("urn:li:seniority:6", Decimal("0.6")),
                SegmentValueSpec("urn:li:seniority:7", Decimal("0.6")),
            ),
        ),
        "segment shares of cmp/MEMBER_SENIORITY exceed 1",
        id="segment-total",
    ),
    # LinkedInSpec cross-references
    pytest.param(
        lambda: _li(accounts=(_ACCOUNT, _ACCOUNT)),
        "LinkedIn account 990000001 is defined twice",
        id="dup-account",
    ),
    pytest.param(
        lambda: _li(campaign_groups=(_GROUP.with_(account_id=EMEA),), campaigns=()),
        "campaign group grp names unknown account 990000002",
        id="group-account",
    ),
    pytest.param(
        lambda: _li(campaigns=(_CAMPAIGN.with_(group_key="nope"),)),
        "campaign cmp must name a campaign group of account 990000001",
        id="campaign-group",
    ),
    pytest.param(
        lambda: _li(lead_forms=(_FORM.with_(account_id=EMEA),)),
        "lead form 990000101 names unknown account 990000002",
        id="form-account",
    ),
    pytest.param(
        lambda: _li(creatives=(LinkedInCreativeSpec("cr", "nope", "H", "I"),)),
        "creative cr names unknown campaign 'nope'",
        id="creative-campaign",
    ),
    pytest.param(
        lambda: _li(
            creatives=(LinkedInCreativeSpec("cr", "cmp", "H", "I", lead_form_id=EMEA_FORM),)
        ),
        "creative cr must use a lead form of account 990000001",
        id="creative-foreign-form",
    ),
    pytest.param(
        lambda: _li(segments=(LinkedInSegmentSpec("nope", "MEMBER_SENIORITY"),)),
        "segment names unknown campaign 'nope'",
        id="segment-campaign",
    ),
    pytest.param(
        lambda: _li(
            creatives=(
                LinkedInCreativeSpec("a", "cmp", "H", "I", id=LANDING_CREATIVE),
                LinkedInCreativeSpec("b", "cmp", "H", "I", id=LANDING_CREATIVE),
            )
        ),
        "creative id 990001301 is defined twice",
        id="dup-creative-id",
    ),
    pytest.param(
        lambda: _li(test_lead_share=Decimal("0.6")), "test_lead_share must be <= 0.5", id="tests"
    ),
    pytest.param(
        lambda: _li(segment_top_values=0), "segment_top_values must be >= 1", id="top-values"
    ),
    # HubSpot specs
    pytest.param(
        lambda: StageSpec("Custom", "12"), "lies outside HUBSPOT_STAGE_IDS", id="stage-id"
    ),
    pytest.param(
        lambda: FunnelSpec(match_share=Decimal("1.5")),
        "match_share must be <= 1",
        id="funnel-share",
    ),
    pytest.param(
        lambda: FunnelSpec(late_contact_delay=timedelta(hours=-1)),
        "late_contact_delay must be a non-negative timedelta",
        id="late-delay",
    ),
    pytest.param(
        lambda: FunnelSpec(meeting_sources=()), "needs at least one source", id="no-sources"
    ),
    pytest.param(
        lambda: FunnelSpec(meeting_sources=(("PHONE", Decimal(1)),)),
        "meeting source must be one of",
        id="source-name",
    ),
    pytest.param(
        lambda: FunnelSpec(meeting_sources=(("CRM_UI", Decimal(0)),)),
        "weights must not all be zero",
        id="source-weights",
    ),
    pytest.param(
        lambda: FunnelSpec(mql_delay_days=(5, 1)),
        "mql_delay_days must be (minimum, maximum)",
        id="delay-window",
    ),
    pytest.param(lambda: BulkDealSpec(day_offset=0, count=0), "count must be >= 1", id="bulk-0"),
    pytest.param(
        lambda: BulkDealSpec(day_offset=0, count=5, peak_hour=24),
        "peak_hour must be 0-23",
        id="bulk-hour",
    ),
    pytest.param(
        lambda: BulkDealSpec(day_offset=0, count=5, peak_hour_count=6),
        "peak_hour_count cannot exceed count",
        id="bulk-peak",
    ),
    pytest.param(
        lambda: HubSpotSpec(custom_stages=(StageSpec("Lead", str(HUBSPOT_STAGE_IDS.at(5))),)),
        "repeats a default stage label",
        id="stage-default-label",
    ),
    pytest.param(
        lambda: HubSpotSpec(
            custom_stages=(
                StageSpec("Custom", str(HUBSPOT_STAGE_IDS.at(5))),
                StageSpec("Custom", str(HUBSPOT_STAGE_IDS.at(6))),
            )
        ),
        "custom stage label 'Custom' is defined twice",
        id="stage-dup",
    ),
    pytest.param(lambda: HubSpotSpec(orphan_deals=-1), "orphan_deals must be >= 0", id="orphans"),
    pytest.param(
        lambda: HubSpotSpec(association_page_size=0),
        "association_page_size must be >= 1",
        id="page-size",
    ),
    pytest.param(
        lambda: HubSpotSpec(live_lead_match_share=Decimal("0.12345")),
        "at most 4 decimal places",
        id="match-share",
    ),
    pytest.param(
        lambda: HubSpotSpec(google_crm_properties=cast(Any, ("campaign",))),
        "google_crm_properties must be",
        id="crm-properties",
    ),
    pytest.param(
        lambda: HubSpotSpec(deal_currency="E"), "deal_currency must be an ISO 4217", id="deal-ccy"
    ),
    # Faults
    pytest.param(
        lambda: Fault(cast(Any, "bing"), "search", "timeout"),
        "Fault.source must be one of",
        id="fault-source",
    ),
    pytest.param(
        lambda: Fault("google", "lead_forms", "timeout"),
        "Fault.surface for google must be one of",
        id="fault-surface",
    ),
    pytest.param(
        lambda: Fault("google", "search", cast(Any, "explode")),
        "Fault.kind must be one of",
        id="fault-kind",
    ),
    pytest.param(
        lambda: Fault("google", "search", "http_status"),
        "needs a status between 400 and 599",
        id="fault-no-status",
    ),
    pytest.param(
        lambda: Fault("google", "search", "http_status", status=302),
        "needs a status between 400 and 599",
        id="fault-redirect",
    ),
    pytest.param(
        lambda: Fault("hubspot", "crm_search", "body_error", code="X"),
        "body_error faults are Google",
        id="fault-body-source",
    ),
    pytest.param(
        lambda: Fault("google", "search", "body_error"),
        "needs a Google error code",
        id="fault-body-code",
    ),
    pytest.param(
        lambda: Fault("hubspot", "objects_batch_read", "batch_errors", category="X"),
        "a batch_errors fault needs ids",
        id="fault-batch-ids",
    ),
    pytest.param(
        lambda: Fault("hubspot", "objects_batch_read", "batch_errors", ids=("1",)),
        "a batch_errors fault needs a category",
        id="fault-batch-category",
    ),
    pytest.param(
        lambda: Fault("hubspot", "objects_batch_read", "omit_ids"),
        "a omit_ids fault needs ids",
        id="fault-omit",
    ),
    pytest.param(
        lambda: Fault("hubspot", "objects_batch_read", "non_complete_status", batch_status="DONE"),
        "batch_status must be one of",
        id="fault-batch-status",
    ),
    pytest.param(
        lambda: Fault("hubspot", "objects_batch_read", "continuation_failure", ids=("1",)),
        "continuation_failure applies to associations_batch_read",
        id="fault-continuation",
    ),
    pytest.param(
        lambda: Fault("linkedin", "lead_forms", "no_decoration", ids=("1",)),
        "no_decoration applies to lead form responses",
        id="fault-decoration",
    ),
    pytest.param(
        lambda: Fault("google", "search", "timeout", times=0),
        "Fault.times must be >= 1",
        id="fault-times",
    ),
    pytest.param(
        lambda: Fault("google", "search", "timeout", retry_after=-1),
        "Fault.retry_after must be >= 0",
        id="fault-retry",
    ),
    # Platform changes
    pytest.param(
        lambda: PlatformChange("google", "creative", "c", field="status", value="X", at=NOW),
        "do not support google creative entities",
        id="change-entity",
    ),
    pytest.param(
        lambda: PlatformChange(
            "google", "campaign", "c", field="status", value="X", at=NOW, at_offset_hours=1
        ),
        "exactly one of at and at_offset_hours",
        id="change-two-instants",
    ),
    pytest.param(
        lambda: PlatformChange("google", "campaign", "c", field="status", value="X"),
        "exactly one of at and at_offset_hours",
        id="change-no-instant",
    ),
    pytest.param(
        lambda: PlatformChange(
            "google", "campaign", "c", field="status", value="X", at=NOW.replace(tzinfo=None)
        ),
        "PlatformChange.at must be timezone-aware",
        id="change-naive",
    ),
    pytest.param(
        lambda: PlatformChange(
            "google", "campaign", "c", field="status", disappear=True, at_offset_hours=0
        ),
        "a disappearance changes no field",
        id="change-disappear-field",
    ),
    pytest.param(
        lambda: PlatformChange("google", "budget", "b", field="status", value="X", at=NOW),
        "google budget changes accept amount",
        id="change-field",
    ),
    pytest.param(
        lambda: PlatformChange(
            "google", "campaign", "c", field="status", value="X", at=NOW, reappear_at=NOW
        ),
        "only a disappearance can reappear",
        id="change-reappear",
    ),
    pytest.param(
        lambda: PlatformChange("google", "campaign", "c", field="status", value=" ", at=NOW),
        "status value must be a non-blank string",
        id="change-status",
    ),
    pytest.param(
        lambda: PlatformChange("google", "budget", "b", field="amount", value=Decimal(0), at=NOW),
        "amount must be >= 0.000001",
        id="change-amount",
    ),
    pytest.param(
        lambda: PlatformChange(
            "linkedin", "campaign", "c", field="daily_budget", value=Decimal("1.234"), at=NOW
        ),
        "at most 2 decimal places",
        id="change-linkedin-budget",
    ),
    pytest.param(
        lambda: PlatformChange(
            "google",
            "campaign",
            "c",
            disappear=True,
            at=NOW,
            reappear_at=NOW,
            reappear_at_offset_hours=1,
        ),
        "set at most one of reappear_at and reappear_at_offset_hours",
        id="change-two-reappearances",
    ),
    pytest.param(
        lambda: PlatformChange(
            "google", "campaign", "c", disappear=True, at=NOW, reappear_at=NOW.replace(tzinfo=None)
        ),
        "reappear_at must be timezone-aware",
        id="change-naive-reappearance",
    ),
    # WorldSpec and SearchFilter
    pytest.param(lambda: WorldSpec(" "), "WorldSpec.name must be a non-blank", id="world-name"),
    pytest.param(
        lambda: WorldSpec("w", history_days=0), "history_days must be >= 1", id="history-0"
    ),
    pytest.param(
        lambda: WorldSpec("w", history_days=4000),
        "history_days must be at most 3650",
        id="history-long",
    ),
    pytest.param(lambda: WorldSpec("w", anchor=NOW), "anchor must be a date", id="anchor-datetime"),
    pytest.param(
        lambda: WorldSpec("w", faults=cast(Any, ("boom",))),
        "faults must be Fault instances",
        id="world-faults",
    ),
    pytest.param(
        lambda: WorldSpec("w", platform_changes=cast(Any, ("pause",))),
        "platform_changes must be PlatformChange",
        id="world-changes",
    ),
    pytest.param(
        lambda: SearchFilter("email", "LIKE", "x"),
        "SearchFilter.operator must be one of",
        id="filter-operator",
    ),
    pytest.param(lambda: SearchFilter("email", "EQ"), "EQ needs a value", id="filter-value"),
    pytest.param(
        lambda: SearchFilter("createdate", "BETWEEN", 1),
        "BETWEEN needs value and high_value",
        id="filter-between",
    ),
    pytest.param(lambda: SearchFilter("email", "IN"), "IN needs values", id="filter-in"),
    pytest.param(
        lambda: SearchFilter(" ", "HAS_PROPERTY"),
        "SearchFilter.property must be a non-blank",
        id="filter-property",
    ),
]


@pytest.mark.parametrize(("factory", "message"), INVALID_SPECS)
def test_invalid_specs_raise_value_errors_naming_the_rule(
    factory: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        factory()


def test_spec_values_are_frozen_read_only_copies() -> None:
    days = {0: DayMetrics(impressions=1)}
    explicit = ExplicitDays(days)
    days[1] = DayMetrics()
    assert list(explicit.days) == [0]
    with pytest.raises(TypeError):
        cast(dict[int, DayMetrics], explicit.days)[2] = DayMetrics()
    assert DayMetrics().is_zero
    assert not DayMetrics(impressions=1).is_zero
    assert _P.is_active(0)
    assert not _P.with_(active_from_offset=1).is_active(0)
    assert not _P.with_(active_until_offset=-1).is_active(0)
    assert _P.trend_factor(10, -210) == 1.0
    assert _P.with_(trend=Decimal("-0.5")).trend_factor(10, 0) == pytest.approx(0.05)
    assert QualityScoreProfile(7, change_offset=-3, changed_score=4).score_at(-3) == 4
    assert QualityScoreProfile(7, change_offset=-3, changed_score=4).score_at(-4) == 7
    change = PlatformChange(
        "google",
        "campaign",
        "g_brand",
        disappear=True,
        at_offset_hours=-24,
        reappear_at_offset_hours=24,
    )
    assert change.instant(ANCHOR) == midnight(ANCHOR) - timedelta(hours=24)
    assert change.reappear_instant(ANCHOR) == midnight(ANCHOR) + timedelta(hours=24)
    eastern = datetime(2026, 9, 1, 8, tzinfo=ZoneInfo("America/New_York"))
    absolute = PlatformChange("google", "campaign", "g_brand", disappear=True, at=eastern)
    assert (absolute.instant(ANCHOR), absolute.reappear_instant(ANCHOR)) == (
        eastern.astimezone(UTC),
        None,
    )
    assert absolute.at is not None
    assert absolute.at.tzinfo is UTC
