"""The scenario catalogue: each rule, guard and guarantee made to fire and not to fire.

AAP 0.5.1 (Simulation: "a scenario catalog that makes each rule fire and not fire", interface
``SCENARIOS``) and AAP 0.11.2 (acceptance tests): each acceptance test runs against a scenario of
this module, built so that the guarantee holds in one case and, with a single input moved across
its threshold, does not hold in its twin. Integration tests take their transport-level worlds
(faults, bulk filler, platform changes) from here as well, and the ``default`` scenario is the
end-to-end world in which every section populates on first start.

What a scenario holds:

* a :class:`~ads_engine.simulation.generator.WorldSpec`, the seeded world behind the simulated
  sources (``Scenario.world()`` builds it; the mock transport serves it on the wire);
* a :class:`ContextSpec`, everything a database round trip would add on top of the world: the
  analysis window, threshold overrides, snapshot cadence, source modes, dataset freshness,
  recorded decisions, stored discrepancies, feedback and the CRM bindings;
* an :class:`Expectation`, what the rule output on that context must show.

``Scenario.build_context()`` turns the world into an
:class:`~ads_engine.analysis.context.AnalysisContext` through :func:`context_from_world`, a pure
stand-in for "the sync jobs store the world, then ``analysis.metrics.load_context`` reads it
back", so rule tests need no database. ``backend/tests/integration/test_end_to_end_simulated.py``
cross-checks that the default world yields the same sections either way.

Time: every scenario except ``default`` is anchored on :data:`FIXED_ANCHOR` and read at
:data:`FIXED_NOW` (:func:`FIXED_CLOCK`), so fixtures and expected numbers are reproducible. At that
instant the default window ends on 2026-10-01 (yesterday in both New York and UTC) and spans
2026-09-04 to 2026-10-01; day offsets in specs are relative to the anchor, so the window is
offsets -28 to -1. The ``default`` scenario runs on the real clock unless the caller passes one.

Naming: ``"<topic>.<case>.<variant>"`` with variant ``fires``, ``does_not_fire`` or ``case``
(for example ``k1.gate.fires`` and its twin ``k1.gate.does_not_fire``). The variant names the
guarantee's condition: present in ``fires`` (the rule emits its item, or the flag, finding or
withholding the guarantee names is set), moved across its threshold in ``does_not_fire``.
``Expectation.fires`` separately says whether the rule under test emits its item, so the two can
differ (the coverage twin still emits K1, only without the coverage flag).

Boundaries: imports ``ads_engine.simulation`` (reserved ids), ``simulation.generator``,
``analysis.context``, ``analysis.window``, ``analysis.thresholds``, ``db.models.ops`` and, for
the C0 replay, the pure comparison contract of ``ads_engine.decisions`` with the
``db.models.decisions`` and ``db.models.snapshots`` rows it reads (built transient, never added
to a session). No database, network or file writes; building a context reads the three config
files at most (through :class:`~ads_engine.analysis.thresholds.Thresholds`). Importing the
module builds every scenario spec but never a world.

Example:
    >>> scenario = SCENARIOS["k1.gate.fires"]
    >>> ctx = scenario.build_context()  # doctest: +SKIP
    >>> scenario.expect.values["n_star"]
    61
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal, NamedTuple, cast, get_args

from ads_engine.analysis.context import (
    B1,
    B2,
    B3,
    B4,
    C0,
    C1,
    C1B,
    C2,
    C3A,
    C3B,
    CR1,
    CR2,
    CR3,
    FINDING_CODES,
    FINDING_CONVERSION_TRACKING_ABSENT,
    FINDING_K1_NO_BASELINE,
    FINDING_PIPELINE_UNAVAILABLE,
    FINDING_SEARCH_TERM_COVERAGE,
    FINDING_SEGMENT_WINDOW_UNAVAILABLE,
    FINDING_SKIPPED_STAGE,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_CODES,
    FLAG_CREATIVE_FLOOR,
    FLAG_DEAL_FLOOR,
    FLAG_INCOMPLETE,
    FLAG_MEETING_TIME_PROXY,
    FLAG_NOT_DISTINGUISHABLE,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_SEARCH_TERM_COVERAGE,
    FLAG_SHARED_BUDGET,
    FLAG_SHORT_WINDOW,
    FLAG_SIMULATED,
    FLAG_STALE,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    FLAG_UNVERIFIED_DISCREPANCIES,
    G1,
    HELD_DISCREPANCY_REASON,
    HISTORICAL_REASON,
    K1,
    K2,
    K3,
    RULE_IDS,
    SECTION_ORDER,
    W1,
    W2,
    AccountInfo,
    AdGroupInfo,
    AnalysisContext,
    AssociationFetchInfo,
    AssociationStatus,
    BudgetInfo,
    BudgetPeriod,
    CampaignGroupInfo,
    CampaignInfo,
    CampaignStatus,
    Completeness,
    ContactInfo,
    ContactReadStatus,
    ConversionActionInfo,
    CreativeInfo,
    CreativeTotals,
    CrmData,
    DatasetMode,
    DatasetRef,
    DayFact,
    DealInfo,
    DecisionInfo,
    DecisionOrigin,
    DecisionShape,
    DiscrepancyInfo,
    DiscrepancyKind,
    FeedbackInfo,
    FeedbackKind,
    Freshness,
    KeywordDay,
    KeywordInfo,
    LeadInfo,
    MatchStatus,
    MeetingInfo,
    Observation,
    OpenDiscrepancyState,
    RateBaseline,
    RunKind,
    SearchTermRow,
    SegmentRow,
    Source,
    SubjectKey,
    TargetReadStatus,
    keyword_external_id,
    objective_group,
    search_term_external_id,
)
from ads_engine.analysis.thresholds import (
    CREATIVE_URN_PREFIX,
    FORM_URN_PREFIX,
    ContentAsset,
    CrmBindings,
    GoogleCrmBinding,
    ResolvedBinding,
    StageBinding,
    Thresholds,
    config_fingerprint,
    override_value,
    resolve_config_dir,
)
from ads_engine.analysis.window import DAY_SOURCES, AnalysisWindow, SourceDayInfo, default_end
from ads_engine.db.models.decisions import Decision
from ads_engine.db.models.ops import DATASETS, SOURCES
from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.decisions import (
    Mismatch,
    SubjectState,
    compare,
    describe_decision,
    describe_snapshot,
    verified_match,
)
from ads_engine.decisions import SubjectRef as DecisionRef
from ads_engine.simulation import (
    DEFAULT_SCENARIO,
    DEFAULT_SEED,
    GOOGLE_AD_GROUP_IDS,
    GOOGLE_BUDGET_IDS,
    GOOGLE_CAMPAIGN_IDS,
    GOOGLE_CONVERSION_ACTION_IDS,
    GOOGLE_CRITERION_IDS,
    LINKEDIN_CAMPAIGN_GROUP_IDS,
    LINKEDIN_CAMPAIGN_IDS,
    LINKEDIN_CREATIVE_IDS,
)
from ads_engine.simulation.generator import (
    DEFAULT_SPEC,
    BulkDealSpec,
    ChangeEntity,
    ChangeField,
    ContactRecord,
    DailyProfile,
    DayMetrics,
    DealRecord,
    ExplicitDays,
    Fault,
    FormVersionSpec,
    FunnelSpec,
    GoogleAdGroupSpec,
    GoogleBudgetRecord,
    GoogleBudgetSpec,
    GoogleBulkSpec,
    GoogleCampaignRecord,
    GoogleCampaignSpec,
    GoogleConversionActionSpec,
    GoogleKeywordSpec,
    GoogleSearchTermDay,
    GoogleSearchTermSpec,
    GoogleSearchTermStatus,
    GoogleSpec,
    HubSpotSpec,
    LinkedInAccountSpec,
    LinkedInCampaignGroupSpec,
    LinkedInCampaignRecord,
    LinkedInCampaignSpec,
    LinkedInCampaignStatus,
    LinkedInCreativeSpec,
    LinkedInCtaLabel,
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
    WorldSpec,
    person_contact_id,
    person_object_id,
)

__all__ = [
    "FIXED_ANCHOR",
    "FIXED_CLOCK",
    "FIXED_NOW",
    "MODULES",
    "MODULE_BUDGET",
    "MODULE_CAMPAIGNS",
    "MODULE_CREATIVE",
    "MODULE_END_TO_END",
    "MODULE_GOOGLE_CLIENT",
    "MODULE_HUBSPOT_CLIENT",
    "MODULE_KEYWORDS",
    "MODULE_LEAD_MATCHING",
    "MODULE_LINKEDIN_CLIENT",
    "MODULE_PRECEDENCE",
    "MODULE_SYNC_FALLBACK",
    "MODULE_WHITE_PAPERS",
    "REPLACED_DATASET_OFFSET",
    "SCENARIOS",
    "ContextSpec",
    "CrmPatch",
    "DecisionSpec",
    "DiscrepancySpec",
    "EntityIndex",
    "Expectation",
    "FeedbackSpec",
    "FreshnessOverride",
    "Scenario",
    "SubjectRef",
    "context_from_world",
    "for_module",
    "input_differences",
    "pairs",
    "resolve_bindings",
    "thresholds_with",
    "validate_catalogue",
]

# ---------------------------------------------------------------------------
# Fixed time and module paths
# ---------------------------------------------------------------------------

FIXED_ANCHOR: Final = date(2026, 10, 2)
"""The anchor date of every non-default scenario: spec day offsets are relative to it."""

FIXED_NOW: Final = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
"""The instant every non-default scenario is read at (noon UTC on the anchor)."""


def FIXED_CLOCK() -> datetime:  # noqa: N802 - a constant-like clock, named as the AAP brief does
    """Return :data:`FIXED_NOW`: the injected clock of every non-default scenario."""
    return FIXED_NOW


MODULE_KEYWORDS: Final = "acceptance/test_keywords_rules.py"
"""Acceptance module of the Keywords section (AAP 0.5.4.3)."""
MODULE_CAMPAIGNS: Final = "acceptance/test_campaign_rules.py"
"""Acceptance module of the Campaigns section (AAP 0.5.4.4)."""
MODULE_BUDGET: Final = "acceptance/test_budget_rules.py"
"""Acceptance module of the Budget section (AAP 0.5.4.5)."""
MODULE_CREATIVE: Final = "acceptance/test_creative_rules.py"
"""Acceptance module of the Copy / creative section (AAP 0.5.4.6)."""
MODULE_WHITE_PAPERS: Final = "acceptance/test_white_paper_attribution.py"
"""Acceptance module of the White papers section (AAP 0.5.4.7)."""
MODULE_PRECEDENCE: Final = "acceptance/test_precedence.py"
"""Acceptance module of precedence, suppression and the overlay (AAP 0.5.4.2)."""
MODULE_END_TO_END: Final = "integration/test_end_to_end_simulated.py"
"""Integration module of the end-to-end simulated run."""
MODULE_SYNC_FALLBACK: Final = "integration/test_sync_fallback.py"
"""Integration module of source fallback, recovery and dataset transitions."""
MODULE_LEAD_MATCHING: Final = "integration/test_lead_matching.py"
"""Integration module of durable lead-to-contact matching."""
MODULE_GOOGLE_CLIENT: Final = "integration/test_google_n8n_client.py"
"""Integration module of the n8n webhook client and its partitioning."""
MODULE_LINKEDIN_CLIENT: Final = "integration/test_linkedin_client.py"
"""Integration module of the LinkedIn client."""
MODULE_HUBSPOT_CLIENT: Final = "integration/test_hubspot_client.py"
"""Integration module of the HubSpot client."""

MODULES: Final[tuple[str, ...]] = (
    MODULE_KEYWORDS,
    MODULE_CAMPAIGNS,
    MODULE_BUDGET,
    MODULE_CREATIVE,
    MODULE_WHITE_PAPERS,
    MODULE_PRECEDENCE,
    MODULE_END_TO_END,
    MODULE_SYNC_FALLBACK,
    MODULE_LEAD_MATCHING,
    MODULE_GOOGLE_CLIENT,
    MODULE_LINKEDIN_CLIENT,
    MODULE_HUBSPOT_CLIENT,
)
"""Every test module (relative to ``backend/tests/``) that takes scenarios from this catalogue."""

# Rule ids every catalogue must exercise in some expectation (validate_catalogue).
_REQUIRED_RULES: Final[tuple[str, ...]] = (
    K1,
    K2,
    K3,
    C0,
    C1,
    C1B,
    C2,
    C3A,
    C3B,
    B1,
    B2,
    B3,
    B4,
    CR1,
    CR2,
    CR3,
    W1,
    W2,
    G1,
)

type Mode = DatasetMode
"""A source's dataset mode in a scenario: ``live`` or ``simulated``."""

type Variant = Literal["fires", "does_not_fire", "case"]
"""``fires`` / ``does_not_fire`` twins differ in one input; ``case`` has no twin."""

_VARIANTS: Final[tuple[str, ...]] = ("fires", "does_not_fire", "case")

# ---------------------------------------------------------------------------
# Synthetic dataset ids
# ---------------------------------------------------------------------------

_DATASET_IDS: Final[Mapping[str, int]] = MappingProxyType(
    {"google": 1, "linkedin": 2, "hubspot": 3}
)
_COMPANION_ID: Final = 4

REPLACED_DATASET_OFFSET: Final = 100
"""Read-time ids of replaced datasets: the context's id plus this offset (historical cases)."""

# Hours between scheduled syncs (AAP 0.3.2 entity, lead and CRM jobs run every 3 h).
_SYNC_HOURS: Final = 3
# load_context's snapshot margin around the window (AAP 0.5.1 metrics schema step 5).
_SNAPSHOT_MARGIN: Final = timedelta(days=2)
# AAP 0.3.2 BACKFILL_DAYS default; freshness coverage starts here.
_BACKFILL_DAYS: Final = 180
_ONE_DAY: Final = timedelta(days=1)
_CENT: Final = Decimal("0.01")
# The scale of every amount column (``numeric(18, 6)``, ``db.base``).
_STORED_QUANTUM: Final = Decimal("0.000001")

# Normalised campaign statuses, per source (the connectors' status maps; AAP 0.5.2).
_GOOGLE_STATUS: Final[Mapping[str, str]] = MappingProxyType(
    {"ENABLED": "active", "PAUSED": "paused", "REMOVED": "ended"}
)
_LINKEDIN_STATUS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ACTIVE": "active",
        "PAUSED": "paused",
        "DRAFT": "paused",
        "ARCHIVED": "ended",
        "COMPLETED": "ended",
        "CANCELED": "ended",
        "PENDING_DELETION": "ended",
        "REMOVED": "ended",
    }
)

# Lead-match reasons, worded as sync/lead_match.py and the LinkedIn parser record them.
_REASON_FORM_MAP_MISMATCH: Final = "form map mismatch"
_REASON_UNKNOWN_FORM_VERSION: Final = "unknown form version"
_REASON_NO_EMAIL_ANSWER: Final = "no email answer"
_REASON_EMAIL_UNRETRIEVABLE: Final = "email no longer retrievable"
_REASON_NO_CONTACT: Final = "no HubSpot contact with this email yet"
_REASON_LOOKUP_FAILED: Final = "HubSpot contact lookup failed"
_REASON_REFETCH_FAILED: Final = "LinkedIn lead re-fetch failed"

# LinkedIn demographic pivots and the facet each is stored under (AAP 0.3.3, fact_segment_window).
_SEGMENT_PIVOTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "MEMBER_JOB_FUNCTION": "job_function",
        "MEMBER_SENIORITY": "seniority",
        "MEMBER_INDUSTRY": "industry",
    }
)

# Feedback entity types whose spec key resolves through the EntityIndex.
_INDEXED_FEEDBACK_TYPES: Final[frozenset[str]] = frozenset(
    {"campaign", "budget", "ad_group", "keyword", "creative"}
)

# Dataset names per (source, role): a companion carries only what the generator writes.
_PRIMARY_DATASETS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        source: tuple(name for name in DATASETS if name.startswith(f"{source}."))
        for source in SOURCES
    }
)
_COMPANION_DATASETS: Final[tuple[str, ...]] = (
    "hubspot.contacts",
    "hubspot.associations",
    "hubspot.deal_search",
)

# Faults whose failure persists past every retry make a read fail for good; a fault with
# ``times`` is transient and the retry schedule recovers from it before the build instant.
_FAILING_KINDS: Final[frozenset[str]] = frozenset(
    {"http_status", "timeout", "body_error", "non_complete_status"}
)


# ---------------------------------------------------------------------------
# Declarative inputs a scenario adds on top of its world
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """A decision subject named by its world spec key: a campaign or a Google budget.

    Attributes:
        source: ``google`` or ``linkedin``.
        entity: ``campaign`` or ``budget`` (budgets are Google only).
        key: The spec key of the campaign or budget.

    Raises:
        ValueError: On construction, for an unknown source or entity, a LinkedIn budget or a
            blank key.
    """

    source: Literal["google", "linkedin"]
    entity: Literal["campaign", "budget"]
    key: str

    def __post_init__(self) -> None:
        """Validate the reference."""
        if self.source not in ("google", "linkedin"):
            raise ValueError(f"SubjectRef.source must be google or linkedin, found {self.source!r}")
        if self.entity not in ("campaign", "budget"):
            raise ValueError(f"SubjectRef.entity must be campaign or budget, found {self.entity!r}")
        if self.entity == "budget" and self.source != "google":
            raise ValueError(
                "budget subjects are Google budgets; LinkedIn budgets live on campaigns"
            )
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("SubjectRef.key must be a non-blank spec key")


@dataclass(frozen=True, slots=True)
class DecisionSpec:
    """A decision recorded after the baseline, as a patch on the subject's current decision.

    Fields left ``None`` keep the previously decided value, exactly as ``POST /decisions``
    materialises a patch (AAP 0.5.2). The subject's shape comes from its platform state.

    Attributes:
        subject: The campaign or budget decided on.
        decided_at_offset_hours: When it was recorded, in hours after the anchor's UTC midnight.
        status: Decided normalised status (``active``, ``paused``, ``ended``) of a campaign.
        daily_budget: Decided daily budget (own-budget campaigns, daily shared budgets).
        total_budget: Decided lifetime budget.
        budget_key: Decided shared budget of a ``shared_member`` campaign (resolutions only).
        tombstone: An acknowledged removal (``acknowledge_removal``).
        origin: ``user``, ``recommendation`` or ``resolution``.
    """

    subject: SubjectRef
    decided_at_offset_hours: int
    status: str | None = None
    daily_budget: Decimal | None = None
    total_budget: Decimal | None = None
    budget_key: str | None = None
    tombstone: bool = False
    origin: DecisionOrigin = "user"

    def __post_init__(self) -> None:
        """Validate the status and origin."""
        if self.status is not None and self.status not in ("active", "paused", "ended"):
            raise ValueError(f"DecisionSpec.status must be normalised, found {self.status!r}")
        if self.origin not in ("user", "recommendation", "resolution"):
            raise ValueError(f"DecisionSpec.origin cannot be {self.origin!r}")


@dataclass(frozen=True, slots=True)
class DiscrepancySpec:
    """A stored discrepancy the world's history does not raise by itself.

    Derived discrepancies (a platform change against the last decision) come from the world;
    this spec adds the states only the resolution workflow produces, such as
    ``awaiting_platform`` after ``decision_correct``, or a discrepancy detected after the run.

    Attributes:
        subject: The campaign or budget.
        kind: ``status``, ``budget``, ``untracked`` or ``missing``.
        detected_at_offset_hours: Detection instant, in hours after the anchor's UTC midnight.
        state: ``open`` or ``awaiting_platform``.
        decided_value: The decided state compared, as JSON values.
        platform_value: The platform state, as JSON values.
    """

    subject: SubjectRef
    kind: DiscrepancyKind
    detected_at_offset_hours: int
    state: OpenDiscrepancyState = "open"
    decided_value: Mapping[str, Any] | None = None
    platform_value: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate the kind and state and freeze the value mappings."""
        if self.kind not in get_args(DiscrepancyKind):
            raise ValueError(f"DiscrepancySpec.kind cannot be {self.kind!r}")
        if self.state not in get_args(OpenDiscrepancyState):
            raise ValueError(f"DiscrepancySpec.state cannot be {self.state!r}")
        for name in ("decided_value", "platform_value"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, MappingProxyType(dict(value)))


@dataclass(frozen=True, slots=True)
class FeedbackSpec:
    """An active dismiss, snooze or done on one rule and entity.

    Attributes:
        rule_id: The item's rule.
        kind: ``dismiss``, ``snooze`` or ``done``.
        source: The entity's source.
        entity_type: ``campaign``, ``budget``, ``ad_group``, ``keyword``, ``creative`` or
            ``search_term`` resolve ``key`` through the world (a keyword to its ad group and
            criterion id, as K3 keys it); any other type takes ``key`` as the composite
            external id itself.
        key: A spec key; for ``search_term`` it is ``"{ad_group_key}|{term}"``.
        until_offset_hours: When the suppression ends, in hours after the anchor's UTC midnight.
    """

    rule_id: str
    kind: FeedbackKind
    source: Source
    entity_type: str
    key: str
    until_offset_hours: int = 24 * 30

    def __post_init__(self) -> None:
        """Validate the rule and kind."""
        if self.rule_id not in RULE_IDS:
            raise ValueError(f"FeedbackSpec.rule_id cannot be {self.rule_id!r}")
        if self.kind not in get_args(FeedbackKind):
            raise ValueError(f"FeedbackSpec.kind cannot be {self.kind!r}")


@dataclass(frozen=True, slots=True)
class CrmPatch:
    """A CRM outcome set on the contacts of selected leads, in the context only.

    HubSpot outcomes in the world are drawn per person from portal-wide funnel shares, which
    cannot give one cohort of leads an outcome another cohort lacks (for example young leads
    reaching Qualified Lead while matured ones do not, AAP 0.11.2 "Veto cohort"), nor place a
    meeting exactly on the touch instant. A patch sets those outcomes on the converted context;
    the mock transport keeps serving the unpatched world, so patched scenarios are for pure
    rule tests only.

    Attributes:
        creative_key: Only leads generated by this creative; any creative when ``None``.
        submitted_from_offset: First UTC day offset of the selected submissions, inclusive.
        submitted_to_offset: Last UTC day offset of the selected submissions, inclusive.
        ql_after_touch_hours: The contact enters QL this many hours after the lead's touch.
        clear_ql: The contact never enters QL.
        meeting_after_touch_hours: The contact's meetings are created this many hours after
            the touch (0 = on the touch instant) and start one day later.
    """

    creative_key: str | None = None
    submitted_from_offset: int | None = None
    submitted_to_offset: int | None = None
    ql_after_touch_hours: int | None = None
    clear_ql: bool = False
    meeting_after_touch_hours: int | None = None

    def __post_init__(self) -> None:
        """Validate the patch."""
        if self.clear_ql and self.ql_after_touch_hours is not None:
            raise ValueError("a CrmPatch cannot both set and clear QL")

    def selects(self, creative_key: str, offset: int) -> bool:
        """Whether a lead of ``creative_key`` submitted on UTC day ``offset`` is selected."""
        if self.creative_key is not None and creative_key != self.creative_key:
            return False
        if self.submitted_from_offset is not None and offset < self.submitted_from_offset:
            return False
        return self.submitted_to_offset is None or offset <= self.submitted_to_offset


class FreshnessOverride(NamedTuple):
    """One ``dataset_freshness`` row as a scenario sets it (AAP 0.3.2).

    Attributes:
        completeness: ``complete``, ``incomplete`` or ``unknown``.
        covered_through_offset: Last covered native date, as a day offset from the anchor;
            ``None`` keeps the default (the default window end).
        last_success_offset_hours: Last successful refresh, in hours from the build instant
            (``-30`` is 30 hours ago); ``None`` means it never succeeded.
        reason: Why the dataset is incomplete or unknown.
    """

    completeness: Completeness
    covered_through_offset: int | None = None
    last_success_offset_hours: int | None = -1
    reason: str | None = None


def _frozen_mapping[K, V](value: Mapping[K, V]) -> Mapping[K, V]:
    """Return a read-only copy of ``value`` in its insertion order."""
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class ContextSpec:
    """What a database round trip adds to a world: window, configuration and stored state.

    Attributes:
        window: ``(start, end)`` day offsets from the anchor of a window built as a custom
            window; ``None`` with no ``custom`` is the configured default window
            (``AnalysisWindow.default``).
        custom: Explicit ``(start, end)`` native dates instead of ``window``.
        kind: ``default`` or ``custom`` run (``AnalysisContext.run_kind``).
        threshold_overrides: Dotted keys exactly as in ``config/thresholds.yaml`` (or the
            merged ``crm.booking_sources`` / ``crm.meeting_outcome_exclusions``) with their
            replacement values, for example ``{"window.trusted_min_days": 21}``.
        content_assets: The white paper registry, replacing ``config/content_assets.yaml`` in
            both the world and the thresholds; ``None`` keeps the shipped registry.
        observation_cadence_hours: Hours between entity snapshots, on a grid from the anchor's
            UTC midnight; ``None`` writes no snapshot at all.
        observations_from_offset_hours: No snapshot before this many hours after the anchor's
            UTC midnight (``-108`` = noon five days before the anchor); ``None`` = from the
            start of the snapshot span ``load_context`` reads.
        observation_gaps: ``(start, end)`` hour offsets with snapshots at both ends and none
            strictly between, so ``(-64, -57)`` is a 7-hour gap.
        first_start: The context at first start: one snapshot per entity at the build instant
            (the dataset's first entity sync), so every budget day is unobserved.
        segment_fetched_at_offset_hours: When the latest segment fetch ran, in hours after the
            anchor's UTC midnight; its windows end on the default end at that instant. ``None``
            = at the build instant (the segments match the current default end).
        modes: ``live`` or ``simulated`` per source; every source is simulated by default.
        freshness: Overrides of ``dataset_freshness`` rows by dataset name; a ``hubspot.*``
            override applies to every CRM dataset carrying that name.
        provenance_replaced: The LinkedIn dataset the context reads has since been replaced
            by a live one (historical-only items, AAP 0.3.2); see :meth:`Scenario.datasets_now`.
        crm_bindings: Verified CRM bindings replacing the ones resolved from the portal.
        decisions: Decisions recorded after the baseline (specs or stored rows).
        discrepancies: Stored discrepancies added to the ones the world's history raises.
        post_run_discrepancies: Discrepancies detected after the run, which only the read-time
            overlay sees (:meth:`Scenario.read_time_discrepancies`).
        feedback: Active feedback (specs or stored rows).
        crm_patches: Context-only CRM outcomes (:class:`CrmPatch`).

    Raises:
        ValueError: On construction, when both ``window`` and ``custom`` are set, a window ends
            before it starts, a cadence is not positive, a gap is inverted, or a mode, dataset
            name or kind is unknown.
    """

    window: tuple[int, int] | None = None
    custom: tuple[date, date] | None = None
    kind: RunKind = "default"
    threshold_overrides: Mapping[str, object] = field(default_factory=dict)
    content_assets: tuple[ContentAsset, ...] | None = None
    observation_cadence_hours: int | None = _SYNC_HOURS
    observations_from_offset_hours: int | None = None
    observation_gaps: tuple[tuple[int, int], ...] = ()
    first_start: bool = False
    segment_fetched_at_offset_hours: int | None = None
    modes: Mapping[Source, Mode] = field(default_factory=dict)
    freshness: Mapping[str, FreshnessOverride] = field(default_factory=dict)
    provenance_replaced: bool = False
    crm_bindings: CrmBindings | None = None
    decisions: tuple[DecisionSpec | DecisionInfo, ...] = ()
    discrepancies: tuple[DiscrepancySpec | DiscrepancyInfo, ...] = ()
    post_run_discrepancies: tuple[DiscrepancySpec, ...] = ()
    feedback: tuple[FeedbackSpec | FeedbackInfo, ...] = ()
    crm_patches: tuple[CrmPatch, ...] = ()

    def __post_init__(self) -> None:
        """Validate the spec and freeze its mappings."""
        if self.window is not None and self.custom is not None:
            raise ValueError("ContextSpec takes window offsets or custom dates, not both")
        if self.window is not None and (len(self.window) != 2 or self.window[1] < self.window[0]):
            raise ValueError("ContextSpec.window must be (start, end) offsets with end >= start")
        if self.custom is not None and (len(self.custom) != 2 or self.custom[1] < self.custom[0]):
            raise ValueError("ContextSpec.custom must be (start, end) dates with end >= start")
        if self.kind not in get_args(RunKind):
            raise ValueError(f"ContextSpec.kind cannot be {self.kind!r}")
        if self.observation_cadence_hours is not None and self.observation_cadence_hours <= 0:
            raise ValueError("ContextSpec.observation_cadence_hours must be positive")
        for start, end in self.observation_gaps:
            if end <= start:
                raise ValueError(f"observation gap ({start}, {end}) must end after it starts")
        for source, mode in self.modes.items():
            if source not in SOURCES or mode not in get_args(DatasetMode):
                raise ValueError(f"ContextSpec.modes cannot map {source!r} to {mode!r}")
        for name in self.freshness:
            if name not in DATASETS:
                raise ValueError(f"ContextSpec.freshness names unknown dataset {name!r}")
        for key in self.threshold_overrides:
            if not isinstance(key, str) or "." not in key:
                raise ValueError(f"threshold override {key!r} must be a dotted key")
        object.__setattr__(self, "threshold_overrides", _frozen_mapping(self.threshold_overrides))
        object.__setattr__(self, "modes", _frozen_mapping(self.modes))
        object.__setattr__(self, "freshness", _frozen_mapping(self.freshness))
        if self.content_assets is not None:
            object.__setattr__(self, "content_assets", tuple(self.content_assets))

    def mode(self, source: Source) -> Mode:
        """The dataset mode of ``source`` (simulated unless :attr:`modes` says otherwise)."""
        return self.modes.get(source, "simulated")


@dataclass(frozen=True, slots=True)
class Expectation:
    """What the rule output on a scenario's context must show.

    Attributes:
        rule_id: The rule or guard under test (``analysis.context`` constants); ``None`` for a
            transport-level case that no rule evaluates.
        fires: Whether ``rule_id`` emits its item; ``False`` when the item is absent or
            withheld (``withheld_reason`` then states why). For a transport-level case
            (``rule_id=None``) it is ``True``: the described behaviour occurs.
        values: Exact numbers and texts the output must carry, for example
            ``{"n_star": 300}``, ``{"allocations": (300, 100)}`` or ``{"increase": "+20%"}``.
        withheld_reason: The withheld line's reason, or its leading clause.
        finding: A finding code the run must record.
        flags: Flag codes the item must carry.
        label: An exact label or message, for example a ``segment_window_unavailable`` text.

    Raises:
        ValueError: On construction, for an unknown rule id, finding or flag code.
    """

    rule_id: str | None
    fires: bool
    values: Mapping[str, object] = field(default_factory=dict)
    withheld_reason: str | None = None
    finding: str | None = None
    flags: tuple[str, ...] = ()
    label: str | None = None

    def __post_init__(self) -> None:
        """Validate the codes and freeze ``values``."""
        if self.rule_id is not None and self.rule_id not in RULE_IDS:
            raise ValueError(f"Expectation.rule_id cannot be {self.rule_id!r}")
        if self.finding is not None and self.finding not in FINDING_CODES:
            raise ValueError(f"Expectation.finding cannot be {self.finding!r}")
        for code in self.flags:
            if code not in FLAG_CODES:
                raise ValueError(f"Expectation flag {code!r} is not a flag code")
        object.__setattr__(self, "values", _frozen_mapping(self.values))
        object.__setattr__(self, "flags", tuple(self.flags))


@dataclass(frozen=True, slots=True)
class Scenario:
    """One catalogue entry: a world, what the database adds to it, and the expected output.

    Satisfies :class:`~ads_engine.simulation.generator.ScenarioLike` (``name``, ``spec``), so a
    scenario can be passed to :class:`SimulatedWorld` directly.

    Attributes:
        name: ``"<topic>.<case>.<variant>"``, or ``default``.
        module: The test module under ``backend/tests/`` that runs it.
        guarantee: A short restatement of the AAP 0.11.2 cell the scenario proves.
        variant: ``fires``, ``does_not_fire`` or ``case``.
        pair: The name of the twin; required for ``fires`` and ``does_not_fire``.
        spec: The world.
        context: What the database adds to the world.
        expect: The expected output.
    """

    name: str
    module: str
    guarantee: str
    variant: Variant
    pair: str | None
    spec: WorldSpec
    context: ContextSpec
    expect: Expectation

    @property
    def ids(self) -> EntityIndex:
        """The internal ids, external ids and names the converter assigns to the world."""
        return EntityIndex.of(self.spec)

    @property
    def fixed_time(self) -> bool:
        """Whether the scenario runs on :data:`FIXED_CLOCK` (every scenario but ``default``)."""
        return self.spec.anchor is not None

    def world(
        self, seed: int = DEFAULT_SEED, *, clock: Callable[[], datetime] | None = None
    ) -> SimulatedWorld:
        """Build the scenario's world.

        Args:
            seed: The world seed (``MOCK_SEED``).
            clock: The world clock; :func:`FIXED_CLOCK` for fixed-time scenarios and the real
                clock for ``default`` when ``None``.

        Returns:
            The seeded world, with the scenario's white paper registry when it has one.
        """
        if clock is None and self.fixed_time:
            clock = FIXED_CLOCK
        return SimulatedWorld(
            seed,
            self,
            clock=clock,
            content_assets=self.context.content_assets,
        )

    def thresholds(self) -> Thresholds:
        """Load the configuration with this scenario's overrides and registry applied.

        Returns:
            :meth:`Thresholds.load` of the config files with the scenario's threshold
            overrides and registry applied (the plain load when it has neither), through
            :func:`thresholds_with`, so validation and ``config_hash`` are those of files
            holding the same values. No file is written.

        Raises:
            ConfigError: When an override names a missing key or the resulting configuration
                fails validation (``wp.override_conflict`` expects this).
        """
        return thresholds_with(self.context.threshold_overrides, self.context.content_assets)

    def build_context(
        self, *, seed: int = DEFAULT_SEED, now: datetime | None = None
    ) -> AnalysisContext:
        """Build the analysis context of this scenario without a database.

        Args:
            seed: The world seed.
            now: The run instant; :data:`FIXED_NOW` for fixed-time scenarios, the real clock
                for ``default`` when ``None``.

        Returns:
            The context ``load_context`` would read after the sync jobs stored the world.
        """
        instant = now if now is not None else (FIXED_NOW if self.fixed_time else None)
        clock: Callable[[], datetime] | None = None
        if instant is not None:
            fixed = instant

            def clock() -> datetime:
                return fixed

        world = self.world(seed, clock=clock)
        return context_from_world(world, self.context, self.thresholds(), now=instant)

    def datasets_now(self) -> Mapping[str, DatasetRef]:
        """The datasets active at read time, keyed by source (``companion`` for the companion).

        Equal to the context's datasets unless :attr:`ContextSpec.provenance_replaced`: then a
        live LinkedIn dataset (id + :data:`REPLACED_DATASET_OFFSET`) has replaced the one the
        context read, and LinkedIn-attributed CRM evidence now reads HubSpot's primary.
        """
        active: dict[str, DatasetRef] = {}
        for source in SOURCES:
            if getattr(self.spec, source) is not None:
                typed = cast(Source, source)
                active[source] = DatasetRef(_DATASET_IDS[source], typed, self.context.mode(typed))
        linkedin = active.get("linkedin")
        if self.context.provenance_replaced and linkedin is not None:
            active["linkedin"] = DatasetRef(
                linkedin.dataset_id + REPLACED_DATASET_OFFSET, "linkedin", "live"
            )
        elif linkedin is not None and linkedin.mode == "simulated" and "hubspot" in active:
            active["companion"] = DatasetRef(_COMPANION_ID, "hubspot", "simulated", "companion")
        return MappingProxyType(active)

    def read_time_discrepancies(self) -> tuple[DiscrepancyInfo, ...]:
        """The discrepancies detected after the run, which only the read-time overlay sees."""
        index = self.ids
        anchor = self.spec.anchor if self.spec.anchor is not None else datetime.now(UTC).date()
        return tuple(
            _discrepancy_from_spec(self.spec, index, item, 1_000 + position, anchor)
            for position, item in enumerate(self.context.post_run_discrepancies)
        )


# ---------------------------------------------------------------------------
# Internal ids: the rows the sync jobs would write, numbered in spec order
# ---------------------------------------------------------------------------

type _EntityKey = tuple[str, str, str]


class EntityIndex:
    """The internal id, external id and display name of every explicit entity of a world spec.

    The converter numbers rows per table in spec order, Google before LinkedIn where both
    share a table (accounts, campaigns), and allocates external ids exactly as the generator
    does (``RANGE.at(index)`` in spec order; an explicit creative id when given). Tests use it
    to address entities by spec key, for example ``scenario.ids.id("google", "campaign",
    "g_comp")``. Bulk filler is not indexed: contexts never hold it.

    Keys are ``(source, entity, spec key)`` with entity ``account`` (key: the customer or
    account id), ``campaign_group``, ``campaign``, ``budget``, ``ad_group``, ``keyword``,
    ``conversion_action``, ``creative`` or ``lead_form`` (key: the form id).
    """

    __slots__ = ("_external", "_ids", "_names")

    def __init__(self, spec: WorldSpec) -> None:
        self._ids: dict[_EntityKey, int] = {}
        self._external: dict[_EntityKey, str] = {}
        self._names: dict[_EntityKey, str] = {}
        account_id = 0
        google = spec.google
        google_campaigns = 0
        if google is not None:
            account_id += 1
            self._add(
                ("google", "account", google.customer_id),
                account_id,
                google.customer_id,
                google.name,
            )
            for index, budget in enumerate(google.budgets):
                self._add(
                    ("google", "budget", budget.key),
                    index + 1,
                    str(GOOGLE_BUDGET_IDS.at(index)),
                    budget.name or f"{budget.key} budget",
                )
            for index, campaign in enumerate(google.campaigns):
                self._add(
                    ("google", "campaign", campaign.key),
                    index + 1,
                    str(GOOGLE_CAMPAIGN_IDS.at(index)),
                    campaign.name,
                )
            google_campaigns = len(google.campaigns)
            for index, group in enumerate(google.ad_groups):
                self._add(
                    ("google", "ad_group", group.key),
                    index + 1,
                    str(GOOGLE_AD_GROUP_IDS.at(index)),
                    group.name,
                )
            for index, keyword in enumerate(google.keywords):
                self._add(
                    ("google", "keyword", keyword.key),
                    index + 1,
                    str(GOOGLE_CRITERION_IDS.at(index)),
                    keyword.text,
                )
            for index, action in enumerate(google.conversion_actions):
                self._add(
                    ("google", "conversion_action", action.key),
                    index + 1,
                    str(GOOGLE_CONVERSION_ACTION_IDS.at(index)),
                    action.name,
                )
        linkedin = spec.linkedin
        if linkedin is not None:
            for account in linkedin.accounts:
                account_id += 1
                self._add(
                    ("linkedin", "account", str(account.id)),
                    account_id,
                    str(account.id),
                    account.name,
                )
            for index, li_group in enumerate(linkedin.campaign_groups):
                self._add(
                    ("linkedin", "campaign_group", li_group.key),
                    index + 1,
                    str(LINKEDIN_CAMPAIGN_GROUP_IDS.at(index)),
                    li_group.name,
                )
            for index, li_campaign in enumerate(linkedin.campaigns):
                self._add(
                    ("linkedin", "campaign", li_campaign.key),
                    google_campaigns + index + 1,
                    str(LINKEDIN_CAMPAIGN_IDS.at(index)),
                    li_campaign.name,
                )
            for index, creative in enumerate(linkedin.creatives):
                number = creative.id if creative.id is not None else LINKEDIN_CREATIVE_IDS.at(index)
                self._add(
                    ("linkedin", "creative", creative.key),
                    index + 1,
                    f"{CREATIVE_URN_PREFIX}{number}",
                    creative.headline,
                )
            for index, form in enumerate(linkedin.lead_forms):
                self._add(
                    ("linkedin", "lead_form", str(form.id)),
                    index + 1,
                    f"{FORM_URN_PREFIX}{form.id}",
                    form.name,
                )

    @classmethod
    def of(cls, spec: WorldSpec) -> EntityIndex:
        """Index ``spec`` (cheap: one pass over its entities)."""
        return cls(spec)

    def _add(self, key: _EntityKey, internal: int, external: str, name: str) -> None:
        self._ids[key] = internal
        self._external[key] = external
        self._names[key] = name

    def has(self, source: str, entity: str, key: str) -> bool:
        """Whether the spec defines the entity."""
        return (source, entity, key) in self._ids

    def id(self, source: str, entity: str, key: str) -> int:
        """The entity's internal row id.

        Raises:
            KeyError: When the spec has no such entity.
        """
        try:
            return self._ids[(source, entity, key)]
        except KeyError:
            raise KeyError(f"the world has no {source} {entity} {key!r}") from None

    def external_id(self, source: str, entity: str, key: str) -> str:
        """The entity's platform id (a URN for creatives and lead forms).

        Raises:
            KeyError: When the spec has no such entity.
        """
        self.id(source, entity, key)
        return self._external[(source, entity, key)]

    def name(self, source: str, entity: str, key: str) -> str:
        """The entity's display name.

        Raises:
            KeyError: When the spec has no such entity.
        """
        self.id(source, entity, key)
        return self._names[(source, entity, key)]

    def subject(self, ref: SubjectRef) -> SubjectKey:
        """The :data:`SubjectKey` of a subject reference.

        Raises:
            KeyError: When the spec has no such subject.
        """
        return (ref.entity, self.id(ref.source, ref.entity, ref.key))

    def entity_key(self, source: str, entity: str, key: str) -> str:
        """The entity key ``{source}:{type}:{external id}`` built from the platform id alone.

        Feedback matches campaigns, budgets, ad groups and creatives on it. A keyword's criterion
        id is unique only within its ad group, so keyword items and feedback are keyed on
        :func:`keyword_external_id` instead.
        """
        return f"{source}:{entity}:{self.external_id(source, entity, key)}"


# ---------------------------------------------------------------------------
# Configuration with overrides (Thresholds.load applies them: same parsing, validation and hash)
# ---------------------------------------------------------------------------

# (config directory, config_fingerprint, normalised overrides, registry) -> configuration.
_THRESHOLDS_CACHE: dict[tuple[object, ...], Thresholds] = {}


def thresholds_with(
    overrides: Mapping[str, object], content_assets: Sequence[ContentAsset] | None = None
) -> Thresholds:
    """Load the configuration with dotted-key overrides and an optional registry.

    :meth:`Thresholds.load` reads the config files with its strict loader, replaces existing
    keys only (a misspelt key fails) and validates the result as it validates files holding
    the same values (``window.presets`` must still contain ``window.default_days``), so its
    ``config_hash`` reflects every override and this module parses no YAML itself (AAP
    0.6.2). With no overrides and no registry it is the plain :meth:`Thresholds.load`.

    Results are memoised per config directory, :func:`config_fingerprint` of its files and
    distinct input, with values compared as :func:`override_value` normalises them
    (``Decimal("0.90")`` and ``0.9``, a tuple and a list, are one entry). Any byte edit to a
    config file therefore loads afresh, and unchanged files return the same instance.

    Args:
        overrides: Dotted keys of ``config/thresholds.yaml`` (or ``crm.booking_sources`` and
            ``crm.meeting_outcome_exclusions`` of the mapping) to their new values.
        content_assets: A registry replacing ``config/content_assets.yaml``.

    Returns:
        The validated configuration.

    Raises:
        ConfigError: Naming the file and key, when the config directory or a file is missing
            or unreadable, a file is not valid YAML, an override names a missing key, or
            validation fails.
    """
    directory = resolve_config_dir().absolute()
    fingerprint = config_fingerprint(directory)
    assets = None if content_assets is None else tuple(content_assets)
    cache_key: tuple[object, ...] = (
        str(directory),
        fingerprint,
        tuple(sorted((key, repr(override_value(value))) for key, value in overrides.items())),
        assets,
    )
    cached = _THRESHOLDS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result = Thresholds.load(directory, overrides=overrides, content_assets=assets)
    # Keep only entries of the files as they are now, and only when no edit raced the load.
    for stale in [key for key in _THRESHOLDS_CACHE if key[0] == str(directory)]:
        if stale[1] != fingerprint:
            del _THRESHOLDS_CACHE[stale]
    if config_fingerprint(directory) == fingerprint:
        _THRESHOLDS_CACHE[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# CRM bindings, resolved against the simulated portal as verify_bindings resolves them
# ---------------------------------------------------------------------------

_STAGE_DATE_PREFIX: Final = "hs_v2_date_entered_"
_DATE_TYPES: Final[frozenset[str]] = frozenset({"date", "datetime"})


def _resolve_binding(world: SimulatedWorld, binding: StageBinding) -> ResolvedBinding:
    """Resolve one MQL or QL binding against the world's portal (AAP 0.5.4.7)."""
    if binding.mode == "property":
        name = cast(str, binding.property)
        label = binding.stage_labels[0] if binding.stage_labels else None
        definition = world.hubspot_property("contacts", name)
        if definition is None:
            return ResolvedBinding(
                available=False, label=label, reason=f"property '{name}' not found on contacts"
            )
        if definition.type not in _DATE_TYPES:
            return ResolvedBinding(
                available=False,
                label=label,
                reason=f"property '{name}' has type '{definition.type}', not a date or "
                "datetime property",
            )
        return ResolvedBinding(available=True, property=name, label=label)
    options = world.hubspot_lifecycle_options()
    for label in binding.stage_labels:
        wanted = label.strip().casefold()
        matches = [option for option in options if option.label.strip().casefold() == wanted]
        if not matches:
            continue
        option = min(matches, key=lambda item: (item.label != label, item.hidden))
        date_property = f"{_STAGE_DATE_PREFIX}{option.value}"
        if world.hubspot_property("contacts", date_property) is None:
            return ResolvedBinding(
                available=False,
                stage_id=option.value,
                label=option.label,
                reason=f"stage '{label}' resolved to id '{option.value}', but property "
                f"'{date_property}' not found",
            )
        return ResolvedBinding(
            available=True, property=date_property, stage_id=option.value, label=option.label
        )
    noun = "stage" if len(binding.stage_labels) == 1 else "stages"
    quoted = ", ".join(f"'{label}'" for label in binding.stage_labels)
    reason = f"{noun} {quoted} not found in lifecyclestage options"
    return ResolvedBinding(available=False, reason=reason)


def _resolve_google_link(world: SimulatedWorld) -> GoogleCrmBinding:
    """Verify the optional Google-to-CRM link of the world's portal (AAP 0.3.3)."""
    properties = world.google_crm_properties
    if properties is None:
        return GoogleCrmBinding(
            available=False,
            reason="HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY and HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY "
            "not configured",
        )
    campaign, touch = properties
    problems: list[str] = []
    if world.hubspot_property("contacts", campaign) is None:
        problems.append(f"HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY property '{campaign}' not found")
    definition = world.hubspot_property("contacts", touch)
    if definition is None or definition.type not in _DATE_TYPES:
        problems.append(f"HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY property '{touch}' not usable")
    if problems:
        return GoogleCrmBinding(
            available=False,
            campaign_property=campaign,
            touch_date_property=touch,
            reason="; ".join(problems),
        )
    return GoogleCrmBinding(available=True, campaign_property=campaign, touch_date_property=touch)


def resolve_bindings(world: SimulatedWorld, thresholds: Thresholds, at: datetime) -> CrmBindings:
    """Resolve the MQL, QL and Google bindings of the world's portal, verified at ``at``.

    Mirrors ``HubSpotClient.verify_bindings``: stage labels are matched case-insensitively,
    the first label that matches wins, and anything unresolved is unavailable with a reason,
    never counted as 0. A world without a HubSpot part has nothing to verify.
    """
    if world.spec.hubspot is None:
        return CrmBindings.unavailable("CRM stage bindings not verified", at=at)
    mapping = thresholds.mapping
    return CrmBindings(
        mql=_resolve_binding(world, mapping.mql),
        ql=_resolve_binding(world, mapping.ql),
        google=_resolve_google_link(world),
        verified_at=at,
    )


# ---------------------------------------------------------------------------
# Small pure helpers of the converter
# ---------------------------------------------------------------------------


def _midnight(day: date) -> datetime:
    """The UTC instant at which ``day`` starts in UTC."""
    return datetime.combine(day, time(0), tzinfo=UTC)


def _epoch_ms(instant: datetime) -> int:
    """An aware instant as epoch milliseconds (the CRM Search and Lead Sync wire unit)."""
    return int((instant - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1))


def _stored(amount: Decimal | None) -> Decimal | None:
    """An amount as a ``numeric(18, 6)`` column returns it (``db.base``), at six places.

    Discrepancy detection reads the snapshot and decision rows back from the database, so the
    values it stores carry this exponent; amounts never have more places, so nothing rounds.
    """
    return None if amount is None else amount.quantize(_STORED_QUANTUM)


def _email_hash(email: str) -> str:
    """SHA-256 of a normalised (trimmed, lowercased) email, as lead ingestion stores it."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _crm_type(wire: str) -> str:
    """A HubSpot wire object type (``contacts``) as ``crm_association`` stores it (``contact``)."""
    return wire.removesuffix("s")


def _shape_of(source: str, explicitly_shared: bool) -> DecisionShape:
    """The decision shape of a campaign: a member of a shared budget, or its own budget."""
    return "shared_member" if source == "google" and explicitly_shared else "own_budget"


def _spec_shape(world_spec: WorldSpec, ref: SubjectRef) -> DecisionShape:
    """The decision shape of a subject as its spec defines it (before any platform change)."""
    if ref.entity == "budget":
        return "budget"
    google = world_spec.google
    if ref.source == "google" and google is not None:
        for campaign in google.campaigns:
            if campaign.key == ref.key:
                for budget in google.budgets:
                    if budget.key == campaign.budget_key:
                        return _shape_of("google", budget.explicitly_shared)
    return "own_budget"


def _discrepancy_from_spec(
    world_spec: WorldSpec,
    index: EntityIndex,
    item: DiscrepancySpec,
    discrepancy_id: int,
    anchor: date,
) -> DiscrepancyInfo:
    """Resolve a :class:`DiscrepancySpec` into the stored row a context reads."""
    subject_type, subject_id = index.subject(item.subject)
    return DiscrepancyInfo(
        id=discrepancy_id,
        subject_type=subject_type,
        subject_id=subject_id,
        # Budgets carry no stored name (load_context reads them with ""); campaigns do.
        subject_name=(
            ""
            if item.subject.entity == "budget"
            else index.name(item.subject.source, item.subject.entity, item.subject.key)
        ),
        source=item.subject.source,
        shape=_spec_shape(world_spec, item.subject),
        kind=item.kind,
        decided_value=item.decided_value,
        platform_value=item.platform_value,
        detected_at=_midnight(anchor) + timedelta(hours=item.detected_at_offset_hours),
        state=item.state,
    )


def _context_world(world: SimulatedWorld) -> SimulatedWorld:
    """The world a context is built from: the same world without its bulk filler.

    Bulk filler (thousands of fixed-metric campaigns, keywords or deals) exists for the
    wire-level pagination and partitioning tests; its ids follow every explicit id and its
    metrics never touch an explicit entity's, so dropping it changes no explicit fact while
    keeping every context build fast. A world without filler is returned as it is.
    """
    spec = world.spec
    google_bulk = spec.google is not None and spec.google.bulk is not None
    deal_bulk = spec.hubspot is not None and spec.hubspot.bulk_deals is not None
    if not google_bulk and not deal_bulk:
        return world
    stripped = spec
    if google_bulk:
        stripped = stripped.with_google(bulk=None)
    if deal_bulk:
        stripped = stripped.with_hubspot(bulk_deals=None)
    return SimulatedWorld(
        world.seed,
        stripped,
        clock=world.now,
        content_assets=world.content_assets,
        mapping=world.mapping,
        google_crm_properties=world.google_crm_properties,
    )


@dataclass(slots=True)
class _Open:
    """A discrepancy raised while replaying the snapshot history (internal)."""

    subject: SubjectKey
    source: Source
    kind: DiscrepancyKind
    decided_value: Mapping[str, Any] | None
    platform_value: Mapping[str, Any] | None
    detected_at: datetime
    state: OpenDiscrepancyState = "open"


@dataclass(frozen=True, slots=True)
class _Platform:
    """A decision subject's platform state at one snapshot (internal)."""

    shape: DecisionShape
    status: str | None
    daily_budget: Decimal | None
    total_budget: Decimal | None
    amount_micros: int | None
    budget_id: int | None
    budget_period: str | None


# What decides a C0 comparison: the subject, its snapshot's values, the decision, and whether
# the snapshot was captured after that decision (a tombstone's reappearance).
type _VerdictKey = tuple[SubjectKey, _Platform, DecisionInfo, bool]


# ---------------------------------------------------------------------------
# The converter
# ---------------------------------------------------------------------------


class _Converter:
    """Builds one :class:`AnalysisContext` from a world and a :class:`ContextSpec`.

    Each method stands in for one step of ``analysis.metrics.load_context`` after the sync jobs
    stored the world: active datasets, freshness, entities, daily facts, snapshots with the
    baseline decisions and the discrepancies their history raises, leads with their match
    state, and the CRM evidence of each CRM dataset.
    """

    def __init__(
        self,
        world: SimulatedWorld,
        spec: ContextSpec,
        thresholds: Thresholds,
        now: datetime,
    ) -> None:
        self.world = _context_world(world)
        self.spec = spec
        self.thresholds = thresholds
        self.now = now.astimezone(UTC)
        self.wspec = self.world.spec
        self.index = EntityIndex(self.wspec)
        self.anchor = self.world.anchor
        self.anchor_midnight = _midnight(self.anchor)
        self.sources = self._source_days()
        self.window = self._window()
        self.datasets: dict[Source, DatasetRef] = {}
        for source in SOURCES:
            if getattr(self.wspec, source) is not None:
                typed = cast(Source, source)
                self.datasets[typed] = DatasetRef(_DATASET_IDS[source], typed, spec.mode(typed))
        hubspot = self.datasets.get("hubspot")
        linkedin = self.datasets.get("linkedin")
        self.companion: DatasetRef | None = None
        if linkedin is not None and hubspot is not None and linkedin.mode == "simulated":
            self.companion = DatasetRef(_COMPANION_ID, "hubspot", "simulated", "companion")
        self.linkedin_crm_ref = (
            None if linkedin is None else (self.companion if self.companion else hubspot)
        )
        self.google_crm_ref = hubspot
        self.freshness = self._freshness()
        self.google_external = self._external_keys("google", "campaign")
        self.linkedin_external = self._external_keys("linkedin", "campaign")
        self.budget_external = self._external_keys("google", "budget")
        self.budget_external_ids = {
            self.index.id("google", "budget", key): external
            for external, key in self.budget_external.items()
        }
        self.group_external = self._external_keys("google", "ad_group")
        self.criterion_external = self._external_keys("google", "keyword")
        self.creative_external = self._external_keys("linkedin", "creative")

    # -- window, datasets and freshness ------------------------------------------------

    def _source_days(self) -> SourceDayInfo:
        """The native day boundaries of the world's day sources."""
        google = self.wspec.google
        present = tuple(source for source in DAY_SOURCES if getattr(self.wspec, source) is not None)
        return SourceDayInfo(
            google.time_zone if google is not None else None, present or DAY_SOURCES
        )

    def _window(self) -> AnalysisWindow:
        """The configured default window, or the spec's custom window."""
        if self.spec.custom is not None:
            start, end = self.spec.custom
        elif self.spec.window is not None:
            start = self.anchor + timedelta(days=self.spec.window[0])
            end = self.anchor + timedelta(days=self.spec.window[1])
        else:
            return AnalysisWindow.default(self.thresholds, self.sources, now=self.now)
        return AnalysisWindow.custom(
            start, end, config=self.thresholds, sources=self.sources, now=self.now
        )

    def _external_keys(self, source: str, entity: str) -> dict[str, str]:
        """External id to spec key of every explicit entity of one kind."""
        mapping: dict[str, str] = {}
        spec = getattr(self.wspec, source)
        if spec is None:
            return mapping
        items: Iterable[Any] = {
            "campaign": lambda: spec.campaigns,
            "budget": lambda: spec.budgets,
            "ad_group": lambda: spec.ad_groups,
            "keyword": lambda: spec.keywords,
            "creative": lambda: spec.creatives,
        }[entity]()
        for item in items:
            mapping[self.index.external_id(source, entity, item.key)] = item.key
        return mapping

    def _entities_incomplete(self, source: str) -> bool:
        """Whether entity enumeration of ``source`` cannot be complete (safe absence, AAP 0.3.3).

        A persistent failure of the enumeration surface (Q-G2/Q-G2b for Google, the campaign
        search for LinkedIn) or an ``incomplete`` entities freshness override means no
        removal or disappearance may be inferred. A fault confined to a later page (the
        catalogue worlds enumerate in one page) never fires, so it is not counted.
        """
        override = self.spec.freshness.get(f"{source}.entities")
        if override is not None and override.completeness != "complete":
            return True
        surface = "search" if source == "google" else "ad_campaigns"
        for fault in self.world.faults_for(cast(Source, source), surface):
            if fault.times is not None or fault.kind not in _FAILING_KINDS:
                continue
            if fault.match.get("page") not in (None, "1"):
                continue
            query = fault.match.get("query_id")
            if source != "google" or query in (None, "Q-G2", "Q-G2b"):
                return True
        return False

    def _freshness_row(self, dataset_id: int, name: str) -> Freshness:
        """One freshness row: complete through the default end unless the spec overrides it."""
        attempt = self.now - timedelta(hours=1)
        covered_from = self.now.date() - timedelta(days=_BACKFILL_DAYS)
        override = self.spec.freshness.get(name)
        if override is None:
            source = name.split(".", 1)[0]
            incomplete = name.endswith(".entities") and self._entities_incomplete(source)
            return Freshness(
                dataset_id=dataset_id,
                dataset=name,
                last_attempt_at=attempt,
                last_success_at=attempt,
                covered_from=covered_from,
                covered_through=self.window.default_end,
                completeness="incomplete" if incomplete else "complete",
                reason="entity enumeration did not complete" if incomplete else None,
            )
        through = (
            self.window.default_end
            if override.covered_through_offset is None
            else self.anchor + timedelta(days=override.covered_through_offset)
        )
        success = (
            None
            if override.last_success_offset_hours is None
            else self.now + timedelta(hours=override.last_success_offset_hours)
        )
        return Freshness(
            dataset_id=dataset_id,
            dataset=name,
            last_attempt_at=attempt,
            last_success_at=success,
            covered_from=covered_from,
            covered_through=through,
            completeness=override.completeness,
            reason=override.reason,
        )

    def _freshness(self) -> dict[tuple[int, str], Freshness]:
        """Freshness rows of every active dataset and of the companion."""
        rows: dict[tuple[int, str], Freshness] = {}
        for source, ref in self.datasets.items():
            for name in _PRIMARY_DATASETS[source]:
                rows[(ref.dataset_id, name)] = self._freshness_row(ref.dataset_id, name)
        if self.companion is not None:
            for name in _COMPANION_DATASETS:
                rows[(self.companion.dataset_id, name)] = self._freshness_row(
                    self.companion.dataset_id, name
                )
        return rows

    # -- snapshot instants ---------------------------------------------------------------

    def _instants(self) -> list[datetime]:
        """Every entity sync whose snapshots ``load_context`` reads, ascending.

        The span is ``[utc(window start) - 2 days, utc(window end + 1 day) + 2 days)``, cut at
        the build instant and at ``observations_from_offset_hours``; the grid runs from the
        anchor's UTC midnight at the configured cadence, gap endpoints are kept and nothing
        strictly inside a gap.
        """
        if self.spec.first_start:
            return [self.now]
        cadence = self.spec.observation_cadence_hours
        if cadence is None:
            return []
        lower = _midnight(self.window.start) - _SNAPSHOT_MARGIN
        if self.spec.observations_from_offset_hours is not None:
            lower = max(
                lower,
                self.anchor_midnight + timedelta(hours=self.spec.observations_from_offset_hours),
            )
        upper = _midnight(self.window.end + _ONE_DAY) + _SNAPSHOT_MARGIN
        step = timedelta(hours=cadence)
        first = math.ceil((lower - self.anchor_midnight) / step)
        found: set[datetime] = set()
        instant = self.anchor_midnight + first * step
        while instant <= self.now and instant < upper:
            found.add(instant)
            instant += step
        for start, end in self.spec.observation_gaps:
            low = self.anchor_midnight + timedelta(hours=start)
            high = self.anchor_midnight + timedelta(hours=end)
            found = {item for item in found if not low < item < high}
            for edge in (low, high):
                if lower <= edge <= self.now and edge < upper:
                    found.add(edge)
        return sorted(found)

    # -- snapshot history: observations, baseline decisions, discrepancies ---------------

    def replay(self) -> None:
        """Replay every entity sync of the snapshot span in order.

        Each sync writes one snapshot per tracked entity; the first writes the baseline
        decision of every campaign and explicitly shared budget in its own shape (AAP 0.5.2),
        so the dataset is taken to start at the first snapshot the context reads. While no
        enumeration of a source completes, its baseline stays open and a subject first
        returned later is baselined too (AAP 0.3.3). Decisions of the spec are recorded at
        their instants, and after every later sync each subject is judged as
        ``sync.snapshots.detect_discrepancies`` judges it (:meth:`_detect`), through
        the domain's own pure contract: :func:`~ads_engine.decisions.compare` and
        :func:`~ads_engine.decisions.verified_match` on transient snapshot and decision rows,
        with the values :func:`~ads_engine.decisions.describe_snapshot` and
        :func:`~ads_engine.decisions.describe_decision` store. A mismatch raises its kind or
        refreshes the unresolved row of that kind (latest values, first ``detected_at``); only
        a verified match of the whole snapshot closes the subject's ``status``, ``budget`` and
        ``untracked`` rows, and ``missing`` closes only by an acknowledged removal or, once
        ``awaiting_platform``, by a verified match from a complete enumeration. A
        :class:`DiscrepancySpec` in state ``awaiting_platform`` that names a discrepancy the
        history has open at its instant records ``decision_correct`` on it there.
        """
        self.observations: dict[tuple[str, int], list[Observation]] = {}
        self.google_seen: dict[str, GoogleCampaignRecord] = {}
        self.linkedin_seen: dict[str, LinkedInCampaignRecord] = {}
        self.budgets_seen: dict[str, GoogleBudgetRecord] = {}
        self.google_tracked: set[str] = set()
        self.history: dict[SubjectKey, list[DecisionInfo]] = {}
        self.last_platform: dict[SubjectKey, _Platform] = {}
        self.subject_source: dict[SubjectKey, Source] = {}
        self.subject_currency: dict[SubjectKey, str] = {}
        self.subject_names: dict[SubjectKey, str] = {}
        self.decision_rows: dict[DecisionInfo, Decision] = {}
        self.verdicts: dict[_VerdictKey, tuple[tuple[Mismatch, ...], bool]] = {}
        self.open: dict[tuple[SubjectKey, str], _Open] = {}
        self.google_incomplete = self._entities_incomplete("google")
        self.linkedin_incomplete = self._entities_incomplete("linkedin")
        pending = sorted(
            self.spec.decisions,
            key=lambda item: (
                item.decided_at
                if isinstance(item, DecisionInfo)
                else self.anchor_midnight + timedelta(hours=item.decided_at_offset_hours)
            ),
        )
        self.consumed: set[int] = set()
        awaiting = sorted(
            (
                (self.anchor_midnight + timedelta(hours=item.detected_at_offset_hours), position)
                for position, item in enumerate(self.spec.discrepancies)
                if isinstance(item, DiscrepancySpec) and item.state == "awaiting_platform"
            ),
        )
        tracked: set[SubjectKey] = set()
        instants = self._instants()
        for position, instant in enumerate(instants):
            platform = self._snapshot(instant)
            if position == 0:
                for key, state in platform.items():
                    self._record(self._baseline(key, state, instant))
            pending = self._apply_decisions(pending, instant)
            awaiting = self._await_platform(awaiting, instant)
            if position > 0:
                self._detect(instant, platform, tracked)
            tracked |= platform.keys()
        self._apply_decisions(pending, self.now)
        self._await_platform(awaiting, self.now)

    def _await_platform(
        self, awaiting: list[tuple[datetime, int]], at: datetime
    ) -> list[tuple[datetime, int]]:
        """Record ``decision_correct`` on open discrepancies due by ``at``; return the rest."""
        remaining: list[tuple[datetime, int]] = []
        for instant, position in awaiting:
            if instant > at:
                remaining.append((instant, position))
                continue
            item = cast(DiscrepancySpec, self.spec.discrepancies[position])
            open_key = (self.index.subject(item.subject), item.kind)
            found = self.open.get(open_key)
            if found is not None:
                self.open[open_key] = replace(found, state="awaiting_platform")
                self.consumed.add(position)
        return remaining

    def _observe(self, entity_type: str, entity_id: int, observation: Observation) -> None:
        self.observations.setdefault((entity_type, entity_id), []).append(observation)

    def _snapshot(self, at: datetime) -> dict[SubjectKey, _Platform]:
        """Write one entity sync's snapshots; return the decision subjects' platform states."""
        platform: dict[SubjectKey, _Platform] = {}
        if self.wspec.google is not None:
            budgets = {budget.key: budget for budget in self.world.google_budgets(at)}
            referenced: set[str] = set()
            for record in self.world.google_campaigns(at):
                key = record.key
                if not self.index.has("google", "campaign", key):
                    continue
                if record.status == "REMOVED" and key not in self.google_tracked:
                    continue
                self.google_tracked.add(key)
                self.google_seen[key] = record
                campaign_id = self.index.id("google", "campaign", key)
                budget_id = self.index.id("google", "budget", record.budget_key)
                status = _GOOGLE_STATUS[record.status]
                micros = (
                    record.budget_amount_micros
                    if record.budget_amount_micros is not None
                    else record.budget_total_amount_micros
                )
                self._observe(
                    "campaign",
                    campaign_id,
                    Observation(
                        captured_at=at,
                        status=status,
                        budget_daily=record.budget_amount,
                        budget_total=record.budget_total_amount,
                        budget_amount_micros=micros,
                        budget_id=budget_id,
                        payload={
                            "raw_status": record.status,
                            "explicitly_shared": record.budget_explicitly_shared,
                            "budget_period": record.budget_period,
                        },
                    ),
                )
                subject: SubjectKey = ("campaign", campaign_id)
                self.subject_source[subject] = "google"
                self.subject_currency[subject] = self.wspec.google.currency
                self.subject_names[subject] = record.name
                platform[subject] = _Platform(
                    shape=_shape_of("google", record.budget_explicitly_shared),
                    status=status,
                    daily_budget=record.budget_amount,
                    total_budget=record.budget_total_amount,
                    amount_micros=micros,
                    budget_id=budget_id,
                    budget_period=record.budget_period,
                )
                referenced.add(record.budget_key)
            for budget_key in sorted(referenced):
                budget = budgets[budget_key]
                self.budgets_seen[budget_key] = budget
                budget_id = self.index.id("google", "budget", budget_key)
                micros = (
                    budget.amount_micros
                    if budget.amount_micros is not None
                    else budget.total_amount_micros
                )
                self._observe(
                    "budget",
                    budget_id,
                    Observation(
                        captured_at=at,
                        budget_daily=budget.amount,
                        budget_total=budget.total_amount,
                        budget_amount_micros=micros,
                        payload={
                            "period": budget.period,
                            "explicitly_shared": budget.explicitly_shared,
                        },
                    ),
                )
                if budget.explicitly_shared:
                    subject = ("budget", budget_id)
                    self.subject_source[subject] = "google"
                    self.subject_currency[subject] = self.wspec.google.currency
                    self.subject_names[subject] = (
                        f"Shared budget {self.budget_external_ids[budget_id]}"
                    )
                    platform[subject] = _Platform(
                        shape="budget",
                        status=None,
                        daily_budget=budget.amount,
                        total_budget=budget.total_amount,
                        amount_micros=micros,
                        budget_id=None,
                        budget_period=budget.period,
                    )
            for keyword in self.world.google_keywords(at):
                if keyword.status == "REMOVED" or not self.index.has(
                    "google", "keyword", keyword.key
                ):
                    continue
                self._observe(
                    "keyword",
                    self.index.id("google", "keyword", keyword.key),
                    Observation(
                        captured_at=at,
                        status=keyword.status,
                        quality_score=keyword.quality_score,
                        payload={"status": keyword.status, "match_type": keyword.match_type},
                    ),
                )
            for action in self.world.google_conversion_actions(at):
                self._observe(
                    "conversion_action",
                    self.index.id("google", "conversion_action", action.key),
                    Observation(
                        captured_at=at,
                        status=action.status,
                        payload={
                            "status": action.status,
                            "include_in_conversions_metric": action.include_in_conversions_metric,
                            "counting_type": action.counting_type,
                            "attribution_model": action.attribution_model,
                        },
                    ),
                )
        if self.wspec.linkedin is not None:
            for account in self.wspec.linkedin.accounts:
                for campaign in self.world.linkedin_campaigns(account.id, at):
                    self.linkedin_seen[campaign.key] = campaign
                    campaign_id = self.index.id("linkedin", "campaign", campaign.key)
                    status = _LINKEDIN_STATUS[campaign.status]
                    self._observe(
                        "campaign",
                        campaign_id,
                        Observation(
                            captured_at=at,
                            status=status,
                            budget_daily=campaign.daily_budget,
                            budget_total=campaign.total_budget,
                            payload={
                                "raw_status": campaign.status,
                                "explicitly_shared": False,
                                "budget_period": None,
                                "campaign_group": (
                                    f"urn:li:sponsoredCampaignGroup:{campaign.group_id}"
                                ),
                            },
                        ),
                    )
                    subject = ("campaign", campaign_id)
                    self.subject_source[subject] = "linkedin"
                    self.subject_currency[subject] = campaign.currency
                    self.subject_names[subject] = campaign.name
                    platform[subject] = _Platform(
                        shape="own_budget",
                        status=status,
                        daily_budget=campaign.daily_budget,
                        total_budget=campaign.total_budget,
                        amount_micros=None,
                        budget_id=None,
                        budget_period=None,
                    )
        self.last_platform.update(platform)
        return platform

    def _baseline(self, key: SubjectKey, state: _Platform, at: datetime) -> DecisionInfo:
        """The baseline decision of a subject: its platform state, in its own shape."""
        # A Google own-budget campaign carries only the amount its budget period defines (the
        # snapshot leaves the other unset); a shared member owns no amount at all.
        daily = None if state.shape == "shared_member" else state.daily_budget
        total = None if state.shape == "shared_member" else state.total_budget
        return DecisionInfo(
            subject_type=key[0],
            subject_id=key[1],
            shape=state.shape,
            status=state.status,
            daily_budget=daily,
            total_budget=total,
            budget_id=state.budget_id if state.shape == "shared_member" else None,
            tombstone=False,
            decided_at=at,
            origin="baseline",
        )

    def _record(self, decision: DecisionInfo) -> None:
        self.history.setdefault(decision.key, []).append(decision)
        self.history[decision.key].sort(key=lambda item: item.decided_at)
        if decision.tombstone:
            # acknowledge_removal resolves the missing discrepancy at once (AAP 0.5.4.4).
            self._close(decision.key, "missing")

    def decision_at(self, key: SubjectKey, at: datetime) -> DecisionInfo | None:
        """The latest decision of a subject recorded at or before ``at``."""
        found: DecisionInfo | None = None
        for decision in self.history.get(key, ()):
            if decision.decided_at <= at:
                found = decision
        return found

    def _apply_decisions(
        self, pending: list[DecisionSpec | DecisionInfo], until: datetime
    ) -> list[DecisionSpec | DecisionInfo]:
        """Record every pending spec decision decided at or before ``until``."""
        remaining: list[DecisionSpec | DecisionInfo] = []
        for item in pending:
            if isinstance(item, DecisionInfo):
                if item.decided_at <= until:
                    self._record(item)
                else:
                    remaining.append(item)
                continue
            instant = self.anchor_midnight + timedelta(hours=item.decided_at_offset_hours)
            if instant > until:
                remaining.append(item)
                continue
            self._record(self._materialise(item, instant))
        return remaining

    def _materialise(self, item: DecisionSpec, at: datetime) -> DecisionInfo:
        """Apply a decision patch to the subject's current decision (AAP 0.5.2 patch model)."""
        key = self.index.subject(item.subject)
        base = self.decision_at(key, at)
        state = self.last_platform.get(key)
        shape: DecisionShape = (
            base.shape
            if base is not None
            else (state.shape if state is not None else _spec_shape(self.wspec, item.subject))
        )

        def pick(value: Any, previous: Any, platform: Any) -> Any:
            if value is not None:
                return value
            return previous if base is not None else platform

        status = pick(item.status, base.status if base else None, state.status if state else None)
        daily = pick(
            item.daily_budget,
            base.daily_budget if base else None,
            state.daily_budget if state else None,
        )
        total = pick(
            item.total_budget,
            base.total_budget if base else None,
            state.total_budget if state else None,
        )
        budget_id = (
            self.index.id("google", "budget", item.budget_key)
            if item.budget_key is not None
            else pick(None, base.budget_id if base else None, state.budget_id if state else None)
        )
        if item.tombstone and shape != "budget" and item.status is None:
            status = "ended"
        if shape == "budget":
            status = None
            budget_id = None
        elif shape == "shared_member":
            daily = total = None
        else:
            budget_id = None
        return DecisionInfo(
            subject_type=key[0],
            subject_id=key[1],
            shape=shape,
            status=status,
            daily_budget=daily,
            total_budget=total,
            budget_id=budget_id,
            tombstone=item.tombstone,
            decided_at=at,
            origin=item.origin,
        )

    # -- discrepancy detection: the domain's pure C0 contract on transient rows ----------

    def _complete(self, source: Source) -> bool:
        """Whether every entity enumeration of ``source`` completes (AAP 0.3.3 safe absence).

        A replay's completeness is fixed per source (a persistent enumeration fault or an
        ``entities`` freshness override), so the dataset's baseline is sealed after its first
        sync exactly when this holds (``sync.snapshots.baseline_sealed``).
        """
        return not (self.google_incomplete if source == "google" else self.linkedin_incomplete)

    def _snapshot_row(self, key: SubjectKey, state: _Platform, at: datetime) -> EntitySnapshot:
        """This sync's ``entity_snapshot`` of a subject, as a transient row (never stored)."""
        source = self.subject_source[key]
        return EntitySnapshot(
            dataset_id=self.datasets[source].dataset_id,
            source=source,
            entity_type=key[0],
            entity_id=key[1],
            captured_at=at,
            status=state.status,
            budget_daily=_stored(state.daily_budget),
            budget_total=_stored(state.total_budget),
            budget_amount_micros=state.amount_micros,
            budget_id=state.budget_id,
            payload={},
        )

    def _decision_row(self, decision: DecisionInfo) -> Decision:
        """A decision as a transient ``decision`` row (never stored), built once per decision."""
        row = self.decision_rows.get(decision)
        if row is None:
            source = self.subject_source[decision.key]
            row = Decision(
                dataset_id=self.datasets[source].dataset_id,
                source=source,
                subject_type=decision.subject_type,
                subject_id=decision.subject_id,
                shape=decision.shape,
                status=decision.status,
                daily_budget=_stored(decision.daily_budget),
                total_budget=_stored(decision.total_budget),
                budget_id=decision.budget_id,
                tombstone=decision.tombstone,
                decided_at=decision.decided_at,
                origin=decision.origin,
                patch={},
            )
            self.decision_rows[decision] = row
        return row

    def _subject_state(
        self,
        key: SubjectKey,
        state: _Platform,
        decision: DecisionInfo | None,
        snapshot: EntitySnapshot | None,
    ) -> SubjectState:
        """What ``decisions.load_subject_state`` reads for a subject after this sync.

        The entity rows follow the subject's newest snapshot (``state``): its shape, budget
        period, own budget fields and, for a shared member, the shared budget it draws from.
        """
        subject_type, subject_id = key
        source = self.subject_source[key]
        member = state.shape == "shared_member"
        period = (
            cast(BudgetPeriod, state.budget_period)
            if state.budget_period in get_args(BudgetPeriod)
            else None
        )
        if source == "linkedin":
            has_daily = state.daily_budget is not None
            has_total = state.total_budget is not None
        else:
            has_daily = not member and period == "DAILY"
            has_total = not member and period == "CUSTOM_PERIOD"
        shared_id = state.budget_id if member else None
        decided_id = None if decision is None else decision.budget_id
        return SubjectState(
            ref=DecisionRef(type=subject_type, id=subject_id),
            dataset_id=self.datasets[source].dataset_id,
            dataset_state="active",
            source="google" if source == "google" else "linkedin",
            currency=self.subject_currency[key],
            name=self.subject_names[key],
            shape=state.shape,
            has_daily_budget=has_daily,
            has_total_budget=has_total,
            budget_period=period,
            budget_id=shared_id,
            budget_external_id=None if shared_id is None else self.budget_external_ids[shared_id],
            latest_snapshot=snapshot,
            current_decision=None if decision is None else self._decision_row(decision),
            decided_budget_external_id=(
                None if decided_id is None else self.budget_external_ids.get(decided_id)
            ),
        )

    def _verdict(
        self, key: SubjectKey, state: _Platform, decision: DecisionInfo, at: datetime
    ) -> tuple[tuple[Mismatch, ...], bool]:
        """The mismatches of :func:`~ads_engine.decisions.compare` and its verified match.

        Both read only the snapshot's values, the subject state those values fix, the
        decision and whether the snapshot was captured after it, so one result serves every
        sync at which all of them are unchanged.
        """
        memo: _VerdictKey = (key, state, decision, at > decision.decided_at)
        found = self.verdicts.get(memo)
        if found is None:
            snapshot = self._snapshot_row(key, state, at)
            subject = self._subject_state(key, state, decision, snapshot)
            row = self._decision_row(decision)
            found = (
                tuple(compare(snapshot, row, state=subject)),
                verified_match(snapshot, row, state=subject),
            )
            self.verdicts[memo] = found
        return found

    def _raise(
        self,
        key: SubjectKey,
        kind: DiscrepancyKind,
        at: datetime,
        decided: Mapping[str, Any] | None,
        platform: Mapping[str, Any] | None,
    ) -> None:
        """Open a discrepancy, or refresh the subject's unresolved one of the same kind.

        As ``decisions.raise_discrepancy`` does, a refreshed row takes this comparison's
        decided and platform values and keeps its ``detected_at`` and state.
        """
        decided_value = None if decided is None else MappingProxyType(dict(decided))
        platform_value = None if platform is None else MappingProxyType(dict(platform))
        found = self.open.get((key, kind))
        if found is not None:
            self.open[(key, kind)] = replace(
                found, decided_value=decided_value, platform_value=platform_value
            )
            return
        self.open[(key, kind)] = _Open(
            subject=key,
            source=self.subject_source[key],
            kind=kind,
            decided_value=decided_value,
            platform_value=platform_value,
            detected_at=at,
        )

    def _close(self, key: SubjectKey, *kinds: DiscrepancyKind) -> None:
        """Resolve the subject's unresolved discrepancies of ``kinds``."""
        for kind in kinds:
            self.open.pop((key, kind), None)

    def _missing(self, key: SubjectKey, decision: DecisionInfo, at: datetime) -> None:
        """Judge a tracked subject a complete enumeration did not return.

        ``missing`` against the current decision, valued by
        :func:`~ads_engine.decisions.describe_decision`; under a tombstone the absence is the
        acknowledged state, so nothing is raised and the subject's ``status`` and ``budget``
        rows (a reappearance that went away again) close.
        """
        if decision.tombstone:
            self._close(key, "status", "budget")
            return
        subject = self._subject_state(key, self.last_platform[key], decision, None)
        decided = describe_decision(self._decision_row(decision), state=subject)
        self._raise(key, "missing", at, decided, None)

    def _detect(
        self, at: datetime, platform: Mapping[SubjectKey, _Platform], tracked: set[SubjectKey]
    ) -> None:
        """Judge every subject after one entity sync, as ``sync.snapshots`` does.

        A returned subject without a decision is ``untracked`` (valued by
        :func:`~ads_engine.decisions.describe_snapshot`) once the baseline is sealed, and is
        baselined while it is open (``ensure_baseline``). A tombstone is judged only on a
        complete enumeration. Otherwise every mismatch :func:`~ads_engine.decisions.compare`
        finds is raised or refreshed, and with none the subject's ``status``, ``budget`` and
        ``untracked`` rows close once :func:`~ads_engine.decisions.verified_match` holds, with
        an ``awaiting_platform`` ``missing`` row too when the enumeration is complete. A
        tracked subject a complete enumeration did not return goes to :meth:`_missing`.
        :meth:`decision_at` never returns a decision recorded after ``at``, so no snapshot
        here predates the decision it is judged against.
        """
        for key in sorted(tracked | platform.keys()):
            complete = self._complete(self.subject_source[key])
            decision = self.decision_at(key, at)
            state = platform.get(key)
            if state is None:
                if complete and decision is not None:
                    self._missing(key, decision, at)
                continue
            if decision is None:
                if not complete:
                    self._record(self._baseline(key, state, at))
                    continue
                snapshot = self._snapshot_row(key, state, at)
                subject = self._subject_state(key, state, None, snapshot)
                self._raise(key, "untracked", at, None, describe_snapshot(snapshot, state=subject))
                continue
            if decision.tombstone and not complete:
                continue
            mismatches, verified = self._verdict(key, state, decision, at)
            for mismatch in mismatches:
                self._raise(key, mismatch.kind, at, mismatch.decided_value, mismatch.platform_value)
            if mismatches or not verified:
                continue
            self._close(key, "status", "budget", "untracked")
            missing = self.open.get((key, "missing"))
            if missing is not None and missing.state == "awaiting_platform" and complete:
                self._close(key, "missing")

    # -- entities ------------------------------------------------------------------------

    def entities(self) -> None:
        """Entity rows as the latest entity sync left them (AAP 0.5.2 account tables).

        A campaign keeps the state of its newest snapshot (a campaign no longer listed keeps
        its last known row); without snapshots the world's state at the build instant is
        read. Bulk filler is never indexed and never loaded.
        """
        self.accounts: dict[int, AccountInfo] = {}
        self.groups: dict[int, CampaignGroupInfo] = {}
        self.campaigns: dict[int, CampaignInfo] = {}
        self.budgets: dict[int, BudgetInfo] = {}
        self.ad_groups: dict[int, AdGroupInfo] = {}
        self.keywords: dict[int, KeywordInfo] = {}
        self.creatives: dict[int, CreativeInfo] = {}
        self.actions: dict[int, ConversionActionInfo] = {}
        google = self.wspec.google
        if google is not None:
            self._google_entities(google)
        linkedin = self.wspec.linkedin
        if linkedin is not None:
            self._linkedin_entities(linkedin)

    def _google_entities(self, google: GoogleSpec) -> None:
        account_id = self.index.id("google", "account", google.customer_id)
        self.accounts[account_id] = AccountInfo(
            id=account_id,
            source="google",
            external_id=google.customer_id,
            name=google.name,
            currency=google.currency,
            timezone=google.time_zone,
        )
        current = {record.key: record for record in self.world.google_campaigns(self.now)}
        budgets_now = {record.key: record for record in self.world.google_budgets(self.now)}
        used_budgets: set[str] = set()
        for spec in google.campaigns:
            record = self.google_seen.get(spec.key) or current.get(spec.key)
            if record is None:
                continue
            campaign_id = self.index.id("google", "campaign", spec.key)
            used_budgets.add(record.budget_key)
            self.campaigns[campaign_id] = CampaignInfo(
                id=campaign_id,
                source="google",
                account_id=account_id,
                external_id=self.index.external_id("google", "campaign", spec.key),
                name=record.name,
                status=cast(CampaignStatus, _GOOGLE_STATUS[record.status]),
                raw_status=record.status,
                objective_or_channel=record.channel,
                objective_group=objective_group("google", record.channel),
                campaign_group_id=None,
                budget_id=self.index.id("google", "budget", record.budget_key),
                daily_budget=record.budget_amount,
                total_budget=record.budget_total_amount,
                budget_period=cast(BudgetPeriod, record.budget_period),
                currency=google.currency,
            )
        for budget_key in sorted(used_budgets):
            budget = self.budgets_seen.get(budget_key) or budgets_now[budget_key]
            budget_id = self.index.id("google", "budget", budget_key)
            self.budgets[budget_id] = BudgetInfo(
                id=budget_id,
                external_id=self.index.external_id("google", "budget", budget_key),
                # The budget table stores no name, so load_context reads every budget with "".
                name="",
                amount=budget.amount if budget.amount is not None else budget.total_amount,
                amount_micros=(
                    budget.amount_micros
                    if budget.amount_micros is not None
                    else budget.total_amount_micros
                ),
                period=cast(BudgetPeriod, budget.period),
                explicitly_shared=budget.explicitly_shared,
                currency=google.currency,
            )
        groups = {record.key: record for record in self.world.google_ad_groups(self.now)}
        for spec_group in google.ad_groups:
            group = groups.get(spec_group.key)
            if group is None:
                continue
            group_id = self.index.id("google", "ad_group", group.key)
            self.ad_groups[group_id] = AdGroupInfo(
                id=group_id,
                campaign_id=self.index.id("google", "campaign", group.campaign_key),
                external_id=self.index.external_id("google", "ad_group", group.key),
                name=group.name,
                status=group.status,
            )
        group_keys = {spec_group.key: spec_group.campaign_key for spec_group in google.ad_groups}
        for keyword in self.world.google_keywords(self.now):
            if not self.index.has("google", "keyword", keyword.key):
                continue
            spec_keyword = next(item for item in google.keywords if item.key == keyword.key)
            keyword_id = self.index.id("google", "keyword", keyword.key)
            self.keywords[keyword_id] = KeywordInfo(
                id=keyword_id,
                ad_group_id=self.index.id("google", "ad_group", spec_keyword.ad_group_key),
                campaign_id=self.index.id(
                    "google", "campaign", group_keys[spec_keyword.ad_group_key]
                ),
                criterion_id=str(keyword.criterion_id),
                text=keyword.text,
                match_type=keyword.match_type,
                status=keyword.status,
                quality_score=keyword.quality_score,
            )
        for action in self.world.google_conversion_actions(self.now):
            action_id = self.index.id("google", "conversion_action", action.key)
            self.actions[action_id] = ConversionActionInfo(
                id=action_id,
                external_id=str(action.id),
                name=action.name,
                status=action.status,
                include_in_conversions_metric=action.include_in_conversions_metric,
                counting_type=action.counting_type,
                attribution_model=action.attribution_model,
            )

    def _linkedin_entities(self, linkedin: LinkedInSpec) -> None:
        for account in self.world.linkedin_accounts():
            account_id = self.index.id("linkedin", "account", str(account.id))
            self.accounts[account_id] = AccountInfo(
                id=account_id,
                source="linkedin",
                external_id=str(account.id),
                name=account.name,
                currency=account.currency,
            )
            for group in self.world.linkedin_campaign_groups(account.id, self.now):
                group_id = self.index.id("linkedin", "campaign_group", group.key)
                self.groups[group_id] = CampaignGroupInfo(
                    id=group_id,
                    account_id=account_id,
                    external_id=str(group.id),
                    name=group.name,
                    status=group.status,
                    daily_budget=group.daily_budget,
                    total_budget=group.total_budget,
                )
            current = {
                record.key: record for record in self.world.linkedin_campaigns(account.id, self.now)
            }
            for spec in linkedin.campaigns:
                if spec.account_id != account.id:
                    continue
                record = self.linkedin_seen.get(spec.key) or current.get(spec.key)
                if record is None:
                    continue
                campaign_id = self.index.id("linkedin", "campaign", spec.key)
                self.campaigns[campaign_id] = CampaignInfo(
                    id=campaign_id,
                    source="linkedin",
                    account_id=account_id,
                    external_id=str(record.id),
                    name=record.name,
                    status=cast(CampaignStatus, _LINKEDIN_STATUS[record.status]),
                    raw_status=record.status,
                    objective_or_channel=record.objective,
                    objective_group=objective_group("linkedin", record.objective),
                    campaign_group_id=self.index.id("linkedin", "campaign_group", spec.group_key),
                    budget_id=None,
                    daily_budget=record.daily_budget,
                    total_budget=record.total_budget,
                    budget_period=None,
                    currency=record.currency,
                )
            for creative in self.world.linkedin_creatives(account.id, None, self.now):
                key = self.creative_external.get(creative.urn)
                campaign_key = self.linkedin_external.get(str(creative.campaign_id))
                if key is None or campaign_key is None:
                    continue
                creative_id = self.index.id("linkedin", "creative", key)
                self.creatives[creative_id] = CreativeInfo(
                    id=creative_id,
                    campaign_id=self.index.id("linkedin", "campaign", campaign_key),
                    urn=creative.urn,
                    intended_status=creative.intended_status,
                    headline=creative.headline,
                    intro_text=creative.intro_text,
                    cta_label=creative.cta_label,
                    lead_form_urn=creative.lead_form_urn,
                )

    # -- daily facts -----------------------------------------------------------------------

    def facts(self) -> None:
        """Window facts on each source's native dates, plus K1's trailing baseline.

        Google rows are the account time zone's dates and LinkedIn rows UTC dates, exactly as
        returned; nothing is rebucketed (AAP 0.5.4.1). A LinkedIn campaign day sums its
        creatives' rows; a creative's window totals count its delivery days (impressions > 0).
        """
        start, end = self.window.start, self.window.end
        days: dict[int, list[DayFact]] = {}
        self.keyword_days: dict[int, list[KeywordDay]] = {}
        self.search_terms: list[SearchTermRow] = []
        self.creative_totals: dict[int, CreativeTotals] = {}
        self.k1_baseline: RateBaseline | None = None
        if self.wspec.google is not None:
            for row in self.world.google_campaign_daily(start, end):
                campaign_id = self._google_campaign(row.campaign_id)
                if campaign_id is None:
                    continue
                days.setdefault(campaign_id, []).append(
                    DayFact(
                        date=row.date,
                        cost=row.cost,
                        impressions=row.impressions,
                        clicks=row.clicks,
                        conversions=row.conversions,
                        conversions_value=row.conversions_value,
                        budget_lost_is=row.search_budget_lost_impression_share,
                        impression_share=row.search_impression_share,
                    )
                )
            self._k1_baseline(end)
            self._keyword_facts(start, end)
            self._search_term_facts(start, end)
        if self.wspec.linkedin is not None:
            self._linkedin_facts(start, end, days)
        self.campaign_days = {
            campaign_id: tuple(sorted(rows, key=lambda item: item.date))
            for campaign_id, rows in sorted(days.items())
        }

    def _google_campaign(self, external: int) -> int | None:
        key = self.google_external.get(str(external))
        if key is None:
            return None
        campaign_id = self.index.id("google", "campaign", key)
        return campaign_id if campaign_id in self.campaigns else None

    def _k1_baseline(self, end: date) -> None:
        """K1's account rate: conversions / clicks over ``k1.baseline_days`` ending at ``end``."""
        span = self.thresholds.int("k1.baseline_days")
        start = end - timedelta(days=span - 1)
        conversions = Decimal(0)
        clicks = 0
        rows = 0
        for row in self.world.google_campaign_daily(start, end):
            if self._google_campaign(row.campaign_id) is None:
                continue
            conversions += row.conversions
            clicks += row.clicks
            rows += 1
        if rows:
            self.k1_baseline = RateBaseline(
                conversions=conversions, clicks=clicks, start=start, end=end
            )

    def _keyword_facts(self, start: date, end: date) -> None:
        for row in self.world.google_keyword_daily(start, end):
            key = self.criterion_external.get(str(row.criterion_id))
            if key is None:
                continue
            keyword_id = self.index.id("google", "keyword", key)
            if keyword_id not in self.keywords:
                continue
            self.keyword_days.setdefault(keyword_id, []).append(
                KeywordDay(
                    keyword_id=keyword_id,
                    date=row.date,
                    cost=row.cost,
                    clicks=row.clicks,
                    impressions=row.impressions,
                    conversions=row.conversions,
                    historical_quality_score=row.historical_quality_score,
                    average_cpc=row.average_cpc,
                )
            )
        for rows in self.keyword_days.values():
            rows.sort(key=lambda item: item.date)

    def _search_term_facts(self, start: date, end: date) -> None:
        """Total search-term rows over a date range (the ``search_term_window`` grain).

        Rows are keyed per (ad group, campaign, matched criterion, term); status and matched
        keyword come from the latest date.
        """
        totals: dict[tuple[int, int, str | None, str], list[Any]] = {}
        for row in self.world.google_search_term_daily(start, end):
            group_key = self.group_external.get(str(row.ad_group_id))
            campaign_id = self._google_campaign(row.campaign_id)
            if group_key is None or campaign_id is None:
                continue
            group_id = self.index.id("google", "ad_group", group_key)
            criterion = None if row.criterion_id is None else str(row.criterion_id)
            key = (group_id, campaign_id, criterion, row.search_term)
            entry = totals.get(key)
            if entry is None:
                entry = [Decimal(0), 0, 0, Decimal(0), Decimal(0), row.date, row]
                totals[key] = entry
            entry[0] += row.cost
            entry[1] += row.clicks
            entry[2] += row.impressions
            entry[3] += row.conversions
            entry[4] += row.conversions_value
            if row.date >= entry[5]:
                entry[5] = row.date
                entry[6] = row
        # load_context's order: ad group, matched criterion (none last), then criterion and
        # term in code-point order (its sort keys use the "C" collation).
        for (group_id, campaign_id, criterion, term), entry in sorted(
            totals.items(),
            key=lambda item: (item[0][0], item[0][2] is None, item[0][2] or "", item[0][3]),
        ):
            latest = cast(GoogleSearchTermDay, entry[6])
            self.search_terms.append(
                SearchTermRow(
                    ad_group_id=group_id,
                    campaign_id=campaign_id,
                    search_term=term,
                    term_status=latest.status,
                    matched_criterion_id=criterion,
                    matched_keyword_text=latest.keyword_text,
                    matched_match_type=latest.match_type,
                    cost=entry[0],
                    clicks=entry[1],
                    impressions=entry[2],
                    conversions=entry[3],
                    conversions_value=entry[4],
                )
            )

    def _linkedin_facts(self, start: date, end: date, days: dict[int, list[DayFact]]) -> None:
        linkedin = cast(LinkedInSpec, self.wspec.linkedin)
        per_day: dict[tuple[int, date], list[Any]] = {}
        per_creative: dict[int, list[Any]] = {}
        for account in linkedin.accounts:
            for row in self.world.linkedin_creative_daily(account.id, start, end):
                campaign_key = self.linkedin_external.get(str(row.campaign_id))
                creative_key = self.creative_external.get(f"{CREATIVE_URN_PREFIX}{row.creative_id}")
                if campaign_key is None or creative_key is None:
                    continue
                campaign_id = self.index.id("linkedin", "campaign", campaign_key)
                creative_id = self.index.id("linkedin", "creative", creative_key)
                if campaign_id not in self.campaigns:
                    continue
                values = (
                    row.cost,
                    row.impressions,
                    row.clicks,
                    row.form_opens,
                    row.leads,
                    row.landing_page_clicks,
                    row.website_conversions,
                )
                day = per_day.setdefault((campaign_id, row.date), [Decimal(0), 0, 0, 0, 0, 0, 0])
                creative = per_creative.setdefault(
                    creative_id, [campaign_id, Decimal(0), 0, 0, 0, 0, 0, 0, 0]
                )
                for position, value in enumerate(values):
                    day[position] += value
                    creative[position + 1] += value
                if row.impressions > 0:
                    creative[8] += 1
        for (campaign_id, day_date), sums in per_day.items():
            days.setdefault(campaign_id, []).append(
                DayFact(
                    date=day_date,
                    cost=sums[0],
                    impressions=sums[1],
                    clicks=sums[2],
                    form_opens=sums[3],
                    leads=sums[4],
                    landing_page_clicks=sums[5],
                    website_conversions=sums[6],
                )
            )
        for creative_id, sums in sorted(per_creative.items()):
            self.creative_totals[creative_id] = CreativeTotals(
                creative_id=creative_id,
                campaign_id=sums[0],
                cost=sums[1],
                impressions=sums[2],
                clicks=sums[3],
                form_opens=sums[4],
                leads=sums[5],
                landing_page_clicks=sums[6],
                website_conversions=sums[7],
                delivery_days=sums[8],
            )

    # -- demographic segments --------------------------------------------------------------

    def segments(self) -> None:
        """Segment rows of every preset window ending on the default end at the latest fetch.

        The fetch runs at ``segment_fetched_at_offset_hours`` (the build instant by default),
        so its windows end on the default window end of that instant: a fetch a day behind
        yields windows ending a day earlier, which no current run matches (C3a's
        ``segment_window_unavailable`` "not yet available" case).
        """
        self.segment_rows: list[SegmentRow] = []
        self.segment_windows: tuple[tuple[date, date], ...] = ()
        linkedin = self.wspec.linkedin
        if linkedin is None:
            return
        offset = self.spec.segment_fetched_at_offset_hours
        fetched_at = self.now if offset is None else self.anchor_midnight + timedelta(hours=offset)
        end = default_end(self.sources, now=fetched_at)
        windows: set[tuple[date, date]] = set()
        for account in linkedin.accounts:
            for pivot, facet in _SEGMENT_PIVOTS.items():
                for preset in self.window.presets:
                    start = end - timedelta(days=preset - 1)
                    for record in self.world.linkedin_segments(account.id, pivot, start, end):
                        key = self.linkedin_external.get(str(record.campaign_id))
                        if key is None:
                            continue
                        campaign_id = self.index.id("linkedin", "campaign", key)
                        if campaign_id not in self.campaigns:
                            continue
                        windows.add((start, end))
                        self.segment_rows.append(
                            SegmentRow(
                                campaign_id=campaign_id,
                                facet=facet,
                                value=record.value,
                                window_start=start,
                                window_end=end,
                                impressions=record.impressions,
                                clicks=record.clicks,
                                cost=record.cost,
                                leads=record.leads,
                            )
                        )
        # load_context's order within a window: campaign, then facet and value in code-point
        # order (its sort keys use the "C" collation).
        self.segment_rows.sort(key=lambda row: (row.campaign_id, row.facet, row.value))
        self.segment_windows = tuple(sorted(windows))

    # -- faults as the sync jobs would have recorded them ----------------------------------

    def _faults(
        self,
        source: Source,
        surface: str,
        attributes: Mapping[str, str],
        *,
        include_transient: bool = False,
    ) -> tuple[Fault, ...]:
        """The spec faults of one surface whose effect outlasts the sync schedule.

        A fault with ``times`` is transient: the retry schedule recovers from it before the
        build instant, so it leaves no trace in stored state (``include_transient`` keeps it,
        for ingestion-time faults such as a lead's form decoration, which happen once). A fault
        qualified by ``page`` "1" hits the first (for batch reads, the only) page and applies;
        one qualified by a later page disturbs a paged read past its first page, which only the
        transport-level tests exercise, so the context ignores it.
        """
        found: list[Fault] = []
        for fault in self.world.faults_for(source, surface):
            page = fault.match.get("page")
            if (fault.times is not None and not include_transient) or page not in (None, "1"):
                continue
            if all(
                attributes.get(name) == value
                for name, value in fault.match.items()
                if name != "page"
            ):
                found.append(fault)
        return tuple(found)

    # -- leads and their match state -----------------------------------------------------

    def leads(self) -> None:
        """LinkedIn leads submitted within ``window.utc_bounds()``, test leads never stored.

        Each lead's ``lead_match`` row in the effective CRM dataset follows the matching job
        (AAP 0.3.3): ``no_email`` for a decoration that does not describe the response's
        version, an unknown version or no email answer; ``error`` when the email can no longer
        be re-fetched or the HubSpot lookup keeps failing; ``no_match`` while no contact has
        the email; ``matched`` with ``contact_read_status`` ``error`` when the contact's
        refresh by id fails.
        """
        self.lead_rows: list[LeadInfo] = []
        self.lead_sources: list[str] = []
        linkedin = self.wspec.linkedin
        if linkedin is None:
            return
        start, end = self.window.utc_bounds()
        responses: list[LinkedInLeadResponse] = []
        for account in linkedin.accounts:
            responses.extend(
                response
                for response in self.world.linkedin_lead_responses(
                    account.id, _epoch_ms(start), _epoch_ms(end)
                )
                if not response.test_lead
            )
        responses.sort(key=lambda item: (item.submitted_at, item.id))
        matching = self.linkedin_crm_ref is not None
        for position, response in enumerate(responses, start=1):
            campaign_key = self.linkedin_external.get(str(response.campaign_id))
            if campaign_key is None:
                continue
            campaign_id = self.index.id("linkedin", "campaign", campaign_key)
            if campaign_id not in self.campaigns:
                continue
            creative_urn = (
                None
                if response.creative_id is None
                else f"{CREATIVE_URN_PREFIX}{response.creative_id}"
            )
            creative_key = (
                None if creative_urn is None else self.creative_external.get(creative_urn)
            )
            status: MatchStatus | None = None
            reason: str | None = None
            contact: str | None = None
            read: ContactReadStatus | None = None
            if matching:
                status, reason, contact, read = self._match(response)
            self.lead_rows.append(
                LeadInfo(
                    id=position,
                    external_id=response.id,
                    campaign_id=campaign_id,
                    creative_id=(
                        None
                        if creative_key is None
                        else self.index.id("linkedin", "creative", creative_key)
                    ),
                    creative_urn=creative_urn if creative_key is not None else None,
                    lead_form_urn=f"{FORM_URN_PREFIX}{response.form_id}",
                    lead_form_version=response.form_version,
                    submitted_at=response.submitted_at,
                    match_status=status,
                    contact_hs_id=contact,
                    contact_read_status=read,
                    match_reason=reason,
                )
            )
            source_key = self.creative_external.get(
                f"{CREATIVE_URN_PREFIX}{response.source_creative_id}"
            )
            self.lead_sources.append(source_key or "")

    def _match(
        self, response: LinkedInLeadResponse
    ) -> tuple[MatchStatus, str | None, str | None, ContactReadStatus | None]:
        """One lead's ``lead_match`` state: status, reason, contact id and read status."""
        account = {"account_id": str(response.account_id)}
        for fault in self._faults(
            "linkedin", "lead_form_responses", account, include_transient=True
        ):
            if response.id not in fault.ids:
                continue
            if fault.kind == "decoration_mismatch":
                return ("no_email", _REASON_FORM_MAP_MISMATCH, None, None)
            if fault.kind == "no_decoration":
                return ("no_email", _REASON_UNKNOWN_FORM_VERSION, None, None)
        version = self.world.linkedin_form_version(response.form_id, response.form_version)
        if version is None:
            return ("no_email", _REASON_UNKNOWN_FORM_VERSION, None, None)
        email_questions = set(version.email_question_ids)
        if not any(
            answer.question_id in email_questions and answer.answer.strip()
            for answer in response.answers
        ):
            return ("no_email", _REASON_NO_EMAIL_ANSWER, None, None)
        for fault in self._faults("linkedin", "lead_form_responses_batch", account):
            if fault.kind in ("omit_ids", "batch_errors") and response.id in fault.ids:
                return ("error", _REASON_EMAIL_UNRETRIEVABLE, None, None)
            if fault.kind in _FAILING_KINDS:
                return ("error", _REASON_REFETCH_FAILED, None, None)
        for fault in self._faults("hubspot", "objects_batch_read", {"object_type": "contacts"}):
            if fault.kind in _FAILING_KINDS:
                return ("error", _REASON_LOOKUP_FAILED, None, None)
        record = self.world.hubspot_contact_by_email_hash(_email_hash(response.email))
        if record is None or record.created_at > self.now:
            return ("no_match", _REASON_NO_CONTACT, None, None)
        unread = self._unreadable("contacts", (record.id,))
        return ("matched", None, record.id, "error" if unread else "ok")

    # -- CRM evidence per CRM dataset --------------------------------------------------------

    def crm(self) -> None:
        """The CRM dataset(s) a context reads: the effective one for LinkedIn and the primary.

        While LinkedIn is simulated its leads match into the companion, which holds their
        contacts, meetings, deals and fetches; HubSpot's primary holds the Google-touch
        contacts. Otherwise one dataset serves both and is built once.
        """
        self.linkedin_crm: CrmData | None = None
        self.google_crm: CrmData | None = None
        google_ref = self.google_crm_ref
        if self.wspec.hubspot is None or google_ref is None:
            return
        bindings = self.spec.crm_bindings or resolve_bindings(self.world, self.thresholds, self.now)
        lead_contacts = tuple(
            dict.fromkeys(
                lead.contact_hs_id
                for lead in self.lead_rows
                if lead.match_status == "matched" and lead.contact_hs_id is not None
            )
        )
        linkedin_ref = self.linkedin_crm_ref
        if linkedin_ref is not None and linkedin_ref == google_ref:
            shared = self._crm_data(google_ref, bindings, lead_contacts, google=True)
            self.linkedin_crm = shared
            self.google_crm = shared
            return
        if linkedin_ref is not None:
            self.linkedin_crm = self._crm_data(linkedin_ref, bindings, lead_contacts, google=False)
        self.google_crm = self._crm_data(google_ref, bindings, (), google=True)

    def _crm_data(
        self,
        ref: DatasetRef,
        bindings: CrmBindings,
        contact_ids: Sequence[str],
        *,
        google: bool,
    ) -> CrmData:
        """Everything one CRM dataset holds for the context (``load_context`` step 7)."""
        state = _CrmState(bindings)
        for record in self.world.hubspot_contacts_by_id(contact_ids):
            state.contacts[record.id] = self._contact_info(record, bindings)
        google_ids: list[str] = []
        if google and bindings.google.available:
            for record in self._google_touch_contacts(bindings.google):
                google_ids.append(record.id)
                state.contacts.setdefault(record.id, self._contact_info(record, bindings))
        for contact_id in sorted(state.contacts):
            for target in ("meetings", "deals"):
                self._associate(state, "contacts", contact_id, target)
        window_deals, searched = self._window_deals()
        for deal in window_deals:
            state.deals.setdefault(deal.id, _deal_info(deal))
            self._associate(state, "deals", deal.id, "contacts")
        if contact_ids:
            self._apply_patches(state)
        freshness = self.freshness.get((ref.dataset_id, "hubspot.deal_search"))
        complete = (
            searched
            and freshness is not None
            and freshness.completeness == "complete"
            and freshness.covered_from is not None
            and freshness.covered_through is not None
            and freshness.covered_from <= self.window.start
            and freshness.covered_through >= self.window.end
        )
        # Every stored deal created from window start to the run instant is a window deal,
        # as load_context selects them: the search's deals and any deal read as a contact's
        # association target (which, without a deal-to-contact fetch, stays unknown).
        lower = self.window.utc_bounds()[0]
        window_deal_ids = tuple(
            deal.hs_id
            for deal in sorted(
                state.deals.values(),
                key=lambda item: (item.created_at or lower, item.hs_id),
            )
            if deal.created_at is not None and lower <= deal.created_at <= self.now
        )
        touch_lower = self._google_touch_range()[0]
        discovered = sorted(
            (state.contacts[contact_id] for contact_id in google_ids),
            key=lambda item: (item.google_touch_at or touch_lower, item.hs_id),
        )
        return CrmData(
            dataset=ref,
            bindings=bindings,
            contacts=MappingProxyType(dict(sorted(state.contacts.items()))),
            meetings=MappingProxyType(dict(sorted(state.meetings.items()))),
            deals=MappingProxyType(dict(sorted(state.deals.items()))),
            # load_context's order: keys sorted, targets in code-point order.
            associations=MappingProxyType(
                {key: tuple(sorted(ids)) for key, ids in sorted(state.associations.items())}
            ),
            fetches=MappingProxyType(dict(sorted(state.fetches.items()))),
            window_deal_ids=window_deal_ids,
            deal_search_complete=complete,
            google_contacts=tuple(discovered),
        )

    def _contact_info(self, record: ContactRecord, bindings: CrmBindings) -> ContactInfo:
        """A contact's stage dates read through the verified bindings, as the refresh stores."""
        values = self.world.hubspot_properties(record)

        def bound(binding: ResolvedBinding) -> datetime | None:
            if not binding.available or binding.property is None:
                return None
            return _as_instant(values.get(binding.property))

        return ContactInfo(
            hs_id=record.id,
            lifecycle_stage=record.lifecycle_stage,
            mql_at=bound(bindings.mql),
            ql_at=bound(bindings.ql),
            google_campaign_ref=record.google_campaign_ref,
            google_touch_at=record.google_touch_at,
        )

    def _google_touch_range(self) -> tuple[datetime, datetime]:
        """The half-open instant range Google-touch discovery covers, as ``load_context`` reads.

        The window's Google-native instants, ``[start 00:00, end + 1 day 00:00)`` in the
        Google account's time zone (UTC while none is known; AAP 0.5.4.1 native days). The
        one account zone is both the per-campaign veto's and the zone budget histories date
        shared-budget member days in, so this range serves both vetoes.
        """
        zone = self.sources.google_timezone or "UTC"
        lower, _ = AnalysisWindow.day_bounds(self.window.start, zone)
        _, upper = AnalysisWindow.day_bounds(self.window.end, zone)
        return lower, upper

    def _google_touch_contacts(self, link: GoogleCrmBinding) -> list[ContactRecord]:
        """Google-touch contacts whose touch date falls within :meth:`_google_touch_range`."""
        if link.campaign_property is None or link.touch_date_property is None:
            return []
        start, end = self._google_touch_range()
        found = self.world.hubspot_search(
            "contacts",
            (
                SearchFilter(
                    link.touch_date_property,
                    "BETWEEN",
                    value=_epoch_ms(start),
                    high_value=_epoch_ms(end) - 1,
                ),
                SearchFilter(link.campaign_property, "HAS_PROPERTY"),
            ),
            sort_property=link.touch_date_property,
        )
        return [record for record in found if isinstance(record, ContactRecord)]

    def _window_deals(self) -> tuple[list[DealRecord], bool]:
        """Portal deals created from window start to run time, and whether the search ran."""
        for fault in self._faults("hubspot", "crm_search", {"object_type": "deals"}):
            if fault.kind in _FAILING_KINDS:
                return [], False
        found = self.world.hubspot_search(
            "deals",
            (
                SearchFilter(
                    "createdate",
                    "BETWEEN",
                    value=_epoch_ms(_midnight(self.window.start)),
                    high_value=_epoch_ms(self.now),
                ),
            ),
            sort_property="createdate",
        )
        return [record for record in found if isinstance(record, DealRecord)], True

    def _unreadable(self, wire: str, ids: Sequence[str]) -> set[str]:
        """Ids a by-id batch read of ``wire`` objects does not return (faults only)."""
        missing: set[str] = set()
        for fault in self._faults("hubspot", "objects_batch_read", {"object_type": wire}):
            if fault.kind in _FAILING_KINDS:
                return set(ids)
            if fault.kind in ("omit_ids", "batch_errors"):
                missing |= set(fault.ids) & set(ids)
        return missing

    def _associate(self, state: _CrmState, from_wire: str, object_id: str, to_wire: str) -> None:
        """One association fetch, its continuation and its target reads (AAP 0.3.3).

        ``NO_ASSOCIATIONS_FOUND`` is a genuine zero (``ok_empty``); any other failure of the
        input is ``error``; a failed continuation is ``partial`` with the first page kept.
        Targets that the batch read does not return are missing, never absent.
        """
        ids = self.world.hubspot_associations(from_wire, to_wire, object_id)
        status: AssociationStatus = "ok" if ids else "ok_empty"
        stored: tuple[str, ...] = ids
        attributes = {"object_type": from_wire, "to_object_type": to_wire}
        for fault in self._faults("hubspot", "associations_batch_read", attributes):
            if fault.kind in _FAILING_KINDS:
                status, stored = "error", ()
                break
            if object_id not in fault.ids:
                continue
            if fault.kind == "batch_errors" and not _no_associations(fault):
                status, stored = "error", ()
                break
            if fault.kind == "continuation_failure" and status == "ok":
                page = cast(HubSpotSpec, self.wspec.hubspot).association_page_size
                status, stored = "partial", ids[:page]
        from_type, to_type = _crm_type(from_wire), _crm_type(to_wire)
        target: TargetReadStatus | None = None
        missing: tuple[str, ...] = ()
        if status in ("ok", "partial") and stored:
            unread = self._unreadable(to_wire, stored)
            readable = [item for item in stored if item not in unread]
            returned: set[str] = set()
            if to_wire == "meetings":
                for meeting in self.world.hubspot_meetings(readable):
                    returned.add(meeting.id)
                    state.meetings.setdefault(meeting.id, _meeting_info(meeting))
            elif to_wire == "deals":
                for deal in self.world.hubspot_deals(readable):
                    returned.add(deal.id)
                    state.deals.setdefault(deal.id, _deal_info(deal))
            else:
                returned = {record.id for record in self.world.hubspot_contacts_by_id(readable)}
            missing = tuple(item for item in stored if item not in returned)
            target = "ok" if not missing else "error" if len(missing) == len(stored) else "partial"
        if status != "error":
            state.associations[(from_type, object_id, to_type)] = stored
        state.fetches[(from_type, object_id, to_type)] = AssociationFetchInfo(
            object_type=from_type,
            object_id=object_id,
            to_type=to_type,
            status=status,
            target_read_status=target,
            missing_target_ids=missing,
        )

    def _apply_patches(self, state: _CrmState) -> None:
        """Apply the spec's context-only CRM patches to the selected leads' contacts."""
        for patch in self.spec.crm_patches:
            for lead, creative_key in zip(self.lead_rows, self.lead_sources, strict=True):
                contact_id = lead.contact_hs_id
                if contact_id is None or contact_id not in state.contacts:
                    continue
                offset = (lead.submitted_at.astimezone(UTC).date() - self.anchor).days
                if not patch.selects(creative_key, offset):
                    continue
                contact = state.contacts[contact_id]
                if patch.clear_ql:
                    contact = replace(contact, ql_at=None)
                if patch.ql_after_touch_hours is not None and state.bindings.ql.available:
                    contact = replace(
                        contact,
                        ql_at=lead.submitted_at + timedelta(hours=patch.ql_after_touch_hours),
                    )
                state.contacts[contact_id] = contact
                if patch.meeting_after_touch_hours is None:
                    continue
                created = lead.submitted_at + timedelta(hours=patch.meeting_after_touch_hours)
                for meeting_id in state.associations.get(("contact", contact_id, "meeting"), ()):
                    meeting = state.meetings.get(meeting_id)
                    if meeting is not None:
                        state.meetings[meeting_id] = replace(
                            meeting,
                            created_at=created,
                            start_at=created + _ONE_DAY,
                            meeting_timestamp=created + _ONE_DAY,
                        )

    # -- stored decisions, discrepancies and feedback --------------------------------------

    def read_observations(self) -> dict[tuple[str, int], list[Observation]]:
        """The snapshots ``load_context`` reads: keywords only at the window's two ends.

        Campaign, budget and conversion-action observations bracket the window (the replay
        span already is ``load_context``'s range); a keyword keeps only its first observation
        at or after the window's start and its last at or before its end in Google's native
        day, the two Q-G7 quality scores K3's snapshot fallback compares (AAP 0.5.4.3).
        """
        zone = self.sources.google_timezone or "UTC"
        lower, _ = AnalysisWindow.day_bounds(self.window.start, zone)
        _, upper = AnalysisWindow.day_bounds(self.window.end, zone)
        read: dict[tuple[str, int], list[Observation]] = {}
        for key, rows in self.observations.items():
            if key[0] != "keyword":
                read[key] = rows
                continue
            inside = [row for row in rows if lower <= row.captured_at <= upper]
            if inside:
                read[key] = [inside[0]] if len(inside) == 1 else [inside[0], inside[-1]]
        return read

    def read_latest_observations(self) -> dict[tuple[str, int], Observation]:
        """Each campaign's and budget's newest snapshot at or before the build instant.

        ``load_context`` reads these independently of the window, as the current state budget
        proposals are sized from. The replay writes every snapshot this converter models
        (cut at the build instant) in the order the sync jobs would insert them, so each
        entity's newest replayed observation stands for its newest stored one. Among
        snapshots sharing the newest ``captured_at``, the last one replayed wins: the row
        ``load_context``'s ``ORDER BY captured_at DESC, id DESC LIMIT 1`` probe returns (ids
        ascend in insertion order), and the same row the window series ends on at that
        instant, so the current state never depends on which of the two a rule reads.
        """
        latest: dict[tuple[str, int], Observation] = {}
        for key, rows in self.observations.items():
            if key[0] not in ("campaign", "budget"):
                continue
            best: Observation | None = None
            for row in rows:
                if row.captured_at > self.now:
                    continue
                if best is None or row.captured_at >= best.captured_at:
                    best = row
            if best is not None:
                latest[key] = best
        return latest

    def _inferred_shape(self, key: SubjectKey) -> DecisionShape:
        """The shape of a subject with no decision: ``budget``, ``shared_member`` or own."""
        subject_type, subject_id = key
        if subject_type == "budget":
            return "budget"
        campaign = self.campaigns.get(subject_id)
        if campaign is not None and campaign.budget_id is not None:
            budget = self.budgets.get(campaign.budget_id)
            if budget is not None and budget.explicitly_shared:
                return "shared_member"
        return "own_budget"

    def _subject_name(self, key: SubjectKey) -> str:
        subject_type, subject_id = key
        if subject_type == "campaign":
            campaign = self.campaigns.get(subject_id)
            return f"Campaign #{subject_id}" if campaign is None else campaign.name
        budget = self.budgets.get(subject_id)
        return f"Budget #{subject_id}" if budget is None else budget.name

    def discrepancies(self) -> tuple[DiscrepancyInfo, ...]:
        """Unresolved discrepancies: the history's, then the spec's (which replace a match).

        Each carries its subject's name and decision shape as ``load_context`` reads them:
        the shape of the subject's latest decision, or the inferred shape of a subject that
        has none (an ``untracked`` one).
        """
        found: dict[tuple[SubjectKey, str], DiscrepancyInfo] = {}
        ordered = sorted(self.open.values(), key=lambda item: (item.detected_at, item.subject))
        for number, item in enumerate(ordered, start=1):
            decision = self.decision_at(item.subject, self.now)
            found[(item.subject, item.kind)] = DiscrepancyInfo(
                id=number,
                subject_type=item.subject[0],
                subject_id=item.subject[1],
                subject_name=self._subject_name(item.subject),
                source=item.source,
                shape=self._inferred_shape(item.subject) if decision is None else decision.shape,
                kind=item.kind,
                decided_value=item.decided_value,
                platform_value=item.platform_value,
                detected_at=item.detected_at,
                state=item.state,
            )
        for position, extra in enumerate(self.spec.discrepancies):
            if position in self.consumed:
                continue
            info = (
                extra
                if isinstance(extra, DiscrepancyInfo)
                else _discrepancy_from_spec(
                    self.wspec, self.index, extra, 500 + position, self.anchor
                )
            )
            found[(info.key, info.kind)] = info
        return tuple(sorted(found.values(), key=lambda item: (item.detected_at, item.id)))

    def decisions(self) -> dict[SubjectKey, DecisionInfo]:
        """The current (latest) decision per subject at the build instant."""
        current: dict[SubjectKey, DecisionInfo] = {}
        for key in sorted(self.history):
            decision = self.decision_at(key, self.now)
            if decision is not None:
                current[key] = decision
        return current

    def feedback(self) -> tuple[FeedbackInfo, ...]:
        """Active feedback (``until`` after the build instant), keyed as items key it."""
        rows: list[FeedbackInfo] = []
        for item in self.spec.feedback:
            if isinstance(item, FeedbackInfo):
                info = item
            else:
                info = FeedbackInfo(
                    rule_id=item.rule_id,
                    entity_type=item.entity_type,
                    entity_key=self._feedback_key(item),
                    kind=item.kind,
                    until=self.anchor_midnight + timedelta(hours=item.until_offset_hours),
                )
            if info.active_at(self.now):
                rows.append(info)
        return tuple(rows)

    def _feedback_key(self, item: FeedbackSpec) -> str:
        """The ``{source}:{type}:{external id}`` key of a feedback spec's entity.

        Keywords and search terms are keyed as the rules key their items: a keyword by its ad
        group and criterion id (:func:`keyword_external_id`, a criterion id being unique only
        within its ad group), a search term by :func:`search_term_external_id`. Every other
        indexed type takes its platform id (:meth:`EntityIndex.entity_key`).

        Raises:
            KeyError: When the world has no such indexed entity.
        """
        if item.entity_type == "keyword":
            keyword_criterion = self.index.external_id(item.source, "keyword", item.key)
            google = self.wspec.google
            group_key = next(
                spec.ad_group_key
                for spec in (() if google is None else google.keywords)
                if spec.key == item.key
            )
            external = keyword_external_id(
                self.index.external_id("google", "ad_group", group_key), keyword_criterion
            )
            return f"{item.source}:keyword:{external}"
        if item.entity_type in _INDEXED_FEEDBACK_TYPES:
            return self.index.entity_key(item.source, item.entity_type, item.key)
        if item.entity_type == "search_term":
            group_key, _, term = item.key.partition("|")
            criterion: str | None = None
            google = self.wspec.google
            for spec in () if google is None else google.search_terms:
                if spec.ad_group_key == group_key and spec.term == term:
                    keyword = spec.matched_keyword_key
                    criterion = (
                        None
                        if keyword is None
                        else self.index.external_id("google", "keyword", keyword)
                    )
            external = search_term_external_id(
                self.index.external_id("google", "ad_group", group_key), criterion, term
            )
            return f"{item.source}:search_term:{external}"
        return f"{item.source}:{item.entity_type}:{item.key}"

    # -- assembly ----------------------------------------------------------------------------

    def build(self) -> AnalysisContext:
        """Run every step and assemble the context."""
        self.replay()
        self.entities()
        self.facts()
        self.segments()
        self.leads()
        self.crm()
        window_key = (self.window.start, self.window.end)
        return AnalysisContext(
            window=self.window,
            thresholds=self.thresholds,
            now=self.now,
            run_kind=self.spec.kind,
            datasets=MappingProxyType(dict(self.datasets)),
            linkedin_crm=self.linkedin_crm,
            google_crm=self.google_crm,
            freshness=MappingProxyType(self.freshness),
            accounts=MappingProxyType(self.accounts),
            campaign_groups=MappingProxyType(self.groups),
            campaigns=MappingProxyType(self.campaigns),
            budgets=MappingProxyType(self.budgets),
            ad_groups=MappingProxyType(self.ad_groups),
            keywords=MappingProxyType(self.keywords),
            creatives=MappingProxyType(self.creatives),
            conversion_actions=MappingProxyType(self.actions),
            campaign_days=MappingProxyType(self.campaign_days),
            creative_totals=MappingProxyType(self.creative_totals),
            keyword_days=MappingProxyType(
                {key: tuple(rows) for key, rows in sorted(self.keyword_days.items())}
            ),
            search_terms=tuple(self.search_terms),
            k1_baseline=self.k1_baseline,
            segments=tuple(
                row for row in self.segment_rows if (row.window_start, row.window_end) == window_key
            ),
            segment_windows=self.segment_windows,
            observations=MappingProxyType(
                {key: tuple(rows) for key, rows in sorted(self.read_observations().items())}
            ),
            latest_observations=MappingProxyType(
                dict(sorted(self.read_latest_observations().items()))
            ),
            leads=tuple(self.lead_rows),
            discrepancies=self.discrepancies(),
            decisions=MappingProxyType(self.decisions()),
            feedback=self.feedback(),
        )


@dataclass(slots=True)
class _CrmState:
    """The mutable rows of one CRM dataset while the converter builds it (internal)."""

    bindings: CrmBindings
    contacts: dict[str, ContactInfo] = field(default_factory=dict)
    meetings: dict[str, MeetingInfo] = field(default_factory=dict)
    deals: dict[str, DealInfo] = field(default_factory=dict)
    associations: dict[tuple[str, str, str], tuple[str, ...]] = field(default_factory=dict)
    fetches: dict[tuple[str, str, str], AssociationFetchInfo] = field(default_factory=dict)


def _as_instant(value: object) -> datetime | None:
    """A HubSpot date or datetime property value as a UTC instant (dates at midnight UTC)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, date):
        return _midnight(value)
    return None


def _no_associations(fault: Fault) -> bool:
    """Whether a 207 error is HubSpot's "no associations" answer, a genuine zero."""
    return (
        fault.category == "OBJECT_NOT_FOUND"
        and fault.sub_category == "crm.associations.NO_ASSOCIATIONS_FOUND"
    )


def _meeting_info(record: MeetingRecord) -> MeetingInfo:
    return MeetingInfo(
        hs_id=record.id,
        created_at=record.created_at,
        start_at=record.start_at,
        meeting_timestamp=record.timestamp,
        outcome=record.outcome,
        meeting_source=record.source,
    )


def _deal_info(record: DealRecord) -> DealInfo:
    return DealInfo(
        hs_id=record.id,
        created_at=record.created_at,
        stage=record.stage,
        pipeline=record.pipeline,
        amount=record.amount,
    )


def context_from_world(
    world: SimulatedWorld,
    spec: ContextSpec,
    thresholds: Thresholds,
    *,
    now: datetime | None = None,
) -> AnalysisContext:
    """Convert a world into the context ``load_context`` would read after a full sync.

    A pure stand-in for the database round trip (AAP 0.5.1 Simulation; parent brief): the
    world's state is converted exactly as the sync jobs would store it and
    ``analysis.metrics.load_context`` would read it back, on the active primary datasets with
    synthetic ids (the companion CRM dataset while LinkedIn is simulated).

    Args:
        world: The seeded world; its bulk filler is left out of the context.
        spec: What the database adds to the world.
        thresholds: The configuration the run uses (:meth:`Scenario.thresholds`).
        now: The run instant; the world's clock when ``None``.

    Returns:
        The analysis context.

    Raises:
        ValueError: When ``now`` is naive, the spec names an entity the world lacks, or a
            recorded decision that a later sync compares breaks its shape's column rules
            (which the ``decision`` table's check constraint enforces).
        InvalidWindow: When the spec's window is not a valid window at ``now``.
    """
    instant = world.now() if now is None else now
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"context_from_world needs an aware instant, found {instant!r}")
    try:
        return _Converter(world, spec, thresholds, instant).build()
    except KeyError as exc:
        raise ValueError(str(exc.args[0]) if exc.args else str(exc)) from exc


# ---------------------------------------------------------------------------
# Catalogue building blocks
# ---------------------------------------------------------------------------

_WINDOW: Final[tuple[int, ...]] = tuple(range(-28, 0))
"""Day offsets of the default 28-day window at :data:`FIXED_NOW` (2026-09-04 .. 2026-10-01)."""

_WEEKENDS: Final[frozenset[int]] = frozenset(
    offset for offset in _WINDOW if (FIXED_ANCHOR + timedelta(days=offset)).weekday() >= 5
)
"""Weekend offsets of the default window: -27, -26, -20, -19, -13, -12, -6 and -5."""

_NA_ACCOUNT: Final = 990_000_001
_EU_ACCOUNT: Final = 990_000_002
_NO_ASSETS: Final[tuple[ContentAsset, ...]] = ()
_HOURS_PER_DAY: Final = 24

# Leaf types of :func:`input_differences`: one delivery profile or one day's metrics is one
# input however many days it spans.
_LEAF_TYPES: Final = (ExplicitDays, DailyProfile, DayMetrics, QualityScoreProfile)


def _offsets(start: int, end: int) -> tuple[int, ...]:
    """The day offsets ``start`` .. ``end``, both inclusive."""
    return tuple(range(start, end + 1))


def _day_of(offset: int) -> str:
    """The ISO date ``offset`` days from :data:`FIXED_ANCHOR`."""
    return (FIXED_ANCHOR + timedelta(days=offset)).isoformat()


def _split_int(total: int, count: int) -> list[int]:
    """Split ``total`` over ``count`` days evenly, the remainder on the last days."""
    base, rest = divmod(total, count)
    return [base + (1 if index >= count - rest else 0) for index in range(count)]


def _split_decimal(total: Decimal, count: int) -> list[Decimal]:
    """Split ``total`` over ``count`` days: whole units split like integers, else 6 places.

    Whole totals keep whole daily values (so a whole conversion total stays an unweighted
    count), the remainder on the last days; other totals take equal 6-place shares with the
    rounding residue on the last day, so the days always sum to ``total`` exactly.
    """
    if total == total.to_integral_value():
        return [Decimal(part) for part in _split_int(int(total), count)]
    share = (total / count).quantize(Decimal("0.000001"))
    parts = [share] * count
    parts[-1] = total - share * (count - 1)
    return parts


def _split_money(total: Decimal, count: int) -> list[Decimal]:
    """Split an amount over ``count`` days in cents, the residue on the last day."""
    share = (total / count).quantize(_CENT, rounding="ROUND_DOWN")
    parts = [share] * count
    parts[-1] = total - share * (count - 1)
    return parts


def _spread(
    offsets: Sequence[int],
    *,
    cost: str | Decimal = "0",
    impressions: int = 0,
    clicks: int = 0,
    conversions: str | Decimal = "0",
    form_opens: int = 0,
    leads: int = 0,
    website_conversions: int = 0,
) -> ExplicitDays:
    """Exact window totals spread evenly over ``offsets`` (integer remainders on the last days).

    Because every integer total puts its remainder on the same trailing days, a day never has
    more clicks than impressions, or more leads than form opens, when the totals do not.
    """
    count = len(offsets)
    costs = _split_money(Decimal(cost), count)
    totals = {
        "impressions": _split_int(impressions, count),
        "clicks": _split_int(clicks, count),
        "form_opens": _split_int(form_opens, count),
        "leads": _split_int(leads, count),
        "website_conversions": _split_int(website_conversions, count),
    }
    values = _split_decimal(Decimal(conversions), count)
    days: dict[int, DayMetrics] = {}
    for index, offset in enumerate(offsets):
        days[offset] = DayMetrics(
            cost=costs[index],
            impressions=totals["impressions"][index],
            clicks=totals["clicks"][index],
            conversions=values[index],
            form_opens=totals["form_opens"][index],
            leads=totals["leads"][index],
            website_conversions=totals["website_conversions"][index],
        )
    return ExplicitDays(days)


def _daily(offsets: Iterable[int], metrics: DayMetrics) -> ExplicitDays:
    """The same day's ``metrics`` on every offset."""
    return ExplicitDays(dict.fromkeys(offsets, metrics))


def _combine(*profiles: ExplicitDays) -> ExplicitDays:
    """Profiles over disjoint offsets merged into one.

    Raises:
        ValueError: When two profiles share an offset.
    """
    days: dict[int, DayMetrics] = {}
    for profile in profiles:
        for offset, metrics in profile.days.items():
            if offset in days:
                raise ValueError(f"profiles overlap on day offset {offset}")
            days[offset] = metrics
    return ExplicitDays(days)


def _metrics(
    cost: str = "0",
    impressions: int = 0,
    clicks: int = 0,
    conversions: str = "0",
    **extra: Any,
) -> DayMetrics:
    """One day's metrics from string amounts."""
    return DayMetrics(
        cost=Decimal(cost),
        impressions=impressions,
        clicks=clicks,
        conversions=Decimal(conversions),
        **extra,
    )


def _world(
    name: str,
    *,
    google: GoogleSpec | None = None,
    linkedin: LinkedInSpec | None = None,
    hubspot: HubSpotSpec | None = None,
    faults: Iterable[Fault] = (),
    changes: Iterable[PlatformChange] = (),
) -> WorldSpec:
    """A world anchored at :data:`FIXED_ANCHOR` (every non-default scenario's time)."""
    return WorldSpec(
        name=name,
        anchor=FIXED_ANCHOR,
        google=google,
        linkedin=linkedin,
        hubspot=hubspot,
        faults=tuple(faults),
        platform_changes=tuple(changes),
    )


def input_differences(first: Scenario, second: Scenario) -> tuple[str, ...]:
    """The inputs in which two scenarios differ, as dotted paths into spec and context.

    A twin pair must differ in exactly one input (AAP 0.11.2: "with a single input moved
    across its threshold"). An input is a leaf field, one delivery profile (however many days
    it spans), one day's metrics, or a whole tuple whose length differs (an entity, fault or
    platform change added or removed). The world's ``name`` is ignored.

    Example:
        >>> input_differences(SCENARIOS["k1.gate.fires"], SCENARIOS["k1.gate.does_not_fire"])
        ('spec.google.search_terms[1].profile',)
    """
    found: list[str] = []
    _diff("spec", first.spec, second.spec, found)
    _diff("context", first.context, second.context, found)
    return tuple(path for path in found if path != "spec.name")


def _diff(path: str, left: object, right: object, found: list[str]) -> None:
    """Append every differing input below ``path`` to ``found``."""
    if left is right:
        return
    if isinstance(left, _LEAF_TYPES) or type(left) is not type(right):
        if left != right:
            found.append(path)
        return
    if hasattr(left, "__dataclass_fields__"):
        for name in left.__dataclass_fields__:
            _diff(f"{path}.{name}", getattr(left, name), getattr(right, name), found)
        return
    if isinstance(left, tuple) and isinstance(right, tuple) and not hasattr(left, "_fields"):
        if len(left) != len(right):
            found.append(path)
            return
        for position, (one, two) in enumerate(zip(left, right, strict=True)):
            _diff(f"{path}[{position}]", one, two, found)
        return
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        for key in sorted({*left, *right}, key=str):
            if key not in left or key not in right:
                found.append(f"{path}[{key!r}]")
            else:
                _diff(f"{path}[{key!r}]", left[key], right[key], found)
        return
    if left != right:
        found.append(path)


def _case(
    name: str,
    module: str,
    guarantee: str,
    spec: WorldSpec,
    expect: Expectation,
    context: ContextSpec | None = None,
) -> Scenario:
    """A single-case scenario (``variant="case"``) named ``"{name}.case"``."""
    return Scenario(
        name=f"{name}.case",
        module=module,
        guarantee=guarantee,
        variant="case",
        pair=None,
        spec=spec,
        context=context if context is not None else ContextSpec(content_assets=_NO_ASSETS),
        expect=expect,
    )


def _pair(
    name: str,
    module: str,
    guarantee: str,
    *,
    fires: tuple[WorldSpec, Expectation],
    does_not_fire: tuple[WorldSpec, Expectation],
    context: ContextSpec | None = None,
    dnf_context: ContextSpec | None = None,
) -> tuple[Scenario, Scenario]:
    """A ``fires`` / ``does_not_fire`` twin pair named ``"{name}.fires"`` and its twin.

    The twin moves one input across its threshold: in the world (``does_not_fire[0]``) or,
    with ``dnf_context``, in the context.
    """
    base = context if context is not None else ContextSpec(content_assets=_NO_ASSETS)
    first = f"{name}.fires"
    second = f"{name}.does_not_fire"
    return (
        Scenario(first, module, guarantee, "fires", second, fires[0], base, fires[1]),
        Scenario(
            second,
            module,
            guarantee,
            "does_not_fire",
            first,
            does_not_fire[0],
            dnf_context if dnf_context is not None else base,
            does_not_fire[1],
        ),
    )


# ---------------------------------------------------------------------------
# Keywords to adjust (AAP 0.5.4.3; acceptance/test_keywords_rules.py)
# ---------------------------------------------------------------------------

_LAST_CLICK: Final = GoogleConversionActionSpec("purchase", "Purchase")
_DATA_DRIVEN: Final = GoogleConversionActionSpec(
    "purchase", "Purchase", attribution_model="GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"
)
_MANY_PER_CLICK: Final = GoogleConversionActionSpec(
    "purchase", "Purchase", counting_type="MANY_PER_CLICK"
)
_SEARCH_BUDGET: Final = GoogleBudgetSpec("search-budget", Decimal("100000.00"), name="Search")
_SEARCH: Final = GoogleCampaignSpec("search", "Search - CRM", "search-budget")
_CORE: Final = GoogleAdGroupSpec("core", "search", "Core CRM")
_KW_CRM: Final = GoogleKeywordSpec("kw-crm", "core", "crm software", match_type="PHRASE")
_TARGET_TERM: Final = "free crm templates"

# K1 gate: 5,600 base clicks and 280 conversions in the window, so with T target clicks
# r = 280 / (5,600 + T) and n* = ceil(3 / r) = 61 for T = 60 and T = 61.
_K1_BASE: Final = _spread(_WINDOW, cost="5600", impressions=56_000, clicks=5_600, conversions="280")


def _target(clicks: int, *, cost: str = "61", conversions: str = "0") -> ExplicitDays:
    """The K1 target term's window delivery."""
    return _spread(
        _WINDOW, cost=cost, impressions=clicks * 10, clicks=clicks, conversions=conversions
    )


def _keywords_world(
    name: str,
    *,
    base: ExplicitDays = _K1_BASE,
    target: ExplicitDays | None = None,
    target_status: GoogleSearchTermStatus = "NONE",
    target_keyword: str = "kw-crm",
    extra_terms: Sequence[GoogleSearchTermSpec] = (),
    keywords: Sequence[GoogleKeywordSpec] = (_KW_CRM,),
    action: GoogleConversionActionSpec = _LAST_CLICK,
    ad_group: GoogleAdGroupSpec = _CORE,
) -> WorldSpec:
    """One search campaign: a converting base term, an optional K1 target and extra terms."""
    terms = [GoogleSearchTermSpec("crm software", "core", "kw-crm", status="ADDED", profile=base)]
    if target is not None:
        terms.append(
            GoogleSearchTermSpec(
                _TARGET_TERM, "core", target_keyword, status=target_status, profile=target
            )
        )
    terms.extend(extra_terms)
    google = GoogleSpec(
        budgets=(_SEARCH_BUDGET,),
        campaigns=(_SEARCH,),
        ad_groups=(ad_group,),
        keywords=tuple(keywords),
        search_terms=tuple(terms),
        conversion_actions=(action,),
    )
    return _world(name, google=google)


def _k1_fires(n_star: int, clicks: int, spend: str) -> Expectation:
    return Expectation(
        K1, True, {"n_star": n_star, "clicks": clicks, "wasted_spend": spend, "conversions": 0}
    )


def _k1_withheld(n_star: int, clicks: int) -> Expectation:
    return Expectation(
        K1,
        False,
        {"n_star": n_star, "observed": clicks, "required": n_star},
        withheld_reason=f"clicks {clicks} of {n_star} required",
    )


def _keyword_scenarios() -> Iterator[Scenario]:
    """K1, K2, K3 and search-term coverage."""
    module = MODULE_KEYWORDS
    yield from _pair(
        "k1.gate",
        module,
        "K1 gate: fires at n* clicks / withheld at n*-1 with clicks observed and required",
        fires=(_keywords_world("k1.gate", target=_target(61)), _k1_fires(61, 61, "61.00")),
        does_not_fire=(_keywords_world("k1.gate", target=_target(60)), _k1_withheld(61, 60)),
    )
    for case, clicks, conversions, target, n_star, offsets in (
        ("k1.low_rate_1pct", 29_700, "300", 300, 300, _offsets(-90, -1)),
        ("k1.low_rate_01pct", 27_000, "30", 3_000, 3_000, _offsets(-90, -1)),
    ):
        base = _spread(
            offsets,
            cost=str(clicks),
            impressions=clicks * 10,
            clicks=clicks,
            conversions=conversions,
        )
        yield from _pair(
            case,
            module,
            f"K1 low baseline rate: n* = {n_star:,} with no upper cap",
            fires=(
                _keywords_world(case, base=base, target=_target(target, cost=str(target))),
                _k1_fires(n_star, target, f"{target}.00"),
            ),
            does_not_fire=(
                _keywords_world(case, base=base, target=_target(target - 1, cost=str(target))),
                _k1_withheld(n_star, target - 1),
            ),
        )
    fractional = _spread(
        _offsets(-50, -1), cost="9936", impressions=99_360, clicks=9_936, conversions="470"
    )
    yield from _pair(
        "k1.fractional_baseline",
        module,
        "K1 non-binary baseline: a fractional data-driven baseline r = 0.047 gives n* = 64",
        fires=(
            _keywords_world(
                "k1.fractional_baseline",
                base=fractional,
                target=_target(64, cost="64"),
                action=_DATA_DRIVEN,
            ),
            _k1_fires(64, 64, "64.00"),
        ),
        does_not_fire=(
            _keywords_world(
                "k1.fractional_baseline",
                base=fractional,
                target=_target(63, cost="64"),
                action=_DATA_DRIVEN,
            ),
            _k1_withheld(64, 63),
        ),
    )
    for case, conversions, rate in (
        ("k1.many_per_click", "140", "1.4"),
        ("k1.rate_one", "100", "1"),
    ):
        base = _spread(_WINDOW, cost="180", impressions=900, clicks=90, conversions=conversions)
        yield from _pair(
            case,
            module,
            f"K1 non-binary baseline: r = {rate} gives the 10-click floor, no probability claimed",
            fires=(
                _keywords_world(
                    case, base=base, target=_target(10, cost="20"), action=_MANY_PER_CLICK
                ),
                Expectation(
                    K1,
                    True,
                    {
                        "n_star": 10,
                        "baseline_rate": rate,
                        "clicks": 10,
                        "claims_probability": False,
                    },
                ),
            ),
            does_not_fire=(
                _keywords_world(
                    case, base=base, target=_target(9, cost="20"), action=_MANY_PER_CLICK
                ),
                _k1_withheld(10, 9),
            ),
        )
    few = _spread(_WINDOW, cost="20", impressions=200, clicks=20, conversions="10")
    fewer = _spread(_WINDOW, cost="20", impressions=200, clicks=20, conversions="9")
    yield from _pair(
        "k1.no_baseline",
        module,
        "K1 unusable baseline: 10 baseline conversions evaluate K1 / 9 withhold it account-wide",
        fires=(
            _keywords_world("k1.no_baseline", base=few, target=_target(10, cost="10")),
            Expectation(K1, True, {"n_star": 10, "baseline_conversions": 10}),
        ),
        does_not_fire=(
            _keywords_world("k1.no_baseline", base=fewer, target=_target(10, cost="10")),
            Expectation(
                K1,
                False,
                {"baseline_conversions": 9, "required": 10},
                finding=FINDING_K1_NO_BASELINE,
                withheld_reason="baseline conversions 9 of 10 required",
            ),
        ),
    )
    no_conversions = _spread(_WINDOW, cost="5600", impressions=56_000, clicks=5_600)
    yield _case(
        "k1.zero_rate",
        module,
        "K1 unusable baseline: r = 0 withholds K1 account-wide with k1_no_baseline",
        _keywords_world("k1.zero_rate", base=no_conversions, target=_target(61)),
        Expectation(K1, False, {"baseline_rate": "0"}, finding=FINDING_K1_NO_BASELINE),
    )
    idle = ExplicitDays({offset: _metrics(impressions=40) for offset in _WINDOW})
    yield _case(
        "k1.rate_unavailable",
        module,
        "K1 unusable baseline: no clicks in the baseline (r unavailable) withholds K1",
        _keywords_world("k1.rate_unavailable", base=idle),
        Expectation(K1, False, {"baseline_rate": None}, finding=FINDING_K1_NO_BASELINE),
    )
    yield from _pair(
        "k1.fractional_conversion",
        module,
        "K1 non-firing: a 0.3 fractional conversion counts as converting",
        fires=(
            _keywords_world("k1.fractional_conversion", target=_target(61)),
            _k1_fires(61, 61, "61.00"),
        ),
        does_not_fire=(
            _keywords_world(
                "k1.fractional_conversion",
                target=_combine(
                    _spread(_offsets(-28, -2), cost="59", impressions=590, clicks=59),
                    _daily((-1,), _metrics("2", 20, 2, "0.3")),
                ),
            ),
            Expectation(K1, False, {"conversions": "0.3"}),
        ),
    )
    yield from _pair(
        "k1.spend_below_cpa",
        module,
        "K1 non-firing: spend below one account cost per conversion",
        fires=(
            _keywords_world("k1.spend_below_cpa", target=_target(61)),
            _k1_fires(61, 61, "61.00"),
        ),
        does_not_fire=(
            _keywords_world("k1.spend_below_cpa", target=_target(61, cost="15")),
            Expectation(K1, False, {"spend": "15.00", "account_cpa": "20.05"}),
        ),
    )
    skipped: tuple[tuple[str, GoogleSearchTermStatus], ...] = (
        ("k1.excluded_term", "EXCLUDED"),
        ("k1.added_excluded_term", "ADDED_EXCLUDED"),
    )
    for case, status in skipped:
        yield from _pair(
            case,
            module,
            f"K1 non-firing: an {status} term is skipped",
            fires=(_keywords_world(case, target=_target(61)), _k1_fires(61, 61, "61.00")),
            does_not_fire=(
                _keywords_world(case, target=_target(61), target_status=status),
                Expectation(K1, False, {"term_status": status}),
            ),
        )
    templates = GoogleKeywordSpec("kw-templates", "core", _TARGET_TERM, match_type="EXACT")
    yield _case(
        "k1.added_term",
        module,
        "K1 ADDED term: the negative-term candidate names the matched criterion, never a pause",
        _keywords_world(
            "k1.added_term",
            target=_target(61),
            target_status="ADDED",
            target_keyword="kw-templates",
            keywords=(_KW_CRM, templates),
        ),
        Expectation(
            K1,
            True,
            {
                "action": "Add as exact-match negative",
                "matched_criterion": str(GOOGLE_CRITERION_IDS.at(1)),
                "matched_keyword": _TARGET_TERM,
                "pauses_keyword": False,
            },
        ),
    )
    yield from _k2_scenarios(module)
    yield from _k3_scenarios(module)
    coverage_world = _keywords_world("keywords.coverage", target=_target(61))
    google = cast(GoogleSpec, coverage_world.google)

    def with_residual(cost: str) -> WorldSpec:
        residual = _spread(_WINDOW, cost=cost, impressions=2_800)
        keyword = replace(_KW_CRM, profile=residual)
        return coverage_world.with_(google=replace(google, keywords=(keyword,)))

    yield from _pair(
        "keywords.coverage",
        module,
        "Coverage: search-term spend below 80% of keyword spend flags items / at 80% or above not",
        fires=(
            with_residual("1500"),
            Expectation(
                K1,
                True,
                {"search_term_spend": "5661.00", "keyword_spend": "7161.00"},
                finding=FINDING_SEARCH_TERM_COVERAGE,
                flags=(FLAG_SEARCH_TERM_COVERAGE,),
            ),
        ),
        does_not_fire=(
            with_residual("1400"),
            Expectation(
                K1,
                True,
                {"search_term_spend": "5661.00", "keyword_spend": "7061.00"},
                finding=FINDING_SEARCH_TERM_COVERAGE,
            ),
        ),
    )


def _k2_scenarios(module: str) -> Iterator[Scenario]:
    """K2 expansion candidates and adjacent terms."""

    def candidate(conversions: str, cost: str) -> GoogleSearchTermSpec:
        profile = _spread(_WINDOW, cost=cost, impressions=300, clicks=30, conversions=conversions)
        return GoogleSearchTermSpec("crm software pricing", "core", "kw-crm", profile=profile)

    def world(name: str, term: GoogleSearchTermSpec, *extra: GoogleSearchTermSpec) -> WorldSpec:
        return _keywords_world(name, extra_terms=(term, *extra))

    fires = Expectation(K2, True, {"conversions": 3, "cost_per_conversion": "10.00"})
    yield from _pair(
        "k2.min_conversions",
        module,
        "K2: 3 conversions fire / 2 do not",
        fires=(world("k2.min_conversions", candidate("3", "30")), fires),
        does_not_fire=(
            world("k2.min_conversions", candidate("2", "30")),
            Expectation(
                K2, False, {"conversions": 2}, withheld_reason="conversions 2 of 3 required"
            ),
        ),
    )
    yield from _pair(
        "k2.cpa_above_campaign",
        module,
        "K2: cost/conversion at or below the campaign's fires / above it does not",
        fires=(world("k2.cpa_above_campaign", candidate("3", "30")), fires),
        does_not_fire=(
            world("k2.cpa_above_campaign", candidate("3", "90")),
            Expectation(K2, False, {"cost_per_conversion": "30.00"}),
        ),
    )
    neighbours: tuple[tuple[str, int, GoogleSearchTermStatus], ...] = (
        ("crm pricing", 7, "NONE"),
        ("software pricing", 6, "NONE"),
        ("crm software price", 5, "NONE"),
        ("crm pricing tiers", 4, "NONE"),
        ("crm software cost", 3, "NONE"),
        ("pricing crm software", 2, "NONE"),
        ("crm software pricing free", 8, "EXCLUDED"),
        ("email marketing tool", 9, "NONE"),
    )
    extra = tuple(
        GoogleSearchTermSpec(
            term,
            "core",
            "kw-crm",
            status=status,
            profile=_spread(
                _offsets(-clicks, -1), cost=str(clicks), impressions=clicks * 10, clicks=clicks
            ),
        )
        for term, clicks, status in neighbours
    )
    yield _case(
        "k2.adjacent_terms",
        module,
        "K2 adjacent terms: capped at 5 and excluding existing keywords and excluded terms",
        world("k2.adjacent_terms", candidate("3", "30"), *extra),
        Expectation(
            K2,
            True,
            {
                "adjacent_terms": (
                    "crm pricing",
                    "software pricing",
                    "crm software price",
                    "crm pricing tiers",
                    "crm software cost",
                ),
                "excluded": ("crm software", "crm software pricing free", "pricing crm software"),
            },
        ),
    )


def _k3_world(name: str, profile: ExplicitDays | None = None, **keyword: Any) -> WorldSpec:
    """One keyword with its own delivery (no search terms), for K3."""
    review = GoogleKeywordSpec("kw-review", "core", "crm demo", profile=profile, **keyword)
    google = GoogleSpec(
        budgets=(_SEARCH_BUDGET,),
        campaigns=(_SEARCH,),
        ad_groups=(_CORE,),
        keywords=(review,),
        conversion_actions=(_LAST_CLICK,),
    )
    return _world(name, google=google)


def _qs_days(scores: Mapping[int, int | None], *, cost: str = "5.00") -> ExplicitDays:
    """K3 keyword delivery on every window day: 5 clicks, 1 conversion, the given QS."""
    return ExplicitDays(
        {
            offset: _metrics(cost, 50, 5, "1", historical_quality_score=scores.get(offset, 7))
            for offset in _WINDOW
        }
    )


def _k3_scenarios(module: str) -> Iterator[Scenario]:
    """K3 quality-score drops and CPC rises."""
    drop = dict.fromkeys(_offsets(-14, -1), 6)
    half_drop = {offset: 7 if offset % 2 else 6 for offset in _offsets(-7, -2)} | {-1: None}
    yield from _pair(
        "k3.qs_drop",
        module,
        "K3: a one-point quality-score drop fires / a 0.5-point drop does not",
        fires=(
            _k3_world("k3.qs_drop", _qs_days(drop)),
            Expectation(K3, True, {"qs_first": "7", "qs_last": "6"}),
        ),
        does_not_fire=(
            _k3_world("k3.qs_drop", _qs_days(half_drop)),
            Expectation(K3, False, {"qs_first": "7", "qs_last": "6.5"}),
        ),
    )
    five: dict[int, int | None] = dict.fromkeys(_offsets(-5, -1), 6) | {-7: None, -6: None}
    four: dict[int, int | None] = dict.fromkeys(_offsets(-4, -1), 6)
    four |= {-7: None, -6: None, -5: None}
    yield from _pair(
        "k3.qs_days",
        module,
        "K3: quality scores on 5 days of the last span fire / fewer than 5 QS days do not",
        fires=(
            _k3_world("k3.qs_days", _qs_days(five)),
            Expectation(K3, True, {"qs_days_last": 5}),
        ),
        does_not_fire=(
            _k3_world("k3.qs_days", _qs_days(four)),
            Expectation(K3, False, {"qs_days_last": 4, "required": 5}),
        ),
    )
    absent = _qs_days(dict.fromkeys(_WINDOW))
    yield from _pair(
        "k3.snapshot_fallback",
        module,
        "K3: with daily scores absent the Q-G7 snapshots at window start and end are compared",
        fires=(
            _k3_world(
                "k3.snapshot_fallback",
                absent,
                qs_profile=QualityScoreProfile(7, -10, 6, present_share=Decimal(0)),
            ),
            Expectation(K3, True, {"qs_start": 7, "qs_end": 6, "basis": "snapshots"}),
        ),
        does_not_fire=(
            _k3_world(
                "k3.snapshot_fallback",
                absent,
                qs_profile=QualityScoreProfile(7, present_share=Decimal(0)),
            ),
            Expectation(K3, False, {"qs_start": 7, "qs_end": 7}),
        ),
    )

    def cpc(second_conversions: str) -> ExplicitDays:
        first = {
            o: _metrics("5.00", 50, 5, "1", historical_quality_score=7) for o in _offsets(-28, -15)
        }
        second = {
            o: _metrics("6.00", 50, 5, second_conversions, historical_quality_score=7)
            for o in _offsets(-14, -1)
        }
        return ExplicitDays(first | second)

    yield from _pair(
        "k3.cpc_rise",
        module,
        "K3: CPC up 20% with flat conversions fires / CPC and conversions rising together do not",
        fires=(
            _k3_world("k3.cpc_rise", cpc("1")),
            Expectation(K3, True, {"cpc_ratio": "1.2", "cpa_ratio": "1.2"}),
        ),
        does_not_fire=(
            _k3_world("k3.cpc_rise", cpc("1.2")),
            Expectation(K3, False, {"cpc_ratio": "1.2", "cpa_ratio": "1"}),
        ),
    )


# ---------------------------------------------------------------------------
# LinkedIn and HubSpot building blocks
# ---------------------------------------------------------------------------

_LI_ACCOUNT: Final = LinkedInAccountSpec(_NA_ACCOUNT, "Simulated Ads - North America")
_LI_GROUP: Final = LinkedInCampaignGroupSpec("grp", _NA_ACCOUNT, "Always-on lead gen")
_EMAIL_FORM_ID: Final = 990_000_101
_NO_EMAIL_FORM_ID: Final = 990_000_102


def _questions(*, email: bool, base: int = 1000) -> tuple[QuestionSpec, ...]:
    """A lead form's questions; with ``email`` the work email question is asked."""
    questions = [
        QuestionSpec(base + 1, "firstName", "FIRST_NAME", "First name"),
        QuestionSpec(base + 2, "lastName", "LAST_NAME", "Last name"),
        QuestionSpec(base + 4, "company", "COMPANY_NAME", "Company name"),
    ]
    if email:
        questions.insert(2, QuestionSpec(base + 3, "workEmail", "WORK_EMAIL", "Work email"))
    return tuple(questions)


def _form(
    form_id: int = _EMAIL_FORM_ID,
    name: str = "Report download",
    *versions: FormVersionSpec,
    account: int = _NA_ACCOUNT,
) -> LinkedInLeadFormSpec:
    """A lead form; one version asking the work email unless ``versions`` are given."""
    chosen = versions or (FormVersionSpec(1, _questions(email=True)),)
    return LinkedInLeadFormSpec(form_id, account, name, chosen)


_EMAIL_FORM: Final = _form()
_NO_EMAIL_FORM: Final = LinkedInLeadFormSpec(
    _NO_EMAIL_FORM_ID,
    _NA_ACCOUNT,
    "Webinar sign-up (no email question)",
    (FormVersionSpec(1, _questions(email=False, base=2000)),),
)

_FLAT_FUNNEL: Final = FunnelSpec(
    match_share=Decimal(1),
    late_contact_share=Decimal(0),
    mql_share=Decimal(0),
    ql_share=Decimal(0),
    skipped_stage_share=Decimal(0),
    pre_touch_share=Decimal(0),
    meeting_share=Decimal(0),
    canceled_share=Decimal(0),
    meeting_created_after_start_share=Decimal(0),
    two_contact_meeting_share=Decimal(0),
    deal_share=Decimal(0),
    pre_touch_deal_share=Decimal(0),
    repeat_person_share=Decimal(0),
)
"""Every person gets a contact on the touch and nothing else, so CRM outcomes are exact."""


def _funnel(**shares: str | int | tuple[int, int] | tuple[tuple[str, int], ...]) -> FunnelSpec:
    """:data:`_FLAT_FUNNEL` with the given shares, delay windows or meeting sources replaced.

    Shares are given as ``"0.5"`` or ``1``; ``meeting_sources`` as ``((source, weight), ...)``.
    """
    values: dict[str, Any] = {}
    for name, value in shares.items():
        if name == "meeting_sources" and isinstance(value, tuple):
            values[name] = tuple(
                (str(source), Decimal(weight)) for source, weight in cast(Any, value)
            )
        elif isinstance(value, tuple):
            values[name] = value
        else:
            values[name] = Decimal(value)
    return replace(_FLAT_FUNNEL, **values)


def _li_campaign(
    key: str,
    name: str,
    *,
    daily: str | None = "100.00",
    total: str | None = None,
    objective: str = "LEAD_GENERATION",
    account: int = _NA_ACCOUNT,
    group: str = "grp",
) -> LinkedInCampaignSpec:
    """An active LinkedIn campaign with a daily (or lifetime) budget."""
    return LinkedInCampaignSpec(
        key,
        account,
        group,
        name,
        objective=objective,
        daily_budget=None if daily is None else Decimal(daily),
        total_budget=None if total is None else Decimal(total),
    )


def _li_creative(
    key: str,
    campaign: str,
    profile: ExplicitDays,
    *,
    form: int | None = _EMAIL_FORM_ID,
    headline: str | None = None,
    cta: LinkedInCtaLabel = "DOWNLOAD",
    creative_id: int | None = None,
) -> LinkedInCreativeSpec:
    """A lead-gen creative (``form=None`` makes it a landing-page creative)."""
    return LinkedInCreativeSpec(
        key,
        campaign,
        headline or f"{key} headline",
        f"{key} intro text",
        id=creative_id,
        cta_label=cta,
        lead_form_id=form,
        landing_page_url=None if form is not None else "https://www.sim.invalid/offer",
        profile=profile,
    )


def _linkedin(
    campaigns: Sequence[LinkedInCampaignSpec],
    creatives: Sequence[LinkedInCreativeSpec],
    *,
    forms: Sequence[LinkedInLeadFormSpec] = (_EMAIL_FORM,),
    accounts: Sequence[LinkedInAccountSpec] = (_LI_ACCOUNT,),
    groups: Sequence[LinkedInCampaignGroupSpec] = (_LI_GROUP,),
    segments: Sequence[LinkedInSegmentSpec] = (),
) -> LinkedInSpec:
    """A LinkedIn advertiser with no test leads and every response carrying its creative."""
    return LinkedInSpec(
        accounts=tuple(accounts),
        campaign_groups=tuple(groups),
        campaigns=tuple(campaigns),
        creatives=tuple(creatives),
        lead_forms=tuple(forms),
        segments=tuple(segments),
    )


def _leadgen(
    leads: Mapping[int, int],
    *,
    cost: str = "97.00",
    offsets: Sequence[int] = _WINDOW,
    impressions: int = 2_000,
    clicks: int = 20,
) -> ExplicitDays:
    """Lead-gen delivery: ``cost`` on every offset, ``leads[offset]`` leads on its day."""
    return ExplicitDays(
        {
            offset: DayMetrics(
                cost=Decimal(cost),
                impressions=impressions,
                clicks=clicks,
                form_opens=leads.get(offset, 0) + 10,
                leads=leads.get(offset, 0),
            )
            for offset in offsets
        }
    )


def _lead_days(total: int, offsets: Sequence[int]) -> dict[int, int]:
    """``total`` leads spread over ``offsets`` (remainder on the last days)."""
    return dict(zip(offsets, _split_int(total, len(offsets)), strict=True))


_MATURED: Final[tuple[int, ...]] = _offsets(-28, -16)
"""Window days whose leads are at least ``crm.lead_maturity_days`` (14) before the window end."""

_QUIET_GOOGLE: Final = GoogleSpec(
    budgets=(GoogleBudgetSpec("quiet-budget", Decimal("50.00"), name="Quiet"),),
    campaigns=(
        GoogleCampaignSpec(
            "quiet",
            "Search - Quiet",
            "quiet-budget",
            profile=_daily(_WINDOW, _metrics("10.00", 200, 10, "1")),
        ),
    ),
    conversion_actions=(_LAST_CLICK,),
)
"""A converting Google campaign that sets Google's day boundary (America/New_York)."""


def _pipeline_world(
    name: str,
    leads: Mapping[int, int],
    *,
    forms: Sequence[LinkedInLeadFormSpec] = (_EMAIL_FORM,),
    extra_creatives: Sequence[LinkedInCreativeSpec] = (),
    funnel: FunnelSpec = _FLAT_FUNNEL,
    faults: Iterable[Fault] = (),
    hubspot: HubSpotSpec | None = None,
) -> WorldSpec:
    """A capped LinkedIn lead-gen campaign (97.00 of 100.00 a day) with a flat CRM funnel."""
    campaign = _li_campaign("li-pipeline", "Pipeline Report - Lead gen")
    creative = _li_creative("cr-pipeline", "li-pipeline", _leadgen(leads))
    return _world(
        name,
        linkedin=_linkedin((campaign,), (creative, *extra_creatives), forms=forms),
        hubspot=hubspot if hubspot is not None else HubSpotSpec(funnel=funnel),
        faults=faults,
    )


# ---------------------------------------------------------------------------
# Campaigns to pause or launch (AAP 0.5.4.4; acceptance/test_campaign_rules.py)
# ---------------------------------------------------------------------------

_BRAND_BUDGET: Final = GoogleBudgetSpec("brand-budget", Decimal("100.00"), name="Brand")
_POOL: Final = GoogleBudgetSpec("pool", Decimal("500.00"), explicitly_shared=True, name="Pool A")
_POOL_B: Final = GoogleBudgetSpec(
    "pool-b", Decimal("300.00"), explicitly_shared=True, name="Pool B"
)
_STEADY: Final = _daily(_WINDOW, _metrics("20.00", 400, 20, "2"))
_BRAND: Final = SubjectRef("google", "campaign", "brand")


def _c0_world(
    name: str,
    *,
    changes: Iterable[PlatformChange] = (),
    faults: Iterable[Fault] = (),
    late: Sequence[GoogleCampaignSpec] = (),
    late_budgets: Sequence[GoogleBudgetSpec] = (),
) -> WorldSpec:
    """Google own-budget, shared-budget and LinkedIn campaigns for discrepancy cases."""
    google = GoogleSpec(
        budgets=(_BRAND_BUDGET, _POOL, _POOL_B, *late_budgets),
        campaigns=(
            GoogleCampaignSpec("brand", "Brand - Exact", "brand-budget", profile=_STEADY),
            GoogleCampaignSpec("member-a", "Generic - Member A", "pool", profile=_STEADY),
            GoogleCampaignSpec("member-b", "Generic - Member B", "pool", profile=_STEADY),
            GoogleCampaignSpec("member-c", "Generic - Member C", "pool-b", profile=_STEADY),
            *late,
        ),
        conversion_actions=(_LAST_CLICK,),
    )
    creative = _li_creative("cr-brand", "li-brand", _leadgen({}, cost="20.00"), form=None)
    linkedin = _linkedin(
        (_li_campaign("li-brand", "Brand - LinkedIn", daily="50.00"),), (creative,), forms=()
    )
    return _world(name, google=google, linkedin=linkedin, changes=changes, faults=faults)


def _change(
    key: str, field_name: ChangeField | None, value: str | None, at: int, **extra: Any
) -> PlatformChange:
    """A Google platform change ``at`` hours after the anchor's UTC midnight."""
    amount: str | Decimal | None = value
    if field_name == "amount" and value is not None:
        amount = Decimal(value)
    entity: ChangeEntity = "budget" if field_name == "amount" else "campaign"
    return PlatformChange(
        "google", entity, key, field=field_name, value=amount, at_offset_hours=at, **extra
    )


def _no_open() -> Expectation:
    return Expectation(C0, False, {"open": 0})


def _c0_scenarios(module: str) -> Iterator[Scenario]:
    """C0 discrepancies, their kinds by subject type, removal and the resolution lifecycle."""
    yield from _pair(
        "c0.google_budget_micro",
        module,
        "C0 budget precision: a Google budget differing by 1 micro / identical micros",
        fires=(
            _c0_world(
                "c0.google_budget_micro",
                changes=(_change("brand-budget", "amount", "100.000001", -30),),
            ),
            Expectation(
                C0,
                True,
                {"kind": "budget", "decided_micros": 100_000_000, "platform_micros": 100_000_001},
            ),
        ),
        does_not_fire=(
            _c0_world(
                "c0.google_budget_micro",
                changes=(_change("brand-budget", "amount", "100.00", -30),),
            ),
            _no_open(),
        ),
    )
    yield from _pair(
        "c0.linkedin_minor_unit",
        module,
        "C0 budget precision: a LinkedIn budget differing by one minor unit / identical",
        fires=(
            _c0_world(
                "c0.linkedin_minor_unit",
                changes=(
                    PlatformChange(
                        "linkedin",
                        "campaign",
                        "li-brand",
                        field="daily_budget",
                        value=Decimal("50.01"),
                        at_offset_hours=-30,
                    ),
                ),
            ),
            Expectation(C0, True, {"kind": "budget", "decided": "50.00", "platform": "50.01"}),
        ),
        does_not_fire=(
            _c0_world(
                "c0.linkedin_minor_unit",
                changes=(
                    PlatformChange(
                        "linkedin",
                        "campaign",
                        "li-brand",
                        field="daily_budget",
                        value=Decimal("50.00"),
                        at_offset_hours=-30,
                    ),
                ),
            ),
            _no_open(),
        ),
    )
    yield from _pair(
        "c0.status",
        module,
        "C0 status: a status mismatch raises a status discrepancy / matching status does not",
        fires=(
            _c0_world("c0.status", changes=(_change("brand", "status", "PAUSED", -30),)),
            Expectation(C0, True, {"kind": "status", "decided": "active", "platform": "paused"}),
        ),
        does_not_fire=(
            _c0_world("c0.status", changes=(_change("brand", "status", "ENABLED", -30),)),
            _no_open(),
        ),
    )
    late = GoogleCampaignSpec("late", "Late - Untracked", "brand-budget", profile=_STEADY)
    unseen = _change("late", None, None, -24 * 400, disappear=True, reappear_at_offset_hours=-48)
    yield from _pair(
        "c0.untracked_campaign",
        module,
        "C0 untracked: offers only Adopt platform state and Record intended state, the intended "
        "state complete for the campaign's shape, auto-closing when the platform matches",
        fires=(
            _c0_world("c0.untracked_campaign", late=(late,), changes=(unseen,)),
            Expectation(
                C0,
                True,
                {
                    "kind": "untracked",
                    "subject_type": "campaign",
                    "shape": "own_budget",
                    "resolutions": ("adopt_platform", "record_intended"),
                    "record_intended_fields": ("status", "daily_budget"),
                },
            ),
        ),
        does_not_fire=(_c0_world("c0.untracked_campaign", late=(late,)), _no_open()),
    )
    pool_late = GoogleBudgetSpec(
        "pool-late", Decimal("250.00"), explicitly_shared=True, name="Pool late"
    )
    late_member = GoogleCampaignSpec("late-member", "Late - Member", "pool-late", profile=_STEADY)
    yield _case(
        "c0.untracked_budget",
        module,
        "C0 by subject type: record_intended on an untracked DAILY budget accepts {daily_budget} "
        "and rejects status or total_budget with 422; on a shared member it accepts {status} only",
        _c0_world(
            "c0.untracked_budget",
            late=(late_member,),
            late_budgets=(pool_late,),
            changes=(
                _change(
                    "late-member",
                    None,
                    None,
                    -24 * 400,
                    disappear=True,
                    reappear_at_offset_hours=-48,
                ),
            ),
        ),
        Expectation(
            C0,
            True,
            {
                "kind": "untracked",
                "subject_type": "budget",
                "shape": "budget",
                "resolutions": ("adopt_platform", "record_intended"),
                "record_intended_accepts": ("daily_budget",),
                "record_intended_rejects": ("status", "total_budget"),
                "member_record_intended_accepts": ("status",),
            },
        ),
    )
    yield _case(
        "c0.shared_budget_amount",
        module,
        "C0 by subject type: a shared-budget amount change raises one budget-subject budget "
        "discrepancy and none on its members",
        _c0_world("c0.shared_budget_amount", changes=(_change("pool", "amount", "550.00", -30),)),
        Expectation(
            C0,
            True,
            {
                "kind": "budget",
                "subject_type": "budget",
                "budget_discrepancies": 1,
                "member_discrepancies": 0,
            },
        ),
    )
    yield _case(
        "c0.member_moved",
        module,
        "C0 by subject type: a member moved to another budget raises a member budget "
        "(assignment) discrepancy",
        _c0_world("c0.member_moved", changes=(_change("member-b", "budget_key", "pool-b", -30),)),
        Expectation(
            C0,
            True,
            {
                "kind": "budget",
                "subject_type": "campaign",
                "shape": "shared_member",
                "compares": "budget_id",
            },
        ),
    )
    yield _case(
        "c0.removed",
        module,
        "C0 missing: Q-G2b returns REMOVED, so the campaign is snapshotted as ended",
        _c0_world("c0.removed", changes=(_change("brand", "status", "REMOVED", -30),)),
        Expectation(C0, True, {"kind": "status", "platform": "ended", "raw_status": "REMOVED"}),
    )
    gone = _change("brand", None, None, -30, disappear=True)
    q_g2 = Fault("google", "search", "http_status", status=503, match={"query_id": "Q-G2"})
    q_g2b = Fault("google", "search", "http_status", status=503, match={"query_id": "Q-G2b"})
    yield from _pair(
        "c0.missing",
        module,
        "C0 missing: not returned after a complete scan raises missing / a deferred Q-G2 keyset "
        "page raises none and leaves google.entities incomplete",
        fires=(
            _c0_world("c0.missing", changes=(gone,)),
            Expectation(C0, True, {"kind": "missing", "subject_type": "campaign"}),
        ),
        does_not_fire=(
            _c0_world("c0.missing", changes=(gone,), faults=(q_g2,)),
            Expectation(C0, False, {"open": 0, "google.entities": "incomplete"}),
        ),
    )
    yield _case(
        "c0.missing_qg2b_failed",
        module,
        "C0 missing: a failed Q-G2b chunk raises no missing and leaves google.entities incomplete",
        _c0_world("c0.missing_qg2b_failed", changes=(gone,), faults=(q_g2b,)),
        Expectation(C0, False, {"open": 0, "google.entities": "incomplete"}),
    )
    li_gone = PlatformChange(
        "linkedin", "campaign", "li-brand", at_offset_hours=-30, disappear=True
    )
    li_short = Fault("linkedin", "ad_campaigns", "http_status", status=503)
    yield from _pair(
        "c0.linkedin_cut_short",
        module,
        "C0 missing: LinkedIn's all-status search cut short raises no missing",
        fires=(
            _c0_world("c0.linkedin_cut_short", changes=(li_gone,)),
            Expectation(C0, True, {"kind": "missing", "source": "linkedin"}),
        ),
        does_not_fire=(
            _c0_world("c0.linkedin_cut_short", changes=(li_gone,), faults=(li_short,)),
            Expectation(C0, False, {"open": 0, "linkedin.entities": "incomplete"}),
        ),
    )
    tombstone = ContextSpec(
        content_assets=_NO_ASSETS,
        decisions=(DecisionSpec(_BRAND, -90, tombstone=True, origin="resolution"),),
    )
    yield from _pair(
        "c0.acknowledge",
        module,
        "C0 acknowledgement: acknowledge_removal resolves at once and no new missing is raised "
        "while the entity stays absent; its reappearance raises a status discrepancy against "
        "the tombstone",
        fires=(
            _c0_world(
                "c0.acknowledge",
                changes=(
                    _change(
                        "brand", None, None, -100, disappear=True, reappear_at_offset_hours=-30
                    ),
                ),
            ),
            Expectation(C0, True, {"kind": "status", "decided": "ended", "tombstone": True}),
        ),
        does_not_fire=(
            _c0_world(
                "c0.acknowledge", changes=(_change("brand", None, None, -100, disappear=True),)
            ),
            Expectation(C0, False, {"open": 0, "missing_raised_after_tombstone": False}),
        ),
        context=tombstone,
    )
    removed_while_gone = _change("brand", "status", "REMOVED", -60)
    yield from _pair(
        "c0.tombstone_ended",
        module,
        "C0 acknowledgement: a tombstoned campaign a complete enumeration returns as REMOVED "
        "(snapshotted as ended) raises a status discrepancy against the tombstone / none while "
        "it stays absent",
        fires=(
            _c0_world(
                "c0.tombstone_ended",
                changes=(
                    _change(
                        "brand", None, None, -100, disappear=True, reappear_at_offset_hours=-30
                    ),
                    removed_while_gone,
                ),
            ),
            Expectation(
                C0,
                True,
                {"kind": "status", "decided": "ended", "platform": "ended", "tombstone": True},
            ),
        ),
        does_not_fire=(
            _c0_world(
                "c0.tombstone_ended",
                changes=(
                    _change("brand", None, None, -100, disappear=True),
                    removed_while_gone,
                ),
            ),
            _no_open(),
        ),
        context=tombstone,
    )
    # Pool B's only member disappears with it; both removals are acknowledged.
    pool_b_removed = ContextSpec(
        content_assets=_NO_ASSETS,
        decisions=(
            DecisionSpec(
                SubjectRef("google", "campaign", "member-c"),
                -90,
                tombstone=True,
                origin="resolution",
            ),
            DecisionSpec(
                SubjectRef("google", "budget", "pool-b"), -90, tombstone=True, origin="resolution"
            ),
        ),
    )
    yield from _pair(
        "c0.tombstone_budget",
        module,
        "C0 acknowledgement: a tombstoned shared budget a complete enumeration returns again "
        "raises one budget discrepancy against the tombstone, since budget subjects have no "
        "status / none while it stays absent",
        fires=(
            _c0_world(
                "c0.tombstone_budget",
                changes=(
                    _change(
                        "member-c", None, None, -100, disappear=True, reappear_at_offset_hours=-30
                    ),
                ),
            ),
            Expectation(
                C0,
                True,
                {
                    "subject_kinds": (("budget", "budget"), ("campaign", "status")),
                    "budget_discrepancies": 1,
                    "member_discrepancies": 1,
                    "tombstone": True,
                },
            ),
        ),
        does_not_fire=(
            _c0_world(
                "c0.tombstone_budget",
                changes=(_change("member-c", None, None, -100, disappear=True),),
            ),
            _no_open(),
        ),
        context=pool_b_removed,
    )
    awaiting = ContextSpec(
        content_assets=_NO_ASSETS,
        discrepancies=(DiscrepancySpec(_BRAND, "missing", -90, state="awaiting_platform"),),
    )
    yield from _pair(
        "c0.decision_correct_missing",
        module,
        "C0 acknowledgement: decision_correct on missing waits as awaiting_platform until a "
        "complete enumeration returns the entity and its snapshot matches",
        fires=(
            _c0_world(
                "c0.decision_correct_missing",
                changes=(_change("brand", None, None, -100, disappear=True),),
            ),
            Expectation(C0, True, {"kind": "missing", "state": "awaiting_platform"}),
        ),
        does_not_fire=(
            _c0_world(
                "c0.decision_correct_missing",
                changes=(
                    _change(
                        "brand", None, None, -100, disappear=True, reappear_at_offset_hours=-30
                    ),
                ),
            ),
            Expectation(C0, False, {"open": 0, "resolved_by": "matching snapshot"}),
        ),
        context=awaiting,
    )
    yield _case(
        "c0.lifecycle_baseline",
        module,
        "C0 lifecycle: no discrepancy right after the baseline (first start)",
        _c0_world("c0.lifecycle_baseline"),
        _no_open(),
        ContextSpec(content_assets=_NO_ASSETS, first_start=True),
    )
    yield _case(
        "c0.lifecycle_awaiting",
        module,
        "C0 lifecycle: an awaiting_platform discrepancy auto-resolves when the platform matches",
        _c0_world(
            "c0.lifecycle_awaiting",
            changes=(
                _change("brand-budget", "amount", "120.00", -100),
                _change("brand-budget", "amount", "100.00", -30),
            ),
        ),
        _no_open(),
        ContextSpec(
            content_assets=_NO_ASSETS,
            discrepancies=(DiscrepancySpec(_BRAND, "budget", -90, state="awaiting_platform"),),
        ),
    )
    paused_and_raised = (
        _change("brand", "status", "PAUSED", -60),
        _change("brand-budget", "amount", "120.00", -60),
        _change("brand", "status", "ENABLED", -30),
    )
    yield from _pair(
        "c0.whole_snapshot",
        module,
        "C0 lifecycle: discrepancies close only when the whole snapshot matches, so a status "
        "that reverts while the budget mismatch remains keeps both open / both reverting "
        "closes both",
        fires=(
            _c0_world("c0.whole_snapshot", changes=paused_and_raised),
            Expectation(
                C0, True, {"subject_kinds": (("campaign", "budget"), ("campaign", "status"))}
            ),
        ),
        does_not_fire=(
            _c0_world(
                "c0.whole_snapshot",
                changes=(*paused_and_raised, _change("brand-budget", "amount", "100.00", -30)),
            ),
            _no_open(),
        ),
    )
    yield _case(
        "c0.refreshed_evidence",
        module,
        "C0 lifecycle: a later mismatch refreshes the unresolved discrepancy of its kind, so one "
        "budget discrepancy shows the latest platform amount and keeps its first detection",
        _c0_world(
            "c0.refreshed_evidence",
            changes=(
                _change("brand-budget", "amount", "120.00", -60),
                _change("brand-budget", "amount", "130.00", -30),
            ),
        ),
        Expectation(
            C0,
            True,
            {
                "open": 1,
                "kind": "budget",
                "decided_micros": 100_000_000,
                "platform_micros": 130_000_000,
                "detected_at_offset_hours": -60,
            },
        ),
    )
    yield _case(
        "c0.lifecycle_exempt",
        module,
        "C0 lifecycle: C0 is never held and its feedback returns 422",
        _c0_world("c0.lifecycle_exempt", changes=(_change("brand", "status", "PAUSED", -30),)),
        Expectation(C0, True, {"kind": "status", "held": False, "feedback_status": 422}),
    )


def _c1_world(name: str, control: ExplicitDays, dud: ExplicitDays) -> WorldSpec:
    """A converting control campaign and a spending, never-converting one (Google)."""
    google = GoogleSpec(
        budgets=(
            GoogleBudgetSpec("control-budget", Decimal("1000.00"), name="Control"),
            GoogleBudgetSpec("dud-budget", Decimal("1000.00"), name="Dud"),
        ),
        campaigns=(
            GoogleCampaignSpec("control", "Search - Converting", "control-budget", profile=control),
            GoogleCampaignSpec("dud", "Search - No output", "dud-budget", profile=dud),
        ),
        conversion_actions=(_LAST_CLICK,),
    )
    return _world(name, google=google)


def _c1_scenarios(module: str) -> Iterator[Scenario]:
    """C1 pause: expected outcomes, spending days and the window minimum."""

    def control(conversions: str, offsets: Sequence[int] = _WINDOW) -> ExplicitDays:
        return _spread(offsets, cost="950", impressions=19_000, clicks=950, conversions=conversions)

    def dud(offsets: Sequence[int]) -> ExplicitDays:
        return _spread(offsets, cost="50", impressions=len(offsets) * 50, clicks=len(offsets))

    # Baseline cost/outcome = 1,000 / V, so the dud's expected outcomes = 50 V / 1,000.
    yield from _pair(
        "c1.expected",
        module,
        "C1: 3.0 expected outcomes fire / 2.9 do not",
        fires=(
            _c1_world("c1.expected", control("60"), dud(_WINDOW)),
            Expectation(C1, True, {"expected_outcomes": "3.0", "spend_days": 28, "outcomes": 0}),
        ),
        does_not_fire=(
            _c1_world("c1.expected", control("58"), dud(_WINDOW)),
            Expectation(C1, False, {"expected_outcomes": "2.9"}),
        ),
    )
    twenty = _offsets(-20, -1)
    custom = ContextSpec(content_assets=_NO_ASSETS, window=(-20, -1), kind="custom")
    yield from _pair(
        "c1.spend_days",
        module,
        "C1: spend on 70% of window days fires / 60% does not",
        fires=(
            _c1_world("c1.spend_days", control("60", twenty), dud(_offsets(-14, -1))),
            Expectation(C1, True, {"spend_days": 14, "window_days": 20}),
        ),
        does_not_fire=(
            _c1_world("c1.spend_days", control("60", twenty), dud(_offsets(-12, -1))),
            Expectation(C1, False, {"spend_days": 12, "window_days": 20}),
        ),
        context=custom,
    )
    thirteen = _offsets(-13, -1)
    yield from _pair(
        "c1.window",
        module,
        "C1: a 14-day window fires / a 13-day window does not",
        fires=(
            _c1_world("c1.window", control("60", thirteen), dud(thirteen)),
            Expectation(C1, True, {"window_days": 14}),
        ),
        does_not_fire=(
            _c1_world("c1.window", control("60", thirteen), dud(thirteen)),
            Expectation(C1, False, {"window_days": 13}, withheld_reason="window 13 days of 14"),
        ),
        context=ContextSpec(content_assets=_NO_ASSETS, window=(-14, -1), kind="custom"),
        dnf_context=ContextSpec(content_assets=_NO_ASSETS, window=(-13, -1), kind="custom"),
    )


def _veto_scenarios(module: str) -> Iterator[Scenario]:
    """C1b pipeline pause, the veto cohort, unresolved matured leads and C2 outcomes."""
    with_assets = ContextSpec(content_assets=_NO_ASSETS)
    yield from _pair(
        "c1b.eligible",
        module,
        "C1b: 20 eligible contacts with QL 0 and no meetings fire / 19 do not",
        fires=(
            _pipeline_world("c1b.eligible", _lead_days(20, _MATURED)),
            Expectation(C1B, True, {"eligible_contacts": 20, "ql": 0, "meetings": 0}),
        ),
        does_not_fire=(
            _pipeline_world("c1b.eligible", _lead_days(19, _MATURED)),
            Expectation(
                C1B,
                False,
                {"eligible_contacts": 19},
                withheld_reason="eligible contacts 19 of 20 required",
            ),
        ),
    )

    def no_email(leads: int) -> LinkedInCreativeSpec:
        return _li_creative(
            "cr-webinar",
            "li-pipeline",
            _leadgen(_lead_days(leads, _MATURED), cost="10.00"),
            form=_NO_EMAIL_FORM_ID,
        )

    forms = (_EMAIL_FORM, _NO_EMAIL_FORM)
    yield from _pair(
        "c1b.floor",
        module,
        "C1b: below the attribution floor fires / at the floor (unmatched leads >= 10%) withheld",
        fires=(
            _pipeline_world(
                "c1b.floor", _lead_days(20, _MATURED), forms=forms, extra_creatives=(no_email(2),)
            ),
            Expectation(C1B, True, {"unresolved_share": "0.0909", "eligible_contacts": 20}),
        ),
        does_not_fire=(
            _pipeline_world(
                "c1b.floor", _lead_days(20, _MATURED), forms=forms, extra_creatives=(no_email(3),)
            ),
            Expectation(
                C1B,
                False,
                {"unresolved_share": "0.1304"},
                flags=(FLAG_ATTRIBUTION_FLOOR,),
                withheld_reason="attribution floor",
            ),
        ),
    )
    association_error = Fault(
        "hubspot",
        "associations_batch_read",
        "http_status",
        status=503,
        match={"object_type": "contacts", "to_object_type": "meetings"},
    )
    yield from _pair(
        "c1b.unknown_evidence",
        module,
        "C1b: known CRM evidence fires / an unknown contact-to-meeting fetch withholds it",
        fires=(
            _pipeline_world("c1b.unknown_evidence", _lead_days(20, _MATURED)),
            Expectation(C1B, True, {"eligible_contacts": 20}),
        ),
        does_not_fire=(
            _pipeline_world(
                "c1b.unknown_evidence", _lead_days(20, _MATURED), faults=(association_error,)
            ),
            Expectation(
                C1B,
                False,
                {"unknown_evidence_share": "1"},
                flags=(FLAG_UNKNOWN_CRM_EVIDENCE,),
                withheld_reason="unknown CRM evidence",
            ),
        ),
    )
    cohort_leads = _lead_days(20, _MATURED) | {-5: 1, -4: 1}
    yield _case(
        "veto.cohort",
        module,
        "Veto cohort: 20 eligible contacts with QL 0, plus young leads with QL 2, still veto",
        _pipeline_world("veto.cohort", cohort_leads),
        Expectation(
            C2,
            True,
            {"outcome": "Hold", "eligible_contacts": 20, "matured_ql": 0, "young_ql": 2},
        ),
        replace(
            with_assets,
            crm_patches=(
                CrmPatch(submitted_from_offset=-5, submitted_to_offset=-4, ql_after_touch_hours=24),
            ),
        ),
    )
    # 25 matured leads: 3 a day on -28/-27, 2 a day on -26..-19, 1 a day on -18..-16. Version 2
    # of the form drops the email question, so leads from its first day on are no_email.
    unresolved_leads = {-28: 3, -27: 3} | dict.fromkeys(_offsets(-26, -19), 2)
    unresolved_leads |= dict.fromkeys(_offsets(-18, -16), 1)

    def versioned(first_no_email_day: int) -> LinkedInLeadFormSpec:
        return _form(
            _EMAIL_FORM_ID,
            "Report download",
            FormVersionSpec(1, _questions(email=True)),
            FormVersionSpec(2, _questions(email=False), active_from_offset=first_no_email_day),
        )

    yield from _pair(
        "veto.unresolved",
        module,
        "Unresolved matured leads: 25 matured with 3 unresolved (12%) make the pipeline "
        "unavailable / 2 unresolved (8%) evaluate the veto; counts shown separately",
        fires=(
            _pipeline_world("veto.unresolved", unresolved_leads, forms=(versioned(-18),)),
            Expectation(
                C2,
                True,
                {
                    "outcome": "Scale",
                    "matured_leads": 25,
                    "matched_leads": 22,
                    "unresolved": 3,
                    "unresolved_share": "0.12",
                },
                flags=(FLAG_PIPELINE_UNAVAILABLE,),
            ),
        ),
        does_not_fire=(
            _pipeline_world("veto.unresolved", unresolved_leads, forms=(versioned(-17),)),
            Expectation(
                C2,
                True,
                {
                    "outcome": "Hold",
                    "matured_leads": 25,
                    "matched_leads": 23,
                    "unresolved": 2,
                    "unresolved_share": "0.08",
                    "eligible_contacts": 23,
                },
            ),
        ),
    )
    yield _case(
        "veto.repeat_contact",
        module,
        "Unresolved matured leads: two leads from one contact count once among eligible "
        "contacts but twice in the share's denominator",
        _pipeline_world(
            "veto.repeat_contact", {-25: 1, -20: 1}, funnel=_funnel(repeat_person_share=1)
        ),
        Expectation(
            C2,
            False,
            {"matured_leads": 2, "matched_leads": 2, "eligible_contacts": 1},
            withheld_reason="leads 2 of 5 required",
        ),
    )
    scale_leads = _lead_days(21, _MATURED)
    yield _case(
        "c2.outcome_scale",
        module,
        "C2 outcomes: Scale with a B1 item when the veto passes",
        _pipeline_world("c2.outcome_scale", scale_leads),
        Expectation(C2, True, {"outcome": "Scale", "b1": True, "proposed_daily_budget": "120.00"}),
        replace(
            with_assets,
            crm_patches=(
                CrmPatch(
                    submitted_from_offset=-28, submitted_to_offset=-26, ql_after_touch_hours=72
                ),
            ),
        ),
    )
    yield _case(
        "c2.outcome_hold",
        module,
        "C2 outcomes: Hold with no B1 under the veto",
        _pipeline_world("c2.outcome_hold", scale_leads),
        Expectation(C2, True, {"outcome": "Hold", "b1": False, "eligible_contacts": 21, "ql": 0}),
    )
    yield _case(
        "c2.qlstage_unresolved",
        module,
        "C2 outcomes: Scale flagged pipeline_unavailable when the QL stage is unresolved",
        _pipeline_world(
            "c2.qlstage_unresolved",
            scale_leads,
            hubspot=HubSpotSpec(
                funnel=_FLAT_FUNNEL, removed_stage_labels=("Qualified Lead", "Sales Qualified Lead")
            ),
        ),
        Expectation(
            C2,
            True,
            {"outcome": "Scale", "b1": True},
            finding=FINDING_PIPELINE_UNAVAILABLE,
            flags=(FLAG_PIPELINE_UNAVAILABLE,),
        ),
    )


def _budget_world(
    name: str,
    profile: ExplicitDays,
    *,
    daily: str | None = "100.00",
    total: str | None = None,
    changes: Iterable[PlatformChange] = (),
) -> WorldSpec:
    """One LinkedIn lead-gen campaign on UTC days, for budget-day classification."""
    campaign = _li_campaign("li-cap", "Capped - Lead gen", daily=daily, total=total)
    creative = _li_creative("cr-cap", "li-cap", profile)
    return _world(name, linkedin=_linkedin((campaign,), (creative,)), changes=changes)


def _capped(offsets: Sequence[int], leads_per_day: int = 2) -> ExplicitDays:
    """97.00 of a 100.00 daily budget (a capped day) on each offset, with leads."""
    return _leadgen(dict.fromkeys(offsets, leads_per_day), offsets=offsets)


def _cap_scenarios(module: str) -> Iterator[Scenario]:
    """C2 cap evidence and the budget-day classification behind every budget gate."""
    seven = ContextSpec(content_assets=_NO_ASSETS, window=(-7, -1), kind="custom")
    yield from _pair(
        "c2.cap_evidence",
        module,
        "C2 cap evidence: 5 capped of 7 known-budget days is eligible / one capped day plus "
        "six idle days is not",
        fires=(
            _budget_world("c2.cap_evidence", _capped(_offsets(-7, -3))),
            Expectation(
                C2, True, {"known_days": 7, "spending_days": 5, "capped_days": 5, "idle_days": 2}
            ),
        ),
        does_not_fire=(
            _budget_world("c2.cap_evidence", _capped((-7,), leads_per_day=6)),
            Expectation(
                C2, False, {"known_days": 7, "spending_days": 1, "capped_days": 1, "idle_days": 6}
            ),
        ),
        context=seven,
    )
    yield _case(
        "c2.cap_evidence_short",
        module,
        "C2 cap evidence: 4 known-budget days are withheld with observed and required",
        _budget_world("c2.cap_evidence_short", _capped(_offsets(-7, -1))),
        Expectation(
            C2,
            False,
            {"known_days": 4, "required": 7, "unobserved_days": 3},
            withheld_reason="known-budget days 4 of 7 required",
        ),
        replace(seven, observations_from_offset_hours=-4 * _HOURS_PER_DAY),
    )
    capped = _capped(_WINDOW)
    raise_at_noon = PlatformChange(
        "linkedin",
        "campaign",
        "li-cap",
        field="daily_budget",
        value=Decimal("120.00"),
        at_offset_hours=-3 * _HOURS_PER_DAY + 12,
    )
    yield _case(
        "budget_days.intraday",
        module,
        "Intraday budget uncertainty: a budget raised at noon makes that day changed_intraday, "
        "excluded from utilization and cap counts",
        _budget_world("budget_days.intraday", capped, changes=(raise_at_noon,)),
        Expectation(C2, True, {"changed_intraday_days": (_day_of(-3),), "known_days": 27}),
    )
    yield _case(
        "budget_days.first_snapshot_noon",
        module,
        "Intraday budget uncertainty: a first snapshot at noon makes that day unobserved",
        _budget_world("budget_days.first_snapshot_noon", capped),
        Expectation(C2, True, {"unobserved_days": (_day_of(-28),), "known_days": 27}),
        ContextSpec(
            content_assets=_NO_ASSETS, observations_from_offset_hours=-28 * _HOURS_PER_DAY + 12
        ),
    )
    yield from _pair(
        "budget_days.gap_7h",
        module,
        "Intraday budget uncertainty: a 7-hour observation gap makes the spanned day unobserved "
        "/ a day bracketed by unchanged observations at most 6 h apart is known",
        fires=(
            _budget_world("budget_days.gap_7h", capped),
            Expectation(C2, True, {"unobserved_days": (_day_of(-3),), "known_days": 27}),
        ),
        does_not_fire=(
            _budget_world("budget_days.gap_7h", capped),
            Expectation(C2, True, {"unobserved_days": (), "known_days": 28}),
        ),
        context=ContextSpec(content_assets=_NO_ASSETS, observation_gaps=((-64, -57),)),
        dnf_context=ContextSpec(content_assets=_NO_ASSETS, observation_gaps=((-63, -57),)),
    )
    yield _case(
        "budget_days.before_first_snapshot",
        module,
        "Intraday budget uncertainty: days before the first snapshot are unknown",
        _budget_world("budget_days.before_first_snapshot", capped),
        Expectation(C2, True, {"unobserved_days": 18, "known_days": 10}),
        ContextSpec(content_assets=_NO_ASSETS, observations_from_offset_hours=-10 * _HOURS_PER_DAY),
    )


def _segment_world(name: str, *, share: str = "0.10", leads_available: bool = True) -> WorldSpec:
    """A LinkedIn campaign whose Directors segment converts at half the campaign's CPL."""
    campaign = _li_campaign("li-seg", "Pipeline Report - Lead gen", daily="500.00")
    profile = _spread(
        _WINDOW, cost="5000", impressions=100_000, clicks=1_000, form_opens=300, leads=100
    )
    segment = LinkedInSegmentSpec(
        "li-seg",
        "MEMBER_SENIORITY",
        values=(SegmentValueSpec("urn:li:seniority:6", Decimal(share), Decimal("0.5")),),
        leads_available=leads_available,
    )
    linkedin = _linkedin(
        (campaign,), (_li_creative("cr-seg", "li-seg", profile),), segments=(segment,)
    )
    return _world(
        name, google=_QUIET_GOOGLE, linkedin=linkedin, hubspot=HubSpotSpec(funnel=_FLAT_FUNNEL)
    )


def _c3_scenarios(module: str) -> Iterator[Scenario]:
    """C3a segment launches (and their window labels) and C3b keyword-cluster launches."""
    fires = Expectation(
        C3A,
        True,
        {
            "facet": "seniority",
            "value": "urn:li:seniority:6",
            "segment_leads": 20,
            "cpl_ratio": "0.5",
            "impression_share": "0.10",
        },
    )
    yield _case(
        "c3a.preset",
        module,
        "C3a: fires on a preset window ending on the default window end",
        _segment_world("c3a.preset"),
        fires,
    )
    yield from _pair(
        "c3a.share",
        module,
        "C3a: a 14% impression share fires / a 16% share does not",
        fires=(
            _segment_world("c3a.share", share="0.14"),
            Expectation(C3A, True, {"impression_share": "0.14", "segment_leads": 28}),
        ),
        does_not_fire=(
            _segment_world("c3a.share", share="0.16"),
            Expectation(C3A, False, {"impression_share": "0.16"}),
        ),
    )
    yield from _pair(
        "c3a.no_lead_pivot",
        module,
        "C3a: does not fire on a pivot without leads",
        fires=(_segment_world("c3a.no_lead_pivot"), fires),
        does_not_fire=(
            _segment_world("c3a.no_lead_pivot", leads_available=False),
            Expectation(C3A, False, {"segment_leads": None}),
        ),
    )
    yield _case(
        "c3a.not_yet_available",
        module,
        "C3a: the latest segment fetch ends a day earlier (first start with the Google "
        "account's time zone behind UTC), so it is withheld as not yet available",
        _segment_world("c3a.not_yet_available"),
        Expectation(
            C3A,
            False,
            finding=FINDING_SEGMENT_WINDOW_UNAVAILABLE,
            label=(
                f"Segment data for the 28-day window ending {_day_of(-1)} not yet available; "
                f"latest available ends {_day_of(-2)}"
            ),
        ),
        ContextSpec(content_assets=_NO_ASSETS, segment_fetched_at_offset_hours=2),
    )
    yield _case(
        "c3a.custom_window",
        module,
        "C3a: a 30-day custom window is withheld with the preset-windows-only label",
        _segment_world("c3a.custom_window"),
        Expectation(
            C3A,
            False,
            finding=FINDING_SEGMENT_WINDOW_UNAVAILABLE,
            label=(
                "Segment data exists only for the preset windows 14, 28, 56, 90 ending "
                f"{_day_of(-1)}"
            ),
        ),
        ContextSpec(content_assets=_NO_ASSETS, window=(-30, -1), kind="custom"),
    )
    cluster = tuple(
        GoogleSearchTermSpec(
            term,
            "core",
            "kw-crm",
            profile=_spread(_WINDOW, cost="20", impressions=200, clicks=20, conversions="2"),
        )
        for term in ("hubspot alternative", "hubspot alternative free", "cheap hubspot alternative")
    )
    yield from _pair(
        "c3b.cluster",
        module,
        "C3b: a converting cluster without a matching campaign fires / its head token present "
        "in an ad group name does not",
        fires=(
            _keywords_world("c3b.cluster", extra_terms=cluster),
            Expectation(
                C3B,
                True,
                {"head_tokens": ("alternative", "hubspot"), "members": 3, "conversions": 6},
            ),
        ),
        does_not_fire=(
            _keywords_world(
                "c3b.cluster",
                extra_terms=cluster,
                ad_group=replace(_CORE, name="HubSpot Alternative Terms"),
            ),
            Expectation(C3B, False, {"head_tokens": ("alternative", "hubspot")}),
        ),
    )


def _campaign_scenarios() -> Iterator[Scenario]:
    """Every Campaigns to pause or launch scenario."""
    module = MODULE_CAMPAIGNS
    yield from _c0_scenarios(module)
    yield from _c1_scenarios(module)
    yield from _veto_scenarios(module)
    yield from _cap_scenarios(module)
    yield from _c3_scenarios(module)


# ---------------------------------------------------------------------------
# Budget changes (AAP 0.5.4.5; acceptance/test_budget_rules.py)
# ---------------------------------------------------------------------------


def _li_budgets(
    name: str,
    entries: Sequence[tuple[str, str | None, ExplicitDays]],
    *,
    changes: Iterable[PlatformChange] = (),
) -> WorldSpec:
    """LinkedIn lead-gen campaigns ``(key, daily budget, delivery)``, one creative each."""
    campaigns = tuple(
        _li_campaign(key, f"{key} - Lead gen", daily=daily) for key, daily, _ in entries
    )
    creatives = tuple(
        _li_creative(f"cr-{key}", key, profile) for key, _daily_budget, profile in entries
    )
    return _world(name, linkedin=_linkedin(campaigns, creatives), changes=changes)


def _per_day(spend: Mapping[int, str], leads: int) -> ExplicitDays:
    """Delivery with each day's own spend and ``leads`` leads on every delivering day."""
    return ExplicitDays(
        {
            offset: DayMetrics(
                cost=Decimal(cost), impressions=2_000, clicks=20, form_opens=leads + 10, leads=leads
            )
            for offset, cost in spend.items()
        }
    )


_POOL_SHARED: Final = GoogleBudgetSpec(
    "pool", Decimal("1000.00"), explicitly_shared=True, name="Shared pool"
)


def _shared_world(
    name: str,
    members: Sequence[tuple[str, str, str, str]],
    *,
    changes: Iterable[PlatformChange] = (),
    extra_budgets: Sequence[GoogleBudgetSpec] = (),
) -> WorldSpec:
    """A Google shared budget: members ``(key, budget key, daily spend, total conversions)``.

    A converting own-budget campaign (``solo``: 1,000.00 a day of 1,100.00, 1,400 conversions)
    sets most of the account baseline.
    """
    campaigns = [
        GoogleCampaignSpec(
            "solo",
            "Search - Solo",
            "solo-budget",
            profile=_spread(
                _WINDOW, cost="28000", impressions=280_000, clicks=28_000, conversions="1400"
            ),
        )
    ]
    for key, budget, daily, conversions in members:
        channel = "DISPLAY" if key.endswith("-display") else "SEARCH"
        total = Decimal(daily) * len(_WINDOW)
        campaigns.append(
            GoogleCampaignSpec(
                key,
                f"Generic - {key}",
                budget,
                channel=channel,
                profile=_spread(
                    _WINDOW,
                    cost=total,
                    impressions=int(total) * 10,
                    clicks=int(total),
                    conversions=conversions,
                ),
            )
        )
    google = GoogleSpec(
        budgets=(
            GoogleBudgetSpec("solo-budget", Decimal("1100.00"), name="Solo"),
            _POOL_SHARED,
            *extra_budgets,
        ),
        campaigns=tuple(campaigns),
        conversion_actions=(_LAST_CLICK,),
    )
    return _world(name, google=google, changes=changes)


def _b4_google(name: str, profile: ExplicitDays, action: GoogleConversionActionSpec) -> WorldSpec:
    """A Google search campaign for B4 with one conversion action."""
    google = GoogleSpec(
        budgets=(GoogleBudgetSpec("b4-budget", Decimal("150.00"), name="B4"),),
        campaigns=(
            GoogleCampaignSpec("weekly", "Search - Weekly pattern", "b4-budget", profile=profile),
        ),
        conversion_actions=(action,),
    )
    return _world(name, google=google)


def _weekly(
    weekday: DayMetrics, weekend: DayMetrics | None, *, weekend_days: Iterable[int] = _WEEKENDS
) -> ExplicitDays:
    """Weekday metrics on every weekday of the window, weekend metrics on ``weekend_days``."""
    chosen = set(weekend_days)
    days = {offset: weekday for offset in _WINDOW if offset not in _WEEKENDS}
    if weekend is not None:
        days |= {offset: weekend for offset in _WINDOW if offset in chosen}
    return ExplicitDays(days)


def _budget_scenarios() -> Iterator[Scenario]:
    """B1 to B4, G1 and shared budgets."""
    module = MODULE_BUDGET
    seven = ContextSpec(content_assets=_NO_ASSETS, window=(-7, -1), kind="custom")
    ten = ContextSpec(content_assets=_NO_ASSETS, window=(-10, -1), kind="custom")
    yield _case(
        "b1.step",
        module,
        "B1: exactly +20% of the latest effective budget, rounded to the minor unit",
        _budget_world("b1.step", _leadgen(dict.fromkeys(_WINDOW, 2), cost="80.83"), daily="83.33"),
        Expectation(B1, True, {"current": "83.33", "proposed": "100.00", "step": "0.20"}),
    )
    efficient = _leadgen(dict.fromkeys(_WINDOW, 7), cost="140.00")
    yield from _pair(
        "g1.b2_target",
        module,
        "G1 on a B2 target: B2 fires and G1 blocks every increase on its target (C2 Scale, B1, "
        "a B3 reallocation to it, a W1 naming it); bypassing the guard trips the runner assertion",
        fires=(
            _li_budgets(
                "g1.b2_target",
                (("efficient", "200.00", efficient), ("hot", "100.00", _capped(_WINDOW, 1))),
            ),
            Expectation(G1, True, {"blocked_target": "hot", "b2": True, "surviving_increases": 0}),
        ),
        does_not_fire=(
            _li_budgets(
                "g1.b2_target",
                (("efficient", "200.00", efficient), ("hot", "100.00", _capped(_WINDOW, 5))),
            ),
            Expectation(G1, False, {"blocked_target": None, "b1": True}),
        ),
    )
    control = ("control", "500.00", _leadgen(dict.fromkeys(_WINDOW, 15), cost="300.00"))
    yield _case(
        "b2.spending_days",
        module,
        "B2/B3 spending-day gates: 7 known-budget days with 4 spending days withhold B2 and B3 "
        "with spending days 4 of 5",
        _li_budgets(
            "b2.spending_days",
            (control, ("hot", "100.00", _per_day(dict.fromkeys(_offsets(-7, -4), "97.00"), 1))),
        ),
        Expectation(
            B2,
            False,
            {"known_days": 7, "spending_days": 4, "required": 5, "b3_withheld": True},
            withheld_reason="spending days 4 of 5 required",
        ),
        seven,
    )
    slack = dict.fromkeys(_offsets(-10, -7), "40.00") | dict.fromkeys((-6, -5), "80.00")
    yield from _pair(
        "b3.idle_known",
        module,
        "B2/B3 spending-day gates: 10 known days with 6 spending and 4 known idle days, u_d <= "
        "0.5 on 8, fire B3 / 10 known idle days and no spending day do not",
        fires=(
            _li_budgets("b3.idle_known", (("slack", "100.00", _per_day(slack, 2)),)),
            Expectation(
                B3,
                True,
                {
                    "known_days": 10,
                    "spending_days": 6,
                    "idle_days": 4,
                    "under_utilized_days": 8,
                    "proposed": "88.00",
                },
            ),
        ),
        does_not_fire=(
            _li_budgets("b3.idle_known", (("slack", "100.00", ExplicitDays()),)),
            Expectation(B3, False, {"known_days": 10, "spending_days": 0, "idle_days": 10}),
        ),
        context=ten,
    )

    def hot(high_days: int) -> ExplicitDays:
        spend = {
            offset: "95.00" if index < high_days else "60.00"
            for index, offset in enumerate(_offsets(-7, -1))
        }
        return _per_day(spend, 1)

    yield from _pair(
        "b2.share",
        module,
        "B2/B3 spending-day gates: high-utilization days on 5 of 7 (71%) fire B2 / 4 of 7 "
        "(57%) do not",
        fires=(
            _li_budgets("b2.share", (control, ("hot", "100.00", hot(5)))),
            Expectation(B2, True, {"high_utilization_days": 5, "known_days": 7}),
        ),
        does_not_fire=(
            _li_budgets("b2.share", (control, ("hot", "100.00", hot(4)))),
            Expectation(B2, False, {"high_utilization_days": 4, "known_days": 7}),
        ),
        context=seven,
    )
    yield _case(
        "budget.unknown_not_idle",
        module,
        "B2/B3 spending-day gates: unknown days are never counted as idle",
        _li_budgets(
            "budget.unknown_not_idle",
            (("slack", "100.00", _per_day(dict.fromkeys(_offsets(-6, -1), "40.00"), 2)),),
        ),
        Expectation(
            B3,
            False,
            {"known_days": 6, "idle_days": 0, "unobserved_days": 4},
            withheld_reason="known-budget days 6 of 7 required",
        ),
        replace(ten, observations_from_offset_hours=-6 * _HOURS_PER_DAY),
    )
    yield from _shared_scenarios(module)
    yield from _allocation_scenarios(module)
    yield from _b4_scenarios(module)
    yield _case(
        "budget.lifetime",
        module,
        "Exclusion: lifetime budgets are excluded from cap rules (utilization n/a)",
        _budget_world("budget.lifetime", _capped(_WINDOW), daily=None, total="5000.00"),
        Expectation(B1, False, {"utilization": "n/a", "budget_period": "lifetime"}),
    )


def _shared_scenarios(module: str) -> Iterator[Scenario]:
    """Shared budgets: dated membership, member safety, aggregate B2 and the budget subject."""
    poor = (("m1", "pool", "700.00", "1400"), ("m2", "pool", "270.00", "100"))
    good = (("m1", "pool", "700.00", "1400"), ("m2", "pool", "270.00", "400"))
    yield from _pair(
        "shared.poor_member",
        module,
        "Shared budgets: a poor-efficiency member inside a high-utilization shared budget blocks "
        "the budget-level B1 / a good-efficiency member does not",
        fires=(
            _shared_world("shared.poor_member", poor),
            Expectation(
                G1,
                True,
                {"blocked_target": "pool", "poor_member": "m2"},
                flags=(FLAG_SHARED_BUDGET,),
            ),
        ),
        does_not_fire=(
            _shared_world("shared.poor_member", good),
            Expectation(
                B1, True, {"subject": "pool", "proposed": "1200.00"}, flags=(FLAG_SHARED_BUDGET,)
            ),
        ),
    )
    yield _case(
        "shared.aggregate_b2",
        module,
        "Shared budgets: an aggregate B2 blocks the budget-level increase too",
        _shared_world(
            "shared.aggregate_b2",
            (("m1", "pool", "700.00", "280"), ("m2", "pool", "270.00", "100")),
        ),
        Expectation(
            B2, True, {"subject_type": "budget", "subject": "pool"}, flags=(FLAG_SHARED_BUDGET,)
        ),
    )
    # m1 and m2 spend 970.00 of the 1,000.00 pool before m3 joins and 1,070.00 after, so every
    # budget-active day is capped and the pool is cap-constrained, as B1 requires.
    joiner = (
        ("m1", "pool", "700.00", "1200"),
        ("m2", "pool", "270.00", "400"),
        ("m3", "m3-budget", "100.00", "200"),
    )
    yield _case(
        "shared.membership_mid_window",
        module,
        "Shared budgets: a campaign that joined mid-window counts only on its member days",
        _shared_world(
            "shared.membership_mid_window",
            joiner,
            extra_budgets=(GoogleBudgetSpec("m3-budget", Decimal("150.00"), name="M3 own"),),
            changes=(_change("m3", "budget_key", "pool", -10 * _HOURS_PER_DAY),),
        ),
        Expectation(B1, True, {"member": "m3", "member_days": 10, "changed_day": _day_of(-11)}),
    )
    yield _case(
        "shared.member_changed_intraday",
        module,
        "Shared budgets: a member that changed budgets during a day makes that day unknown",
        _shared_world(
            "shared.member_changed_intraday",
            good,
            extra_budgets=(_POOL_B,),
            changes=(_change("m2", "budget_key", "pool-b", -5 * _HOURS_PER_DAY + 16),),
        ),
        Expectation(B1, True, {"unknown_days": (_day_of(-5),)}),
    )
    yield _case(
        "shared.decision_subject",
        module,
        "Shared budgets: a shared-budget decision writes only the budget subject and creates no "
        "discrepancy on any member",
        _shared_world(
            "shared.decision_subject",
            good,
            changes=(_change("pool", "amount", "1200.00", -90),),
        ),
        Expectation(
            C0,
            False,
            {"decision_subject": "budget", "budget_discrepancies": 0, "member_discrepancies": 0},
        ),
        ContextSpec(
            content_assets=_NO_ASSETS,
            decisions=(
                DecisionSpec(
                    SubjectRef("google", "budget", "pool"), -100, daily_budget=Decimal("1200.00")
                ),
            ),
        ),
    )
    # Priority decision: every Google campaign shares one objective group (AAP 0.5.4.1
    # "Outcome by source"; analysis.context.objective_group), and shared budgets are Google
    # only, so a shared budget cannot span objective groups. The case pins that a SEARCH and a
    # DISPLAY member stay one group, so efficiency is computed and B1 is not withheld.
    yield _case(
        "shared.mixed_objectives",
        module,
        "Shared budgets: members on different Google channels share the single Google objective "
        "group, so efficiency is computed (mixed groups, which withhold, cannot arise for Google)",
        _shared_world(
            "shared.mixed_objectives",
            (("m1", "pool", "700.00", "1400"), ("m2-display", "pool", "270.00", "400")),
        ),
        Expectation(B1, True, {"objective_groups": ("google",)}, flags=(FLAG_SHARED_BUDGET,)),
    )


def _allocation_scenarios(module: str) -> Iterator[Scenario]:
    """B3 headroom allocation to surviving B1 receivers."""
    donors = (
        ("donor-a", "520.00", _leadgen(dict.fromkeys(_WINDOW, 5), cost="200.00")),
        ("donor-b", "420.00", _leadgen(dict.fromkeys(_WINDOW, 5), cost="200.00")),
    )
    receiver = ("receiver", "2000.00", _leadgen(dict.fromkeys(_WINDOW, 98), cost="1960.00"))
    yield _case(
        "b3.allocation",
        module,
        "B3 allocation: donors freeing 300 and 200 against one receiver with a B1 increase of "
        "400 give allocations 300 and 100; the unallocated 100 stays a plain decrease; the B1 "
        "card shows 400 funded by reallocation and 0 from new budget",
        _li_budgets("b3.allocation", (*donors, receiver)),
        Expectation(
            B3,
            True,
            {
                "freed": ("300.00", "200.00"),
                "allocations": ("300.00", "100.00"),
                "unallocated": "100.00",
                "b1_increase": "400.00",
                "funded_by_reallocation": "400.00",
                "from_new_budget": "0.00",
            },
        ),
    )
    yield _case(
        "b3.no_receiver",
        module,
        "B3 allocation: with no receiver the freed amount is a plain decrease",
        _li_budgets("b3.no_receiver", donors),
        Expectation(B3, True, {"freed": ("300.00", "200.00"), "allocations": ()}),
    )


def _b4_scenarios(module: str) -> Iterator[Scenario]:
    """B4 weekday/weekend patterns and the Google counting contract."""
    li_weekday = DayMetrics(
        cost=Decimal(100), impressions=2_000, clicks=20, form_opens=20, leads=10
    )
    li_weekend = DayMetrics(cost=Decimal(100), impressions=2_000, clicks=20, form_opens=12, leads=2)

    def linkedin(profile: ExplicitDays) -> WorldSpec:
        return _budget_world("b4.linkedin", profile, daily="500.00")

    yield from _pair(
        "b4.linkedin",
        module,
        "B4: LinkedIn leads run the rate test, p < 0.05 fires / minimums missed are withheld with "
        "their sample sizes",
        fires=(
            linkedin(_weekly(li_weekday, li_weekend)),
            Expectation(
                B4,
                True,
                {"weekend_days": 8, "weekdays": 20, "weekend_cpo": "50.00", "weekday_cpo": "10.00"},
            ),
        ),
        does_not_fire=(
            linkedin(_weekly(li_weekday, li_weekend, weekend_days=(-27, -20, -13))),
            Expectation(
                B4,
                False,
                {"weekend_days": 3, "required": 4},
                withheld_reason="3 weekend days of 4 required",
            ),
        ),
    )
    weekday = _metrics("100.00", 2_000, 100, "10")
    weekend = _metrics("100.00", 2_000, 100, "2")
    pattern = _weekly(weekday, weekend)
    one_day = replace(
        _LAST_CLICK,
        model_changes=(
            (-10, "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"),
            (-9, "GOOGLE_ADS_LAST_CLICK"),
        ),
    )
    yield from _pair(
        "b4.google_last_click",
        module,
        "B4: Google with every included action last-click on every day and whole-number values "
        "runs the test / a data-driven action on any day is withheld (weighted attribution), "
        "even when every daily total is a whole number",
        fires=(
            _b4_google("b4.google_last_click", pattern, _LAST_CLICK),
            Expectation(B4, True, {"contract": "all included actions last-click on all 28 days"}),
        ),
        does_not_fire=(
            _b4_google("b4.google_last_click", pattern, one_day),
            Expectation(B4, False, withheld_reason="weighted attribution: rate test not valid"),
        ),
    )
    yield _case(
        "b4.contract_unknown",
        module,
        "B4: a day before the first Q-G8 snapshot leaves the counting contract unknown",
        _b4_google("b4.contract_unknown", pattern, _LAST_CLICK),
        Expectation(B4, False, withheld_reason="conversion counting contract unknown"),
        ContextSpec(content_assets=_NO_ASSETS, observations_from_offset_hours=-20 * _HOURS_PER_DAY),
    )
    fractional = ExplicitDays(dict(pattern.days) | {-8: _metrics("100.00", 2_000, 100, "2.5")})
    yield _case(
        "b4.fractional",
        module,
        "B4: last-click with a fractional daily value is withheld (fractional conversions)",
        _b4_google("b4.fractional", fractional, _LAST_CLICK),
        Expectation(B4, False, withheld_reason="fractional conversions"),
    )
    yield _case(
        "b4.no_exposure",
        module,
        "B4: zero weekend spend is withheld (no exposure)",
        _b4_google("b4.no_exposure", _weekly(weekday, None), _LAST_CLICK),
        Expectation(B4, False, withheld_reason="no exposure"),
    )


# ---------------------------------------------------------------------------
# Copy / creative changes (AAP 0.5.4.6; acceptance/test_creative_rules.py)
# ---------------------------------------------------------------------------


def _funnel_days(
    impressions: int, clicks: int, form_opens: int, leads: int, *, cost: str | None = None
) -> ExplicitDays:
    """A creative's window funnel totals spread over the default window."""
    return _spread(
        _WINDOW,
        cost=cost if cost is not None else str(Decimal(impressions) / 100),
        impressions=impressions,
        clicks=clicks,
        form_opens=form_opens,
        leads=leads,
    )


_BASELINE_FUNNEL: Final = _funnel_days(100_000, 1_500, 1_000, 200)
"""The account-baseline campaign's single creative: click 1.5%, open 1%, completion 20%."""

_LEAD_GEN_OBJECTIVE: Final = "LEAD_GENERATION"


def _site_days(impressions: int, clicks: int, website_conversions: int) -> ExplicitDays:
    """A landing-page creative's window totals (no lead form) spread over the default window."""
    return _spread(
        _WINDOW,
        cost=str(Decimal(impressions) / 100),
        impressions=impressions,
        clicks=clicks,
        website_conversions=website_conversions,
    )


def _creative_world(
    name: str,
    variants: Sequence[tuple[str, ExplicitDays]],
    *,
    baseline: ExplicitDays | None = _BASELINE_FUNNEL,
    objective: str = _LEAD_GEN_OBJECTIVE,
) -> WorldSpec:
    """A campaign with the given variants, beside a one-creative baseline campaign.

    Both campaigns share ``objective``, so the account baseline pools the same objective
    group. Outside lead gen the creatives are landing-page creatives (no lead form).
    """
    lead_gen = objective == _LEAD_GEN_OBJECTIVE
    form = _EMAIL_FORM_ID if lead_gen else None
    base_name = "Benchmarks - Lead gen" if lead_gen else "Benchmarks - Website visits"
    campaigns = [
        _li_campaign(
            "li-copy", "Pipeline Report - Copy tests", daily="2000.00", objective=objective
        )
    ]
    creatives = [
        _li_creative(key, "li-copy", profile, headline=f"Headline {key}", form=form)
        for key, profile in variants
    ]
    if baseline is not None:
        campaigns.append(_li_campaign("li-base", base_name, daily="2000.00", objective=objective))
        creatives.append(_li_creative("cr-base", "li-base", baseline, form=form))
    return _world(name, linkedin=_linkedin(campaigns, creatives))


def _creative_scenarios() -> Iterator[Scenario]:
    """CR1 distinguishability, CR2 weak stage, CR3 next test and the variant minimum."""
    module = MODULE_CREATIVE
    strong = _funnel_days(20_000, 200, 400, 200)
    yield from _pair(
        "cr1.distinguishable",
        module,
        "CR1: distinguishable after correction / ahead but not yet distinguishable",
        fires=(
            _creative_world(
                "cr1.distinguishable",
                (("cr-a", strong), ("cr-b", _funnel_days(20_000, 200, 400, 100))),
            ),
            Expectation(
                CR1,
                True,
                {"pair": ("cr-a", "cr-b"), "distinguishable": True, "test": "two_proportion_z"},
            ),
        ),
        does_not_fire=(
            _creative_world(
                "cr1.distinguishable",
                (("cr-a", strong), ("cr-b", _funnel_days(20_000, 200, 400, 180))),
            ),
            Expectation(
                CR1,
                False,
                {"ahead": "cr-a", "distinguishable": False},
                flags=(FLAG_NOT_DISTINGUISHABLE,),
            ),
        ),
    )
    yield _case(
        "cr1.holm_flip",
        module,
        "CR1: the Holm adjustment flips a raw p of 0.047 to an adjusted 0.14 (not distinguishable)",
        _creative_world(
            "cr1.holm_flip",
            (
                ("cr-a", _funnel_days(10_000, 100, 300, 130)),
                ("cr-b", _funnel_days(10_000, 100, 300, 100)),
                ("cr-c", _funnel_days(10_000, 100, 300, 115)),
            ),
        ),
        Expectation(
            CR1,
            False,
            {"pair": ("cr-a", "cr-b"), "p_value": "0.0466", "p_adjusted": "0.1399"},
            flags=(FLAG_NOT_DISTINGUISHABLE,),
        ),
    )
    yield _case(
        "cr1.fisher",
        module,
        "CR1: Fisher's exact test is chosen when an expected cell is below 5",
        _creative_world(
            "cr1.fisher",
            (
                ("cr-a", _funnel_days(1_000, 20, 30, 6)),
                ("cr-b", _funnel_days(1_000, 20, 30, 1)),
            ),
        ),
        Expectation(CR1, False, {"test": "fisher_exact", "p_value": "0.1243"}),
    )
    same = _funnel_days(10_000, 100, 300, 100)
    yield _case(
        "cr1.identical_rates",
        module,
        "CR1: identical rates make the sample still needed not estimable (no observed difference)",
        _creative_world("cr1.identical_rates", (("cr-a", same), ("cr-b", same))),
        Expectation(
            CR1, False, {"sample_needed": "not estimable", "reason": "no observed difference"}
        ),
    )
    # Priority decision: thresholds.yaml requires cr.min_impressions >= 1, so a creative with
    # zero impressions is never a variant and CR1's zero-impression "not estimable" guard cannot
    # be reached through a valid configuration. The case pins the reachable behaviour: the
    # zero-impression creative is excluded and the remaining pair is compared as usual.
    yield _case(
        "cr1.zero_impressions",
        module,
        "CR1: a creative with zero impressions is never a variant (cr.min_impressions >= 1), so "
        "it is excluded and the remaining pair is compared",
        _creative_world(
            "cr1.zero_impressions",
            (
                ("cr-a", same),
                ("cr-b", _funnel_days(10_000, 100, 300, 120)),
                ("cr-c", ExplicitDays()),
            ),
        ),
        Expectation(CR1, False, {"variants": 2, "excluded": ("cr-c",)}),
    )
    weak_click = (
        ("cr-a", _funnel_days(20_000, 100, 200, 40)),
        ("cr-b", _funnel_days(20_000, 100, 200, 40)),
    )
    yield from _pair(
        "cr2.weak_stage",
        module,
        "CR2: a stage whose Wilson upper bound is below the account baseline is weak / none is",
        fires=(
            _creative_world("cr2.weak_stage", weak_click),
            Expectation(CR2, True, {"stage": "click_rate", "rate": "0.005", "baseline": "0.0121"}),
        ),
        does_not_fire=(
            _creative_world(
                "cr2.weak_stage", weak_click, baseline=_funnel_days(100_000, 200, 1_000, 200)
            ),
            Expectation(CR2, False, label="No stage measurably below baseline"),
        ),
    )
    # Priority decision: AAP 0.5.4.6 says non-lead-gen campaigns use only click rate, and no
    # other scenario analyses a non-lead-gen campaign (the default world's website campaign
    # has one variant), so this case adds one for acceptance/test_creative_rules.py. Its
    # variants differ only in clicks (120 vs 80) and have no leads, so CR1 can call the pair
    # distinguishable only on clicks / impressions.
    yield _case(
        "cr2.click_only",
        module,
        "CR2: a non-lead-gen campaign measures the click rate only, and CR1 compares clicks / "
        "impressions",
        _creative_world(
            "cr2.click_only",
            (("cr-a", _site_days(20_000, 120, 12)), ("cr-b", _site_days(20_000, 80, 4))),
            baseline=_site_days(100_000, 1_500, 30),
            objective="WEBSITE_VISIT",
        ),
        Expectation(
            CR2,
            True,
            {
                "stage": "click_rate",
                "stages": ("click_rate",),
                "rate": "0.005",
                "baseline": "0.0121",
            },
        ),
    )
    yield _case(
        "cr3.click",
        module,
        "CR3: a weak click rate changes only the headline",
        _creative_world("cr3.click", weak_click),
        Expectation(
            CR3, True, {"stage": "click_rate", "variable": "headline", "unit": "impressions"}
        ),
    )
    yield _case(
        "cr3.open",
        module,
        "CR3: an open-rate test changes the call-to-action label only",
        _creative_world(
            "cr3.open",
            (
                ("cr-a", _funnel_days(20_000, 300, 50, 10)),
                ("cr-b", _funnel_days(20_000, 300, 50, 10)),
            ),
        ),
        Expectation(
            CR3, True, {"stage": "open_rate", "variable": "cta_label", "unit": "impressions"}
        ),
    )
    yield _case(
        "cr3.completion",
        module,
        "CR3 units: a completion-rate sample is stated in form opens, with the projection to "
        "impressions through the observed open rate",
        _creative_world(
            "cr3.completion",
            (
                ("cr-a", _funnel_days(20_000, 300, 400, 20)),
                ("cr-b", _funnel_days(20_000, 300, 400, 20)),
            ),
        ),
        Expectation(
            CR3,
            True,
            {
                "stage": "completion_rate",
                "variable": "form_questions",
                "unit": "form opens",
                "projected_unit": "impressions",
                "observed_open_rate": "0.02",
            },
        ),
    )
    # Priority decision: a stage with zero exposure has no Wilson interval, so CR2 can never
    # report it and CR3 never tests it; every variant has at least cr.min_impressions
    # impressions, so click and open rate always have exposure. With zero form opens the open
    # rate (0%) is the weak stage, which is the right advice: change the CTA label, not the
    # form nobody opened. CR3's "zero exposure" guard is therefore reachable only through
    # analysis.creative.plan_next_test, and this case pins the behaviour evaluate() shows.
    yield _case(
        "cr3.zero_exposure",
        module,
        "CR3 units: a stage with zero exposure (no form opens) is never tested; the 0% open "
        "rate is weak, so the test changes only the call-to-action label, sized in impressions",
        _creative_world(
            "cr3.zero_exposure",
            (("cr-a", _funnel_days(20_000, 300, 0, 0)), ("cr-b", _funnel_days(20_000, 300, 0, 0))),
        ),
        Expectation(
            CR3, True, {"stage": "open_rate", "variable": "cta_label", "unit": "impressions"}
        ),
    )
    yield _case(
        "cr3.not_estimable_zero_baseline",
        module,
        "CR3 units: a zero baseline rate is not estimable",
        _creative_world(
            "cr3.not_estimable_zero_baseline",
            (
                ("cr-a", _funnel_days(20_000, 300, 400, 0)),
                ("cr-b", _funnel_days(20_000, 300, 400, 0)),
            ),
            baseline=_funnel_days(100_000, 1_500, 1_000, 0),
        ),
        Expectation(CR3, False, {"sample_needed": "not estimable", "reason": "baseline rate is 0"}),
    )
    yield _case(
        "cr3.not_estimable_target_over_100",
        module,
        "CR3 units: a target above 100% (baseline x 1.2 > 1) is not estimable",
        _creative_world(
            "cr3.not_estimable_target_over_100",
            (
                ("cr-a", _funnel_days(20_000, 300, 300, 255)),
                ("cr-b", _funnel_days(20_000, 300, 300, 255)),
            ),
            baseline=_funnel_days(100_000, 1_500, 1_000, 900),
        ),
        Expectation(
            CR3,
            False,
            {
                "stage": "completion_rate",
                "sample_needed": "not estimable",
                "reason": "target above 100%",
            },
        ),
    )
    leader = _funnel_days(5_000, 50, 50, 10)
    yield from _pair(
        "cr.variants",
        module,
        "Variants: a creative with 1,000 impressions is a variant / under 1,000 is excluded",
        fires=(
            _creative_world(
                "cr.variants", (("cr-a", leader), ("cr-b", _funnel_days(1_000, 10, 10, 2)))
            ),
            Expectation(CR1, True, {"variants": 2}),
        ),
        does_not_fire=(
            _creative_world(
                "cr.variants", (("cr-a", leader), ("cr-b", _funnel_days(999, 10, 10, 2)))
            ),
            Expectation(CR1, False, {"variants": 1}),
        ),
    )


# ---------------------------------------------------------------------------
# White papers (AAP 0.5.4.7; acceptance/test_white_paper_attribution.py)
# ---------------------------------------------------------------------------

_PAPER_FORMS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "guide-a": 990_000_101,
        "guide-b": 990_000_201,
        "guide-c": 990_000_301,
        "guide-d": 990_000_401,
    }
)
_CHEAP_FORM: Final = 990_000_501
_UNLISTED_FORM: Final = 990_000_601
"""A lead form no registry asset lists, so only a creative override routes its leads."""
_LANDING_CREATIVE: Final = 990_001_301
_EU_FORM: Final = 990_000_102


def _asset(slug: str, *forms: int, creatives: Sequence[int] = ()) -> ContentAsset:
    """A white paper registry entry."""
    return ContentAsset(
        slug,
        slug.replace("-", " ").title(),
        tuple(f"{FORM_URN_PREFIX}{form}" for form in forms),
        tuple(f"{CREATIVE_URN_PREFIX}{creative}" for creative in creatives),
    )


_PAPERS: Final[tuple[ContentAsset, ...]] = tuple(
    _asset(slug, form) for slug, form in _PAPER_FORMS.items()
)


@dataclass(frozen=True, slots=True)
class _Paper:
    """One white paper creative: its form, window spend and leads per day offset."""

    key: str
    form: int | None
    cost: str
    leads: Mapping[int, int]
    campaign: str = "li-wp"
    creative_id: int | None = None


def _paper_days(cost: str, leads: Mapping[int, int]) -> ExplicitDays:
    """Window spend split evenly, with each day's leads."""
    costs = _split_money(Decimal(cost), len(_WINDOW))
    return ExplicitDays(
        {
            offset: DayMetrics(
                cost=costs[index],
                impressions=1_000,
                clicks=10,
                form_opens=leads.get(offset, 0) + 5,
                leads=leads.get(offset, 0),
            )
            for index, offset in enumerate(_WINDOW)
        }
    )


_EU_LI_ACCOUNT: Final = LinkedInAccountSpec(_EU_ACCOUNT, "Simulated Ads - EMEA", "EUR")
_EU_GROUP: Final = LinkedInCampaignGroupSpec("grp-eu", _EU_ACCOUNT, "EMEA - Lead gen")
_PAPER_CAMPAIGNS: Final[Mapping[str, LinkedInCampaignSpec]] = MappingProxyType(
    {
        "li-wp": _li_campaign("li-wp", "Pipeline Report - Lead gen", daily="2000.00"),
        "li-hot": _li_campaign("li-hot", "Hot - Lead gen", daily="100.00"),
        "li-cheap": _li_campaign("li-cheap", "Cheap leads - Lead gen", daily="2000.00"),
        "li-eu": _li_campaign(
            "li-eu", "Pipeline Report - EMEA", daily="2000.00", account=_EU_ACCOUNT, group="grp-eu"
        ),
    }
)


def _papers_world(
    name: str,
    papers: Sequence[_Paper],
    *,
    funnel: FunnelSpec = _FLAT_FUNNEL,
    faults: Iterable[Fault] = (),
    unknown_creative_share: str = "0",
    forms: Sequence[LinkedInLeadFormSpec] | None = None,
    hubspot: HubSpotSpec | None = None,
) -> WorldSpec:
    """White paper creatives in their campaigns, each form asking the work email."""
    campaign_keys = list(dict.fromkeys(paper.campaign for paper in papers))
    campaigns = tuple(_PAPER_CAMPAIGNS[key] for key in campaign_keys)
    creatives = tuple(
        _li_creative(
            paper.key,
            paper.campaign,
            _paper_days(paper.cost, paper.leads),
            form=paper.form,
            creative_id=paper.creative_id,
        )
        for paper in papers
    )
    europe = "li-eu" in campaign_keys
    if forms is None:
        form_ids = sorted({paper.form for paper in papers if paper.form is not None})
        forms = tuple(
            _form(
                form,
                f"White paper form {form}",
                account=_EU_ACCOUNT if form == _EU_FORM else _NA_ACCOUNT,
            )
            for form in form_ids
        )
    linkedin = replace(
        _linkedin(
            campaigns,
            creatives,
            forms=forms,
            accounts=(_LI_ACCOUNT, _EU_LI_ACCOUNT) if europe else (_LI_ACCOUNT,),
            groups=(_LI_GROUP, _EU_GROUP) if europe else (_LI_GROUP,),
        ),
        unknown_creative_share=Decimal(unknown_creative_share),
    )
    return _world(
        name,
        linkedin=linkedin,
        hubspot=hubspot if hubspot is not None else HubSpotSpec(funnel=funnel),
        faults=faults,
    )


def _papers_context(
    *patches: CrmPatch,
    assets: tuple[ContentAsset, ...] = _PAPERS[:1],
    **extra: Any,
) -> ContextSpec:
    """A white paper registry (``guide-a`` by default) plus context-only CRM patches.

    The simulated world lists every form and override creative of the registry, so each
    scenario's registry names only the assets its world has. A world may add a form no asset
    lists (:data:`_UNLISTED_FORM`), whose leads reach an asset only through an override.
    """
    return ContextSpec(content_assets=assets, crm_patches=tuple(patches), **extra)


_TEN_LEADS: Final = dict.fromkeys(_offsets(-28, -19), 1)
"""Ten matured leads, one a day from -28 to -19."""
_QL_FUNNEL: Final = _funnel(ql_share=1, ql_delay_days=(1, 2))
_BOOKED_FUNNEL: Final = _funnel(
    meeting_share=1, meeting_delay_days=(1, 2), meeting_sources=(("MEETINGS_PUBLIC", 1),)
)


def _ql_after(creative: str, hours: int = 48) -> CrmPatch:
    """Every lead of ``creative`` reaches QL ``hours`` after its touch."""
    return CrmPatch(creative_key=creative, ql_after_touch_hours=hours)


def _cohort(a_cost: str = "50", d_cost: str = "300", *faults: Fault) -> WorldSpec:
    """Four assets with cost/QL 10, 30 and 50 on A, B, C (5 QL each) and none on D."""
    five = dict.fromkeys(_offsets(-28, -24), 1)
    papers = (
        _Paper("cr-a", _PAPER_FORMS["guide-a"], a_cost, five),
        _Paper("cr-b", _PAPER_FORMS["guide-b"], "150", five),
        _Paper("cr-c", _PAPER_FORMS["guide-c"], "250", five),
        _Paper("cr-d", _PAPER_FORMS["guide-d"], d_cost, five),
    )
    return _papers_world("w.cohort", papers, faults=faults)


_COHORT_CONTEXT: Final = _papers_context(
    _ql_after("cr-a"), _ql_after("cr-b"), _ql_after("cr-c"), assets=_PAPERS
)


def _one_paper(name: str, funnel: FunnelSpec, *faults: Fault, cost: str = "1000") -> WorldSpec:
    """One white paper creative with ten matured leads and the given CRM funnel."""
    paper = _Paper("cr-a", _PAPER_FORMS["guide-a"], cost, _TEN_LEADS)
    return _papers_world(name, (paper,), funnel=funnel, faults=faults)


def _white_paper_scenarios() -> Iterator[Scenario]:
    """Attribution chain, causal checks, stage binding, floors, credit and W1/W2."""
    module = MODULE_WHITE_PAPERS
    registry = _papers_context()
    yield from _causal_scenarios(module, registry)
    yield from _stage_scenarios(module, registry)
    yield from _floor_scenarios(module, registry)
    yield from _credit_scenarios(module, registry)
    yield from _w_rule_scenarios(module)


def _causal_scenarios(module: str, registry: ContextSpec) -> Iterator[Scenario]:
    """Causal ordering of MQL, QL, meetings and deals, and the meeting-time proxy."""
    events = _funnel(
        mql_share=1,
        ql_share=1,
        meeting_share=1,
        meeting_sources=(("MEETINGS_PUBLIC", 1),),
        deal_share=1,
        pre_touch_deal_share=1,
        mql_delay_days=(1, 3),
        ql_delay_days=(1, 3),
        meeting_delay_days=(1, 3),
        deal_delay_days=(2, 4),
    )
    yield from _pair(
        "wp.causal_pre_touch",
        module,
        "Causal check: MQL, QL, meetings and deals before the touch are excluded and reported",
        fires=(
            _one_paper("wp.causal_pre_touch", replace(events, pre_touch_share=Decimal(1))),
            Expectation(
                W1,
                False,
                {
                    "already_before_touch": 10,
                    "mql": 0,
                    "ql": 0,
                    "meetings_booked": 0,
                    "pre_touch_deals_excluded": 10,
                },
            ),
        ),
        does_not_fire=(
            _one_paper("wp.causal_pre_touch", events),
            Expectation(
                W1,
                False,
                {"already_before_touch": 0, "mql": 10, "ql": 10, "pre_touch_deals_excluded": 10},
            ),
        ),
        context=registry,
    )
    yield _case(
        "wp.meeting_on_touch",
        module,
        "Causal check: a meeting created on the touch instant is counted",
        _one_paper("wp.meeting_on_touch", _BOOKED_FUNNEL),
        Expectation(W1, False, {"meetings_on_touch_counted": True, "meetings_booked": 13}),
        _papers_context(CrmPatch(meeting_after_touch_hours=0)),
    )
    yield _case(
        "wp.meeting_starts_before_touch",
        module,
        "Causal check: a meeting created after the touch but starting before it is excluded",
        _one_paper(
            "wp.meeting_starts_before_touch",
            replace(_BOOKED_FUNNEL, meeting_created_after_start_share=Decimal(1)),
        ),
        Expectation(W1, False, {"meetings_booked": 0, "excluded_started_before_touch": 13}),
        registry,
    )
    yield _case(
        "wp.meeting_canceled",
        module,
        "Causal check: cancelled meetings are excluded",
        _one_paper("wp.meeting_canceled", replace(_BOOKED_FUNNEL, canceled_share=Decimal(1))),
        Expectation(W1, False, {"meetings_booked": 0, "canceled_excluded": 13}),
        registry,
    )
    yield _case(
        "wp.meeting_proxy",
        module,
        "Meeting proxy: a meeting from a non-booking source lands in ordering unverified, "
        "outside cost per meeting",
        _one_paper(
            "wp.meeting_proxy",
            replace(_BOOKED_FUNNEL, meeting_sources=(("CRM_UI", Decimal(1)),)),
        ),
        Expectation(
            W1,
            False,
            {"meetings_booked": 0, "other_meetings": 13, "cost_per_meeting": "—"},
            flags=(FLAG_MEETING_TIME_PROXY,),
        ),
        registry,
    )


def _stage_scenarios(module: str, registry: ContextSpec) -> Iterator[Scenario]:
    """Stage binding: MQL label resolution, skipped stages, independent QL and costs."""
    mql = _funnel(mql_share=1, mql_delay_days=(1, 2))
    yield from _pair(
        "wp.mql_label",
        module,
        "Stage binding: MQL resolves from its stage label / an unresolved label renders "
        "unavailable, not 0",
        fires=(
            _papers_world(
                "wp.mql_label",
                (_Paper("cr-a", _PAPER_FORMS["guide-a"], "1000", _TEN_LEADS),),
                hubspot=HubSpotSpec(funnel=mql),
            ),
            Expectation(W1, False, {"mql_label": "First meeting booked", "mql": 10}),
        ),
        does_not_fire=(
            _papers_world(
                "wp.mql_label",
                (_Paper("cr-a", _PAPER_FORMS["guide-a"], "1000", _TEN_LEADS),),
                hubspot=HubSpotSpec(funnel=mql, removed_stage_labels=("First meeting booked",)),
            ),
            Expectation(W1, False, {"mql": "unavailable"}),
        ),
        context=registry,
    )
    yield _case(
        "wp.skipped_stage",
        module,
        "Stage binding: a skipped stage is reported and not counted",
        _one_paper(
            "wp.skipped_stage",
            _funnel(mql_share=1, ql_share=1, skipped_stage_share=1, ql_delay_days=(1, 2)),
        ),
        Expectation(W1, False, {"skipped": 10, "mql": 0, "ql": 10}, finding=FINDING_SKIPPED_STAGE),
        registry,
    )
    yield _case(
        "wp.ql_independent",
        module,
        "Stage binding: QL is observed independently of MQL; MQL and QL are never combined",
        _one_paper("wp.ql_independent", _QL_FUNNEL),
        Expectation(W1, False, {"mql": 0, "ql": 10, "blended_fields": ()}),
        registry,
    )
    costs = replace(
        _QL_FUNNEL,
        meeting_share=Decimal(1),
        meeting_delay_days=(1, 2),
        meeting_sources=(("MEETINGS_PUBLIC", Decimal(1)),),
    )
    yield from _pair(
        "wp.costs",
        module,
        "Costs: cost per QL and per meeting arithmetic / QL = 0 renders an em dash",
        fires=(
            _one_paper("wp.costs", costs),
            Expectation(
                W1,
                False,
                {
                    "spend": "1000.00",
                    "ql": 10,
                    "cost_per_ql": "100.00",
                    "meetings_booked": 13,
                    "cost_per_meeting": "76.92",
                },
            ),
        ),
        does_not_fire=(
            _one_paper("wp.costs", replace(costs, ql_share=Decimal(0))),
            Expectation(W1, False, {"ql": 0, "cost_per_ql": "—", "reason": "no QL yet"}),
        ),
        context=registry,
    )


def _floor_scenarios(module: str, registry: ContextSpec) -> Iterator[Scenario]:
    """Contact, creative, unknown-evidence and orphan-deal floors."""
    # 1,000 leads: 50 a day on -28..-11, one on -10 and 99 on -9..-1. Form version 2 drops the
    # email question, so leads from its first day on are unmatched (100 = 10.0%, 99 = 9.9%).
    leads = dict.fromkeys(_offsets(-28, -11), 50) | {-10: 1} | dict.fromkeys(_offsets(-9, -1), 11)
    # Every matched lead (submitted on -10 at the latest) reaches MQL and QL, books a meeting
    # and opens a deal one to two days later, so every count and both cost denominators are
    # above 0 by the run.
    outcomes = _funnel(
        mql_share=1,
        ql_share=1,
        meeting_share=1,
        meeting_sources=(("MEETINGS_PUBLIC", 1),),
        deal_share=1,
        mql_delay_days=(1, 2),
        ql_delay_days=(1, 2),
        meeting_delay_days=(1, 2),
        deal_delay_days=(1, 2),
    )

    def floor_world(name: str, first_no_email_day: int, funnel: FunnelSpec) -> WorldSpec:
        form = _form(
            _PAPER_FORMS["guide-a"],
            "White paper form",
            FormVersionSpec(1, _questions(email=True)),
            FormVersionSpec(2, _questions(email=False), active_from_offset=first_no_email_day),
        )
        paper = _Paper("cr-a", _PAPER_FORMS["guide-a"], "5000", leads)
        return _papers_world(name, (paper,), funnel=funnel, forms=(form,))

    yield from _pair(
        "wp.contact_floor",
        module,
        "Floors: the contact floor at 10% renders counts as >= N and costs as <= X / not at 9.9%",
        fires=(
            floor_world("wp.contact_floor", -10, outcomes),
            Expectation(
                W1,
                False,
                {
                    "leads": 1000,
                    "unmatched": 100,
                    "contact_share": "0.10",
                    "spend": "5000.00",
                    "mql": "≥ 900",
                    "ql": "≥ 900",
                    "meetings_booked": "≥ 1099",
                    "deals": "≥ 900",
                    "cost_per_ql": "≤ 5.56",
                    "cost_per_meeting": "≤ 4.55",
                },
                flags=(FLAG_ATTRIBUTION_FLOOR,),
            ),
        ),
        does_not_fire=(
            floor_world("wp.contact_floor", -9, outcomes),
            Expectation(
                W1,
                False,
                {
                    "leads": 1000,
                    "unmatched": 99,
                    "contact_share": "0.099",
                    "spend": "5000.00",
                    "mql": 901,
                    "ql": 901,
                    "meetings_booked": 1100,
                    "deals": 901,
                    "cost_per_ql": "5.55",
                    "cost_per_meeting": "4.55",
                },
            ),
        ),
        context=registry,
    )
    yield _case(
        "wp.contact_floor_no_outcomes",
        module,
        "Floors: under the contact floor a zero QL or booked-meeting count renders its cost as "
        "an em dash, never a <= X ceiling",
        floor_world("wp.contact_floor_no_outcomes", -10, _FLAT_FUNNEL),
        Expectation(
            W1,
            False,
            {
                "leads": 1000,
                "unmatched": 100,
                "contact_share": "0.10",
                "ql": "≥ 0",
                "meetings_booked": "≥ 0",
                "cost_per_ql": "—",
                "cost_per_meeting": "—",
                "ql_cost_note": "no QL yet",
                "meeting_cost_note": "no meetings yet",
            },
            flags=(FLAG_ATTRIBUTION_FLOOR,),
        ),
        registry,
    )
    yield _case(
        "wp.creative_floor",
        module,
        "Floors: leads without a creative at 10% or more label the per-creative breakdown a floor",
        _papers_world(
            "wp.creative_floor",
            (_Paper("cr-a", _PAPER_FORMS["guide-a"], "1000", dict.fromkeys(_offsets(-28, -9), 2)),),
            unknown_creative_share="0.25",
        ),
        Expectation(W1, False, {"creative_floor": True}, flags=(FLAG_CREATIVE_FLOOR,)),
        registry,
    )
    deals_error = Fault(
        "hubspot",
        "associations_batch_read",
        "http_status",
        status=503,
        match={"object_type": "contacts", "to_object_type": "deals"},
    )
    yield from _pair(
        "wp.assoc_error",
        module,
        "Incomplete associations: an error association fetch makes the evidence unknown and "
        "withholds W2 and C1b / an ok_empty fetch is a genuine zero",
        fires=(
            _cohort("50", "300", deals_error),
            Expectation(
                W2,
                False,
                {"unknown_evidence_share": "1"},
                flags=(FLAG_UNKNOWN_CRM_EVIDENCE,),
                withheld_reason="unknown CRM evidence",
            ),
        ),
        does_not_fire=(
            _cohort("50", "300"),
            Expectation(
                W2, True, {"asset": "guide-d", "deals": 0, "association_status": "ok_empty"}
            ),
        ),
        context=_COHORT_CONTEXT,
    )
    deals = _funnel(deal_share=1, deal_delay_days=(2, 3))
    first_contact, first_deal = _first_person_ids()
    two_deals = replace(deals, pre_touch_deal_share=Decimal(1))
    yield _case(
        "wp.target_read_partial",
        module,
        "Target-read failures: a deal batch read that omits one deal makes target reads partial, "
        "the contact's evidence unknown, W2 and C1b withheld and the C2 veto unavailable",
        _one_paper(
            "wp.target_read_partial",
            two_deals,
            Fault(
                "hubspot",
                "objects_batch_read",
                "omit_ids",
                ids=(first_deal,),
                match={"object_type": "deals"},
            ),
        ),
        Expectation(
            W2,
            False,
            {"target_read_status": "partial", "missing_target_ids": (first_deal,)},
            flags=(FLAG_UNKNOWN_CRM_EVIDENCE,),
        ),
        registry,
    )
    yield _case(
        "wp.meeting_read_failed",
        module,
        "Target-read failures: a failed meeting read makes the evidence unknown",
        _one_paper(
            "wp.meeting_read_failed",
            _BOOKED_FUNNEL,
            Fault(
                "hubspot",
                "objects_batch_read",
                "http_status",
                status=503,
                match={"object_type": "meetings"},
            ),
        ),
        Expectation(W2, False, {"target_read_status": "error"}, flags=(FLAG_UNKNOWN_CRM_EVIDENCE,)),
        registry,
    )
    yield _case(
        "wp.contact_refresh_failed",
        module,
        "Target-read failures: a failed contact refresh makes stage counts unknown, never zero",
        _one_paper(
            "wp.contact_refresh_failed",
            _QL_FUNNEL,
            Fault(
                "hubspot",
                "objects_batch_read",
                "omit_ids",
                ids=(first_contact,),
                match={"object_type": "contacts"},
            ),
        ),
        Expectation(
            W1,
            False,
            {"contact_read_status": "error", "ql": "≥ 10"},
            flags=(FLAG_UNKNOWN_CRM_EVIDENCE,),
        ),
        registry,
    )
    yield _case(
        "wp.orphan_unread_target",
        module,
        "Target-read failures: an orphan-deal fetch with an unread target is excluded from the "
        "known scope",
        _one_paper(
            "wp.orphan_unread_target",
            deals,
            Fault(
                "hubspot",
                "objects_batch_read",
                "omit_ids",
                ids=(first_contact,),
                match={"object_type": "contacts"},
            ),
        ),
        Expectation(
            W1, False, {"window_deals": 10, "assessed_deals": 9, "unassessed": (first_deal,)}
        ),
        registry,
    )
    yield _case(
        "wp.orphan_share",
        module,
        "Orphan deals: the share is taken over the assessed scope",
        _papers_world(
            "wp.orphan_share",
            (_Paper("cr-a", _PAPER_FORMS["guide-a"], "1000", _TEN_LEADS),),
            hubspot=HubSpotSpec(funnel=deals, orphan_deals=10),
        ),
        Expectation(
            W1,
            False,
            {"orphan_deals": 3, "assessed_deals": 13, "orphan_share": "0.2308"},
            flags=(FLAG_DEAL_FLOOR,),
        ),
        registry,
    )
    yield _case(
        "wp.deal_search_incomplete",
        module,
        "Orphan deals: an incomplete deal search renders the share unknown",
        _one_paper("wp.deal_search_incomplete", deals),
        Expectation(W1, False, {"orphan_share": "unknown"}, flags=(FLAG_DEAL_FLOOR,)),
        replace(
            registry,
            freshness={
                "hubspot.deal_search": FreshnessOverride(
                    "incomplete", reason="result limit reached at finest partition"
                )
            },
        ),
    )


def _credit_scenarios(module: str, registry: ContextSpec) -> Iterator[Scenario]:
    """Credit and de-duplication, override precedence, currencies and asset overlap."""
    repeat = _funnel(repeat_person_share=1, ql_share=1, ql_delay_days=(5, 5))

    def touches_world(name: str, delay: int) -> WorldSpec:
        """One person touching cr-a1 on -28 and cr-a2 on -25; every event ``delay`` days later.

        Event delays run from the person's first touch, so 1 day puts MQL, QL, the booked
        meeting's creation and the deal between the two touches, and 5 days after both.
        """
        events = _funnel(
            repeat_person_share=1,
            mql_share=1,
            ql_share=1,
            meeting_share=1,
            meeting_sources=(("MEETINGS_PUBLIC", 1),),
            deal_share=1,
            mql_delay_days=(delay, delay),
            ql_delay_days=(delay, delay),
            meeting_delay_days=(delay, delay),
            deal_delay_days=(delay, delay),
        )
        return _papers_world(
            name,
            (
                _Paper("cr-a1", _PAPER_FORMS["guide-a"], "500", {-28: 1}),
                _Paper("cr-a2", _PAPER_FORMS["guide-a"], "500", {-25: 1}),
            ),
            funnel=events,
        )

    yield _case(
        "wp.credit_latest_touch",
        module,
        "Credit and de-duplication: MQL, QL, meetings and deals are credited to the latest prior "
        "touch's creative, with per-creative rows summing to the asset total",
        touches_world("wp.credit_latest_touch", 5),
        Expectation(
            W1,
            False,
            {
                "credited_creative": "cr-a2",
                "mql": 1,
                "ql": 1,
                "meetings_booked": 2,
                "deals": 1,
                "rows_sum_to_total": True,
            },
        ),
        registry,
    )
    yield _case(
        "wp.credit_between_touches",
        module,
        "Credit and de-duplication: MQL, QL, meetings and deals between a contact's two touches "
        "are credited to the earlier touch's creative, the latest touch at or before each event, "
        "not the contact's last touch",
        touches_world("wp.credit_between_touches", 1),
        Expectation(
            W1,
            False,
            {
                "credited_creative": "cr-a1",
                "last_touch_creative": "cr-a2",
                "mql": 1,
                "ql": 1,
                "meetings_booked": 2,
                "deals": 1,
                "rows_sum_to_total": True,
            },
        ),
        registry,
    )
    yield _case(
        "wp.meeting_two_contacts",
        module,
        "Credit and de-duplication: a meeting associated with two contacts is counted once",
        _one_paper(
            "wp.meeting_two_contacts",
            replace(_BOOKED_FUNNEL, two_contact_meeting_share=Decimal(1)),
        ),
        Expectation(W1, False, {"shared_meetings_counted_once": True}),
        registry,
    )
    yield _case(
        "wp.repeat_touch_one_mql",
        module,
        "Credit and de-duplication: repeat touches give one MQL",
        _papers_world(
            "wp.repeat_touch_one_mql",
            (_Paper("cr-a", _PAPER_FORMS["guide-a"], "1000", {-28: 1, -26: 1, -24: 1}),),
            funnel=_funnel(repeat_person_share=1, mql_share=1, mql_delay_days=(1, 1)),
        ),
        Expectation(W1, False, {"leads": 3, "contacts": 1, "mql": 1}),
        registry,
    )
    # The overridden creative's call-to-action form is listed under no asset, so the override
    # alone routes its spend and its leads to guide-c (check_overrides accepts it: the form
    # maps to no asset). Its leads reach QL, so their credit follows the lead to the asset.
    override = _asset("guide-c", _PAPER_FORMS["guide-c"], creatives=(_LANDING_CREATIVE,))
    yield _case(
        "wp.override_precedence",
        module,
        "Credit and de-duplication: the creative override applies to both spend and leads: the "
        "overridden creative's spend and the leads on its unlisted form resolve to the "
        "override's asset",
        _papers_world(
            "wp.override_precedence",
            (
                _Paper(
                    "cr-c", _PAPER_FORMS["guide-c"], "250", dict.fromkeys(_offsets(-28, -24), 1)
                ),
                _Paper(
                    "cr-override",
                    _UNLISTED_FORM,
                    "500",
                    dict.fromkeys(_offsets(-23, -21), 1),
                    creative_id=_LANDING_CREATIVE,
                ),
            ),
            funnel=_QL_FUNNEL,
        ),
        Expectation(
            W1,
            False,
            {
                "asset": "guide-c",
                "spend": "750.00",
                "leads": 8,
                "ql": 8,
                "override_creative": f"{CREATIVE_URN_PREFIX}{_LANDING_CREATIVE}",
                "override_spend": "500.00",
                "override_leads": 3,
                "override_ql": 3,
            },
        ),
        _papers_context(assets=(override,)),
    )
    conflicting = (
        _asset("guide-a", _PAPER_FORMS["guide-a"], creatives=(_LANDING_CREATIVE,)),
        _asset("guide-b", _PAPER_FORMS["guide-b"], creatives=(_LANDING_CREATIVE,)),
    )
    yield _case(
        "wp.override_conflict",
        module,
        "Credit and de-duplication: a conflicting override fails config load",
        _papers_world(
            "wp.override_conflict",
            (
                _Paper("cr-a", _PAPER_FORMS["guide-a"], "250", {-28: 1}),
                _Paper("cr-b", _PAPER_FORMS["guide-b"], "250", {-27: 1}),
                _Paper("cr-landing", None, "500", {}, creative_id=_LANDING_CREATIVE),
            ),
        ),
        Expectation(W1, False, {"raises": "ConfigError"}),
        _papers_context(assets=conflicting),
    )
    euro = _asset("guide-a", _PAPER_FORMS["guide-a"], _EU_FORM)
    yield _case(
        "wp.currency_cohorts",
        module,
        "Currency separation: two currencies give two tables, with no cross-currency total or "
        "median",
        _papers_world(
            "wp.currency_cohorts",
            (
                _Paper("cr-a", _PAPER_FORMS["guide-a"], "1000", _TEN_LEADS),
                _Paper("cr-a-eu", _EU_FORM, "800", _TEN_LEADS, campaign="li-eu"),
            ),
            funnel=_QL_FUNNEL,
        ),
        Expectation(
            W1,
            False,
            {"cohorts": ("EUR", "USD"), "rows_for_asset": 2, "cross_currency_total": False},
        ),
        _papers_context(assets=(euro,)),
    )
    yield _case(
        "wp.cross_asset_overlap",
        module,
        "Cross-asset: a contact who touched two assets is credited to both, with the any-touch "
        "overlap note",
        _papers_world(
            "wp.cross_asset_overlap",
            (
                _Paper("cr-a", _PAPER_FORMS["guide-a"], "500", {-28: 1}),
                _Paper("cr-b", _PAPER_FORMS["guide-b"], "500", {-26: 1}),
            ),
            funnel=repeat,
        ),
        Expectation(W1, False, {"note": "counts overlap across assets", "assets_credited": 2}),
        _papers_context(assets=_PAPERS[:2]),
    )


def _w_rule_scenarios(module: str) -> Iterator[Scenario]:
    """W1 double down (only unblocked campaigns) and W2 retire or rework."""
    yield from _pair(
        "w1.cohort",
        module,
        "W1: QL >= 3 at cost/QL 10 against a cohort median of 30 fires / 25 does not "
        "(0.8 x median = 24)",
        fires=(
            _cohort("50", "300"),
            Expectation(W1, True, {"asset": "guide-a", "cost_per_ql": "10.00", "median": "30.00"}),
        ),
        does_not_fire=(
            _cohort("125", "300"),
            Expectation(W1, False, {"asset": "guide-a", "cost_per_ql": "25.00", "median": "30.00"}),
        ),
        context=_COHORT_CONTEXT,
    )
    yield from _pair(
        "w2.cohort",
        module,
        "W2: QL 0 with spend 10 x the cohort median cost/QL fires / 2 x does not",
        fires=(
            _cohort("50", "300"),
            Expectation(W2, True, {"asset": "guide-d", "spend_to_median": "10"}),
        ),
        does_not_fire=(
            _cohort("50", "60"),
            Expectation(W2, False, {"asset": "guide-d", "spend_to_median": "2"}),
        ),
        context=_COHORT_CONTEXT,
    )
    five = dict.fromkeys(_offsets(-28, -24), 1)
    papers = (
        _Paper("cr-a", _PAPER_FORMS["guide-a"], "50", five),
        _Paper(
            "cr-a-hot",
            _PAPER_FORMS["guide-a"],
            "2716",
            dict.fromkeys(_offsets(-28, -3), 1),
            campaign="li-hot",
        ),
        _Paper("cr-b", _PAPER_FORMS["guide-b"], "750", five),
        _Paper("cr-c", _PAPER_FORMS["guide-c"], "1250", five),
        _Paper(
            "cr-cheap", _CHEAP_FORM, "500", dict.fromkeys(_WINDOW, 9) | {-1: 7}, campaign="li-cheap"
        ),
    )
    yield _case(
        "w1.unblocked_only",
        module,
        "W1 lists only the campaigns G1 leaves unblocked (the high-utilization, poor-efficiency "
        "campaign is left out)",
        _papers_world("w1.unblocked_only", papers),
        Expectation(
            W1,
            True,
            {
                "asset": "guide-a",
                "campaigns": ("Pipeline Report - Lead gen",),
                "blocked": ("Hot - Lead gen",),
            },
        ),
        _papers_context(
            _ql_after("cr-a"),
            _ql_after("cr-a-hot", 24),
            _ql_after("cr-b"),
            _ql_after("cr-c"),
            assets=_PAPERS[:3],
        ),
    )


# ---------------------------------------------------------------------------
# Precedence and suppression (AAP 0.5.4.2; acceptance/test_precedence.py)
# ---------------------------------------------------------------------------

_SEARCH_SUBJECT: Final = SubjectRef("google", "campaign", "search")
_TARGET_KEY: Final = f"core|{_TARGET_TERM}"


def _precedence_scenarios() -> Iterator[Scenario]:
    """P1 to P7, feedback suppression, historical-only items and deterministic ranking."""
    module = MODULE_PRECEDENCE
    k1_world = _keywords_world("precedence.k1", target=_target(61))
    plain = ContextSpec(content_assets=_NO_ASSETS)
    # Priority decision: the pause lands after the window (02:00 New York on the anchor day,
    # before FIXED_NOW), so the snapshots still raise the status discrepancy P1 holds on while
    # the window keeps the target's 61 clicks. A pause inside the window stops delivery from
    # its native day and drops the term below K1's n* of 61, so K1 would rightly not fire.
    paused = PlatformChange(
        "google", "campaign", "search", field="status", value="PAUSED", at_offset_hours=6
    )
    yield _case(
        "p1.holds",
        module,
        "P1: items depending on a discrepant campaign are held until it is resolved",
        k1_world.with_(platform_changes=(paused,)),
        Expectation(K1, True, {"held": True, "held_reason": HELD_DISCREPANCY_REASON}),
    )
    yield _case(
        "p1.post_run_discrepancy",
        module,
        "P1: a discrepancy detected after the run holds items at read time through the overlay, "
        "and the banner count updates at once",
        k1_world,
        Expectation(
            K1,
            True,
            {
                "held": False,
                "held_now": True,
                "open_discrepancies": 1,
                "held_reason_now": HELD_DISCREPANCY_REASON,
            },
            flags=(FLAG_UNVERIFIED_DISCREPANCIES,),
        ),
        replace(
            plain,
            post_run_discrepancies=(
                DiscrepancySpec(
                    _SEARCH_SUBJECT,
                    "status",
                    13,
                    decided_value={"status": "active"},
                    platform_value={"status": "paused"},
                ),
            ),
        ),
    )
    yield _case(
        "p1.c0_exempt",
        module,
        "P1: C0 items are exempt from holds and pinned first",
        k1_world.with_(platform_changes=(paused,)),
        Expectation(C0, True, {"held": False, "pinned": True, "kind": "status"}),
    )
    tracking_off = _spread(_WINDOW, cost="5600", impressions=56_000, clicks=5_600)
    yield from _pair(
        "p2.tracking_absent",
        module,
        "P2: spend with zero outcomes records conversion_tracking_absent and suppresses the "
        "zero-outcome rules (K1, C1, B2's zero-outcome branch, W2) / recorded outcomes do not",
        fires=(
            _keywords_world("p2.tracking_absent", base=tracking_off, target=_target(61)),
            Expectation(
                K1,
                False,
                {"suppressed_rules": ("K1", "C1", "B2", "W2"), "source": "google"},
                finding=FINDING_CONVERSION_TRACKING_ABSENT,
            ),
        ),
        does_not_fire=(
            _keywords_world("p2.tracking_absent", target=_target(61)),
            _k1_fires(61, 61, "61.00"),
        ),
    )
    yield _case(
        "p3.simulated",
        module,
        "P3: items on simulated data carry data_status simulated and the simulated flag",
        k1_world,
        Expectation(K1, True, {"data_status": "simulated"}, flags=(FLAG_SIMULATED,)),
    )
    yield _case(
        "p3.mixed",
        module,
        "P3: items joining a live LinkedIn dataset with simulated CRM data carry data_status mixed",
        _pipeline_world("p3.mixed", _lead_days(20, _MATURED)),
        Expectation(C1B, True, {"data_status": "mixed"}),
        replace(plain, modes={"linkedin": "live", "hubspot": "simulated"}),
    )
    stale_metrics = FreshnessOverride("complete", last_success_offset_hours=-30)
    yield _case(
        "p4.stale",
        module,
        "P4: an item is stale when a required dataset last succeeded over 24 hours ago",
        k1_world,
        Expectation(K1, True, {"stale_dataset": "google.metrics_daily"}, flags=(FLAG_STALE,)),
        replace(plain, freshness={"google.metrics_daily": stale_metrics}),
    )
    yield _case(
        "p4.incomplete",
        module,
        "P4: an item is incomplete when a required dataset is not complete over the window",
        k1_world,
        Expectation(
            K1, True, {"incomplete_dataset": "google.search_terms"}, flags=(FLAG_INCOMPLETE,)
        ),
        replace(
            plain,
            freshness={
                "google.search_terms": FreshnessOverride(
                    "incomplete", reason="result limit reached at finest partition"
                )
            },
        ),
    )
    yield _case(
        "p4.entity_sync_does_not_clear_metrics",
        module,
        "P4: a successful entity sync does not clear a stale metrics dataset",
        k1_world,
        Expectation(K1, True, {"stale_dataset": "google.metrics_daily"}, flags=(FLAG_STALE,)),
        replace(
            plain,
            freshness={
                "google.entities": FreshnessOverride("complete", last_success_offset_hours=-1),
                "google.metrics_daily": stale_metrics,
            },
        ),
    )
    # Priority decision: the target delivers its 61 clicks inside the 20-day custom window. The
    # 28-day k1_world spreads them over 28 days, leaving 45 in this window, below K1's n* of 61
    # (the 90-day baseline is unchanged), so K1 would rightly not fire.
    short_target = _spread(_offsets(-20, -1), cost="61", impressions=610, clicks=61)
    yield _case(
        "p5.configured_min",
        module,
        "P5: the short-window flag uses the configured minimum (window.trusted_min_days: 21 "
        "flags a 20-day window)",
        _keywords_world("p5.configured_min", target=short_target),
        Expectation(
            K1, True, {"window_days": 20, "trusted_min_days": 21}, flags=(FLAG_SHORT_WINDOW,)
        ),
        replace(
            plain,
            window=(-20, -1),
            kind="custom",
            threshold_overrides={"window.trusted_min_days": 21},
        ),
    )
    capped = _budget_world("p5.sustained_min", _capped(_offsets(-7, -1)))
    yield from _pair(
        "p5.sustained_min",
        module,
        "P5: sustained-pattern rules evaluate on a 7-day window / are withheld below 7 days",
        fires=(capped, Expectation(C2, True, {"window_days": 7}, flags=(FLAG_SHORT_WINDOW,))),
        does_not_fire=(
            capped,
            Expectation(
                C2, False, {"window_days": 6, "required": 7}, withheld_reason="window 6 days of 7"
            ),
        ),
        context=replace(plain, window=(-7, -1), kind="custom"),
        dnf_context=replace(plain, window=(-6, -1), kind="custom"),
    )
    yield _case(
        "p6.withheld_sizes",
        module,
        "P6: withheld lines carry their observed and required sample sizes",
        _keywords_world("p6.withheld_sizes", target=_target(60)),
        Expectation(
            K1,
            False,
            {"observed": 60, "required": 61, "unit": "clicks"},
            withheld_reason="clicks 60 of 61 required",
        ),
    )
    yield _case(
        "p7.no_winner_wording",
        module,
        "P7: no winner wording without distinguishability",
        _creative_world(
            "p7.no_winner_wording",
            (
                ("cr-a", _funnel_days(20_000, 200, 400, 200)),
                ("cr-b", _funnel_days(20_000, 200, 400, 180)),
            ),
        ),
        Expectation(CR1, False, {"winner_wording": False}, flags=(FLAG_NOT_DISTINGUISHABLE,)),
    )
    for kind, hours in (("dismiss", 24 * 30), ("snooze", 24 * 7), ("done", 24 * 30)):
        yield _case(
            f"feedback.{kind}",
            module,
            f"Feedback: {kind} suppresses the rule and entity, with the count stored in "
            "run_suppression",
            k1_world,
            Expectation(K1, False, {"suppressed": {kind: 1}, "hidden_by": kind}),
            replace(
                plain,
                feedback=(
                    FeedbackSpec(K1, cast(Any, kind), "google", "search_term", _TARGET_KEY, hours),
                ),
            ),
        )
    linkedin_item = _pipeline_world("historical", _lead_days(20, _MATURED))
    replaced = replace(plain, provenance_replaced=True)
    yield _case(
        "historical.activation",
        module,
        "Historical-only items: after a LinkedIn dataset activation and before the rerun, items "
        "computed from the old dataset are historical with every action disabled",
        linkedin_item,
        Expectation(
            C1B, True, {"historical": True, "held_now": True, "held_reason_now": HISTORICAL_REASON}
        ),
        replaced,
    )
    yield _case(
        "historical.custom_run",
        module,
        "Historical-only items: an older custom run stays historical indefinitely",
        linkedin_item,
        Expectation(C1B, True, {"historical": True, "run_kind": "custom"}),
        replace(replaced, kind="custom", window=(-28, -1)),
    )
    yield _case(
        "historical.feedback_409",
        module,
        "Historical-only items: feedback on a historical item returns 409 subject_retired",
        linkedin_item,
        Expectation(C1B, True, {"feedback_status": 409, "error": "subject_retired"}),
        replaced,
    )
    yield _case(
        "historical.provenance_frozen",
        module,
        "Historical-only items: a card computed from simulated data keeps simulated provenance "
        "after LinkedIn goes live, while data_status_now reports live",
        linkedin_item,
        Expectation(C1B, True, {"provenance_mode": "simulated", "data_status_now": "live"}),
        replaced,
    )
    tie = GoogleSearchTermSpec("free crm tools", "core", "kw-crm", profile=_target(70, cost="70"))
    yield _case(
        "ranking.deterministic",
        module,
        "Ranking: ties are broken by entity name, then id, so runs are deterministic",
        _keywords_world("ranking.deterministic", target=_target(70, cost="70"), extra_terms=(tie,)),
        Expectation(K1, True, {"order": (_TARGET_TERM, "free crm tools"), "n_star": 62}),
    )


# ---------------------------------------------------------------------------
# Transport-level integration cases (integration/test_*.py)
# ---------------------------------------------------------------------------


def _lead_response_id(creative_key: str, offset: int, index: int = 0) -> str:
    """The id the world gives the ``index``-th lead of ``creative_key`` on day ``offset``.

    Computed exactly as the generator seeds response ids at :data:`DEFAULT_SEED` (the seed
    every catalogue world runs with), so lead-level faults can name a response before any
    world is built; ``integration/test_lead_matching.py`` and :func:`context_from_world`
    (which honours these faults) confirm the ids against the built world.
    """
    day = (FIXED_ANCHOR + timedelta(days=offset)).isoformat()
    digest = hashlib.sha256(
        f"{DEFAULT_SEED}:lead-id:{creative_key}:{day}:{index}".encode()
    ).hexdigest()
    return (
        f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
        f"-{int(digest[32], 16) % 10}"
    )


def _first_person_ids() -> tuple[str, str]:
    """The contact id and first deal id of the first person in a one-creative catalogue world.

    In the :func:`_one_paper` and :func:`_sources_world` worlds the only creative is the first
    of the spec, and its first lead (the only response on day -28, see :data:`_TEN_LEADS`)
    creates the first person. The ids derive from those coordinates alone
    (:func:`~ads_engine.simulation.generator.person_contact_id`), so, like
    :func:`_lead_response_id`, faults and expectations can name them before any world is
    built; the catalogue tests confirm them through the faults that name them.
    """
    contact = person_contact_id(0, FIXED_ANCHOR + timedelta(days=-28), 0)
    return contact, person_object_id("deals", contact, 0)


def _sources_world(name: str, *faults: Fault, funnel: FunnelSpec = _FLAT_FUNNEL) -> WorldSpec:
    """Every source: the K1 search campaign, the capped LinkedIn campaign and a CRM."""
    google = cast(GoogleSpec, _keywords_world(name, target=_target(61)).google)
    campaign = _li_campaign("li-pipeline", "Pipeline Report - Lead gen")
    creative = _li_creative("cr-pipeline", "li-pipeline", _leadgen(_TEN_LEADS))
    return _world(
        name,
        google=google,
        linkedin=_linkedin((campaign,), (creative,)),
        hubspot=HubSpotSpec(funnel=funnel),
        faults=faults,
    )


def _transport(
    name: str, module: str, guarantee: str, spec: WorldSpec, **values: object
) -> Scenario:
    """A transport-level case no rule evaluates (``rule_id=None``)."""
    return _case(name, module, guarantee, spec, Expectation(None, True, values))


def _integration_scenarios() -> Iterator[Scenario]:
    """Source fallback, lead matching and per-surface connector cases."""
    yield from _fallback_scenarios(MODULE_SYNC_FALLBACK)
    yield from _lead_matching_scenarios(MODULE_LEAD_MATCHING)
    yield from _google_client_scenarios(MODULE_GOOGLE_CLIENT)
    yield from _linkedin_client_scenarios(MODULE_LINKEDIN_CLIENT)
    yield from _hubspot_client_scenarios(MODULE_HUBSPOT_CLIENT)


def _fallback_scenarios(module: str) -> Iterator[Scenario]:
    """Access unavailable or denied, recovery, transient and defect classification."""
    yield _transport(
        "fallback.missing_credentials",
        module,
        "Access unavailable: missing environment switches every source to simulated under auto",
        _sources_world("fallback.missing_credentials"),
        mode="simulated",
        reason="credentials not configured",
    )
    yield _transport(
        "fallback.http_403",
        module,
        "Access denied: a 401/403 switches the source to simulated with the platform's code",
        _sources_world(
            "fallback.http_403", Fault("linkedin", "ad_account", "http_status", status=403)
        ),
        source="linkedin",
        classification="access_denied",
        mode="simulated",
    )
    yield _transport(
        "fallback.n8n_200_permission_denied",
        module,
        "Access denied: HTTP 200 from n8n carrying PERMISSION_DENIED switches Google to simulated",
        _sources_world(
            "fallback.n8n_200_permission_denied",
            Fault("google", "search", "body_error", code="PERMISSION_DENIED"),
        ),
        source="google",
        http_status=200,
        classification="access_denied",
        mode="simulated",
    )
    yield _transport(
        "fallback.forced_simulated",
        module,
        "Forced simulation: DATA_MODE=simulated never probes or promotes, even when probes pass",
        _sources_world("fallback.forced_simulated"),
        data_mode="simulated",
        probes=0,
        promoted=False,
    )
    yield _transport(
        "fallback.leadsync_403_only",
        module,
        "Partial permissions: LinkedIn ads readable while Lead Sync returns 403 stay simulated, "
        "with no dataset flip",
        _sources_world(
            "fallback.leadsync_403_only",
            Fault("linkedin", "lead_form_responses", "http_status", status=403),
        ),
        source="linkedin",
        mode="simulated",
        flips=0,
    )
    yield _transport(
        "fallback.recovery_all_pass",
        module,
        "Recovery: when every capability passes, a live dataset is built and activated only after "
        "its backfill",
        _sources_world("fallback.recovery_all_pass"),
        probe="all pass",
        activated_after_backfill=True,
    )
    yield _transport(
        "fallback.build_failure",
        module,
        "Recovery: a build failure discards the building dataset and leaves the simulated one "
        "active",
        _sources_world(
            "fallback.build_failure", Fault("linkedin", "ad_analytics", "http_status", status=500)
        ),
        building_discarded=True,
        active_mode="simulated",
    )
    for name, fault in (
        (
            "fallback.transient_429",
            Fault("linkedin", "ad_campaigns", "http_status", status=429, retry_after=1),
        ),
        (
            "fallback.transient_5xx",
            Fault("hubspot", "objects_batch_read", "http_status", status=503),
        ),
        ("fallback.timeout", Fault("google", "search", "timeout")),
    ):
        yield _transport(
            name,
            module,
            "Transient: 429, 5xx and timeouts retry three times with backoff, then the affected "
            "datasets turn stale; never mock",
            _sources_world(name, fault),
            attempts=3,
            stale=True,
            mock=False,
        )
    yield _transport(
        "fallback.defect_400",
        module,
        "Defects: a 400 fails the job with its query id, with no retry and no mock",
        _sources_world(
            "fallback.defect_400",
            Fault("google", "search", "http_status", status=400, match={"query_id": "Q-G3"}),
        ),
        job="failed",
        retried=False,
        query_id="Q-G3",
    )
    yield _transport(
        "fallback.call_budget_deferral",
        module,
        "Call budget: calls beyond GOOGLE_DAILY_CALL_BUDGET are deferred and the datasets stay "
        "incomplete until they finish",
        _world(
            "fallback.call_budget_deferral",
            google=GoogleSpec(
                bulk=GoogleBulkSpec(campaigns=25_000), conversion_actions=(_LAST_CLICK,)
            ),
        ),
        call_budget=3,
        deferred=True,
        completeness="incomplete",
    )
    yield _case(
        "fallback.companion",
        module,
        "Companion CRM dataset: LinkedIn simulated while HubSpot is live reads the companion's "
        "matches, contacts and freshness; the Google-to-CRM link reads the live primary",
        _sources_world("fallback.companion"),
        Expectation(None, True, {"linkedin_crm_role": "companion", "google_crm_mode": "live"}),
        ContextSpec(content_assets=_NO_ASSETS, modes={"hubspot": "live"}),
    )
    yield _case(
        "fallback.isolation",
        module,
        "Isolation: simulated HubSpot contacts never collide with live ids and leave every "
        "computation when the live dataset activates",
        _sources_world("fallback.isolation"),
        Expectation(None, True, {"reserved_contact_ids": True, "collisions": 0}),
        ContextSpec(content_assets=_NO_ASSETS, modes={"linkedin": "live"}),
    )


def _lead_matching_scenarios(module: str) -> Iterator[Scenario]:
    """Durable lead-to-contact matching: retries, staging and terminal cases."""
    first = _lead_response_id("cr-pipeline", -28)
    yield _transport(
        "leadmatch.late_contact",
        module,
        "Late contacts: a contact created two days after the lead is matched on retry",
        _sources_world(
            "leadmatch.late_contact", funnel=replace(_FLAT_FUNNEL, late_contact_share=Decimal(1))
        ),
        first_attempt="no_match",
        matched_on_retry=True,
        contact_delay_days=2,
    )
    yield _transport(
        "leadmatch.hubspot_5xx_then_ok",
        module,
        "Transient failure: a HubSpot 5xx leaves the row error, then it is matched",
        _sources_world(
            "leadmatch.hubspot_5xx_then_ok",
            Fault(
                "hubspot",
                "objects_batch_read",
                "http_status",
                status=503,
                times=1,
                match={"object_type": "contacts"},
            ),
        ),
        first_attempt="error",
        then="matched",
    )
    yield _transport(
        "leadmatch.staged_recovery",
        module,
        "Staged CRM recovery: every backfill-range lead is attempted in the building dataset; "
        "activation is refused while any row is pending or a hubspot.* freshness row is unknown",
        _sources_world("leadmatch.staged_recovery"),
        activation_refused_while_pending=True,
        reset_to_pending=0,
    )
    for name, kind, status, reason in (
        (
            "leadmatch.form_map_mismatch",
            "decoration_mismatch",
            "no_email",
            _REASON_FORM_MAP_MISMATCH,
        ),
        (
            "leadmatch.unknown_form_version",
            "no_decoration",
            "no_email",
            _REASON_UNKNOWN_FORM_VERSION,
        ),
    ):
        yield _case(
            name,
            module,
            f"Lead matching: {reason} ends the lead as {status}",
            _sources_world(
                name, Fault("linkedin", "lead_form_responses", cast(Any, kind), ids=(first,))
            ),
            Expectation(None, True, {"response_id": first, "status": status, "reason": reason}),
        )
    yield _case(
        "leadmatch.email_unretrievable",
        module,
        "Terminal cases: an email no longer retrievable ends as error and counts unresolved",
        _sources_world(
            "leadmatch.email_unretrievable",
            Fault("linkedin", "lead_form_responses_batch", "omit_ids", ids=(first,)),
        ),
        Expectation(
            None,
            True,
            {"response_id": first, "status": "error", "reason": _REASON_EMAIL_UNRETRIEVABLE},
        ),
    )


def _google_client_scenarios(module: str) -> Iterator[Scenario]:
    """Keyset enumeration, partitioning, page tokens and the call budget."""

    def bulk(name: str, changes: Iterable[PlatformChange] = (), **sizes: Any) -> WorldSpec:
        spec = GoogleSpec(
            budgets=(_SEARCH_BUDGET,),
            campaigns=(
                GoogleCampaignSpec(
                    "removed-mid",
                    "Removed mid-window",
                    "search-budget",
                    profile=_daily(_WINDOW, _metrics("5.00", 50, 5, "1")),
                ),
            ),
            conversion_actions=(_LAST_CLICK,),
            bulk=GoogleBulkSpec(**{key: value for key, value in sizes.items() if key != "tokens"}),
            emit_page_tokens=bool(sizes.get("tokens", False)),
        )
        return _world(name, google=spec, changes=changes)

    yield _transport(
        "google.keyset_25000",
        module,
        "Keyset enumeration: a Q-G2 scan over 25,000 campaigns runs pages of 9,999, 9,999 and "
        "5,002 rows with strictly increasing campaign.id filters",
        bulk("google.keyset_25000", campaigns=24_999),
        pages=(9_999, 9_999, 5_002),
        campaigns=25_000,
    )
    yield _transport(
        "google.keyset_ad_groups",
        module,
        "Keyset enumeration: Q-G4 runs the same keyset scan over ad groups",
        bulk("google.keyset_ad_groups", campaigns=1, ad_groups_per_campaign=25_000),
        pages=(9_999, 9_999, 5_002),
        ad_groups=25_000,
    )
    removal = PlatformChange(
        "google",
        "campaign",
        "removed-mid",
        field="status",
        value="REMOVED",
        at_offset_hours=-5 * _HOURS_PER_DAY,
    )
    yield _transport(
        "google.dated_overflow",
        module,
        "Dated partitions: exactly 10,000 rows with no token are halved by date, then by "
        "campaign.id ranges that also cover a campaign removed mid-window, then by ad_group.id",
        bulk("google.dated_overflow", (removal,), campaigns=5_000),
        splits=("date", "campaign.id", "ad_group.id"),
        removed_campaign_covered=True,
    )
    yield _transport(
        "google.qg5_keyset",
        module,
        "Dated partitions: a single ad group and day of Q-G5 at 10,000 rows is read by criterion "
        "keyset",
        bulk(
            "google.qg5_keyset",
            campaigns=1,
            ad_groups_per_campaign=1,
            keywords_in_one_ad_group=10_000,
        ),
        keyset="ad_group_criterion.criterion_id",
    )
    yield _transport(
        "google.qg6_finest_incomplete",
        module,
        "Dated partitions: a single (day, ad group) of Q-G6 at 10,000 rows is stored and marked "
        "incomplete",
        bulk(
            "google.qg6_finest_incomplete",
            campaigns=1,
            ad_groups_per_campaign=1,
            search_term_rows_per_day=10_000,
        ),
        completeness="incomplete",
        reason="result limit reached at finest partition",
    )
    yield _transport(
        "google.qg7_15000_keywords",
        module,
        "Q-G7: ad-group range halving, then criterion keyset in a 15,000-keyword ad group",
        bulk(
            "google.qg7_15000_keywords",
            campaigns=1,
            ad_groups_per_campaign=1,
            keywords_in_one_ad_group=15_000,
        ),
        keyset_pages=(9_999, 5_001),
    )
    yield _transport(
        "google.token_followed",
        module,
        "Request: a nextPageToken is followed at once when the response carries one",
        bulk("google.token_followed", campaigns=12_000, tokens=True),
        page_token_followed=True,
    )
    yield _transport(
        "google.budget_exhaustion",
        module,
        "Budget: exhausting the call budget mid-scan or mid-partition leaves the dataset "
        "incomplete",
        bulk("google.budget_exhaustion", campaigns=24_999),
        call_budget=2,
        completeness="incomplete",
    )


def _linkedin_client_scenarios(module: str) -> Iterator[Scenario]:
    """Analytics element cap, next links and all-status entity listing."""
    many = tuple(
        _li_creative(
            f"cr-bulk-{index:03d}",
            "li-bulk",
            _daily(_offsets(-180, -1), DayMetrics(cost=Decimal("1.00"), impressions=10, clicks=1)),
            form=None,
        )
        for index in range(100)
    )
    campaign = _li_campaign("li-bulk", "Bulk creatives - Website visits", objective="WEBSITE_VISIT")
    yield _transport(
        "linkedin.analytics_cap",
        module,
        "Analytics: a response at the 15,000-element cap is split again by halving its date range",
        _world("linkedin.analytics_cap", linkedin=_linkedin((campaign,), many, forms=())),
        elements_cap=15_000,
        split="date_range",
        creatives=100,
        days=180,
    )
    yield _transport(
        "linkedin.next_link",
        module,
        "Analytics: a next paging link is followed when present",
        _world(
            "linkedin.next_link",
            linkedin=replace(_linkedin((campaign,), many, forms=()), analytics_next_links=True),
        ),
        next_followed=True,
        elements_cap=15_000,
        creatives=100,
        days=180,
    )
    statuses: tuple[LinkedInCampaignStatus, ...] = (
        "ACTIVE",
        "PAUSED",
        "ARCHIVED",
        "CANCELED",
        "DRAFT",
        "PENDING_DELETION",
        "COMPLETED",
    )
    listed = tuple(
        replace(_li_campaign(f"li-{status.lower()}", f"Campaign {status}"), status=status)
        for status in statuses
    )
    yield _transport(
        "linkedin.all_status",
        module,
        "Forms and entities: campaigns are listed in every status, so the tracked set is diffed "
        "against the full list",
        _world("linkedin.all_status", linkedin=_linkedin(listed, (), forms=())),
        statuses=statuses,
    )


def _hubspot_client_scenarios(module: str) -> Iterator[Scenario]:
    """CRM Search overflow, association continuation and 207s, target reads, Google discovery."""
    yield _transport(
        "hubspot.search_overflow",
        module,
        "Search overflow: a day with total 12,000 is split into hourly partitions; an hour above "
        "10,000 is incomplete",
        _sources_world("hubspot.search_overflow").with_(
            hubspot=HubSpotSpec(
                funnel=_FLAT_FUNNEL,
                bulk_deals=BulkDealSpec(-3, 12_000, peak_hour=12, peak_hour_count=10_500),
            )
        ),
        day_total=12_000,
        hourly=True,
        completeness="incomplete",
    )
    booked = _sources_world("hubspot.assoc_continuation", funnel=_BOOKED_FUNNEL)
    yield _transport(
        "hubspot.assoc_continuation",
        module,
        "Associations contract: a continuation re-sends {id, after} only for inputs that returned "
        "paging.next.after",
        booked.with_hubspot(association_page_size=1),
        continuation=True,
    )
    contact = _first_person_ids()[0]
    yield _transport(
        "hubspot.assoc_207",
        module,
        "Associations contract: a 207 with errors[] records those inputs as error",
        _sources_world(
            "hubspot.assoc_207",
            Fault(
                "hubspot",
                "associations_batch_read",
                "batch_errors",
                ids=(contact,),
                category="VALIDATION_ERROR",
            ),
        ),
        status=207,
        fetch_status="error",
        object_id=contact,
    )
    yield _transport(
        "hubspot.non_complete",
        module,
        "Associations contract: a status other than COMPLETE is a failed call",
        _sources_world(
            "hubspot.non_complete",
            Fault(
                "hubspot",
                "associations_batch_read",
                "non_complete_status",
                batch_status="PROCESSING",
            ),
        ),
        failed_call=True,
    )
    deal = _first_person_ids()[1]
    yield _transport(
        "hubspot.target_omitted",
        module,
        "Target reads: a meeting or deal batch read that omits an id sets target_read_status "
        "partial",
        _sources_world(
            "hubspot.target_omitted",
            Fault(
                "hubspot",
                "objects_batch_read",
                "omit_ids",
                ids=(deal,),
                match={"object_type": "deals"},
            ),
            funnel=_funnel(deal_share=1, pre_touch_deal_share=1, deal_delay_days=(2, 3)),
        ),
        target_read_status="partial",
        missing_target_ids=(deal,),
    )
    yield _transport(
        "hubspot.google_discovery",
        module,
        "Google contacts: discovery runs only when both Google properties are configured and "
        "verified",
        _sources_world("hubspot.google_discovery").with_(
            hubspot=HubSpotSpec(
                funnel=_FLAT_FUNNEL,
                google_crm_properties=("google_campaign_id", "google_ads_touch_at"),
                google_touch_contacts=20,
            )
        ),
        google_link="available",
        discovered_contacts=20,
    )


# ---------------------------------------------------------------------------
# The default world and the catalogue
# ---------------------------------------------------------------------------


def _default_scenario() -> Scenario:
    """The first-start world: every section populates in the end-to-end simulated run."""
    return Scenario(
        name=DEFAULT_SCENARIO,
        module=MODULE_END_TO_END,
        guarantee=(
            "Simulated run: with DATA_MODE=simulated and no credentials, sync, analysis and UI "
            "produce all five sections (Keywords K1/K2/K3; Campaigns C1, C3a, C3b; Budget B4 "
            "and withheld B1-B3 lines; Creative CR1-CR3; White papers with W1), each item "
            "showing evidence, window, source boundaries and simulated labels"
        ),
        variant="case",
        pair=None,
        spec=DEFAULT_SPEC,
        context=ContextSpec(),
        expect=Expectation(
            None,
            True,
            {"populated_sections": SECTION_ORDER, "data_status": "simulated"},
            flags=(FLAG_SIMULATED,),
        ),
    )


def _build_catalogue() -> Mapping[str, Scenario]:
    """Every scenario, keyed by name, in topic order.

    Raises:
        ValueError: When two scenarios share a name.
    """
    found: dict[str, Scenario] = {}
    duplicates: list[str] = []
    topics: tuple[Callable[[], Iterator[Scenario]], ...] = (
        _keyword_scenarios,
        _campaign_scenarios,
        _budget_scenarios,
        _creative_scenarios,
        _white_paper_scenarios,
        _precedence_scenarios,
        _integration_scenarios,
    )
    for scenario in (_default_scenario(), *(item for topic in topics for item in topic())):
        if scenario.name in found:
            duplicates.append(scenario.name)
        found[scenario.name] = scenario
    if duplicates:
        raise ValueError(f"duplicate scenario names: {', '.join(sorted(set(duplicates)))}")
    return MappingProxyType(found)


def _catalogue_problems(scenarios: Mapping[str, Scenario]) -> list[str]:
    """Every structural problem of a catalogue (see :func:`validate_catalogue`)."""
    problems: list[str] = []
    for name, scenario in scenarios.items():
        if name != scenario.name:
            problems.append(f"{name!r} is keyed under another name than {scenario.name!r}")
        if scenario.variant not in _VARIANTS:
            problems.append(f"{name}: unknown variant {scenario.variant!r}")
        if scenario.module not in MODULES:
            problems.append(f"{name}: unknown module {scenario.module!r}")
        if name != DEFAULT_SCENARIO and name.rsplit(".", 1)[-1] != (
            "case" if scenario.variant == "case" else scenario.variant
        ):
            problems.append(f"{name}: the name must end in its variant {scenario.variant!r}")
        if scenario.variant == "case":
            if scenario.pair is not None:
                problems.append(f"{name}: a case has no pair")
            continue
        twin = scenarios.get(scenario.pair or "")
        if twin is None:
            problems.append(f"{name}: pair {scenario.pair!r} does not resolve")
            continue
        wanted = "does_not_fire" if scenario.variant == "fires" else "fires"
        if twin.variant != wanted or twin.pair != name:
            problems.append(f"{name}: pair {twin.name!r} is not its {wanted} twin")
        if twin.module != scenario.module:
            problems.append(f"{name}: pair {twin.name!r} runs in another module")
    covered_modules = {scenario.module for scenario in scenarios.values()}
    problems.extend(
        f"module {module} has no scenario" for module in MODULES if module not in covered_modules
    )
    covered_rules = {s.expect.rule_id for s in scenarios.values() if s.expect.rule_id is not None}
    problems.extend(
        f"rule {rule} appears in no expectation"
        for rule in _REQUIRED_RULES
        if rule not in covered_rules
    )
    return problems


def validate_catalogue(scenarios: Mapping[str, Scenario] | None = None) -> None:
    """Check the catalogue's structure (called once at import; builds no world).

    Every ``fires`` scenario has a ``does_not_fire`` twin naming it back (and vice versa) in
    the same module, pair names resolve, names end in their variant, every module of
    :data:`MODULES` has at least one scenario, every rule id K1 .. W2 and G1 appears in some
    expectation, and names are unique (enforced while the catalogue is built).

    Args:
        scenarios: The catalogue to check; :data:`SCENARIOS` when ``None``.

    Raises:
        ValueError: Listing every problem found.
    """
    problems = _catalogue_problems(SCENARIOS if scenarios is None else scenarios)
    if problems:
        raise ValueError("invalid scenario catalogue: " + "; ".join(problems))


def pairs() -> Iterator[tuple[Scenario, Scenario]]:
    """Every ``(fires, does_not_fire)`` twin pair, in name order.

    Example:
        >>> fires, twin = next(pairs())
        >>> (fires.variant, twin.variant)
        ('fires', 'does_not_fire')
    """
    for name in sorted(SCENARIOS):
        scenario = SCENARIOS[name]
        if scenario.variant == "fires" and scenario.pair is not None:
            yield scenario, SCENARIOS[scenario.pair]


def for_module(module: str) -> tuple[Scenario, ...]:
    """The scenarios a test module runs, in name order (for ``pytest.mark.parametrize``).

    Args:
        module: A path under ``backend/tests/``, for example :data:`MODULE_KEYWORDS`.

    Raises:
        ValueError: When ``module`` is not one of :data:`MODULES`.
    """
    if module not in MODULES:
        raise ValueError(f"unknown test module {module!r}; expected one of {', '.join(MODULES)}")
    return tuple(SCENARIOS[name] for name in sorted(SCENARIOS) if SCENARIOS[name].module == module)


SCENARIOS: Final[Mapping[str, Scenario]] = _build_catalogue()
"""The catalogue: ``default`` and every ``"<topic>.<case>.<variant>"`` scenario (specs only)."""

validate_catalogue()
