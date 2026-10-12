"""The rule contract: shared constants, the analysis context and the evidence types.

AAP 0.5.1 defines the rule contract without naming its file, so it lives here: each rule module
exposes ``evaluate(ctx: AnalysisContext) -> RuleResult``, and a :class:`RuleResult` carries
:class:`Candidate` items, :class:`Finding` observations, :class:`Withheld` patterns and
suppression counts. Rule modules, ``precedence.py``, ``runner.py``, ``overlay.py`` and the
simulation scenarios import their contract types from this module, never from each other, so
the analysis package has no import cycles.

The module holds four kinds of definitions:

* **Constants** (AAP 0.5.3, 0.5.5, 0.11.1): rule ids (``K1`` … ``W2``), the five sections and
  their groups, flag codes, finding codes, data statuses, suppression reasons, banner codes and
  tones, objective groups and the fixed hold, unverified and historical texts, with
  :func:`format_instant`, the one rendering of an instant inside those texts (in
  ``DISPLAY_TIMEZONE``). Rule ids are the same strings as the ``config/thresholds.yaml``
  namespaces, the evidence, the tests and ``docs/recommendation-rules.md``.
* **Output types**: :class:`EntityRef`, :class:`DecisionSubject`, :class:`Flag`,
  :class:`Metric`, :class:`Baseline`, :class:`Sample`, :class:`TestResult`,
  :class:`ProvenanceEntry`, :class:`DatasetStatus`, :class:`Evidence`, :class:`Candidate`,
  :class:`Finding`, :class:`Withheld` and :class:`RuleResult`. Each ``to_json()`` renders the
  AAP 0.5.3 schema exactly, with ``Decimal`` as fixed-point strings and dates as ISO strings.
* **Input types**: the entities, daily facts, observations, leads, CRM records, decisions,
  discrepancies and feedback a rule reads, already aggregated by ``metrics.load_context``.
* :class:`AnalysisContext`: one immutable bundle of those inputs for one run, plus
  :meth:`AnalysisContext.make_evidence`, through which every rule builds its evidence so the
  window, thresholds, dataset statuses and frozen provenance are recorded identically, and
  :func:`google_link_gap` with :func:`no_crm_link_flag`, through which every rule decides
  whether its Google items carry ``no_crm_link`` and with which text (AAP 0.3.3, 0.5.4.1).

Value conventions (decision 6): money, conversions, rates and shares are ``Decimal``; instants
are tz-aware ``datetime``; daily facts carry each source's native ``date`` (Google in the
account's time zone, LinkedIn in UTC) and are never rebucketed. A missing value is ``None``
("unavailable"), never 0.

Dataset isolation (decision 8): a context holds only rows of each source's active primary
dataset, plus the effective CRM dataset for LinkedIn-attributed evidence (the companion while
LinkedIn is simulated), so live and simulated rows never join within one source.

Purity: dataclasses, constants and pure helpers only. No database, network, settings or
logging access, and no import of another analysis module except ``thresholds``, ``window`` and
``shortfall`` (the dated Q-G6 reason grammar :class:`Freshness` judges a window by).

Example:
    >>> ref = EntityRef("google", "campaign", 7, "123", "Brand")
    >>> ref.key
    'google:campaign:123'
    >>> Metric("spend", "Spend", Decimal("12.340000"), "currency", currency="USD").to_json()
    {'key': 'spend', 'label': 'Spend', 'value': '12.340000', 'unit': 'currency', 'currency': 'USD'}
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, ClassVar, Final, Literal, Protocol, get_args, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ads_engine.analysis.shortfall import shortfall_spans, shortfall_within
from ads_engine.analysis.thresholds import ContentAsset, CrmBindings, Thresholds
from ads_engine.analysis.window import UTC_BOUNDARY, AnalysisWindow
from ads_engine.db.models.analysis import DATA_STATUSES, FEEDBACK_KINDS, RECOMMENDATION_SECTIONS
from ads_engine.db.models.ops import DATASETS, SOURCES

__all__ = [
    "ALERT_BANNERS",
    "B1",
    "B2",
    "B3",
    "B4",
    "BANNER_INCOMPLETE_DATASETS",
    "BANNER_OPEN_DISCREPANCIES",
    "BANNER_ORDER",
    "BANNER_SHORT_WINDOW",
    "BANNER_SIMULATED_SOURCES",
    "BANNER_STALE_DATASETS",
    "BANNER_TONES",
    "BANNER_TRACKING_ABSENT",
    "BUDGET",
    "C0",
    "C1",
    "C1B",
    "C2",
    "C3A",
    "C3B",
    "CAMPAIGNS",
    "COHORT_GROUP_PREFIX",
    "CR1",
    "CR2",
    "CR3",
    "CREATIVE",
    "CREATIVE_GROUP_PREFIX",
    "CRM_OBJECT_TYPES",
    "DATA_LIVE",
    "DATA_MIXED",
    "DATA_SIMULATED",
    "DATA_STALE",
    "DATA_STATUS_VALUES",
    "ENTITY_ACCOUNT",
    "ENTITY_AD_GROUP",
    "ENTITY_BUDGET",
    "ENTITY_CAMPAIGN",
    "ENTITY_CONTENT_ASSET",
    "ENTITY_CREATIVE",
    "ENTITY_CREATIVE_PAIR",
    "ENTITY_KEYWORD",
    "ENTITY_SEARCH_TERM",
    "ENTITY_SEGMENT",
    "ENTITY_TERM_CLUSTER",
    "ENTITY_TYPES",
    "EXTRA_HEADLINE_METRIC",
    "FEEDBACK_SUPPRESSION",
    "FINDING_CODES",
    "FINDING_CONVERSION_TRACKING_ABSENT",
    "FINDING_INCOMPLETE_DATASET",
    "FINDING_K1_NO_BASELINE",
    "FINDING_NO_CRM_LINK",
    "FINDING_PIPELINE_UNAVAILABLE",
    "FINDING_SEARCH_TERM_COVERAGE",
    "FINDING_SEGMENT_WINDOW_UNAVAILABLE",
    "FINDING_SKIPPED_STAGE",
    "FLAG_ATTRIBUTION_FLOOR",
    "FLAG_CODES",
    "FLAG_CREATIVE_FLOOR",
    "FLAG_DEAL_FLOOR",
    "FLAG_INCOMPLETE",
    "FLAG_MEETING_TIME_PROXY",
    "FLAG_NOT_DISTINGUISHABLE",
    "FLAG_NO_CRM_LINK",
    "FLAG_PIPELINE_UNAVAILABLE",
    "FLAG_SEARCH_TERM_COVERAGE",
    "FLAG_SHARED_BUDGET",
    "FLAG_SHORT_WINDOW",
    "FLAG_SIMULATED",
    "FLAG_STALE",
    "FLAG_UNKNOWN_CRM_EVIDENCE",
    "FLAG_UNVERIFIED_DISCREPANCIES",
    "FULL_STATE_DATASETS",
    "G1",
    "GROUP_BLOCK",
    "GROUP_CHANGES",
    "GROUP_DISCREPANCIES",
    "GROUP_EXPAND",
    "GROUP_LAUNCH",
    "GROUP_ORDER",
    "GROUP_PAUSE",
    "GROUP_REVIEW",
    "GROUP_SCALE",
    "GROUP_TITLES",
    "HELD_DISCREPANCY_REASON",
    "HISTORICAL_REASON",
    "K1",
    "K2",
    "K3",
    "KEYWORDS",
    "LINKEDIN_LEAD_GEN_OBJECTIVE",
    "OBJECTIVE_GROUPS",
    "OBJ_GOOGLE",
    "OBJ_LINKEDIN_LEAD_GEN",
    "OBJ_LINKEDIN_OTHER",
    "OUTCOME_FIELDS",
    "OUTCOME_LABELS",
    "RULE_IDS",
    "SECTION_ORDER",
    "SECTION_TITLES",
    "SUPPRESSION_REASONS",
    "SUPPRESS_FEEDBACK_DISMISS",
    "SUPPRESS_FEEDBACK_DONE",
    "SUPPRESS_FEEDBACK_SNOOZE",
    "SUPPRESS_G1_BLOCKED",
    "SUPPRESS_TRACKING_ABSENT",
    "TONES",
    "UNVERIFIED_TEMPLATE",
    "W1",
    "W2",
    "WHITE_PAPERS",
    "AccountInfo",
    "AdGroupInfo",
    "AnalysisContext",
    "AssociationFetchInfo",
    "AssociationStatus",
    "Baseline",
    "BudgetInfo",
    "BudgetPeriod",
    "CampaignGroupInfo",
    "CampaignInfo",
    "CampaignStatus",
    "Candidate",
    "Completeness",
    "ContactInfo",
    "ContactReadStatus",
    "ConversionActionInfo",
    "CreativeInfo",
    "CreativeTotals",
    "CrmData",
    "CrmScope",
    "DatasetMode",
    "DatasetRef",
    "DatasetRole",
    "DatasetStatus",
    "DayFact",
    "DealInfo",
    "DecisionInfo",
    "DecisionOrigin",
    "DecisionShape",
    "DecisionSubject",
    "DiscrepancyInfo",
    "DiscrepancyKind",
    "EntityRef",
    "Evidence",
    "FeedbackInfo",
    "FeedbackKind",
    "Finding",
    "Flag",
    "Freshness",
    "KeywordDay",
    "KeywordInfo",
    "LeadInfo",
    "MatchStatus",
    "MeetingInfo",
    "Metric",
    "Number",
    "Observation",
    "OpenDiscrepancyState",
    "Projection",
    "ProvenanceEntry",
    "RateBaseline",
    "RuleResult",
    "RunKind",
    "Sample",
    "SearchTermRow",
    "SegmentRow",
    "Source",
    "SubjectKey",
    "SubjectType",
    "SupportsToJson",
    "TargetReadStatus",
    "TestResult",
    "Tone",
    "W",
    "Withheld",
    "cohort_group",
    "cohort_group_currency",
    "content_asset_external_id",
    "creative_group",
    "creative_group_campaign",
    "creative_pair_external_id",
    "display_zone",
    "format_instant",
    "google_link_gap",
    "json_value",
    "keyword_external_id",
    "no_crm_link_flag",
    "objective_group",
    "search_term_external_id",
    "segment_external_id",
    "short_window_message",
    "term_cluster_external_id",
    "unverified_message",
]

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Source = Literal["google", "linkedin", "hubspot"]
"""An external data source (``db.models.ops.SOURCES``)."""

SubjectType = Literal["campaign", "budget"]
"""The kind of entity a decision or discrepancy is about (AAP 0.5.2)."""

SubjectKey = tuple[SubjectType, int]
"""A decision subject as ``(subject_type, internal id)`` of the active dataset."""

CrmScope = Literal["linkedin", "google"]
"""Which CRM dataset a piece of evidence reads.

``linkedin``: the effective CRM dataset of the LinkedIn dataset in use (the companion while
LinkedIn is simulated). ``google``: HubSpot's active primary dataset (AAP 0.3.3).
"""

Number = Decimal | int
"""A count or an exact amount; never ``float``."""

DatasetMode = Literal["live", "simulated"]
"""Whether a dataset holds real platform data or seeded mock data."""

DatasetRole = Literal["primary", "companion"]
"""A source's own dataset, or the simulated CRM companion of a LinkedIn dataset."""

Completeness = Literal["complete", "incomplete", "unknown"]
"""Completeness of a dataset over its covered range (``dataset_freshness.completeness``)."""

RunKind = Literal["default", "custom"]
"""``default``: the worker's run of the configured default window; ``custom``: any other."""

CampaignStatus = Literal["active", "paused", "ended"]
"""A campaign's normalised status."""

BudgetPeriod = Literal["DAILY", "CUSTOM_PERIOD"]
"""A budget's period: a daily cap or a lifetime/custom-period amount."""

DecisionShape = Literal["own_budget", "shared_member", "budget"]
"""The shape of a decision subject's expected state (AAP 0.5.2)."""

DecisionOrigin = Literal["baseline", "user", "recommendation", "resolution"]
"""Who or what wrote a decision row."""

DiscrepancyKind = Literal["status", "budget", "untracked", "missing"]
"""The kinds of C0 discrepancy (AAP 0.5.4.4)."""

OpenDiscrepancyState = Literal["open", "awaiting_platform"]
"""The unresolved discrepancy states a context holds."""

FeedbackKind = Literal["dismiss", "snooze", "done"]
"""Feedback on an item (``db.models.analysis.FEEDBACK_KINDS``)."""

MatchStatus = Literal["pending", "matched", "no_match", "no_email", "error"]
"""A ``lead_match`` row's status in one CRM dataset."""

ContactReadStatus = Literal["ok", "error"]
"""Whether a matched contact's latest refresh read it."""

AssociationStatus = Literal["ok", "ok_empty", "partial", "error"]
"""Outcome of one (object, target type) association fetch."""

TargetReadStatus = Literal["ok", "partial", "error"]
"""Outcome of reading the targets of a non-empty association fetch."""

Tone = Literal["neutral", "info", "warning", "critical", "success"]
"""A banner or badge tone (AAP 0.4.2 ``Badge``/``Banner``)."""

# ---------------------------------------------------------------------------
# Rule ids (AAP 0.5.4, 0.11.1)
# ---------------------------------------------------------------------------

K1: Final = "K1"
"""Block / negative-keyword candidate: spend with exactly zero conversions."""
K2: Final = "K2"
"""Expansion candidate: a converting search term at or better than campaign efficiency."""
K3: Final = "K3"
"""Keyword review: declining quality score, or CPC rising without a matching conversion rise."""
C0: Final = "C0"
"""Discrepancy: platform state differs from the last decision; pinned first, never held."""
C1: Final = "C1"
"""Pause: steady spend with no output over a real window."""
C1B: Final = "C1b"
"""Pipeline pause (LinkedIn): leads not becoming Qualified Leads."""
C2: Final = "C2"
"""Scale or Hold: cap-constrained with good output, judged against the pipeline veto."""
C3A: Final = "C3a"
"""Launch idea: an underused but proven demographic segment (LinkedIn)."""
C3B: Final = "C3b"
"""Launch idea: a converting keyword cluster without a matching campaign (Google)."""
B1: Final = "B1"
"""Budget increase for a C2 "Scale" outcome."""
B2: Final = "B2"
"""Do not increase: high utilization with poor efficiency."""
B3: Final = "B3"
"""Under-utilized budget: lower it, freeing amount for B1 receivers."""
B4: Final = "B4"
"""Weekday / weekend pattern, gated by sample size and a Poisson rate test."""
CR1: Final = "CR1"
"""Creative distinguishability: Holm-adjusted pairwise tests."""
CR2: Final = "CR2"
"""Weak funnel stage: Wilson interval below the account baseline."""
CR3: Final = "CR3"
"""Next test: the single variable to change for the weak stage."""
W1: Final = "W1"
"""White paper double down: cost per Qualified Lead well below the cohort median."""
W2: Final = "W2"
"""White paper retire or rework: spend with no Qualified Lead."""
W: Final = "W"
"""A white-paper asset row with no W1/W2 action: "No change warranted" or "Insufficient data"."""
G1: Final = "G1"
"""Guard: no spend increase on a high-utilization, poor-efficiency target."""

RULE_IDS: Final[tuple[str, ...]] = (
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
    W,
    G1,
)
"""Every rule id, in catalogue order."""

# ---------------------------------------------------------------------------
# Sections and groups (AAP 0.5.3, 0.5.4.4, 0.5.5)
# ---------------------------------------------------------------------------

KEYWORDS: Final = "keywords"
"""Section: Keywords to adjust (Google)."""
CAMPAIGNS: Final = "campaigns"
"""Section: Campaigns to pause or launch."""
BUDGET: Final = "budget"
"""Section: Budget changes."""
CREATIVE: Final = "creative"
"""Section: Copy/creative changes (LinkedIn)."""
WHITE_PAPERS: Final = "white_papers"
"""Section: White papers."""

SECTION_ORDER: Final[tuple[str, ...]] = RECOMMENDATION_SECTIONS
"""The five sections in the fixed order the API returns and the UI renders them."""

SECTION_TITLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        KEYWORDS: "Keywords to adjust",
        CAMPAIGNS: "Campaigns to pause or launch",
        BUDGET: "Budget changes",
        CREATIVE: "Copy/creative changes",
        WHITE_PAPERS: "White papers",
    }
)
"""Display title of each section, in the user's wording."""

GROUP_BLOCK: Final = "block"
"""Keywords group: block / negative-keyword candidates (K1)."""
GROUP_EXPAND: Final = "expand"
"""Keywords group: expansion candidates (K2)."""
GROUP_REVIEW: Final = "review"
"""Keywords group: keywords flagged for review (K3)."""
GROUP_DISCREPANCIES: Final = "discrepancies"
"""Campaigns group: discrepancies to resolve first (C0)."""
GROUP_PAUSE: Final = "pause"
"""Campaigns group: pause candidates (C1, C1b)."""
GROUP_SCALE: Final = "scale"
"""Campaigns group: scale or hold (C2)."""
GROUP_LAUNCH: Final = "launch"
"""Campaigns group: launch ideas (C3a, C3b)."""
GROUP_CHANGES: Final = "changes"
"""Budget group: every budget item (B1-B4)."""

CREATIVE_GROUP_PREFIX: Final = "campaign:"
"""Prefix of a creative group key: one group per campaign."""
COHORT_GROUP_PREFIX: Final = "cohort:"
"""Prefix of a white-paper group key: one group per currency cohort."""

GROUP_ORDER: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        KEYWORDS: (GROUP_BLOCK, GROUP_EXPAND, GROUP_REVIEW),
        CAMPAIGNS: (GROUP_DISCREPANCIES, GROUP_PAUSE, GROUP_SCALE, GROUP_LAUNCH),
        BUDGET: (GROUP_CHANGES,),
        CREATIVE: (),
        WHITE_PAPERS: (),
    }
)
"""The static groups of each section, in display order.

Creative and white-paper groups are dynamic (:func:`creative_group`, :func:`cohort_group`), so
their tuples are empty and ``precedence.py`` orders them itself.
"""

GROUP_TITLES: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        KEYWORDS: MappingProxyType(
            {
                GROUP_BLOCK: "Block / negative-keyword candidates",
                GROUP_EXPAND: "Expansion candidates",
                GROUP_REVIEW: "Review",
            }
        ),
        CAMPAIGNS: MappingProxyType(
            {
                GROUP_DISCREPANCIES: "Discrepancies",
                GROUP_PAUSE: "Pause",
                GROUP_SCALE: "Scale",
                GROUP_LAUNCH: "Launch ideas",
            }
        ),
        BUDGET: MappingProxyType({GROUP_CHANGES: "Budget changes"}),
        CREATIVE: MappingProxyType({}),
        WHITE_PAPERS: MappingProxyType({}),
    }
)
"""Display title of each static group, per section, in :data:`GROUP_ORDER` order."""


def creative_group(campaign_id: int) -> str:
    """Return the creative-section group key of one campaign: ``campaign:{id}``.

    AAP 0.5.4.6: copy analysis is shown as one block per campaign.

    Raises:
        TypeError: When ``campaign_id`` is not an ``int`` (``bool`` is rejected).
    """
    if isinstance(campaign_id, bool) or not isinstance(campaign_id, int):
        raise TypeError(f"campaign_id must be an int, found {campaign_id!r}")
    return f"{CREATIVE_GROUP_PREFIX}{campaign_id}"


def cohort_group(currency: str) -> str:
    """Return the white-paper group key of one currency cohort: ``cohort:{CUR}``.

    AAP 0.5.4.7: every asset row, total and median sits within one currency, so each cohort is
    its own group.

    Raises:
        ValueError: When ``currency`` is not a non-blank string.
    """
    if not isinstance(currency, str) or not currency.strip():
        raise ValueError(f"currency must be a non-blank currency code, found {currency!r}")
    return f"{COHORT_GROUP_PREFIX}{currency.strip().upper()}"


def creative_group_campaign(group: str) -> int | None:
    """Return the campaign id of a :func:`creative_group` key, or ``None`` for any other key."""
    if not group.startswith(CREATIVE_GROUP_PREFIX):
        return None
    tail = group.removeprefix(CREATIVE_GROUP_PREFIX)
    return int(tail) if tail.isdigit() else None


def cohort_group_currency(group: str) -> str | None:
    """Return the currency of a :func:`cohort_group` key, or ``None`` for any other key."""
    if not group.startswith(COHORT_GROUP_PREFIX):
        return None
    return group.removeprefix(COHORT_GROUP_PREFIX) or None


# ---------------------------------------------------------------------------
# Flag and finding codes (AAP 0.5.3)
# ---------------------------------------------------------------------------

FLAG_SIMULATED: Final = "simulated"
"""Data state: built from simulated data (P3)."""
FLAG_STALE: Final = "stale"
"""Data state: a required dataset is stale (P4), "Data as of …"."""
FLAG_INCOMPLETE: Final = "incomplete"
"""Data state: a required dataset is not complete over the window (P4)."""
FLAG_SHORT_WINDOW: Final = "short_window"
"""Data state: the window is shorter than ``window.trusted_min_days`` (P5)."""
FLAG_UNVERIFIED_DISCREPANCIES: Final = "unverified_discrepancies"
"""Discrepancies: "Unverified while N discrepancies are open" (P1)."""
FLAG_ATTRIBUTION_FLOOR: Final = "attribution_floor"
"""Attribution: 10% or more of leads unmatched in HubSpot; counts are floors."""
FLAG_CREATIVE_FLOOR: Final = "creative_floor"
"""Attribution: leads without a creative; the per-creative breakdown is a floor."""
FLAG_DEAL_FLOOR: Final = "deal_floor"
"""Attribution: orphan or unassessed deals; deal counts are floors."""
FLAG_UNKNOWN_CRM_EVIDENCE: Final = "unknown_crm_evidence"
"""Attribution: some matched contacts' CRM evidence could not be read."""
FLAG_MEETING_TIME_PROXY: Final = "meeting_time_proxy"
"""Attribution: meeting booking time proxied by record creation (ordering unverified)."""
FLAG_PIPELINE_UNAVAILABLE: Final = "pipeline_unavailable"
"""Pipeline: the CRM pipeline veto could not be evaluated."""
FLAG_NO_CRM_LINK: Final = "no_crm_link"
"""Pipeline: the Google-to-CRM link is not configured or not verified."""
FLAG_NOT_DISTINGUISHABLE: Final = "not_distinguishable"
"""Statistics: creative variants are not yet distinguishable (P7)."""
FLAG_SEARCH_TERM_COVERAGE: Final = "search_term_coverage"
"""Statistics: search-term spend covers less than 80% of keyword spend."""
FLAG_SHARED_BUDGET: Final = "shared_budget"
"""Budget scope: the subject is a Google explicitly shared budget."""

FLAG_CODES: Final[tuple[str, ...]] = (
    FLAG_SIMULATED,
    FLAG_STALE,
    FLAG_INCOMPLETE,
    FLAG_SHORT_WINDOW,
    FLAG_UNVERIFIED_DISCREPANCIES,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_CREATIVE_FLOOR,
    FLAG_DEAL_FLOOR,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    FLAG_MEETING_TIME_PROXY,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_NO_CRM_LINK,
    FLAG_NOT_DISTINGUISHABLE,
    FLAG_SEARCH_TERM_COVERAGE,
    FLAG_SHARED_BUDGET,
)
"""Every flag code of AAP 0.5.3 ``Flag``, in the UI's badge order."""

FINDING_NO_CRM_LINK: Final = "no_crm_link"
"""Finding: Google items have no CRM link."""
FINDING_SEARCH_TERM_COVERAGE: Final = "search_term_coverage"
"""Finding: search-term coverage of keyword spend for the Keywords section."""
FINDING_K1_NO_BASELINE: Final = "k1_no_baseline"
"""Finding: K1 withheld account-wide for want of a usable conversion-rate baseline."""
FINDING_SEGMENT_WINDOW_UNAVAILABLE: Final = "segment_window_unavailable"
"""Finding: C3a withheld because no segment data matches the run's window."""
FINDING_PIPELINE_UNAVAILABLE: Final = "pipeline_unavailable"
"""Finding: the pipeline is unavailable for an entity, with the reason."""
FINDING_SKIPPED_STAGE: Final = "skipped_stage"
"""Finding: contacts skipped a bound stage and are not counted."""
FINDING_CONVERSION_TRACKING_ABSENT: Final = "conversion_tracking_absent"
"""Finding: a source spent with zero outcomes; zero-outcome rules suppressed (P2)."""
FINDING_INCOMPLETE_DATASET: Final = "incomplete_dataset"
"""Finding: a dataset a section uses is not complete over the window."""

FINDING_CODES: Final[tuple[str, ...]] = (
    FINDING_NO_CRM_LINK,
    FINDING_SEARCH_TERM_COVERAGE,
    FINDING_K1_NO_BASELINE,
    FINDING_SEGMENT_WINDOW_UNAVAILABLE,
    FINDING_PIPELINE_UNAVAILABLE,
    FINDING_SKIPPED_STAGE,
    FINDING_CONVERSION_TRACKING_ABSENT,
    FINDING_INCOMPLETE_DATASET,
)
"""Every finding code of AAP 0.5.3 ``Finding``."""

# ---------------------------------------------------------------------------
# Data statuses and suppression reasons (AAP 0.3.3, 0.5.3)
# ---------------------------------------------------------------------------

DATA_LIVE: Final = "live"
"""Item computed from live data only."""
DATA_SIMULATED: Final = "simulated"
"""Item computed from simulated data only."""
DATA_STALE: Final = "stale"
"""Item computed from live data of which a required dataset is stale."""
DATA_MIXED: Final = "mixed"
"""Item built on a join between a live and a simulated dataset."""

DATA_STATUS_VALUES: Final[tuple[str, ...]] = (DATA_LIVE, DATA_SIMULATED, DATA_STALE, DATA_MIXED)
"""The values of ``recommendation.data_status``; equal to ``db.models.analysis.DATA_STATUSES``."""

SUPPRESS_FEEDBACK_DISMISS: Final = "feedback_dismiss"
"""Suppression: hidden by an active dismiss."""
SUPPRESS_FEEDBACK_SNOOZE: Final = "feedback_snooze"
"""Suppression: hidden by an active snooze."""
SUPPRESS_FEEDBACK_DONE: Final = "feedback_done"
"""Suppression: hidden by an active "done"."""
SUPPRESS_G1_BLOCKED: Final = "g1_blocked"
"""Suppression: a spend increase blocked by the G1 guard."""
SUPPRESS_TRACKING_ABSENT: Final = "tracking_absent"
"""Suppression: a zero-outcome rule suppressed by the P2 tracking finding."""

SUPPRESSION_REASONS: Final[tuple[str, ...]] = (
    SUPPRESS_FEEDBACK_DISMISS,
    SUPPRESS_FEEDBACK_SNOOZE,
    SUPPRESS_FEEDBACK_DONE,
    SUPPRESS_G1_BLOCKED,
    SUPPRESS_TRACKING_ABSENT,
)
"""Every ``run_suppression.reason`` the runner writes."""

FEEDBACK_SUPPRESSION: Final[Mapping[str, str]] = MappingProxyType(
    {
        "dismiss": SUPPRESS_FEEDBACK_DISMISS,
        "snooze": SUPPRESS_FEEDBACK_SNOOZE,
        "done": SUPPRESS_FEEDBACK_DONE,
    }
)
"""The suppression reason each feedback kind (``FEEDBACK_KINDS``) is counted under."""

# ---------------------------------------------------------------------------
# Banners (AAP 0.5.5 fixed order)
# ---------------------------------------------------------------------------

TONES: Final[tuple[Tone, ...]] = get_args(Tone)
"""Every tone, as the design-system ``Badge`` and ``Banner`` wrappers accept them."""

BANNER_OPEN_DISCREPANCIES: Final = "open_discrepancies"
"""Banner: unresolved discrepancies, with the live count; the only ``alert`` banner."""
BANNER_TRACKING_ABSENT: Final = "tracking_absent"
"""Banner: a source spent with no recorded outcomes (P2)."""
BANNER_SIMULATED_SOURCES: Final = "simulated_sources"
"""Banner: the simulated sources, each with its reason (P3)."""
BANNER_STALE_DATASETS: Final = "stale_datasets"
"""Banner: datasets that are stale now."""
BANNER_INCOMPLETE_DATASETS: Final = "incomplete_datasets"
"""Banner: datasets that are not complete."""
BANNER_SHORT_WINDOW: Final = "short_window"
"""Banner: the window is below the trusted minimum (P5)."""

BANNER_ORDER: Final[tuple[str, ...]] = (
    BANNER_OPEN_DISCREPANCIES,
    BANNER_TRACKING_ABSENT,
    BANNER_SIMULATED_SOURCES,
    BANNER_STALE_DATASETS,
    BANNER_INCOMPLETE_DATASETS,
    BANNER_SHORT_WINDOW,
)
"""The fixed order global banners render in."""

BANNER_TONES: Final[Mapping[str, Tone]] = MappingProxyType(
    {
        BANNER_OPEN_DISCREPANCIES: "critical",
        BANNER_TRACKING_ABSENT: "warning",
        BANNER_SIMULATED_SOURCES: "info",
        BANNER_STALE_DATASETS: "warning",
        BANNER_INCOMPLETE_DATASETS: "warning",
        BANNER_SHORT_WINDOW: "warning",
    }
)
"""The tone of each banner code."""

ALERT_BANNERS: Final[frozenset[str]] = frozenset({BANNER_OPEN_DISCREPANCIES})
"""Banners rendered with the ``alert`` role: discrepancies only (AAP 0.5.5)."""

# ---------------------------------------------------------------------------
# Objective groups (AAP 0.5.4.1 "Outcome by source")
# ---------------------------------------------------------------------------

OBJ_GOOGLE: Final = "google"
"""Google campaigns; the outcome is ``metrics.conversions``."""
OBJ_LINKEDIN_LEAD_GEN: Final = "linkedin_lead_gen"
"""LinkedIn ``LEAD_GENERATION`` campaigns; the outcome is ``oneClickLeads``."""
OBJ_LINKEDIN_OTHER: Final = "linkedin_other"
"""Other LinkedIn objectives; the outcome is ``externalWebsiteConversions``."""

OBJECTIVE_GROUPS: Final[tuple[str, ...]] = (OBJ_GOOGLE, OBJ_LINKEDIN_LEAD_GEN, OBJ_LINKEDIN_OTHER)
"""Every objective group; comparisons stay within one source, group and currency."""

LINKEDIN_LEAD_GEN_OBJECTIVE: Final = "LEAD_GENERATION"
"""The LinkedIn ``objectiveType`` whose outcome is native leads."""

OUTCOME_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        OBJ_GOOGLE: "conversions",
        OBJ_LINKEDIN_LEAD_GEN: "leads",
        OBJ_LINKEDIN_OTHER: "website_conversions",
    }
)
"""The :class:`DayFact` / :class:`CreativeTotals` attribute holding each group's outcome."""

OUTCOME_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        OBJ_GOOGLE: "conversions",
        OBJ_LINKEDIN_LEAD_GEN: "leads",
        OBJ_LINKEDIN_OTHER: "website conversions",
    }
)
"""The plural display noun of each group's outcome, for reason sentences."""


def objective_group(source: str, objective_or_channel: str | None) -> str:
    """Return the objective group of a campaign (AAP 0.5.4.1 "Outcome by source").

    Args:
        source: ``google`` or ``linkedin``.
        objective_or_channel: The campaign's stored ``objective_or_channel``: the Google
            advertising channel type or the LinkedIn ``objectiveType``.

    Returns:
        :data:`OBJ_GOOGLE` for every Google campaign; :data:`OBJ_LINKEDIN_LEAD_GEN` for a
        LinkedIn ``LEAD_GENERATION`` campaign; :data:`OBJ_LINKEDIN_OTHER` otherwise.

    Raises:
        ValueError: When ``source`` is not an ad source.

    Example:
        >>> objective_group("linkedin", "LEAD_GENERATION")
        'linkedin_lead_gen'
    """
    if source == "google":
        return OBJ_GOOGLE
    if source == "linkedin":
        objective = (objective_or_channel or "").strip().upper()
        if objective == LINKEDIN_LEAD_GEN_OBJECTIVE:
            return OBJ_LINKEDIN_LEAD_GEN
        return OBJ_LINKEDIN_OTHER
    raise ValueError(f"objective groups exist only for google and linkedin, found {source!r}")


# ---------------------------------------------------------------------------
# Fixed texts (AAP 0.3.2, 0.5.4.2)
# ---------------------------------------------------------------------------

HELD_DISCREPANCY_REASON: Final = "Resolve discrepancy first"
"""``held_reason`` of an item that depends on a discrepant campaign or budget (P1)."""

UNVERIFIED_TEMPLATE: Final = "Unverified while {n} discrepancies are open"
"""Message of the ``unverified_discrepancies`` flag; ``{n}`` is every unresolved one.

The plural template; :func:`unverified_message` renders the singular "Unverified while 1
discrepancy is open" at a count of one.
"""

HISTORICAL_REASON: Final = "Computed from data that has since been replaced"
"""``held_reason_now`` of an item whose provenance datasets are no longer active."""


def unverified_message(n: int) -> str:
    """Render the ``unverified_discrepancies`` message for ``n`` unresolved discrepancies.

    Every count but one renders :data:`UNVERIFIED_TEMPLATE` verbatim; a count of one reads
    "Unverified while 1 discrepancy is open". The compute-time flag (``precedence.py``) and
    the read-time flag (``overlay.py``) both render here, so their wording never differs.

    Example:
        >>> unverified_message(1)
        'Unverified while 1 discrepancy is open'
        >>> unverified_message(3)
        'Unverified while 3 discrepancies are open'
    """
    if n == 1:
        return f"Unverified while {n} discrepancy is open"
    return UNVERIFIED_TEMPLATE.format(n=n)


def short_window_message(days: int, trusted_min_days: int) -> str:
    """Render the short-window text of the P5 banner and the ``short_window`` flag.

    "Window of N days is below the {trusted_min_days}-day trusted minimum", with "1 day" for
    a one-day window; the "{trusted_min_days}-day" adjective never takes a plural. The banner
    (``overlay.py``, from the run's stored minimum) and the flag (``precedence.py``, from the
    window's configured minimum) both render here, so they never drift apart.

    Args:
        days: The window's length in days.
        trusted_min_days: The ``window.trusted_min_days`` the window was judged against.

    Example:
        >>> short_window_message(1, 14)
        'Window of 1 day is below the 14-day trusted minimum'
        >>> short_window_message(20, 21)
        'Window of 20 days is below the 21-day trusted minimum'
    """
    noun = "day" if days == 1 else "days"
    return f"Window of {days} {noun} is below the {trusted_min_days}-day trusted minimum"


# ---------------------------------------------------------------------------
# Instants in message text (AAP 0.5.5 Behaviour, 0.5.4.1)
# ---------------------------------------------------------------------------

_MONTH_ABBREVIATIONS: Final[tuple[str, ...]] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
"""English month abbreviations, fixed so a message never depends on the process locale."""


def display_zone(zone: object, what: str = "display timezone") -> ZoneInfo:
    """Return the IANA zone that ``zone`` names, the ``DISPLAY_TIMEZONE`` instants are shown in.

    The id must load exactly as given, surrounding whitespace included, as ``settings.py``
    stores ``DISPLAY_TIMEZONE`` once validated. The zone only formats instants; it never moves
    a source's day boundary (AAP 0.5.4.1).

    Args:
        zone: The IANA zone id, for example ``"America/New_York"``.
        what: The name of the checked value, for the error message.

    Returns:
        The loaded zone.

    Raises:
        TypeError: When ``zone`` is not a string.
        ValueError: When the tz database cannot load it (blank, malformed or unknown).
    """
    if not isinstance(zone, str):
        raise TypeError(f"{what} must be an IANA time zone name, found {zone!r}")
    try:
        return ZoneInfo(zone)
    except ZoneInfoNotFoundError, ValueError, OSError:
        raise ValueError(f"{what} must be a loadable IANA time zone name, found {zone!r}") from None


def format_instant(value: datetime, zone: str) -> str:
    """Render an instant in ``zone`` for a banner or flag message.

    The text is exactly what ``formatInstant`` in ``web/src/lib/format.ts`` renders for the
    same instant and zone (``Intl.DateTimeFormat`` ``en`` with ``dateStyle: "medium"`` and
    ``timeStyle: "short"``, then the zone id), so an instant inside a message reads like
    every other instant on the screen (AAP 0.5.5 "Instants are formatted in
    DISPLAY_TIMEZONE"): the English month abbreviation, the day and the 12-hour clock
    without a leading zero, two-digit minutes (seconds are dropped), ``AM`` or ``PM``
    (``12:05 AM`` just after midnight, ``12:00 PM`` at noon) and the zone id as given.
    Native days are never passed here: coverage dates stay ISO dates of their source's own
    day boundary (AAP 0.5.4.1).

    Args:
        value: The instant (tz-aware, any offset).
        zone: The IANA zone id to show it in: ``settings.display_timezone``.

    Returns:
        The rendered instant.

    Raises:
        TypeError: When ``value`` is not a ``datetime`` or ``zone`` is not a string.
        ValueError: When ``value`` is naive or ``zone`` is not a loadable IANA zone.

    Example:
        >>> format_instant(datetime(2026, 10, 6, 15, 22, tzinfo=UTC), "America/New_York")
        'Oct 6, 2026, 11:22 AM America/New_York'
        >>> format_instant(datetime(2026, 1, 5, 0, 7, tzinfo=UTC), "UTC")
        'Jan 5, 2026, 12:07 AM UTC'
    """
    _require_instant(value, "instant")
    local = value.astimezone(display_zone(zone))
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return (
        f"{_MONTH_ABBREVIATIONS[local.month - 1]} {local.day}, {local.year}, "
        f"{hour}:{local.minute:02d} {meridiem} {zone}"
    )


# ---------------------------------------------------------------------------
# Entity types and stable entity keys
# ---------------------------------------------------------------------------

ENTITY_ACCOUNT: Final = "account"
"""An ad account (account-wide findings and withheld lines)."""
ENTITY_CAMPAIGN: Final = "campaign"
"""A campaign; ``external_id`` is the platform campaign id."""
ENTITY_BUDGET: Final = "budget"
"""A Google budget; ``external_id`` is the ``campaign_budget`` id."""
ENTITY_AD_GROUP: Final = "ad_group"
"""A Google ad group."""
ENTITY_KEYWORD: Final = "keyword"
"""A Google keyword criterion within its ad group (:func:`keyword_external_id`)."""
ENTITY_SEARCH_TERM: Final = "search_term"
"""A search term within an ad group and matched criterion (:func:`search_term_external_id`)."""
ENTITY_CREATIVE: Final = "creative"
"""A LinkedIn creative; ``external_id`` is its URN."""
ENTITY_CONTENT_ASSET: Final = "content_asset"
"""A white paper within one currency cohort (:func:`content_asset_external_id`)."""
ENTITY_SEGMENT: Final = "segment"
"""A demographic value within a campaign (:func:`segment_external_id`)."""
ENTITY_TERM_CLUSTER: Final = "term_cluster"
"""A search-term cluster, keyed by its head tokens (:func:`term_cluster_external_id`)."""
ENTITY_CREATIVE_PAIR: Final = "creative_pair"
"""Two creatives compared by CR1 (:func:`creative_pair_external_id`)."""

ENTITY_TYPES: Final[tuple[str, ...]] = (
    ENTITY_ACCOUNT,
    ENTITY_CAMPAIGN,
    ENTITY_BUDGET,
    ENTITY_AD_GROUP,
    ENTITY_KEYWORD,
    ENTITY_SEARCH_TERM,
    ENTITY_CREATIVE,
    ENTITY_CONTENT_ASSET,
    ENTITY_SEGMENT,
    ENTITY_TERM_CLUSTER,
    ENTITY_CREATIVE_PAIR,
)
"""Every :attr:`EntityRef.type`."""

_KEY_SEPARATOR: Final = "|"


def keyword_external_id(ad_group_external_id: str, criterion_id: str | int) -> str:
    """Return a keyword's stable key: ``{ad_group_external_id}|{criterion_id}``.

    Google's ``ad_group_criterion.criterion_id`` is unique only within an ad group, so the
    criterion alone would give keywords in different ad groups one key, and feedback on one
    would suppress the other. The ad group's platform id makes the key account-wide unique.
    """
    return f"{ad_group_external_id}{_KEY_SEPARATOR}{criterion_id}"


def search_term_external_id(
    ad_group_external_id: str, matched_criterion_id: str | int | None, term: str
) -> str:
    """Return a search term's stable key: ``{ad_group_external_id}|{matched_criterion_id}|{term}``.

    A term without a matched criterion keeps an empty middle part, so the key stays
    three-part and stable across runs.
    """
    criterion = "" if matched_criterion_id is None else str(matched_criterion_id)
    return _KEY_SEPARATOR.join((ad_group_external_id, criterion, term))


def content_asset_external_id(slug: str, currency: str) -> str:
    """Return a white paper's stable key within one currency cohort: ``{slug}|{CUR}``."""
    return f"{slug}{_KEY_SEPARATOR}{currency.strip().upper()}"


def segment_external_id(campaign_external_id: str, facet: str, value: str) -> str:
    """Return a demographic segment's stable key: ``{campaign_external}|{facet}|{value}``."""
    return _KEY_SEPARATOR.join((campaign_external_id, facet, value))


def term_cluster_external_id(head_tokens: Sequence[str] | frozenset[str]) -> str:
    """Return a keyword cluster's stable key: its distinct head tokens, sorted, space-joined.

    Raises:
        ValueError: When no head token is given.
    """
    tokens = sorted({token for token in head_tokens if token})
    if not tokens:
        raise ValueError("a term cluster needs at least one head token")
    return " ".join(tokens)


def creative_pair_external_id(urn_a: str, urn_b: str) -> str:
    """Return a CR1 pair's stable key: the two creative URNs sorted and ``|``-joined.

    Raises:
        ValueError: When both URNs are the same creative.
    """
    if urn_a == urn_b:
        raise ValueError(f"a creative pair needs two different creatives, found {urn_a!r} twice")
    first, second = sorted((urn_a, urn_b))
    return f"{first}{_KEY_SEPARATOR}{second}"


# ---------------------------------------------------------------------------
# JSON rendering and validation helpers
# ---------------------------------------------------------------------------


@runtime_checkable
class SupportsToJson(Protocol):
    """Any value with a ``to_json()`` rendering, such as the output types of this module."""

    def to_json(self) -> Mapping[str, Any]:
        """Return the value as plain JSON."""
        ...


def json_value(value: object) -> Any:
    """Render a value as plain JSON for ``jsonb`` storage and API responses.

    * ``Decimal`` becomes a fixed-point string (``Decimal("12.340000")`` → ``"12.340000"``,
      never exponent notation), so money keeps its exact precision ("Money is serialised as
      decimal strings").
    * ``datetime`` (tz-aware) becomes an ISO-8601 string in UTC; ``date`` an ISO date.
    * Mappings become ``dict`` with string keys; lists and tuples become lists; sets become
      lists sorted by their JSON text, so the output is deterministic.
    * Objects with ``to_json()`` are rendered by it.
    * ``None``, ``bool``, ``int``, ``str`` and finite ``float`` pass through.

    Raises:
        ValueError: On a non-finite ``Decimal`` or ``float``, or a naive ``datetime``.
        TypeError: On a mapping key that is not a ``str`` or ``int``, or any other type.

    Example:
        >>> json_value({"spend": Decimal("1E+2"), "day": date(2026, 10, 1)})
        {'spend': '100', 'day': '2026-10-01'}
    """
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot render a non-finite float as JSON: {value!r}")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"cannot render a non-finite Decimal as JSON: {value!r}")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError(f"cannot render a naive datetime as an instant: {value!r}")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, SupportsToJson):
        return json_value(value.to_json())
    if isinstance(value, Mapping):
        rendered: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, bool) or not isinstance(key, str | int):
                raise TypeError(f"JSON object keys must be str or int, found {key!r}")
            rendered[str(key)] = json_value(item)
        return rendered
    if isinstance(value, list | tuple):
        return [json_value(item) for item in value]
    if isinstance(value, set | frozenset):
        items = [json_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    raise TypeError(f"cannot render {type(value).__name__} as JSON: {value!r}")


def _require_in(value: object, allowed: Sequence[object], what: str) -> None:
    """Require ``value`` to be one of ``allowed``.

    Raises:
        ValueError: When it is not.
    """
    if value not in allowed:
        raise ValueError(f"{what} must be one of {tuple(allowed)}, found {value!r}")


def _require_text(value: object, what: str) -> None:
    """Require a non-blank string.

    Raises:
        ValueError: When ``value`` is not a string with visible content.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-blank string, found {value!r}")


def _require_id(value: object, what: str, *, optional: bool = False) -> None:
    """Require an integer id (``bool`` rejected), or ``None`` when ``optional``.

    Raises:
        TypeError: When ``value`` is not an ``int``.
    """
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an int id, found {value!r}")


def _require_native_date(value: object, what: str, *, optional: bool = False) -> None:
    """Require a native calendar ``date`` (decision 6); a ``datetime`` is rejected.

    Accepting a ``datetime`` would silently put an instant on whatever day its offset gives,
    the rebucketing the native-day policy forbids.

    Raises:
        TypeError: When ``value`` is not a ``date`` or is a ``datetime``.
    """
    if value is None and optional:
        return
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{what} must be a datetime.date (a native day), found {value!r}")


def _require_instant(value: object, what: str, *, optional: bool = False) -> None:
    """Require a tz-aware ``datetime`` (decision 6), or ``None`` when ``optional``.

    Raises:
        TypeError: When ``value`` is not a ``datetime``.
        ValueError: When it is naive.
    """
    if value is None and optional:
        return
    if not isinstance(value, datetime):
        raise TypeError(f"{what} must be a tz-aware datetime, found {value!r}")
    if value.utcoffset() is None:
        raise ValueError(f"{what} must be tz-aware (an instant), found a naive datetime")


def _require_number(value: object, what: str, *, optional: bool = False) -> None:
    """Require a ``Decimal`` or ``int`` (decision 6: never ``float``; ``bool`` rejected).

    Raises:
        TypeError: When ``value`` is neither.
    """
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise TypeError(f"{what} must be a Decimal or int, found {value!r}")


_DATASET_MODES: Final[tuple[str, ...]] = get_args(DatasetMode)
_DATASET_ROLES: Final[tuple[str, ...]] = get_args(DatasetRole)
_COMPLETENESS_VALUES: Final[tuple[str, ...]] = get_args(Completeness)
_SUBJECT_TYPES: Final[tuple[str, ...]] = get_args(SubjectType)


# ---------------------------------------------------------------------------
# Output types (AAP 0.5.3, 0.5.4.8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityRef:
    """The entity an item, finding or withheld pattern is about.

    :attr:`key` is the stable entity key stored in ``recommendation.entity_id`` and matched by
    ``recommendation_feedback`` (rule id, entity type and key) across runs. It is built from
    the platform's external id, never from an internal row id, so it survives a re-sync and a
    dataset transition of the same platform entity.

    Attributes:
        source: The source the entity belongs to.
        type: One of :data:`ENTITY_TYPES`.
        id: The internal row id in the active dataset, when the entity has a row (campaign,
            budget, ad group, keyword, creative); ``None`` for composite entities.
        external_id: The platform id, or the composite key built by
            :func:`keyword_external_id`, :func:`search_term_external_id`,
            :func:`content_asset_external_id`, :func:`segment_external_id`,
            :func:`term_cluster_external_id` or :func:`creative_pair_external_id`.
        name: The display name. A blank name falls back to ``external_id``, so ranking ties
            (broken by name, then id, then key; :attr:`Candidate.sort_key`) stay well defined.

    Raises:
        ValueError: On construction, for an unknown source or type, or a blank external id.
        TypeError: On construction, when ``id`` is neither an ``int`` nor ``None``.
    """

    source: Source
    type: str
    id: int | None
    external_id: str
    name: str

    def __post_init__(self) -> None:
        """Validate the fields and default a blank name to the external id."""
        _require_in(self.source, SOURCES, "EntityRef.source")
        _require_in(self.type, ENTITY_TYPES, "EntityRef.type")
        _require_id(self.id, "EntityRef.id", optional=True)
        _require_text(self.external_id, "EntityRef.external_id")
        name: object = self.name
        if not isinstance(name, str):
            raise TypeError(f"EntityRef.name must be a string, found {name!r}")
        if not name.strip():
            object.__setattr__(self, "name", self.external_id)

    @property
    def key(self) -> str:
        """The stable entity key: ``{source}:{type}:{external_id}``."""
        return f"{self.source}:{self.type}:{self.external_id}"

    def to_json(self) -> dict[str, Any]:
        """Return AAP 0.5.3 ``Item.entity``: ``{source, type, id, name}``.

        ``id`` is :attr:`external_id`, the platform id or composite key that :attr:`key` and
        the API's ``Item.entity.id`` carry, never the internal row :attr:`id`. Items
        (``evidence.extra.entity``), findings and withheld patterns therefore name an entity
        in one namespace, so a withheld pattern or finding can be tied to the entity's items,
        and every entity, a composite one included, has an id.
        """
        return {
            "source": self.source,
            "type": self.type,
            "id": self.external_id,
            "name": self.name,
        }


@dataclass(frozen=True, slots=True)
class DecisionSubject:
    """The campaign or budget a decision on an item would write, or a spend increase targets.

    A light address only; ``ads_engine.decisions`` owns the write-side types.

    Attributes:
        type: ``campaign`` or ``budget``.
        id: The subject's internal id in the active dataset.

    Raises:
        ValueError: On construction, for an unknown subject type.
        TypeError: On construction, when ``id`` is not an ``int``.
    """

    type: SubjectType
    id: int

    def __post_init__(self) -> None:
        """Validate the subject type and id."""
        _require_in(self.type, _SUBJECT_TYPES, "DecisionSubject.type")
        _require_id(self.id, "DecisionSubject.id")

    @property
    def key(self) -> SubjectKey:
        """The subject as a :data:`SubjectKey`, comparable with ``metrics`` predicates."""
        return (self.type, self.id)

    @classmethod
    def of(cls, key: SubjectKey) -> DecisionSubject:
        """Build a subject from a :data:`SubjectKey`."""
        subject_type, subject_id = key
        return cls(subject_type, subject_id)

    def to_json(self) -> dict[str, Any]:
        """Return AAP 0.5.3 ``Item.decision_subject``: ``{type, id}``."""
        return {"type": self.type, "id": self.id}


@dataclass(frozen=True, slots=True)
class Flag:
    """A badge on an item (AAP 0.5.3 ``Flag``).

    Attributes:
        code: One of :data:`FLAG_CODES`.
        message: The badge's explanatory text, for example "Data as of Oct 1, 2026, 3:10 AM
            UTC".

    Raises:
        ValueError: On construction, for an unknown code or a blank message.
    """

    code: str
    message: str

    def __post_init__(self) -> None:
        """Validate the code and message."""
        _require_in(self.code, FLAG_CODES, "Flag.code")
        _require_text(self.message, "Flag.message")

    def to_json(self) -> dict[str, Any]:
        """Return ``{code, message}``."""
        return {"code": self.code, "message": self.message}


def _require_metric_value(value: object, what: str) -> None:
    """Require a metric or baseline value: ``Decimal``, ``int``, ``str`` or ``None``.

    Raises:
        TypeError: On ``float`` (decision 6), ``bool`` or any other type.
    """
    if value is None or isinstance(value, str):
        return
    _require_number(value, what)


@dataclass(frozen=True, slots=True)
class Metric:
    """One number an item's reason cites (AAP 0.5.3 ``Evidence.metrics``).

    Attributes:
        key: Stable machine key, for example ``spend`` or ``cost_per_conversion``.
        label: Display label.
        value: The value; ``None`` means unavailable, never 0 (decision 6). A string carries a
            pre-rendered display value such as ``"n/a"`` for a lifetime budget's utilization.
        unit: The unit, for example ``currency``, ``clicks``, ``ratio`` or ``days``.
        currency: ISO currency code for money values.
        floor: Whether the value is a floor (rendered "≥ N" or "≤ X", AAP 0.5.4.7).

    Raises:
        ValueError: On construction, for a blank key, label or unit.
        TypeError: On construction, for a ``float`` or ``bool`` value.
    """

    key: str
    label: str
    value: Decimal | int | str | None
    unit: str
    currency: str | None = None
    floor: bool = False

    def __post_init__(self) -> None:
        """Validate the fields."""
        _require_text(self.key, "Metric.key")
        _require_text(self.label, "Metric.label")
        _require_metric_value(self.value, f"Metric {self.key!r} value")
        _require_text(self.unit, "Metric.unit")
        if self.currency is not None:
            _require_text(self.currency, "Metric.currency")

    @property
    def unavailable(self) -> bool:
        """Whether the value is unavailable (``None``)."""
        return self.value is None

    def to_json(self) -> dict[str, Any]:
        """Return ``{key, label, value, unit, currency?, floor?, unavailable?}``."""
        rendered: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "value": json_value(self.value),
            "unit": self.unit,
        }
        if self.currency is not None:
            rendered["currency"] = self.currency
        if self.floor:
            rendered["floor"] = True
        if self.unavailable:
            rendered["unavailable"] = True
        return rendered


@dataclass(frozen=True, slots=True)
class Baseline:
    """A value an item's metrics were compared against (AAP 0.5.3 ``Evidence.baselines``).

    A baseline that no metric of its item shares a unit with carries its own ``unit`` and, for
    money, ``currency``, so it is shown in the account currency like the metrics (AAP 0.5.5:
    the evidence lists the metrics against their baselines "with currency"). C1's account cost
    per outcome is such a baseline: the pause card has no cost-per-outcome metric beside it.
    Both keys are optional additions to the AAP 0.5.3 ``{key, label, value}`` shape: a
    baseline without them, including every baseline frozen before they existed, keeps exactly
    that shape.

    Attributes:
        key: Stable machine key, for example ``account_cost_per_outcome``.
        label: Display label.
        value: The baseline value; ``None`` when unavailable.
        unit: The unit, as :attr:`Metric.unit` names it (for example ``currency`` or
            ``ratio``); ``None`` for a baseline shown in the unit of the metric it pairs with,
            or a unitless one such as K1's conversions per click.
        currency: ISO currency code for a money value; set only together with ``unit``.

    Raises:
        ValueError: On construction, for a blank key, label, unit or currency, or a currency
            without a unit.
        TypeError: On construction, for a ``float`` or ``bool`` value.
    """

    key: str
    label: str
    value: Decimal | int | str | None
    unit: str | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        """Validate the fields."""
        _require_text(self.key, "Baseline.key")
        _require_text(self.label, "Baseline.label")
        _require_metric_value(self.value, f"Baseline {self.key!r} value")
        if self.unit is not None:
            _require_text(self.unit, "Baseline.unit")
        if self.currency is not None:
            _require_text(self.currency, "Baseline.currency")
            if self.unit is None:
                raise ValueError(f"Baseline {self.key!r} has a currency but no unit")

    def to_json(self) -> dict[str, Any]:
        """Return ``{key, label, value, unit?, currency?}``.

        ``unit`` and ``currency`` appear only when set, so a baseline without them renders the
        same JSON as one frozen before the keys existed.
        """
        rendered: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "value": json_value(self.value),
        }
        if self.unit is not None:
            rendered["unit"] = self.unit
        if self.currency is not None:
            rendered["currency"] = self.currency
        return rendered


@dataclass(frozen=True, slots=True)
class Projection:
    """A labelled projection beside a sample (for example CR3's impressions per variant).

    Attributes:
        value: The projected figure.
        unit: Its unit, for example ``impressions`` or ``days``.
        basis: How it was projected, for example "projected through the observed open rate".

    Raises:
        ValueError: On construction, for a blank unit or basis.
        TypeError: On construction, for a value that is not ``Decimal`` or ``int``.
    """

    value: Number
    unit: str
    basis: str

    def __post_init__(self) -> None:
        """Validate the fields."""
        _require_number(self.value, "Projection.value")
        _require_text(self.unit, "Projection.unit")
        _require_text(self.basis, "Projection.basis")

    def to_json(self) -> dict[str, Any]:
        """Return ``{value, unit, basis}``."""
        return {"value": json_value(self.value), "unit": self.unit, "basis": self.basis}


@dataclass(frozen=True, slots=True)
class Sample:
    """A sample size against its minimum, in the unit the rule uses (AAP 0.5.4.8).

    Attributes:
        observed: The sample observed.
        required: The minimum the rule requires.
        unit: The unit, for example ``clicks``, ``impressions``, ``form opens`` or ``days``.
        projected: An optional labelled projection.
        floor: Whether ``observed`` is a floored CRM count, such as W1's Qualified Leads under
            an attribution floor, rendered "≥ N" as the count's own column shows it
            (AAP 0.5.4.7). The minimum is never a floor.

    Raises:
        ValueError: On construction, for a blank unit.
        TypeError: On construction, for a count that is not ``Decimal`` or ``int``, or a
            ``floor`` that is not a ``bool``.
    """

    observed: Number
    required: Number
    unit: str
    projected: Projection | None = None
    floor: bool = False

    def __post_init__(self) -> None:
        """Validate the fields."""
        _require_number(self.observed, "Sample.observed")
        _require_number(self.required, "Sample.required")
        _require_text(self.unit, "Sample.unit")
        if self.projected is not None and not isinstance(self.projected, Projection):
            raise TypeError(f"Sample.projected must be a Projection, found {self.projected!r}")
        if not isinstance(self.floor, bool):
            raise TypeError(f"Sample.floor must be a bool, found {self.floor!r}")

    @property
    def met(self) -> bool:
        """Whether the observed sample reaches the minimum (P6)."""
        return self.observed >= self.required

    def to_json(self) -> dict[str, Any]:
        """Return ``{observed, required, unit, projected?, floor?}``.

        ``floor`` is emitted only when true, so a sample without a floor keeps exactly the
        AAP 0.5.3 ``{observed, required, unit, projected?}`` shape.
        """
        rendered: dict[str, Any] = {
            "observed": json_value(self.observed),
            "required": json_value(self.required),
            "unit": self.unit,
        }
        if self.projected is not None:
            rendered["projected"] = self.projected.to_json()
        if self.floor:
            rendered["floor"] = True
        return rendered


def _require_probability(value: object, what: str, *, optional: bool) -> None:
    """Require a ``Decimal`` in ``[0, 1]``, or ``None`` when ``optional``.

    Raises:
        TypeError: When ``value`` is not a ``Decimal``.
        ValueError: When it lies outside ``[0, 1]``.
    """
    if value is None and optional:
        return
    if not isinstance(value, Decimal):
        raise TypeError(f"{what} must be a Decimal, found {value!r}")
    if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
        raise ValueError(f"{what} must lie in [0, 1], found {value}")


@dataclass(frozen=True, slots=True)
class TestResult:
    """The statistical test behind an item (AAP 0.5.3 ``Evidence.test``).

    Attributes:
        name: The test, for example "Fisher's exact test" or "two-sample Poisson rate test".
        p_value: The raw p-value; ``None`` when the test could not be computed.
        p_adjusted: The multiple-comparison adjusted p-value (Holm), when one applies.
        alpha: The significance level the result is judged at.

    Raises:
        ValueError: On construction, for a blank name or a probability outside ``[0, 1]``.
        TypeError: On construction, for a probability that is not a ``Decimal``.
    """

    __test__: ClassVar[bool] = False  # not a pytest test class, despite the name

    name: str
    p_value: Decimal | None
    p_adjusted: Decimal | None
    alpha: Decimal

    def __post_init__(self) -> None:
        """Validate the name and probabilities."""
        _require_text(self.name, "TestResult.name")
        _require_probability(self.p_value, "TestResult.p_value", optional=True)
        _require_probability(self.p_adjusted, "TestResult.p_adjusted", optional=True)
        _require_probability(self.alpha, "TestResult.alpha", optional=False)

    def to_json(self) -> dict[str, Any]:
        """Return ``{name, p_value, p_adjusted?, alpha}``."""
        rendered: dict[str, Any] = {"name": self.name, "p_value": json_value(self.p_value)}
        if self.p_adjusted is not None:
            rendered["p_adjusted"] = json_value(self.p_adjusted)
        rendered["alpha"] = json_value(self.alpha)
        return rendered


@dataclass(frozen=True, slots=True)
class ProvenanceEntry:
    """One dataset an item read, as it was when the run computed it (AAP 0.3.2).

    Provenance is frozen at compute time and never replaced by the current source status: the
    overlay compares these ids with the currently active ones to mark an item historical.

    Attributes:
        source: The dataset's source.
        dataset_id: The ``source_dataset`` id.
        mode: ``live`` or ``simulated`` at compute time.
        role: ``primary``, or ``companion`` for the simulated CRM of a simulated LinkedIn
            dataset.

    Raises:
        ValueError: On construction, for an unknown source, mode or role.
        TypeError: On construction, when ``dataset_id`` is not an ``int``.
    """

    source: Source
    dataset_id: int
    mode: DatasetMode
    role: DatasetRole

    def __post_init__(self) -> None:
        """Validate the fields."""
        _require_in(self.source, SOURCES, "ProvenanceEntry.source")
        _require_id(self.dataset_id, "ProvenanceEntry.dataset_id")
        _require_in(self.mode, _DATASET_MODES, "ProvenanceEntry.mode")
        _require_in(self.role, _DATASET_ROLES, "ProvenanceEntry.role")

    def to_json(self) -> dict[str, Any]:
        """Return ``{source, dataset_id, mode, role}``."""
        return {
            "source": self.source,
            "dataset_id": self.dataset_id,
            "mode": self.mode,
            "role": self.role,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ProvenanceEntry:
        """Decode a stored ``evidence.provenance`` element (the inverse of :meth:`to_json`).

        Raises:
            KeyError: When a key is missing.
            ValueError: When a value is not allowed.
            TypeError: When ``dataset_id`` is not an ``int``.
        """
        return cls(
            source=data["source"],
            dataset_id=data["dataset_id"],
            mode=data["mode"],
            role=data["role"],
        )


@dataclass(frozen=True, slots=True)
class DatasetStatus:
    """A dataset an item read, with its completeness and coverage at compute time.

    A dataset with a coverage date also records the day boundary that date is in, so the date
    is shown labelled with its source's day boundary (AAP 0.5.5) whatever the source: the
    window's ``source_days`` names the boundary of the day sources only, and HubSpot, which
    holds instants, has none there although its coverage dates are UTC dates.
    :meth:`AnalysisContext.make_evidence` sets it. ``boundary`` is an optional addition to the
    AAP 0.5.3 ``{dataset, completeness, covered_through}`` shape: a status without it,
    including every status frozen before it existed, keeps exactly that shape.

    Attributes:
        dataset: The dataset name, one of ``db.models.ops.DATASETS``.
        completeness: ``complete``, ``incomplete`` or ``unknown``; ``unknown`` when no
            freshness row exists.
        covered_through: The last native date the dataset covers; ``None`` when unknown.
        boundary: The IANA zone whose midnight ends ``covered_through``, for example
            ``America/New_York`` for Google or ``UTC`` for LinkedIn and HubSpot; set only
            together with ``covered_through``. ``None`` when the boundary is unknown.

    Raises:
        ValueError: On construction, for an unknown dataset or completeness, a blank
            ``boundary``, or a ``boundary`` without ``covered_through``.
        TypeError: On construction, when ``covered_through`` is not a native date.
    """

    dataset: str
    completeness: Completeness
    covered_through: date | None = None
    boundary: str | None = None

    def __post_init__(self) -> None:
        """Validate the fields."""
        _require_in(self.dataset, DATASETS, "DatasetStatus.dataset")
        _require_in(self.completeness, _COMPLETENESS_VALUES, "DatasetStatus.completeness")
        _require_native_date(self.covered_through, "DatasetStatus.covered_through", optional=True)
        if self.boundary is not None:
            _require_text(self.boundary, "DatasetStatus.boundary")
            if self.covered_through is None:
                raise ValueError(
                    f"DatasetStatus {self.dataset!r} has a boundary but no covered_through"
                )

    def to_json(self) -> dict[str, Any]:
        """Return ``{dataset, completeness, covered_through, boundary?}``.

        ``boundary`` appears only when set, so a status without it renders the same JSON as
        one frozen before the key existed.
        """
        rendered: dict[str, Any] = {
            "dataset": self.dataset,
            "completeness": self.completeness,
            "covered_through": json_value(self.covered_through),
        }
        if self.boundary is not None:
            rendered["boundary"] = self.boundary
        return rendered


def _tuple_of[T](value: object, kind: type[T], what: str) -> tuple[T, ...]:
    """Return ``value`` as a tuple after checking every element is a ``kind``.

    Raises:
        TypeError: When ``value`` is not a list or tuple, or an element has the wrong type.
    """
    if not isinstance(value, list | tuple):
        raise TypeError(f"{what} must be a tuple, found {value!r}")
    items: list[T] = []
    for item in value:
        if not isinstance(item, kind):
            raise TypeError(f"{what} items must be {kind.__name__}, found {item!r}")
        items.append(item)
    return tuple(items)


def _is_frozen_dataclass(value: object) -> bool:
    """Whether ``value`` is an instance of a ``frozen=True`` dataclass, such as :class:`Metric`.

    Only the flag is checked: the fields may still hold mutable values, so
    :func:`_mutable_part` decides whether the instance is deeply immutable.
    """
    params = getattr(type(value), "__dataclass_params__", None)
    return params is not None and getattr(params, "frozen", False) is True


def _mutable_part(value: object, where: str) -> str | None:
    """Describe the first value inside ``value`` that is not deeply immutable, if any.

    ``value`` is deeply immutable when it is ``None``, a ``bool``, ``int``, ``float``,
    ``str``, ``Decimal``, ``date`` or ``datetime``; a ``tuple`` or ``frozenset`` whose every
    item is deeply immutable; or an instance of a ``frozen=True`` dataclass whose every field
    (:func:`dataclasses.fields`) is deeply immutable. Nothing else is trusted: a ``dict``,
    ``list`` or ``set``, a :class:`~types.MappingProxyType` (it can wrap a ``dict`` its
    creator still holds), a dataclass that is not frozen, or any other object.

    Args:
        value: The value to inspect.
        where: Its path inside the record being checked, for example ``data`` or
            ``inner.rows[0]``; empty for the record itself.

    Returns:
        ``None`` when ``value`` is deeply immutable; otherwise where the first mutable value
        sits and its type, for example ``"field 'data' holds a dict"`` or
        ``"field 'inner.rows[0]' holds a list"``.
    """
    if value is None or isinstance(value, bool | int | float | str | Decimal | date):
        return None
    if isinstance(value, tuple):
        parts = ((f"{where}[{index}]", item) for index, item in enumerate(value))
    elif isinstance(value, frozenset):
        parts = ((f"{where} item", item) for item in value)
    elif _is_frozen_dataclass(value) and is_dataclass(value):
        parts = (
            # An ``init=False`` slot never assigned holds nothing, so it reads as ``None``.
            (f"{where}.{part.name}" if where else part.name, getattr(value, part.name, None))
            for part in fields(value)
        )
    else:
        kind = type(value).__name__
        article = "an" if kind[:1].lower() in {"a", "e", "i", "o", "u"} else "a"
        if is_dataclass(value) and not isinstance(value, type):
            kind = f"{kind} (a dataclass that is not frozen)"
        return f"field {where!r} holds {article} {kind}"
    for path, item in parts:
        found = _mutable_part(item, path)
        if found is not None:
            return found
    return None


def _frozen(value: object, what: str) -> object:
    """Return a deep, read-only copy of a value stored in evidence or a context input.

    As ``thresholds._freeze`` does for the configuration: a mapping becomes a
    :class:`~types.MappingProxyType` over a fresh ``dict`` of frozen values, a list or tuple
    a tuple of frozen items, and a set a ``frozenset`` of frozen items. ``None``, ``bool``,
    ``int``, ``float``, ``str``, ``Decimal``, ``date`` and ``datetime`` are immutable and
    returned unchanged. An instance of a ``frozen=True`` dataclass (:class:`EntityRef`,
    :class:`Metric` …) is returned unchanged, the same object, only when it is deeply
    immutable: every field, at any depth, holds a scalar of those types, a ``tuple`` or
    ``frozenset`` of deeply immutable items, or another deeply immutable frozen dataclass
    (:func:`_mutable_part`). The copy shares no mutable object with ``value``, and
    :func:`json_value` renders it exactly as it renders ``value``: mappings as objects,
    tuples as arrays.

    Args:
        value: The value to freeze.
        what: Where the value sits, for example ``Evidence.extra['rows'][0]``, for errors.

    Raises:
        TypeError: On a frozen dataclass whose fields hold, at any depth, a ``dict``,
            ``list``, ``set``, mapping proxy, non-frozen dataclass or any other object that
            is not deeply immutable, naming the field; and on any other type, for example a
            ``bytearray``, a non-frozen dataclass or another mutable object. Neither can be
            frozen, so each is rejected rather than stored where a later change would alter
            it.
    """
    if value is None or isinstance(value, bool | int | float | str | Decimal | date):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _frozen(item, f"{what}[{key!r}]") for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_frozen(item, f"{what}[{index}]") for index, item in enumerate(value))
    if isinstance(value, set | frozenset):
        return frozenset(_frozen(item, f"{what} item") for item in value)
    if _is_frozen_dataclass(value):
        mutable = _mutable_part(value, "")
        if mutable is None:
            return value
        raise TypeError(
            f"{what} is a frozen dataclass whose {mutable}, so it is not immutable and cannot "
            f"be stored, found {type(value).__name__} {value!r}"
        )
    raise TypeError(
        f"{what} must be a mapping, list, tuple, set, scalar or frozen dataclass so it can be "
        f"frozen, found {type(value).__name__} {value!r}"
    )


def _frozen_mapping(value: object, what: str) -> Mapping[str, Any]:
    """Return a deep, read-only copy (:func:`_frozen`) of a string-keyed mapping.

    Raises:
        TypeError: When ``value`` is not a mapping with string keys, or a nested value cannot
            be frozen.
    """
    if not isinstance(value, Mapping):
        raise TypeError(f"{what} must be a mapping, found {value!r}")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{what} keys must be strings, found {key!r}")
        frozen[key] = _frozen(item, f"{what}[{key!r}]")
    return MappingProxyType(frozen)


def _read_only_mapping(value: object, what: str) -> Mapping[Any, Any]:
    """Return a read-only copy of a mapping whose values are frozen records.

    The values (frozen dataclasses such as :class:`CampaignInfo`) are immutable, so copying the
    outer level keeps the caller's later changes to its own mapping out of the copy.

    Raises:
        TypeError: When ``value`` is not a mapping.
    """
    if not isinstance(value, Mapping):
        raise TypeError(f"{what} must be a mapping, found {value!r}")
    return MappingProxyType(dict(value))


def _read_only_sequences(value: object, what: str) -> Mapping[Any, tuple[Any, ...]]:
    """Return a read-only copy of a mapping whose values are sequences, each made a tuple.

    Raises:
        TypeError: When ``value`` is not a mapping, or one of its values is not a list or
            tuple.
    """
    if not isinstance(value, Mapping):
        raise TypeError(f"{what} must be a mapping, found {value!r}")
    copied: dict[Any, tuple[Any, ...]] = {}
    for key, items in value.items():
        if not isinstance(items, list | tuple):
            raise TypeError(f"{what}[{key!r}] must be a tuple, found {items!r}")
        copied[key] = tuple(items)
    return MappingProxyType(copied)


EXTRA_HEADLINE_METRIC: Final = "headline_metric"
"""The ``evidence.extra`` key naming the item's headline number (AAP 0.1.2).

Its value is the ``key`` of one metric in the same evidence's ``metrics``: the number the
card leads with, chosen by the rule (K1's is its wasted spend), or set by precedence for a
headline only known once every hold is final (C0's count of the recommendations it holds).
Only :meth:`AnalysisContext.make_evidence`, from its ``headline`` argument, and
:meth:`Evidence.with_headline`, from the metric it appends, write it.
"""


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything an item's conclusion rests on, frozen with the run (AAP 0.5.4.8).

    Build it with :meth:`AnalysisContext.make_evidence`, so the window, thresholds, dataset
    statuses and provenance are recorded identically by every rule. ``window``, ``thresholds``
    and ``extra`` are deep-copied on construction into read-only trees: every mapping, at any
    depth, becomes a read-only mapping proxy, every list or tuple a tuple and every set a
    ``frozenset``. Neither a later change to the caller's objects nor a write through the
    stored values can therefore alter frozen evidence, and :meth:`to_json` still renders
    plain JSON objects and arrays. The copies :meth:`with_extra`, :meth:`with_headline` and
    :meth:`with_thresholds` return freeze and check only the values they add, and share every
    other field with the evidence they copy, which is deeply immutable already, so each item's
    evidence is validated once however many precedence steps extend it.

    Attributes:
        window: ``AnalysisWindow.to_json()``: ``{start, end, days, source_days,
            boundaries_differ, description}`` with each source's native day boundary, whether
            those boundaries differ, and the sentence naming each source's range and boundary;
            read-only, ``source_days`` a tuple.
        metrics: Every number the reason cites, with units and currency; keys are unique.
        baselines: The values those metrics were compared against.
        sample: The sample size against its minimum, when the rule is sample-gated.
        test: The statistical test, when one was used.
        thresholds: ``{dotted key: configured value}`` for every threshold applied; read-only.
        datasets: The datasets read, with completeness and coverage, each coverage date with
            its day boundary.
        provenance: One entry per distinct dataset read, frozen at compute time.
        extra: Rule-specific detail, for example white-paper ``rows``, expansion
            ``adjacent_terms``, B1/B3 allocation text, ``depends_on`` or
            ``headline_metric`` (:data:`EXTRA_HEADLINE_METRIC`, the key of the metric a
            card leads with); read-only, with nested lists as tuples.

    Raises:
        TypeError: On construction, when a field has the wrong type, a mapping has a
            non-string key, or a value nested in ``window``, ``thresholds`` or ``extra``
            cannot be frozen: for example a ``bytearray``, a non-frozen dataclass, or a frozen
            dataclass that is not deeply immutable because a field holds, at any depth, a
            ``dict``, ``list``, ``set``, mapping proxy or other mutable object.
        ValueError: On construction, when two metrics share a key.
    """

    window: Mapping[str, Any]
    metrics: tuple[Metric, ...] = ()
    baselines: tuple[Baseline, ...] = ()
    sample: Sample | None = None
    test: TestResult | None = None
    thresholds: Mapping[str, Any] = field(default_factory=dict)
    datasets: tuple[DatasetStatus, ...] = ()
    provenance: tuple[ProvenanceEntry, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Deep-freeze the mappings, normalise the sequences and check element types."""
        object.__setattr__(self, "window", _frozen_mapping(self.window, "Evidence.window"))
        metrics = _tuple_of(self.metrics, Metric, "Evidence.metrics")
        keys = [metric.key for metric in metrics]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"Evidence.metrics repeats the keys {duplicates}")
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(
            self, "baselines", _tuple_of(self.baselines, Baseline, "Evidence.baselines")
        )
        if self.sample is not None and not isinstance(self.sample, Sample):
            raise TypeError(f"Evidence.sample must be a Sample, found {self.sample!r}")
        if self.test is not None and not isinstance(self.test, TestResult):
            raise TypeError(f"Evidence.test must be a TestResult, found {self.test!r}")
        object.__setattr__(
            self, "thresholds", _frozen_mapping(self.thresholds, "Evidence.thresholds")
        )
        object.__setattr__(
            self, "datasets", _tuple_of(self.datasets, DatasetStatus, "Evidence.datasets")
        )
        object.__setattr__(
            self,
            "provenance",
            _tuple_of(self.provenance, ProvenanceEntry, "Evidence.provenance"),
        )
        object.__setattr__(self, "extra", _frozen_mapping(self.extra, "Evidence.extra"))

    def metric(self, key: str) -> Metric:
        """Return the metric with ``key``.

        Raises:
            KeyError: When no metric has that key.
        """
        for metric in self.metrics:
            if metric.key == key:
                return metric
        raise KeyError(key)

    def with_extra(self, **values: Any) -> Evidence:
        """Return a copy with ``values`` merged into :attr:`extra`; this evidence is unchanged.

        ``precedence.py`` uses it for B1/B3 allocation text and the expanded ``depends_on``.
        Only ``values`` are frozen and checked, exactly as construction freezes ``extra``: each
        becomes a read-only tree of its own. The copy's ``extra`` is a new read-only mapping
        over a fresh ``dict``: this evidence's entries in their order, a key already present
        taking its new value in place, then the new keys. Every other field, and every existing
        ``extra`` value, is shared with this evidence, because each is deeply immutable already.
        The copy therefore shares no mutable object with this evidence or with ``values``, and
        a later change to ``values`` cannot reach it.

        Raises:
            TypeError: When a value in ``values`` cannot be frozen (see :class:`Evidence`).
        """
        added = _frozen_mapping(values, "Evidence.extra")
        return self._derived(extra=MappingProxyType({**self.extra, **added}))

    def with_thresholds(self, values: Mapping[str, Any]) -> Evidence:
        """Return a copy with ``values`` merged into :attr:`thresholds`; this one is unchanged.

        ``precedence.py`` uses it to record the configured values a P4 or P5 judgement applied
        (AAP 0.5.4.8). Only ``values`` are frozen and checked, exactly as construction freezes
        ``thresholds``: string keys, every value a read-only tree of its own. The copy's
        ``thresholds`` is a new read-only mapping over a fresh ``dict``: the recorded keys in
        their order, a key already recorded taking its new value in place, then the new keys.
        Every other field is shared with this evidence, because each is deeply immutable
        already, so the copy shares no mutable object with this evidence or with ``values``.

        Args:
            values: ``{dotted threshold key: configured value}`` to record.

        Returns:
            The copy, with ``values`` recorded in ``thresholds``.

        Raises:
            TypeError: When ``values`` is not a mapping, has a key that is not a string, or
                holds a value that cannot be frozen (see :class:`Evidence`).
        """
        added = _frozen_mapping(values, "Evidence.thresholds")
        return self._derived(thresholds=MappingProxyType({**self.thresholds, **added}))

    def with_headline(self, metric: Metric) -> Evidence:
        """Return a copy that leads with ``metric``; this evidence is unchanged.

        ``precedence.py`` uses it for a headline only known once every hold is final: a C0
        item's count of the recommendations its discrepancy holds. ``metric`` is appended after
        every existing metric, whose keys and order are kept, and ``extra.headline_metric``
        (:data:`EXTRA_HEADLINE_METRIC`) is set to its key, so the selection names a metric of
        the copy. Only ``metric`` is checked, as construction checks each metric: its type, and
        that its key is new. As with :meth:`with_extra`, the copy's ``extra`` is a new read-only
        mapping over a fresh ``dict``, and every other field is shared with this evidence,
        because each is deeply immutable already, so the copy shares no mutable object with
        this evidence.

        Args:
            metric: The headline number; no metric of this evidence may have its key.

        Returns:
            The copy, with ``metric`` last in ``metrics`` and named as its headline.

        Raises:
            TypeError: When ``metric`` is not a :class:`Metric`.
            ValueError: When this evidence already names a headline metric, or one of its
                metrics already has ``metric.key``.
        """
        if not isinstance(metric, Metric):
            raise TypeError(f"Evidence.with_headline needs a Metric, found {metric!r}")
        if EXTRA_HEADLINE_METRIC in self.extra:
            raise ValueError(
                f"evidence already names the headline metric "
                f"{self.extra[EXTRA_HEADLINE_METRIC]!r}; {metric.key!r} cannot replace it"
            )
        if any(existing.key == metric.key for existing in self.metrics):
            raise ValueError(f"Evidence.metrics repeats the keys {[metric.key]}")
        return self._derived(
            metrics=(*self.metrics, metric),
            extra=MappingProxyType({**self.extra, EXTRA_HEADLINE_METRIC: metric.key}),
        )

    def _derived(
        self,
        *,
        metrics: tuple[Metric, ...] | None = None,
        thresholds: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Evidence:
        """Return a copy of this evidence with the given fields replaced, skipping validation.

        The copy is assembled field by field, bypassing ``__init__`` and :meth:`__post_init__`:
        the fields taken from this evidence were frozen and validated when it was built and
        are deeply immutable, so they are shared, never copied or checked again. Every
        replacement must already be frozen and validated by the caller (:meth:`with_extra`,
        :meth:`with_thresholds`, :meth:`with_headline`), so the copy holds exactly what
        constructing it from the same values would hold.
        """
        copy = object.__new__(Evidence)
        for name in _EVIDENCE_FIELDS:
            object.__setattr__(copy, name, getattr(self, name))
        if metrics is not None:
            object.__setattr__(copy, "metrics", metrics)
        if thresholds is not None:
            object.__setattr__(copy, "thresholds", thresholds)
        if extra is not None:
            object.__setattr__(copy, "extra", extra)
        return copy

    def to_json(self) -> dict[str, Any]:
        """Return exactly AAP 0.5.3 ``Evidence``.

        Key order: ``window, metrics, baselines, sample?, test?, thresholds, datasets,
        provenance, extra?``; ``sample``, ``test`` and an empty ``extra`` are omitted.
        Decimals render as fixed-point strings and dates as ISO strings (:func:`json_value`).
        """
        rendered: dict[str, Any] = {
            "window": json_value(self.window),
            "metrics": [metric.to_json() for metric in self.metrics],
            "baselines": [baseline.to_json() for baseline in self.baselines],
        }
        if self.sample is not None:
            rendered["sample"] = self.sample.to_json()
        if self.test is not None:
            rendered["test"] = self.test.to_json()
        rendered["thresholds"] = json_value(self.thresholds)
        rendered["datasets"] = [status.to_json() for status in self.datasets]
        rendered["provenance"] = [entry.to_json() for entry in self.provenance]
        if self.extra:
            rendered["extra"] = json_value(self.extra)
        return rendered


_EVIDENCE_FIELDS: Final[tuple[str, ...]] = tuple(part.name for part in fields(Evidence))
"""Every :class:`Evidence` field name, in declaration order, for ``Evidence._derived``."""


def _unique_flags(flags: tuple[Flag, ...]) -> tuple[Flag, ...]:
    """Drop flags whose code already appeared, keeping the first message of each code."""
    seen: set[str] = set()
    unique: list[Flag] = []
    for flag in flags:
        if flag.code not in seen:
            seen.add(flag.code)
            unique.append(flag)
    return tuple(unique)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One action a rule proposes, before precedence ranks, holds or drops it (AAP 0.5.1).

    Rules fill the first twelve fields. ``precedence.py`` fills the derived ones with
    :func:`dataclasses.replace`: it expands :attr:`depends_on`, sets :attr:`held` with
    :attr:`held_reason` and :attr:`data_status`, and pins C0 items.

    Attributes:
        rule_id: One of :data:`RULE_IDS`.
        section: One of :data:`SECTION_ORDER`.
        group: The group key within the section (:data:`GROUP_ORDER`, :func:`creative_group`
            or :func:`cohort_group`).
        entity_ref: The entity the item is about.
        action: The recommended action as a headline, for example "Pause".
        reason: The reason sentence, rendered from this item's evidence only.
        evidence: The frozen evidence.
        rank_key: Sorts ascending within the group; rules negate values to rank descending.
            Ties are broken by ``entity_ref.name``, then ``entity_ref.id`` (entities with an id
            first), then ``entity_ref.key`` (:attr:`sort_key`).
        flags: Badges set by the rule (floors, ``pipeline_unavailable``, ``shared_budget`` …);
            a repeated code keeps its first message.
        required_datasets: The dataset names the rule read, from which the item's ``stale``
            and ``incomplete`` flags derive; repeats are dropped.
        increases_spend_on: The campaigns and budgets whose spend this item would raise (C2
            Scale, B1, a B3 receiver, a W1 double down); G1 removes it when any is blocked.
        decision_subject: The campaign or budget a "Record decision" on this item would write,
            carried only by C1, C1b, C2 "Scale", B1, B2 and B3 items; ``None`` for every other
            item: C0 offers its resolver instead, and the rest offer "Mark done".
        depends_on: Subjects whose unresolved discrepancy holds this item (P1).
        held: Whether the item is held.
        held_reason: Why; required exactly when :attr:`held`.
        data_status: ``live``, ``simulated``, ``stale`` or ``mixed``; set by precedence.
        pinned: Pinned first in its section, oldest first; only C0 items are pinned.

    Raises:
        ValueError: On construction, for an unknown rule id, section, dataset or data status,
            a blank group, action or reason, a hold without a reason (or a reason without a
            hold), or a pinned item that is not C0.
        TypeError: On construction, when a field has the wrong type.
    """

    rule_id: str
    section: str
    group: str
    entity_ref: EntityRef
    action: str
    reason: str
    evidence: Evidence
    rank_key: tuple[Any, ...]
    flags: tuple[Flag, ...]
    required_datasets: tuple[str, ...]
    increases_spend_on: tuple[DecisionSubject, ...]
    decision_subject: DecisionSubject | None
    depends_on: tuple[DecisionSubject, ...] = ()
    held: bool = False
    held_reason: str | None = None
    data_status: str | None = None
    pinned: bool = False

    def __post_init__(self) -> None:
        """Validate the fields and normalise the sequences."""
        _require_in(self.rule_id, RULE_IDS, "Candidate.rule_id")
        _require_in(self.section, SECTION_ORDER, "Candidate.section")
        _require_text(self.group, "Candidate.group")
        if not isinstance(self.entity_ref, EntityRef):
            raise TypeError(f"Candidate.entity_ref must be an EntityRef, found {self.entity_ref!r}")
        _require_text(self.action, "Candidate.action")
        _require_text(self.reason, "Candidate.reason")
        if not isinstance(self.evidence, Evidence):
            raise TypeError(f"Candidate.evidence must be an Evidence, found {self.evidence!r}")
        rank_key: object = self.rank_key
        if not isinstance(rank_key, list | tuple):
            raise TypeError(f"Candidate.rank_key must be a tuple, found {rank_key!r}")
        object.__setattr__(self, "rank_key", tuple(rank_key))
        flags = _tuple_of(self.flags, Flag, "Candidate.flags")
        object.__setattr__(self, "flags", _unique_flags(flags))
        datasets = _tuple_of(self.required_datasets, str, "Candidate.required_datasets")
        for name in datasets:
            _require_in(name, DATASETS, "Candidate.required_datasets item")
        object.__setattr__(self, "required_datasets", tuple(dict.fromkeys(datasets)))
        object.__setattr__(
            self,
            "increases_spend_on",
            _tuple_of(self.increases_spend_on, DecisionSubject, "Candidate.increases_spend_on"),
        )
        if self.decision_subject is not None and not isinstance(
            self.decision_subject, DecisionSubject
        ):
            raise TypeError(
                f"Candidate.decision_subject must be a DecisionSubject, "
                f"found {self.decision_subject!r}"
            )
        depends_on = _tuple_of(self.depends_on, DecisionSubject, "Candidate.depends_on")
        object.__setattr__(self, "depends_on", tuple(dict.fromkeys(depends_on)))
        if self.held:
            _require_text(self.held_reason, "Candidate.held_reason of a held item")
        elif self.held_reason is not None:
            raise ValueError("Candidate.held_reason is set on an item that is not held")
        if self.data_status is not None:
            _require_in(self.data_status, DATA_STATUSES, "Candidate.data_status")
        if self.pinned and self.rule_id != C0:
            raise ValueError(f"only C0 items are pinned, found a pinned {self.rule_id} item")

    @property
    def flag_codes(self) -> tuple[str, ...]:
        """The codes of :attr:`flags`, in order."""
        return tuple(flag.code for flag in self.flags)

    def has_flag(self, code: str) -> bool:
        """Return whether the item carries a flag with ``code``."""
        return code in self.flag_codes

    def with_flags(self, *flags: Flag) -> Candidate:
        """Return a copy with ``flags`` appended; a code already present is not repeated."""
        return replace(self, flags=_unique_flags((*self.flags, *flags)))

    @property
    def is_discrepancy(self) -> bool:
        """Whether this is a C0 item, exempt from holds, feedback and ordinary ranking."""
        return self.rule_id == C0

    @property
    def feedback_key(self) -> tuple[str, str, str]:
        """``(rule_id, entity type, entity key)``, the match key of ``recommendation_feedback``."""
        return (self.rule_id, self.entity_ref.type, self.entity_ref.key)

    @property
    def sort_key(self) -> tuple[tuple[Any, ...], str, tuple[bool, int], str]:
        """``(rank_key, entity name, entity id, entity key)``: deterministic order within a group.

        Ranking ties are broken by entity name, then by :attr:`EntityRef.id`. The id slot is
        ``(False, id)`` for an entity with an internal id and ``(True, 0)`` for a composite
        entity without one, so entities with an id come first and ``int`` is never compared
        with ``None``. :attr:`EntityRef.key` breaks any remaining tie, between id-less entities
        of one name or entities of different types sharing a name and an id, so the order is
        total and independent of input order.
        """
        ref = self.entity_ref
        id_slot = (True, 0) if ref.id is None else (False, ref.id)
        return (self.rank_key, ref.name, id_slot, ref.key)

    @property
    def spend_targets(self) -> frozenset[SubjectKey]:
        """:attr:`increases_spend_on` as subject keys, for the G1 intersection."""
        return frozenset(subject.key for subject in self.increases_spend_on)


@dataclass(frozen=True, slots=True)
class Finding:
    """A non-action observation a rule emits and the run stores (AAP 0.5.3 ``Finding``).

    Attributes:
        code: One of :data:`FINDING_CODES`.
        section: The section it is shown in (:data:`SECTION_ORDER`).
        entity: The entity it is about; ``None`` for a section- or account-wide finding.
        message: The rendered message.
        evidence: The numbers behind the message, deep-copied on construction into a
            read-only tree (as :class:`Evidence` does), so the stored finding never changes.

    Raises:
        ValueError: On construction, for an unknown code or section or a blank message.
        TypeError: On construction, when ``entity`` or ``evidence`` has the wrong type,
            ``evidence`` has a non-string key, or a value nested in it cannot be frozen,
            including a frozen dataclass that is not deeply immutable (see :class:`Evidence`).
    """

    code: str
    section: str
    entity: EntityRef | None
    message: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the fields and deep-freeze the evidence mapping."""
        _require_in(self.code, FINDING_CODES, "Finding.code")
        _require_in(self.section, SECTION_ORDER, "Finding.section")
        if self.entity is not None and not isinstance(self.entity, EntityRef):
            raise TypeError(f"Finding.entity must be an EntityRef, found {self.entity!r}")
        _require_text(self.message, "Finding.message")
        object.__setattr__(self, "evidence", _frozen_mapping(self.evidence, "Finding.evidence"))

    def to_json(self) -> dict[str, Any]:
        """Return ``{code, section, entity?, message, evidence}``."""
        rendered: dict[str, Any] = {"code": self.code, "section": self.section}
        if self.entity is not None:
            rendered["entity"] = self.entity.to_json()
        rendered["message"] = self.message
        rendered["evidence"] = json_value(self.evidence)
        return rendered


@dataclass(frozen=True, slots=True)
class Withheld:
    """A pattern withheld for sample size or validity; it never becomes an item (P6).

    Attributes:
        rule_id: The rule whose pattern was withheld (:data:`RULE_IDS`).
        section: The section that lists it (:data:`SECTION_ORDER`).
        entity: The entity it is about; ``None`` for an account-wide withholding.
        reason: Names the shortfall, for example "spending days 3 of 5 required; known-budget
            days 9 of 7" or "weighted attribution: rate test not valid".
        observed: The sample observed, in ``unit``; ``None`` when no sample applies.
        required: The minimum required, in ``unit``; ``None`` when no sample applies.
        unit: The sample's unit, for example ``clicks`` or ``days``.

    Raises:
        ValueError: On construction, for an unknown rule id or section, or a blank reason or
            unit.
        TypeError: On construction, for a sample size that is not ``Decimal``, ``int`` or
            ``None``.
    """

    rule_id: str
    section: str
    entity: EntityRef | None
    reason: str
    observed: Number | None
    required: Number | None
    unit: str

    def __post_init__(self) -> None:
        """Validate the fields."""
        _require_in(self.rule_id, RULE_IDS, "Withheld.rule_id")
        _require_in(self.section, SECTION_ORDER, "Withheld.section")
        if self.entity is not None and not isinstance(self.entity, EntityRef):
            raise TypeError(f"Withheld.entity must be an EntityRef, found {self.entity!r}")
        _require_text(self.reason, "Withheld.reason")
        _require_number(self.observed, "Withheld.observed", optional=True)
        _require_number(self.required, "Withheld.required", optional=True)
        _require_text(self.unit, "Withheld.unit")

    def to_json(self) -> dict[str, Any]:
        """Return AAP 0.5.3 ``Withheld``: ``{rule_id, entity, reason, sample}``."""
        return {
            "rule_id": self.rule_id,
            "entity": None if self.entity is None else self.entity.to_json(),
            "reason": self.reason,
            "sample": {
                "observed": json_value(self.observed),
                "required": json_value(self.required),
                "unit": self.unit,
            },
        }


@dataclass(frozen=True, slots=True)
class RuleResult:
    """What one rule module's ``evaluate(ctx)`` returns (AAP 0.5.1).

    Attributes:
        candidates: Proposed actions.
        findings: Non-action observations.
        withheld: Patterns below their minimums, with observed and required sizes.
        suppressed: Items the rule itself suppressed, counted by reason; a read-only copy of
            the mapping given, so a stored result's counts never change.

    Raises:
        TypeError: On construction, when an element has the wrong type, ``suppressed`` is not
            a mapping or one of its reasons is not a string.
        ValueError: On construction, for a blank reason or a negative count.
    """

    candidates: tuple[Candidate, ...] = ()
    findings: tuple[Finding, ...] = ()
    withheld: tuple[Withheld, ...] = ()
    suppressed: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise the sequences, copy ``suppressed`` read-only and validate its counts."""
        object.__setattr__(
            self, "candidates", _tuple_of(self.candidates, Candidate, "RuleResult.candidates")
        )
        object.__setattr__(
            self, "findings", _tuple_of(self.findings, Finding, "RuleResult.findings")
        )
        object.__setattr__(
            self, "withheld", _tuple_of(self.withheld, Withheld, "RuleResult.withheld")
        )
        # Every count is checked to be an int below, so the shallow copy is wholly immutable.
        counts = _read_only_mapping(self.suppressed, "RuleResult.suppressed")
        for reason, count in counts.items():
            if not isinstance(reason, str):
                raise TypeError(f"RuleResult.suppressed keys must be strings, found {reason!r}")
            _require_text(reason, "RuleResult.suppressed reason")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(
                    f"RuleResult.suppressed[{reason!r}] must be a non-negative int, found {count!r}"
                )
        object.__setattr__(self, "suppressed", counts)

    def merge(self, *others: RuleResult) -> RuleResult:
        """Return this result followed by ``others``, with ``suppressed`` summed by reason.

        Works as ``a.merge(b, c)``, ``RuleResult.merge(a, b)`` and ``RuleResult().merge(*rs)``;
        the inputs are unchanged and their order is kept.
        """
        results = (self, *others)
        suppressed: dict[str, int] = {}
        for result in results:
            for reason, count in result.suppressed.items():
                suppressed[reason] = suppressed.get(reason, 0) + count
        return RuleResult(
            candidates=tuple(c for result in results for c in result.candidates),
            findings=tuple(f for result in results for f in result.findings),
            withheld=tuple(w for result in results for w in result.withheld),
            suppressed=suppressed,
        )


# ---------------------------------------------------------------------------
# Input types: datasets and freshness (AAP 0.3.2, 0.3.3)
# ---------------------------------------------------------------------------

FULL_STATE_DATASETS: Final[frozenset[str]] = frozenset({"google.entities", "linkedin.entities"})
"""Datasets an entity sync re-reads in full current state, with no native date range.

Their ``dataset_freshness`` rows record no coverage of their own (``sync/freshness.py``
``FreshnessOutcome``: a success with no range is a full-state read), so the window checks of
:class:`Freshness` judge them by completeness and age alone.
"""


@dataclass(frozen=True, slots=True)
class DatasetRef:
    """A ``source_dataset`` a context reads: a source's active primary or a CRM companion.

    Attributes:
        dataset_id: The ``source_dataset`` id.
        source: The dataset's source.
        mode: ``live`` or ``simulated``.
        role: ``primary``, or ``companion`` for the simulated CRM of a simulated LinkedIn
            dataset (always a HubSpot dataset).

    Raises:
        ValueError: On construction, for an unknown source, mode or role, or a companion that
            is not a simulated HubSpot dataset.
        TypeError: On construction, when ``dataset_id`` is not an ``int``.
    """

    dataset_id: int
    source: Source
    mode: DatasetMode
    role: DatasetRole = "primary"

    def __post_init__(self) -> None:
        """Validate the fields and the companion shape."""
        _require_id(self.dataset_id, "DatasetRef.dataset_id")
        _require_in(self.source, SOURCES, "DatasetRef.source")
        _require_in(self.mode, _DATASET_MODES, "DatasetRef.mode")
        _require_in(self.role, _DATASET_ROLES, "DatasetRef.role")
        if self.role == "companion" and (self.source != "hubspot" or self.mode != "simulated"):
            raise ValueError("a companion dataset is always a simulated HubSpot dataset")

    def provenance(self) -> ProvenanceEntry:
        """Return this dataset as a frozen :class:`ProvenanceEntry`."""
        return ProvenanceEntry(self.source, self.dataset_id, self.mode, self.role)


@dataclass(frozen=True, slots=True)
class Freshness:
    """One ``dataset_freshness`` row: when a dataset was refreshed and what it covers.

    Attributes:
        dataset_id: The ``source_dataset`` the row belongs to.
        dataset: The dataset name, one of ``db.models.ops.DATASETS``.
        last_attempt_at: The last refresh attempt, stored as the same instant in UTC.
        last_success_at: The last successful refresh, stored as the same instant in UTC;
            ``None`` before the first one.
        covered_from: The first native date covered.
        covered_through: The last native date covered.
        completeness: ``complete``, ``incomplete`` or ``unknown`` over the covered range.
        reason: Why the dataset is incomplete or unknown, when it is.

    Raises:
        ValueError: On construction, for an unknown dataset or completeness, or a naive
            instant.
        TypeError: On construction, for a wrongly typed id, instant or date.
    """

    dataset_id: int
    dataset: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    covered_from: date | None
    covered_through: date | None
    completeness: Completeness
    reason: str | None = None

    def __post_init__(self) -> None:
        """Validate the fields and store both instants in UTC.

        Two datetimes sharing one ``tzinfo`` compare and subtract by wall time, which is
        wrong across a DST change; UTC storage makes every later comparison, subtraction
        and ``min``/``max`` over these instants exact.
        """
        _require_id(self.dataset_id, "Freshness.dataset_id")
        _require_in(self.dataset, DATASETS, "Freshness.dataset")
        _require_instant(self.last_attempt_at, "Freshness.last_attempt_at", optional=True)
        _require_instant(self.last_success_at, "Freshness.last_success_at", optional=True)
        _require_native_date(self.covered_from, "Freshness.covered_from", optional=True)
        _require_native_date(self.covered_through, "Freshness.covered_through", optional=True)
        _require_in(self.completeness, _COMPLETENESS_VALUES, "Freshness.completeness")
        if self.last_attempt_at is not None:
            object.__setattr__(self, "last_attempt_at", self.last_attempt_at.astimezone(UTC))
        if self.last_success_at is not None:
            object.__setattr__(self, "last_success_at", self.last_success_at.astimezone(UTC))

    def is_stale(self, *, now: datetime, stale_after: timedelta, window_end: date) -> bool:
        """Return whether the dataset is stale for a window ending on ``window_end`` (P4).

        Stale when it last succeeded more than ``stale_after`` (``sources.stale_after_hours``)
        before ``now``, never succeeded, or covers less than the window end (AAP 0.3.2). A
        full-state dataset (:data:`FULL_STATE_DATASETS`) records no date range, so only its
        age can make it stale. The age is the elapsed time between the two instants, taken on
        UTC operands, so it stays exact when ``now`` and the last success straddle a DST
        change.

        Args:
            now: The tz-aware instant the age is judged at.
            stale_after: ``sources.stale_after_hours`` as a duration.
            window_end: The last native date of the window.

        Raises:
            TypeError: When ``now`` is not a ``datetime``.
            ValueError: When ``now`` is naive.
        """
        _require_instant(now, "Freshness.is_stale now")
        if self.last_success_at is None:
            return True
        if now.astimezone(UTC) - self.last_success_at.astimezone(UTC) > stale_after:
            return True
        if self.dataset in FULL_STATE_DATASETS:
            return False
        return self.covered_through is None or self.covered_through < window_end

    @property
    def is_complete(self) -> bool:
        """Whether the dataset is ``complete`` over its covered range."""
        return self.completeness == "complete"

    def is_complete_over(self, *, window_start: date, window_end: date) -> bool:
        """Return whether the dataset is complete over the range ``[window_start, window_end]``.

        AAP 0.3.2 (P4): an item is incomplete "when any of its datasets is not ``complete``
        over the window". :attr:`completeness` describes only the covered range, so a dated
        dataset is complete over the required range only when its coverage starts on or
        before ``window_start`` and ends on or after ``window_end``, and it is ``complete``, or
        ``incomplete`` only for a dated shortfall none of whose days lies in the range: a Q-G6
        finest-partition reason names the days of its partitions at the result limit
        (:func:`~ads_engine.analysis.shortfall.shortfall_spans`), so a range without them
        lacks nothing. An ``unknown`` row, and an ``incomplete`` one whose reason names no day,
        is incomplete over every range. The range is the run's window, or wider where a rule
        reads further back: the overlay passes a K1 item's ``google.metrics_daily`` the start
        of its frozen baseline window. Coverage that stops before ``window_end`` is therefore
        incomplete as well as stale (:meth:`is_stale`). A full-state dataset
        (:data:`FULL_STATE_DATASETS`) has no date range, so its completeness alone decides.

        Example:
            >>> covered = (date(2026, 9, 20), date(2026, 10, 1))
            >>> row = Freshness(3, "google.metrics_daily", None, None, *covered, "complete")
            >>> row.is_complete_over(window_start=date(2026, 9, 20), window_end=date(2026, 10, 1))
            True
            >>> row.is_complete_over(window_start=date(2026, 9, 4), window_end=date(2026, 10, 1))
            False
            >>> row.is_complete_over(window_start=date(2026, 9, 20), window_end=date(2026, 10, 2))
            False
            >>> capped = replace(
            ...     row,
            ...     dataset="google.search_terms",
            ...     completeness="incomplete",
            ...     reason="result limit reached at finest partition (2026-09-24, ad group 7)",
            ... )
            >>> capped.is_complete_over(
            ...     window_start=date(2026, 9, 25), window_end=date(2026, 10, 1)
            ... )
            True
            >>> capped.is_complete_over(
            ...     window_start=date(2026, 9, 20), window_end=date(2026, 10, 1)
            ... )
            False
        """
        if self.dataset in FULL_STATE_DATASETS:
            return self.completeness == "complete"
        if (
            self.covered_from is None
            or self.covered_from > window_start
            or self.covered_through is None
            or self.covered_through < window_end
        ):
            return False
        if self.completeness != "incomplete":
            return self.completeness == "complete"
        spans = shortfall_spans(self.reason)
        return spans is not None and all(
            last < window_start or first > window_end for first, last in spans
        )

    def incomplete_reason_over(self, *, window_start: date, window_end: date) -> str | None:
        """Return what the row's completeness lacks over ``[window_start, window_end]`` (P4).

        For the message of an item or a finding judged by :meth:`is_complete_over`: ``None``
        for a ``complete`` row. A dated shortfall (an ``incomplete`` dated dataset whose reason
        names its days) is restricted to the range's days
        (:func:`~ads_engine.analysis.shortfall.shortfall_within`), keeping a failure note, and
        is ``None`` when none of its days is inside, so a message never names a day outside the
        range. Any other row gives its reason, else its completeness. Coverage short of the
        range is not named here; callers name it from :attr:`covered_from` and
        :attr:`covered_through`, so a row :meth:`is_complete_over` judges incomplete always
        has something to name.

        Example:
            >>> row = Freshness(
            ...     3,
            ...     "google.search_terms",
            ...     None,
            ...     None,
            ...     date(2026, 9, 1),
            ...     date(2026, 10, 1),
            ...     "incomplete",
            ...     "result limit reached at finest partition (2026-09-24, ad group 7; "
            ...     "2026-09-28, ad group 8)",
            ... )
            >>> row.incomplete_reason_over(
            ...     window_start=date(2026, 9, 25), window_end=date(2026, 10, 1)
            ... )
            'result limit reached at finest partition (2026-09-28, ad group 8)'
            >>> row.incomplete_reason_over(
            ...     window_start=date(2026, 9, 29), window_end=date(2026, 10, 1)
            ... )
            >>> replace(row, reason=None).incomplete_reason_over(
            ...     window_start=date(2026, 9, 29), window_end=date(2026, 10, 1)
            ... )
            'incomplete'

        Args:
            window_start: The first native date of the required range.
            window_end: The last native date of the required range.

        Returns:
            The shortfall to name over the range, or ``None`` when the row's completeness
            lacks nothing there.

        Raises:
            ValueError: If ``window_start`` is after ``window_end`` and the row holds a dated
                shortfall.
        """
        if self.completeness == "complete":
            return None
        if (
            self.completeness == "incomplete"
            and self.dataset not in FULL_STATE_DATASETS
            and shortfall_spans(self.reason) is not None
        ):
            return shortfall_within(self.reason, start=window_start, end=window_end)
        return self.reason or self.completeness

    def status(self, *, boundary: str | None = None) -> DatasetStatus:
        """Return the dataset's frozen :class:`DatasetStatus` for evidence.

        Args:
            boundary: The IANA zone whose midnight ends the dataset's coverage dates
                (:meth:`AnalysisContext.make_evidence` passes its source's). It is recorded
                only with a ``covered_through``: a row without one, such as a full-state entity
                dataset, has no date to label.

        Raises:
            ValueError: When ``boundary`` is blank.
        """
        return DatasetStatus(
            self.dataset,
            self.completeness,
            self.covered_through,
            boundary if self.covered_through is not None else None,
        )


# ---------------------------------------------------------------------------
# Input types: entities of the active datasets (AAP 0.5.2)
# ---------------------------------------------------------------------------

_AD_SOURCES: Final[tuple[str, ...]] = ("google", "linkedin")
_CAMPAIGN_STATUSES: Final[tuple[str, ...]] = get_args(CampaignStatus)
_BUDGET_PERIODS: Final[tuple[str, ...]] = get_args(BudgetPeriod)


@dataclass(frozen=True, slots=True)
class AccountInfo:
    """An ad account of the active Google or LinkedIn dataset.

    Attributes:
        id: Internal id.
        source: ``google`` or ``linkedin``.
        external_id: Customer id or sponsored account id.
        name: Display name (the external id when the platform gives none).
        currency: ISO currency code; amounts are never converted or summed across currencies.
        timezone: IANA time zone of the account's native day (Google); ``None`` for LinkedIn.
    """

    id: int
    source: Source
    external_id: str
    name: str
    currency: str
    timezone: str | None = None

    def __post_init__(self) -> None:
        """Validate the id, source and currency."""
        _require_id(self.id, "AccountInfo.id")
        _require_in(self.source, _AD_SOURCES, "AccountInfo.source")
        _require_text(self.currency, "AccountInfo.currency")


@dataclass(frozen=True, slots=True)
class CampaignGroupInfo:
    """A LinkedIn campaign group, shown in campaign evidence (never a decision subject).

    Attributes:
        id: Internal id.
        account_id: The owning :class:`AccountInfo` id.
        external_id: LinkedIn campaign group id.
        name: Display name.
        status: Raw LinkedIn status.
        daily_budget: Group daily budget, when set.
        total_budget: Group total budget, when set.
    """

    id: int
    account_id: int
    external_id: str
    name: str
    status: str
    daily_budget: Decimal | None = None
    total_budget: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CampaignInfo:
    """A campaign of the active Google or LinkedIn dataset, with its current state.

    Attributes:
        id: Internal id.
        source: ``google`` or ``linkedin``.
        account_id: The owning :class:`AccountInfo` id.
        external_id: Platform campaign id.
        name: Display name.
        status: Normalised status: ``active``, ``paused`` or ``ended``.
        raw_status: The platform's own status.
        objective_or_channel: Google advertising channel type or LinkedIn ``objectiveType``.
        objective_group: :func:`objective_group` of ``source`` and ``objective_or_channel``.
        campaign_group_id: LinkedIn campaign group id, when any.
        budget_id: Google ``campaign_budget`` the campaign draws from, when any.
        daily_budget: Current daily budget (LinkedIn ``dailyBudget``, Google ``DAILY``).
        total_budget: Current lifetime / custom-period budget.
        budget_period: Google budget period; ``None`` for LinkedIn.
        currency: The account currency.

    Raises:
        ValueError: On construction, for an unknown source or status, or an objective group
            that disagrees with :func:`objective_group`.
    """

    id: int
    source: Source
    account_id: int
    external_id: str
    name: str
    status: CampaignStatus
    raw_status: str | None
    objective_or_channel: str | None
    objective_group: str
    campaign_group_id: int | None
    budget_id: int | None
    daily_budget: Decimal | None
    total_budget: Decimal | None
    budget_period: BudgetPeriod | None
    currency: str

    def __post_init__(self) -> None:
        """Validate the id, source, status, period and objective group."""
        _require_id(self.id, "CampaignInfo.id")
        _require_in(self.source, _AD_SOURCES, "CampaignInfo.source")
        _require_in(self.status, _CAMPAIGN_STATUSES, "CampaignInfo.status")
        if self.budget_period is not None:
            _require_in(self.budget_period, _BUDGET_PERIODS, "CampaignInfo.budget_period")
        expected = objective_group(self.source, self.objective_or_channel)
        if self.objective_group != expected:
            raise ValueError(
                f"CampaignInfo {self.external_id}: objective_group {self.objective_group!r} "
                f"disagrees with {expected!r} for {self.source} {self.objective_or_channel!r}"
            )
        _require_text(self.currency, "CampaignInfo.currency")


@dataclass(frozen=True, slots=True)
class BudgetInfo:
    """A Google ``campaign_budget``; an explicitly shared one is a decision subject.

    Attributes:
        id: Internal id.
        external_id: Google budget id.
        name: Display name. The platform stores none, so an empty string becomes
            "Shared budget {external_id}" (or "Budget {external_id}" when not shared).
        amount: The budget amount in the account currency.
        amount_micros: The amount at native precision, for the C0 comparison.
        period: ``DAILY`` or ``CUSTOM_PERIOD``.
        explicitly_shared: Whether the budget is shared across campaigns.
        currency: The account currency.

    Raises:
        ValueError: On construction, for an unknown period.
    """

    id: int
    external_id: str
    name: str
    amount: Decimal | None
    amount_micros: int | None
    period: BudgetPeriod
    explicitly_shared: bool
    currency: str

    def __post_init__(self) -> None:
        """Validate the period and fill the default name."""
        _require_id(self.id, "BudgetInfo.id")
        _require_in(self.period, _BUDGET_PERIODS, "BudgetInfo.period")
        _require_text(self.currency, "BudgetInfo.currency")
        name: object = self.name
        if not isinstance(name, str) or not name.strip():
            label = "Shared budget" if self.explicitly_shared else "Budget"
            object.__setattr__(self, "name", f"{label} {self.external_id}")


@dataclass(frozen=True, slots=True)
class AdGroupInfo:
    """A Google ad group.

    Attributes:
        id: Internal id.
        campaign_id: The owning campaign.
        external_id: Google ad group id.
        name: Display name.
        status: Raw status.
    """

    id: int
    campaign_id: int
    external_id: str
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class KeywordInfo:
    """A Google keyword criterion from Q-G7, including keywords with no traffic.

    Attributes:
        id: Internal id.
        ad_group_id: The owning ad group.
        campaign_id: The ad group's campaign.
        criterion_id: Google criterion id (unique only within its ad group).
        text: Keyword text.
        match_type: ``EXACT``, ``PHRASE`` or ``BROAD``.
        status: Raw status.
        quality_score: Current ``quality_info.quality_score``; ``None`` when unpopulated.
    """

    id: int
    ad_group_id: int
    campaign_id: int
    criterion_id: str
    text: str
    match_type: str
    status: str
    quality_score: int | None = None


@dataclass(frozen=True, slots=True)
class CreativeInfo:
    """A LinkedIn creative.

    Attributes:
        id: Internal id.
        campaign_id: The owning campaign.
        urn: ``urn:li:sponsoredCreative:{id}``.
        intended_status: The creative's intended status.
        headline: Post headline.
        intro_text: Post intro text.
        cta_label: Call-to-action label.
        lead_form_urn: Canonical ``urn:li:leadGenForm:{id}`` of its call-to-action form.
    """

    id: int
    campaign_id: int
    urn: str
    intended_status: str | None = None
    headline: str | None = None
    intro_text: str | None = None
    cta_label: str | None = None
    lead_form_urn: str | None = None


@dataclass(frozen=True, slots=True)
class ConversionActionInfo:
    """A Google conversion action from Q-G8, the source of the B4 counting contract.

    Attributes:
        id: Internal id.
        external_id: Google conversion action id.
        name: Display name.
        status: Raw status.
        include_in_conversions_metric: Whether it counts toward ``metrics.conversions``.
        counting_type: ``ONE_PER_CLICK`` or ``MANY_PER_CLICK``.
        attribution_model: For example ``GOOGLE_ADS_LAST_CLICK``.
    """

    id: int
    external_id: str
    name: str
    status: str
    include_in_conversions_metric: bool
    counting_type: str | None = None
    attribution_model: str | None = None


# ---------------------------------------------------------------------------
# Input types: facts aggregated over the window (AAP 0.5.2, 0.5.4.1)
# ---------------------------------------------------------------------------


_FLOOR_COUNTS: Final[tuple[str, ...]] = (
    "form_opens",
    "leads",
    "landing_page_clicks",
    "website_conversions",
)
"""The optional :class:`DayFact` counts a reported floor can stand for (whole numbers)."""

_FLOOR_METRICS: Final[tuple[str, ...]] = ("conversions", "conversions_value", *_FLOOR_COUNTS)
"""Every optional :class:`DayFact` metric summed across fact rows, in field order: the
metrics :attr:`DayFact.reported_floors` can name."""


def _day_floors(fact: DayFact) -> tuple[tuple[str, int | Decimal], ...]:
    """Validate :attr:`DayFact.reported_floors` and return it in :data:`_FLOOR_METRICS` order.

    Raises:
        TypeError: When the floors are not a tuple or list of ``(name, value)`` pairs, or a
            count's value is not an ``int`` or an amount's not a ``Decimal`` or ``int``.
        ValueError: For a name outside :data:`_FLOOR_METRICS`, a name given twice, a floor on
            a metric the day reports, or a negative or non-finite value.
    """
    floors: object = fact.reported_floors
    if not isinstance(floors, tuple | list):
        raise TypeError(f"DayFact.reported_floors must be a tuple of pairs, found {floors!r}")
    found: dict[str, int | Decimal] = {}
    for entry in floors:
        if not isinstance(entry, tuple | list) or len(entry) != 2:
            raise TypeError(f"DayFact.reported_floors holds {entry!r}, not a (name, value) pair")
        name, value = entry
        _require_in(name, _FLOOR_METRICS, "DayFact.reported_floors name")
        what = f"DayFact.reported_floors[{name!r}]"
        if name in found:
            raise ValueError(f"{what} is given twice")
        if getattr(fact, name) is not None:
            raise ValueError(
                f"{what}: a floor stands only for an unavailable metric, and {name} is reported"
            )
        if name in _FLOOR_COUNTS and (isinstance(value, bool) or not isinstance(value, int)):
            raise TypeError(f"{what} must be an int count, found {value!r}")
        _require_number(value, what)
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError(f"{what} must be finite, found {value!r}")
        if value < 0:
            raise ValueError(f"{what} must not be negative, found {value!r}")
        found[name] = value
    return tuple((name, found[name]) for name in _FLOOR_METRICS if name in found)


@dataclass(frozen=True, slots=True)
class DayFact:
    """One campaign's facts on one native date.

    Google rows come from ``fact_campaign_daily`` (dates in the account's time zone); LinkedIn
    rows are ``fact_creative_daily`` summed per campaign per UTC date. Metrics a source does
    not report are ``None``. A LinkedIn optional metric (conversions, conversion value, form
    opens, leads, landing-page clicks, website conversions) is ``None`` as soon as one
    creative row of the day did not report it, so a partial sum never reads as the day's
    count (AAP 0.11.1); the rows that did report it keep their sum in
    :attr:`reported_floors`.

    Attributes:
        date: The source's native date.
        cost: Spend in the account currency.
        impressions: Impressions.
        clicks: Clicks.
        conversions: Google ``metrics.conversions`` (fractional under weighted attribution).
        conversions_value: Google conversion value.
        form_opens: LinkedIn ``oneClickLeadFormOpens``.
        leads: LinkedIn ``oneClickLeads``.
        landing_page_clicks: LinkedIn ``landingPageClicks``.
        website_conversions: LinkedIn ``externalWebsiteConversions``.
        budget_lost_is: Google ``search_budget_lost_impression_share`` (a share in [0, 1]).
        impression_share: Google ``search_impression_share``.
        reported_floors: ``(metric, sum)`` pairs for optional metrics only some of the day's
            fact rows reported: the metric's attribute stays ``None`` (unavailable, never a
            count) and the sum of the rows that did report it is a lower bound of the day's
            value. Stored in field order, at most once per metric; empty when each summed
            metric was reported by every row or by none. Only :meth:`recorded_outcome` reads
            it.

    Raises:
        TypeError: On construction, when ``date`` is not a native date, or a reported floor
            is not a ``(name, value)`` pair of its metric's number type.
        ValueError: On construction, when a reported floor names an unknown or a reported
            metric, repeats one, or is negative or non-finite.
    """

    date: date
    cost: Decimal
    impressions: int
    clicks: int
    conversions: Decimal | None = None
    conversions_value: Decimal | None = None
    form_opens: int | None = None
    leads: int | None = None
    landing_page_clicks: int | None = None
    website_conversions: int | None = None
    budget_lost_is: Decimal | None = None
    impression_share: Decimal | None = None
    reported_floors: tuple[tuple[str, int | Decimal], ...] = ()

    def __post_init__(self) -> None:
        """Require a native date (daily facts are never rebucketed) and valid floors."""
        _require_native_date(self.date, "DayFact.date")
        object.__setattr__(self, "reported_floors", _day_floors(self))

    def outcome(self, group: str) -> Decimal | None:
        """Return the day's outcome for an objective group (:data:`OUTCOME_FIELDS`).

        Raises:
            KeyError: For an unknown objective group.
        """
        value: Decimal | int | None = getattr(self, OUTCOME_FIELDS[group])
        return None if value is None else Decimal(value)

    def recorded_outcome(self, group: str) -> Decimal | None:
        """Return the outcome the day's fact rows recorded for an objective group.

        The complete outcome (:meth:`outcome`) when every row reported it; otherwise the sum
        of the rows that did (:attr:`reported_floors`), a lower bound of the day's outcome;
        ``None`` when no row reported it. For P2's "was any outcome recorded" check only:
        every count, rate and efficiency reads :meth:`outcome`, which keeps a partially
        reported day unavailable.

        Raises:
            KeyError: For an unknown objective group.
        """
        value = self.outcome(group)
        if value is not None:
            return value
        name = OUTCOME_FIELDS[group]
        for metric, floor in self.reported_floors:
            if metric == name:
                return Decimal(floor)
        return None


@dataclass(frozen=True, slots=True)
class CreativeTotals:
    """One LinkedIn creative's totals over the window (CR1-CR3, white papers).

    An optional count is ``None`` unless every window day of the creative reported it: one
    unreported day makes the total unavailable rather than a partial count (AAP 0.11.1), so a
    stage whose numerator or denominator is ``None`` stays unmeasured.

    Attributes:
        creative_id: The creative.
        campaign_id: Its campaign.
        cost: Window spend.
        impressions: Window impressions.
        clicks: Window clicks.
        form_opens: Window lead form opens; ``None`` when a window day did not report them.
        leads: Window native leads; ``None`` when a window day did not report them.
        landing_page_clicks: Window landing-page clicks; ``None`` when a window day did not
            report them.
        website_conversions: Window website conversions; ``None`` when a window day did not
            report them.
        delivery_days: Native days with impressions > 0, for per-variant delivery projections.
    """

    creative_id: int
    campaign_id: int
    cost: Decimal
    impressions: int
    clicks: int
    form_opens: int | None
    leads: int | None
    landing_page_clicks: int | None
    website_conversions: int | None
    delivery_days: int


@dataclass(frozen=True, slots=True)
class KeywordDay:
    """One Google keyword's facts on one native date (Q-G5, K3).

    Attributes:
        keyword_id: The keyword.
        date: Native date in the account's time zone.
        cost: Spend.
        clicks: Clicks.
        impressions: Impressions.
        conversions: Conversions; ``None`` when unreported.
        historical_quality_score: ``metrics.historical_quality_score``; ``None`` when absent.
        average_cpc: ``metrics.average_cpc`` in the account currency.

    Raises:
        TypeError: On construction, when ``date`` is not a native date.
    """

    keyword_id: int
    date: date
    cost: Decimal
    clicks: int
    impressions: int
    conversions: Decimal | None
    historical_quality_score: int | None = None
    average_cpc: Decimal | None = None

    def __post_init__(self) -> None:
        """Require a native date."""
        _require_native_date(self.date, "KeywordDay.date")


@dataclass(frozen=True, slots=True)
class SearchTermRow:
    """One search term's window totals per (ad group, matched criterion, term) (K1, K2, C3b).

    Attributes:
        ad_group_id: The ad group the term matched in.
        campaign_id: The ad group's campaign.
        search_term: The query text.
        term_status: The status on the latest date in the window (``NONE``, ``ADDED``,
            ``EXCLUDED``, ``ADDED_EXCLUDED``).
        matched_criterion_id: ``segments.keyword.ad_group_criterion`` criterion id.
        matched_keyword_text: The matched keyword's text.
        matched_match_type: The matched keyword's match type.
        cost: Window spend.
        clicks: Window clicks.
        impressions: Window impressions.
        conversions: Window conversions; any fractional amount counts as converting.
        conversions_value: Window conversion value.
    """

    ad_group_id: int
    campaign_id: int
    search_term: str
    term_status: str
    matched_criterion_id: str | None
    matched_keyword_text: str | None
    matched_match_type: str | None
    cost: Decimal
    clicks: int
    impressions: int
    conversions: Decimal
    conversions_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RateBaseline:
    """K1's trailing account conversion-rate baseline (``k1.baseline_days`` to window end).

    Attributes:
        conversions: Account conversions over the baseline span (may be fractional).
        clicks: Account clicks over the baseline span.
        start: First native date of the span.
        end: Last native date of the span.

    Raises:
        TypeError: On construction, when a date is not a native date.
    """

    conversions: Decimal
    clicks: int
    start: date
    end: date

    def __post_init__(self) -> None:
        """Require native dates."""
        _require_native_date(self.start, "RateBaseline.start")
        _require_native_date(self.end, "RateBaseline.end")

    @property
    def rate(self) -> Decimal | None:
        """Conversions per click: a rate, not a per-click probability; ``None`` at 0 clicks."""
        if self.clicks <= 0:
            return None
        return self.conversions / Decimal(self.clicks)


@dataclass(frozen=True, slots=True)
class SegmentRow:
    """One LinkedIn demographic value within a campaign over one exact preset window (C3a).

    Attributes:
        campaign_id: The campaign.
        facet: ``job_function``, ``seniority`` or ``industry``.
        value: The demographic value.
        window_start: First date of the fetched window (UTC days).
        window_end: Last date of the fetched window.
        impressions: Segment impressions; ``None`` when the pivot does not return it.
        clicks: Segment clicks; ``None`` when unavailable.
        cost: Segment spend; ``None`` when unavailable.
        leads: Segment leads; ``None`` when unavailable, in which case C3a does not fire.

    Raises:
        TypeError: On construction, when a window date is not a native date.
    """

    campaign_id: int
    facet: str
    value: str
    window_start: date
    window_end: date
    impressions: int | None = None
    clicks: int | None = None
    cost: Decimal | None = None
    leads: int | None = None

    def __post_init__(self) -> None:
        """Require native dates."""
        _require_native_date(self.window_start, "SegmentRow.window_start")
        _require_native_date(self.window_end, "SegmentRow.window_end")


@dataclass(frozen=True, slots=True)
class Observation:
    """One ``entity_snapshot`` row: what an entity looked like at ``captured_at``.

    An observation, never an effective time (AAP 0.5.4.1): budget-day classification derives
    whole-day knowledge from consecutive observations.

    Attributes:
        captured_at: The observation instant.
        status: Normalised status (campaigns), raw status otherwise.
        budget_daily: Daily budget at that instant.
        budget_total: Lifetime / custom-period budget at that instant.
        budget_amount_micros: Google budget amount at native precision.
        budget_id: For campaign rows, the Google budget the campaign drew from.
        quality_score: For keyword rows, the Q-G7 quality score.
        payload: The full snapshot payload (for example a conversion action's
            ``include_in_conversions_metric``, ``attribution_model`` and ``counting_type``),
            deep-copied on construction into a read-only tree (nested lists as tuples).

    Raises:
        TypeError: On construction, when ``captured_at`` is not a ``datetime``, ``payload``
            is not a string-keyed mapping, or a value nested in it cannot be frozen,
            including a frozen dataclass that is not deeply immutable (see :class:`Evidence`).
        ValueError: On construction, when ``captured_at`` is naive.
    """

    captured_at: datetime
    status: str | None = None
    budget_daily: Decimal | None = None
    budget_total: Decimal | None = None
    budget_amount_micros: int | None = None
    budget_id: int | None = None
    quality_score: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Require a tz-aware instant and deep-freeze the payload."""
        _require_instant(self.captured_at, "Observation.captured_at")
        object.__setattr__(self, "payload", _frozen_mapping(self.payload, "Observation.payload"))


# ---------------------------------------------------------------------------
# Input types: leads and CRM (AAP 0.3.3, 0.5.4.7)
# ---------------------------------------------------------------------------

CRM_OBJECT_TYPES: Final[tuple[str, ...]] = ("contact", "meeting", "deal")
"""HubSpot object types as ``crm_association`` stores them."""

_MATCH_STATUSES: Final[tuple[str, ...]] = get_args(MatchStatus)
_CONTACT_READ_STATUSES: Final[tuple[str, ...]] = get_args(ContactReadStatus)
_ASSOCIATION_STATUSES: Final[tuple[str, ...]] = get_args(AssociationStatus)
_TARGET_READ_STATUSES: Final[tuple[str, ...]] = get_args(TargetReadStatus)


@dataclass(frozen=True, slots=True)
class LeadInfo:
    """A LinkedIn lead of the active dataset, with its match state in the effective CRM dataset.

    Attributes:
        id: Internal lead id.
        external_id: LinkedIn lead response id.
        campaign_id: The campaign that produced the lead.
        creative_id: The creative that produced it; ``None`` when LinkedIn gave none.
        creative_urn: That creative's URN, for asset resolution.
        lead_form_urn: Canonical ``urn:li:leadGenForm:{id}`` of the lead's form.
        lead_form_version: The form version the response was submitted on.
        submitted_at: ``submittedAt``, the touch instant for the causal check.
        match_status: The ``lead_match`` status; ``None`` when the CRM dataset has no row for
            the lead, which counts as unresolved.
        contact_hs_id: The matched contact's HubSpot id.
        contact_read_status: Whether the matched contact's latest refresh read it.
        match_reason: The match row's reason, for example "form map mismatch".

    Raises:
        ValueError: On construction, for an unknown match or read status, or a naive
            ``submitted_at``.
        TypeError: On construction, when ``submitted_at`` is not a ``datetime``.
    """

    id: int
    external_id: str
    campaign_id: int
    creative_id: int | None
    creative_urn: str | None
    lead_form_urn: str
    lead_form_version: int
    submitted_at: datetime
    match_status: MatchStatus | None = None
    contact_hs_id: str | None = None
    contact_read_status: ContactReadStatus | None = None
    match_reason: str | None = None

    def __post_init__(self) -> None:
        """Validate the instant and the statuses."""
        _require_instant(self.submitted_at, "LeadInfo.submitted_at")
        if self.match_status is not None:
            _require_in(self.match_status, _MATCH_STATUSES, "LeadInfo.match_status")
        if self.contact_read_status is not None:
            _require_in(
                self.contact_read_status, _CONTACT_READ_STATUSES, "LeadInfo.contact_read_status"
            )

    @property
    def matched(self) -> bool:
        """Whether the lead is matched to a contact in the effective CRM dataset."""
        return self.match_status == "matched" and self.contact_hs_id is not None

    @property
    def unresolved(self) -> bool:
        """Whether the lead is unresolved, so it counts in every population's denominator.

        Unresolved means ``pending``, ``no_match``, ``no_email``, ``error`` or no match row in
        the CRM dataset. Such a lead stays in every population and is never dropped.
        """
        return not self.matched


@dataclass(frozen=True, slots=True)
class ContactInfo:
    """A HubSpot contact of a CRM dataset.

    Attributes:
        hs_id: HubSpot contact id.
        lifecycle_stage: Current ``lifecyclestage``.
        mql_at: The bound MQL date (stage entered or configured property); ``None`` when the
            contact skipped the stage or the binding is unavailable.
        ql_at: The bound Qualified Lead date, observed independently of MQL.
        google_campaign_ref: The configured Google campaign property's value.
        google_touch_at: The configured Google touch-date property's value.
    """

    hs_id: str
    lifecycle_stage: str | None = None
    mql_at: datetime | None = None
    ql_at: datetime | None = None
    google_campaign_ref: str | None = None
    google_touch_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MeetingInfo:
    """A HubSpot meeting.

    Attributes:
        hs_id: HubSpot meeting id.
        created_at: ``hs_createdate``, the labelled proxy for booking time.
        start_at: ``hs_meeting_start_time``.
        meeting_timestamp: ``hs_timestamp``.
        outcome: ``hs_meeting_outcome``, for example ``CANCELED``.
        meeting_source: The meeting source, for example ``MEETINGS_PUBLIC``.
    """

    hs_id: str
    created_at: datetime | None = None
    start_at: datetime | None = None
    meeting_timestamp: datetime | None = None
    outcome: str | None = None
    meeting_source: str | None = None


@dataclass(frozen=True, slots=True)
class DealInfo:
    """A HubSpot deal.

    Attributes:
        hs_id: HubSpot deal id.
        created_at: ``createdate``, the instant the causal check compares with the touch.
        stage: ``dealstage``.
        pipeline: ``pipeline``.
        amount: ``amount``.
    """

    hs_id: str
    created_at: datetime | None = None
    stage: str | None = None
    pipeline: str | None = None
    amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AssociationFetchInfo:
    """The outcome of one (object, target type) association fetch and its target reads.

    Attributes:
        object_type: ``contact``, ``meeting`` or ``deal``.
        object_id: The object's HubSpot id.
        to_type: The target type.
        status: ``ok``, ``ok_empty``, ``partial`` or ``error``.
        target_read_status: ``ok``, ``partial`` or ``error`` for the associated targets'
            batch reads; ``None`` when ``ok_empty``.
        missing_target_ids: Associated ids the target reads did not return.

    Raises:
        ValueError: On construction, for an unknown object type or status.
    """

    object_type: str
    object_id: str
    to_type: str
    status: AssociationStatus
    target_read_status: TargetReadStatus | None = None
    missing_target_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the types and statuses."""
        _require_in(self.object_type, CRM_OBJECT_TYPES, "AssociationFetchInfo.object_type")
        _require_in(self.to_type, CRM_OBJECT_TYPES, "AssociationFetchInfo.to_type")
        _require_in(self.status, _ASSOCIATION_STATUSES, "AssociationFetchInfo.status")
        if self.target_read_status is not None:
            _require_in(
                self.target_read_status,
                _TARGET_READ_STATUSES,
                "AssociationFetchInfo.target_read_status",
            )
        object.__setattr__(self, "missing_target_ids", tuple(self.missing_target_ids))

    @property
    def is_empty(self) -> bool:
        """Whether the fetch succeeded with no associations: a genuine zero."""
        return self.status == "ok_empty"

    @property
    def known(self) -> bool:
        """Whether the association is known evidence (AAP 0.3.3).

        Known when the fetch is ``ok_empty``, or ``ok`` with every target read. Any failed or
        partial step is unknown, and an associated but unread target is never absent.
        """
        if self.status == "ok_empty":
            return True
        return self.status == "ok" and self.target_read_status == "ok"


@dataclass(frozen=True, slots=True)
class CrmData:
    """Everything a context reads from one HubSpot dataset.

    The mappings are copied on construction into read-only mappings and the sequences into
    tuples, so a later change to the caller's collections cannot alter the CRM evidence a
    run reads; their values are frozen records.

    Attributes:
        dataset: The CRM dataset (HubSpot primary, or a simulated companion).
        bindings: The MQL, QL and Google bindings verified against that dataset's portal.
        contacts: Contacts by HubSpot id: the matched leads' contacts and the Google-touch
            contacts; read-only.
        meetings: Associated meetings by HubSpot id; read-only.
        deals: Associated and window deals by HubSpot id; read-only.
        associations: ``(from_type, from_hs_id, to_type)`` to the associated HubSpot ids;
            read-only, each value a tuple.
        fetches: ``(object_type, object_hs_id, to_type)`` to the fetch outcome; read-only.
        window_deal_ids: Portal deals created from window start to run time.
        deal_search_complete: Whether the deal search covered that range completely; when
            not, the orphan-deal share is unknown.
        google_contacts: Contacts found by Google touch-date discovery.

    Raises:
        ValueError: On construction, when ``dataset`` is not a HubSpot dataset.
        TypeError: On construction, when ``bindings`` is not :class:`CrmBindings`, a mapping
            field is not a mapping, or an ``associations`` value is not a list or tuple.
    """

    dataset: DatasetRef
    bindings: CrmBindings
    contacts: Mapping[str, ContactInfo] = field(default_factory=dict)
    meetings: Mapping[str, MeetingInfo] = field(default_factory=dict)
    deals: Mapping[str, DealInfo] = field(default_factory=dict)
    associations: Mapping[tuple[str, str, str], tuple[str, ...]] = field(default_factory=dict)
    fetches: Mapping[tuple[str, str, str], AssociationFetchInfo] = field(default_factory=dict)
    window_deal_ids: tuple[str, ...] = ()
    deal_search_complete: bool = False
    google_contacts: tuple[ContactInfo, ...] = ()

    def __post_init__(self) -> None:
        """Validate the dataset and bindings, and copy the collections read-only."""
        if not isinstance(self.dataset, DatasetRef) or self.dataset.source != "hubspot":
            raise ValueError(f"CrmData.dataset must be a HubSpot dataset, found {self.dataset!r}")
        if not isinstance(self.bindings, CrmBindings):
            raise TypeError(f"CrmData.bindings must be CrmBindings, found {self.bindings!r}")
        for name in ("contacts", "meetings", "deals", "fetches"):
            value = _read_only_mapping(getattr(self, name), f"CrmData.{name}")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "associations",
            _read_only_sequences(self.associations, "CrmData.associations"),
        )
        object.__setattr__(self, "window_deal_ids", tuple(self.window_deal_ids))
        object.__setattr__(self, "google_contacts", tuple(self.google_contacts))

    def targets(self, from_type: str, from_id: str, to_type: str) -> tuple[str, ...]:
        """Return the HubSpot ids associated with ``from_id``; empty when none are stored.

        An empty result is a genuine zero only when :meth:`association_known` holds.
        """
        return self.associations.get((from_type, from_id, to_type), ())

    def fetch(self, object_type: str, object_id: str, to_type: str) -> AssociationFetchInfo | None:
        """Return the fetch outcome for one (object, target type); ``None`` when never fetched."""
        return self.fetches.get((object_type, object_id, to_type))

    def association_known(self, object_type: str, object_id: str, to_type: str) -> bool:
        """Whether the association is known evidence; a missing fetch row is unknown."""
        outcome = self.fetch(object_type, object_id, to_type)
        return outcome is not None and outcome.known


# ---------------------------------------------------------------------------
# Input types: decisions, discrepancies and feedback (AAP 0.5.2, 0.5.4.4)
# ---------------------------------------------------------------------------

_DISCREPANCY_KINDS: Final[tuple[str, ...]] = get_args(DiscrepancyKind)
_OPEN_STATES: Final[tuple[str, ...]] = get_args(OpenDiscrepancyState)


@dataclass(frozen=True, slots=True)
class DecisionInfo:
    """The current (latest) decision of one subject: its complete expected state.

    Attributes:
        subject_type: ``campaign`` or ``budget``.
        subject_id: The subject's internal id.
        shape: ``own_budget``, ``shared_member`` or ``budget``.
        status: Expected status; ``None`` for budget subjects.
        daily_budget: Expected daily budget, when the shape holds one.
        total_budget: Expected lifetime budget, when the shape holds one.
        budget_id: Expected shared budget of a ``shared_member``.
        tombstone: Whether this is an acknowledged removal.
        decided_at: When the decision was recorded.
        origin: ``baseline``, ``user``, ``recommendation`` or ``resolution``.
    """

    subject_type: SubjectType
    subject_id: int
    shape: DecisionShape
    status: str | None
    daily_budget: Decimal | None
    total_budget: Decimal | None
    budget_id: int | None
    tombstone: bool
    decided_at: datetime
    origin: DecisionOrigin

    @property
    def key(self) -> SubjectKey:
        """The subject as a :data:`SubjectKey`."""
        return (self.subject_type, self.subject_id)


@dataclass(frozen=True, slots=True)
class DiscrepancyInfo:
    """An unresolved C0 discrepancy of the active datasets.

    Attributes:
        id: Discrepancy id.
        subject_type: ``campaign`` or ``budget``.
        subject_id: The subject's internal id.
        subject_name: Display name of the subject.
        source: ``google`` or ``linkedin``.
        shape: The subject's decision shape, which fixes the offered resolutions.
        kind: ``status``, ``budget``, ``untracked`` or ``missing``.
        decided_value: The decided state compared; ``None`` for ``untracked`` until
            ``record_intended`` records an intended state and moves it to
            ``awaiting_platform``, after which it holds that state, or a decision recorded
            since. Deep-copied on construction into a read-only tree.
        platform_value: The platform state; ``None`` for ``missing``. Deep-copied on
            construction into a read-only tree.
        detected_at: When it was detected; C0 items are ordered oldest first.
        state: ``open`` or ``awaiting_platform``.

    Raises:
        ValueError: On construction, for an unknown subject type, kind or state.
        TypeError: On construction, when a value is neither ``None`` nor a string-keyed
            mapping, or a value nested in it cannot be frozen, including a frozen dataclass
            that is not deeply immutable (see :class:`Evidence`).
    """

    id: int
    subject_type: SubjectType
    subject_id: int
    subject_name: str
    source: Source
    shape: DecisionShape
    kind: DiscrepancyKind
    decided_value: Mapping[str, Any] | None
    platform_value: Mapping[str, Any] | None
    detected_at: datetime
    state: OpenDiscrepancyState

    def __post_init__(self) -> None:
        """Validate the enumerations and the detection instant; deep-freeze the two values."""
        _require_in(self.subject_type, _SUBJECT_TYPES, "DiscrepancyInfo.subject_type")
        _require_in(self.kind, _DISCREPANCY_KINDS, "DiscrepancyInfo.kind")
        _require_in(self.state, _OPEN_STATES, "DiscrepancyInfo.state")
        _require_instant(self.detected_at, "DiscrepancyInfo.detected_at")
        for name in ("decided_value", "platform_value"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _frozen_mapping(value, f"DiscrepancyInfo.{name}"))

    @property
    def key(self) -> SubjectKey:
        """The subject as a :data:`SubjectKey`."""
        return (self.subject_type, self.subject_id)


@dataclass(frozen=True, slots=True)
class FeedbackInfo:
    """An active dismiss, snooze or done (``until`` after the run time).

    Attributes:
        rule_id: The item's rule.
        entity_type: The item's entity type.
        entity_key: The item's stable entity key (:attr:`EntityRef.key`).
        kind: ``dismiss``, ``snooze`` or ``done``.
        until: When the suppression ends, stored as the same instant in UTC.

    Raises:
        ValueError: On construction, for an unknown kind or a naive ``until``.
        TypeError: On construction, when ``until`` is not a ``datetime``.
    """

    rule_id: str
    entity_type: str
    entity_key: str
    kind: FeedbackKind
    until: datetime

    def __post_init__(self) -> None:
        """Validate the kind and instant, and store ``until`` in UTC.

        Two datetimes sharing one ``tzinfo`` compare by wall time, ignoring a DST fold; UTC
        storage makes every later comparison of ``until`` exact.
        """
        _require_in(self.kind, FEEDBACK_KINDS, "FeedbackInfo.kind")
        _require_instant(self.until, "FeedbackInfo.until")
        object.__setattr__(self, "until", self.until.astimezone(UTC))

    @property
    def key(self) -> tuple[str, str, str]:
        """``(rule_id, entity_type, entity_key)``, compared with :attr:`Candidate.feedback_key`."""
        return (self.rule_id, self.entity_type, self.entity_key)

    @property
    def suppression_reason(self) -> str:
        """The ``run_suppression.reason`` this feedback is counted under."""
        return FEEDBACK_SUPPRESSION[self.kind]

    def active_at(self, now: datetime) -> bool:
        """Whether the suppression is still in force at ``now`` (``until`` is later).

        Both instants are compared on UTC operands, so the answer stays exact in a DST
        fold, where one wall time names two instants.

        Args:
            now: The tz-aware instant to judge at.

        Raises:
            TypeError: When ``now`` is not a ``datetime``.
            ValueError: When ``now`` is naive.
        """
        _require_instant(now, "FeedbackInfo.active_at now")
        return self.until.astimezone(UTC) > now.astimezone(UTC)

    def matches(self, candidate: Candidate) -> bool:
        """Whether this feedback suppresses ``candidate``; C0 items are never suppressed."""
        return not candidate.is_discrepancy and self.key == candidate.feedback_key


# ---------------------------------------------------------------------------
# The analysis context (AAP 0.5.1)
# ---------------------------------------------------------------------------

_DATASET_SOURCE_PREFIXES: Final[Mapping[str, Source]] = MappingProxyType(
    {"google": "google", "linkedin": "linkedin", "hubspot": "hubspot"}
)

_CONTEXT_RECORD_MAPPINGS: Final[tuple[str, ...]] = (
    "datasets",
    "freshness",
    "accounts",
    "campaign_groups",
    "campaigns",
    "budgets",
    "ad_groups",
    "keywords",
    "creatives",
    "conversion_actions",
    "creative_totals",
    "latest_observations",
    "decisions",
)
""":class:`AnalysisContext` mappings whose values are frozen records, copied read-only."""

_CONTEXT_SEQUENCE_MAPPINGS: Final[tuple[str, ...]] = (
    "campaign_days",
    "keyword_days",
    "observations",
)
""":class:`AnalysisContext` mappings whose values are sequences, copied read-only as tuples."""

_CONTEXT_SEQUENCES: Final[tuple[str, ...]] = (
    "search_terms",
    "segments",
    "leads",
    "discrepancies",
    "feedback",
)
""":class:`AnalysisContext` sequences of frozen records, copied as tuples."""


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    """Every input of one analysis run, aggregated and immutable; rules perform no I/O.

    ``metrics.load_context`` builds it from the database; ``simulation/scenarios.py`` and the
    acceptance tests build it directly, filling only what a rule reads, so every collection
    defaults to empty.

    Every collection except :attr:`cache` is copied on construction: mappings into read-only
    mappings (the per-key day, keyword-day and observation sequences into tuples) and
    sequences into tuples. Their elements are frozen records, so a later change to the
    caller's collections cannot alter the inputs a run reads.

    Dataset isolation (decision 8): every row belongs to its source's active primary dataset
    (:attr:`datasets`), except LinkedIn-attributed CRM evidence, which reads
    :attr:`linkedin_crm`, the effective CRM dataset of the LinkedIn dataset in use.

    Attributes:
        window: The analysis window.
        thresholds: The loaded configuration.
        now: The run time; downstream CRM events count as of this instant.
        run_kind: ``default`` or ``custom``.
        display_timezone: ``DISPLAY_TIMEZONE``, the IANA zone the instants inside frozen
            flag messages are rendered in (:func:`format_instant`), such as the ``stale``
            flag's "Data as of …". It only formats instants and never moves a day boundary
            (AAP 0.5.4.1). Defaults to ``UTC``, the setting's own default.
        datasets: The active primary dataset per source; a source may be absent.
        linkedin_crm: The effective CRM dataset for LinkedIn-attributed evidence: the
            companion while LinkedIn is simulated, else HubSpot's active primary.
        google_crm: HubSpot's active primary dataset, which the Google-to-CRM link always
            reads. ``None``, or ``bindings.google.available`` false, means Google items carry
            ``no_crm_link`` (:func:`google_link_gap`).
        freshness: ``dataset_freshness`` rows keyed ``(dataset_id, dataset name)``.
        accounts: Ad accounts by id.
        campaign_groups: LinkedIn campaign groups by id.
        campaigns: Campaigns by id.
        budgets: Google budgets by id.
        ad_groups: Google ad groups by id.
        keywords: Google keywords by id.
        creatives: LinkedIn creatives by id.
        conversion_actions: Google conversion actions by id.
        campaign_days: Per campaign, its window days ascending (one row per native date with
            facts).
        creative_totals: Window totals per creative.
        keyword_days: Per keyword, its window days ascending.
        search_terms: Window totals per (ad group, matched criterion, search term).
        k1_baseline: K1's trailing account conversion-rate baseline, when any facts exist.
        segments: Segment rows whose window equals this run's window exactly.
        segment_windows: Every fetched segment window of the active LinkedIn dataset.
        observations: Snapshots keyed ``(entity_type, entity_id)``, ascending by
            ``captured_at``, covering the window plus bracketing observations.
        latest_observations: Each campaign's and Google budget's newest stored snapshot
            captured at or before :attr:`now`, keyed ``("campaign", id)`` or
            ``("budget", id)`` and read independently of the window: the current state that
            budget proposals (B1, B2, B3) are sized from, even for a custom window that ended
            long before the run. Never merged into :attr:`observations`, whose span is what
            budget-day classification and dated membership read.
        leads: LinkedIn leads submitted within ``window.utc_bounds()``, with their match state
            in :attr:`linkedin_crm`.
        discrepancies: Unresolved (``open`` or ``awaiting_platform``) discrepancies.
        decisions: The current (latest) decision per subject.
        feedback: Active feedback (``until`` after :attr:`now`).
        cache: Per-run memo for derived tables (budget histories, the attribution table), so
            each is computed once; excluded from equality and ``repr``. The one mutable
            field: the rules of a run write to it.

    Raises:
        TypeError: On construction, when ``window``, ``thresholds`` or a CRM dataset has the
            wrong type, ``now`` is not a ``datetime``, ``display_timezone`` is not a string,
            a mapping field is not a mapping, or a sequence field (or a ``campaign_days``,
            ``keyword_days`` or ``observations`` value) is not a list or tuple.
        ValueError: On construction, when ``now`` is naive, ``display_timezone`` is not a
            loadable IANA zone, a dataset is filed under the wrong source or is not primary,
            or a CRM dataset breaks the isolation rule.
    """

    window: AnalysisWindow
    thresholds: Thresholds
    now: datetime
    run_kind: RunKind = "default"
    display_timezone: str = "UTC"
    datasets: Mapping[Source, DatasetRef] = field(default_factory=dict)
    linkedin_crm: CrmData | None = None
    google_crm: CrmData | None = None
    freshness: Mapping[tuple[int, str], Freshness] = field(default_factory=dict)
    accounts: Mapping[int, AccountInfo] = field(default_factory=dict)
    campaign_groups: Mapping[int, CampaignGroupInfo] = field(default_factory=dict)
    campaigns: Mapping[int, CampaignInfo] = field(default_factory=dict)
    budgets: Mapping[int, BudgetInfo] = field(default_factory=dict)
    ad_groups: Mapping[int, AdGroupInfo] = field(default_factory=dict)
    keywords: Mapping[int, KeywordInfo] = field(default_factory=dict)
    creatives: Mapping[int, CreativeInfo] = field(default_factory=dict)
    conversion_actions: Mapping[int, ConversionActionInfo] = field(default_factory=dict)
    campaign_days: Mapping[int, tuple[DayFact, ...]] = field(default_factory=dict)
    creative_totals: Mapping[int, CreativeTotals] = field(default_factory=dict)
    keyword_days: Mapping[int, tuple[KeywordDay, ...]] = field(default_factory=dict)
    search_terms: tuple[SearchTermRow, ...] = ()
    k1_baseline: RateBaseline | None = None
    segments: tuple[SegmentRow, ...] = ()
    segment_windows: tuple[tuple[date, date], ...] = ()
    observations: Mapping[tuple[str, int], tuple[Observation, ...]] = field(default_factory=dict)
    latest_observations: Mapping[tuple[str, int], Observation] = field(default_factory=dict)
    leads: tuple[LeadInfo, ...] = ()
    discrepancies: tuple[DiscrepancyInfo, ...] = ()
    decisions: Mapping[SubjectKey, DecisionInfo] = field(default_factory=dict)
    feedback: tuple[FeedbackInfo, ...] = ()
    cache: dict[str, Any] = field(default_factory=dict, compare=False, hash=False, repr=False)

    def __post_init__(self) -> None:
        """Copy the collections read-only, then validate the run inputs and isolation rules."""
        for name in _CONTEXT_RECORD_MAPPINGS:
            records = _read_only_mapping(getattr(self, name), f"AnalysisContext.{name}")
            object.__setattr__(self, name, records)
        for name in _CONTEXT_SEQUENCE_MAPPINGS:
            series = _read_only_sequences(getattr(self, name), f"AnalysisContext.{name}")
            object.__setattr__(self, name, series)
        for name in (*_CONTEXT_SEQUENCES, "segment_windows"):
            items = getattr(self, name)
            if not isinstance(items, list | tuple):
                raise TypeError(f"AnalysisContext.{name} must be a tuple, found {items!r}")
            object.__setattr__(self, name, tuple(items))
        object.__setattr__(
            self, "segment_windows", tuple(tuple(pair) for pair in self.segment_windows)
        )
        if not isinstance(self.window, AnalysisWindow):
            raise TypeError(
                f"AnalysisContext.window must be an AnalysisWindow, found {self.window!r}"
            )
        if not isinstance(self.thresholds, Thresholds):
            raise TypeError("AnalysisContext.thresholds must be a Thresholds")
        _require_instant(self.now, "AnalysisContext.now")
        _require_in(self.run_kind, get_args(RunKind), "AnalysisContext.run_kind")
        display_zone(self.display_timezone, "AnalysisContext.display_timezone")
        for source, ref in self.datasets.items():
            _require_in(source, SOURCES, "AnalysisContext.datasets key")
            if not isinstance(ref, DatasetRef) or ref.source != source or ref.role != "primary":
                raise ValueError(
                    f"AnalysisContext.datasets[{source!r}] must be that source's primary "
                    f"dataset, found {ref!r}"
                )
        for name, crm in (("linkedin_crm", self.linkedin_crm), ("google_crm", self.google_crm)):
            if crm is not None and not isinstance(crm, CrmData):
                raise TypeError(f"AnalysisContext.{name} must be CrmData, found {crm!r}")
        if self.google_crm is not None and self.google_crm.dataset.role != "primary":
            raise ValueError("google_crm must be HubSpot's primary dataset, never a companion")
        linkedin = self.datasets.get("linkedin")
        if linkedin is not None and self.linkedin_crm is not None:
            expected_role: DatasetRole = "companion" if linkedin.mode == "simulated" else "primary"
            if self.linkedin_crm.dataset.role != expected_role:
                raise ValueError(
                    f"linkedin_crm must be the {expected_role} CRM dataset while LinkedIn is "
                    f"{linkedin.mode}, found a {self.linkedin_crm.dataset.role} dataset"
                )

    # -- lookups -------------------------------------------------------------------------

    def crm(self, scope: CrmScope) -> CrmData | None:
        """Return the CRM dataset a piece of evidence reads (:data:`CrmScope`).

        Raises:
            ValueError: For an unknown scope.
        """
        if scope == "linkedin":
            return self.linkedin_crm
        if scope == "google":
            return self.google_crm
        raise ValueError(f"crm scope must be 'linkedin' or 'google', found {scope!r}")

    def dataset(self, source: Source) -> DatasetRef | None:
        """Return the active primary dataset of ``source``, when it has one."""
        return self.datasets.get(source)

    def dataset_ref_for(
        self, dataset_name: str, *, crm_scope: CrmScope | None
    ) -> DatasetRef | None:
        """Return the dataset a dataset name resolves to for this run.

        ``google.*`` and ``linkedin.*`` resolve to that source's active primary dataset.
        ``hubspot.*`` resolves to :attr:`linkedin_crm` or :attr:`google_crm` per
        ``crm_scope``, and to HubSpot's active primary when ``crm_scope`` is ``None``.

        Raises:
            ValueError: For a name outside ``db.models.ops.DATASETS``.
        """
        _require_in(dataset_name, DATASETS, "dataset name")
        source = _DATASET_SOURCE_PREFIXES[dataset_name.split(".", 1)[0]]
        if source != "hubspot":
            return self.datasets.get(source)
        if crm_scope is None:
            return self.datasets.get("hubspot")
        crm = self.crm(crm_scope)
        return None if crm is None else crm.dataset

    def dataset_id_for(self, dataset_name: str, *, crm_scope: CrmScope | None) -> int | None:
        """Return the ``source_dataset`` id a dataset name resolves to (:meth:`dataset_ref_for`).

        Raises:
            ValueError: For a name outside ``db.models.ops.DATASETS``.
        """
        ref = self.dataset_ref_for(dataset_name, crm_scope=crm_scope)
        return None if ref is None else ref.dataset_id

    def freshness_for(self, dataset_name: str, dataset_id: int | None) -> Freshness | None:
        """Return the freshness row of ``dataset_name`` in ``dataset_id``, when one exists."""
        if dataset_id is None:
            return None
        return self.freshness.get((dataset_id, dataset_name))

    def _coverage_boundary(self, dataset_name: str) -> str | None:
        """Return the IANA zone whose midnight ends ``dataset_name``'s coverage dates.

        ``google.*`` and ``linkedin.*`` dates are their source's native days, so their boundary
        is the one the window records for the source in ``source_days`` (Google's account time
        zone, LinkedIn's UTC); ``None`` when the window lists no such source. ``hubspot.*``
        dates are UTC dates (:data:`UTC_BOUNDARY`).

        Raises:
            ValueError: For a name outside ``db.models.ops.DATASETS``.
        """
        _require_in(dataset_name, DATASETS, "dataset name")
        source = _DATASET_SOURCE_PREFIXES[dataset_name.split(".", 1)[0]]
        if source == "hubspot":
            # HubSpot holds instants and has no native day, but its sync jobs record coverage
            # as UTC dates: the CRM job's state range, deal search and Google-touch discovery
            # each take astimezone(UTC).date(), as the /sources screen labels them.
            return UTC_BOUNDARY
        for item in self.window.source_days:
            if item.source == source:
                return item.boundary
        return None

    @property
    def stale_after(self) -> timedelta:
        """``sources.stale_after_hours`` as a duration (P4).

        Raises:
            ConfigError: When the key is missing from the configuration.
        """
        return timedelta(hours=self.thresholds.int("sources.stale_after_hours"))

    def account_currency(self, campaign_id: int) -> str:
        """Return the account currency of a campaign; amounts never cross currencies.

        Raises:
            KeyError: When the campaign is not in the context.
        """
        campaign = self.campaigns[campaign_id]
        account = self.accounts.get(campaign.account_id)
        return campaign.currency if account is None else account.currency

    def campaign_name(self, campaign_id: int) -> str:
        """Return a campaign's display name, or ``Campaign #{id}`` when it is not loaded."""
        campaign = self.campaigns.get(campaign_id)
        return f"Campaign #{campaign_id}" if campaign is None else campaign.name

    def subject_name(self, key: SubjectKey) -> str:
        """Return the display name of a decision subject (campaign or budget)."""
        subject_type, subject_id = key
        if subject_type == "campaign":
            return self.campaign_name(subject_id)
        budget = self.budgets.get(subject_id)
        return f"Budget #{subject_id}" if budget is None else budget.name

    def resolve_asset(
        self, *, creative_urn: str | None, form_urn: str | None
    ) -> ContentAsset | None:
        """Resolve a white paper with the one precedence for spend and leads (AAP 0.5.4.7).

        The creative's override first, otherwise the asset whose forms list ``form_urn``.
        """
        return self.thresholds.resolve_asset(creative_urn=creative_urn, form_urn=form_urn)

    @property
    def discrepant_subjects(self) -> frozenset[SubjectKey]:
        """Subjects with an unresolved discrepancy, which hold their dependent items (P1)."""
        return frozenset(item.key for item in self.discrepancies)

    # -- evidence ------------------------------------------------------------------------

    def make_evidence(
        self,
        *,
        required_datasets: Sequence[str],
        crm_scope: CrmScope | None,
        metrics: Sequence[Metric],
        baselines: Sequence[Baseline] = (),
        sample: Sample | None = None,
        test: TestResult | None = None,
        threshold_keys: Sequence[str] = (),
        extra: Mapping[str, Any] | None = None,
        headline: str | None,
    ) -> Evidence:
        """Build an item's evidence; every rule builds its evidence through this helper.

        * ``window`` is ``self.window.to_json()``, with each source's day boundary (AAP
          0.5.4.8), ``boundaries_differ`` and the ``description`` sentence, so the evidence
          says when the sources' day boundaries differ (AAP 0.5.4.1).
        * ``thresholds`` is ``self.thresholds.subset(*threshold_keys)``: exactly the configured
          values the rule applied.
        * ``datasets`` has one :class:`DatasetStatus` per required dataset (repeats dropped),
          from its freshness row; ``unknown`` with no coverage when the row or the dataset is
          missing. A status with a ``covered_through`` records the day boundary of that date:
          the window's for a Google or LinkedIn dataset, UTC for a HubSpot one (AAP 0.5.5,
          "Daily dates are shown as stored, labelled with their source's day boundary").
        * ``provenance`` has one :class:`ProvenanceEntry` per distinct dataset read, frozen at
          compute time and never replaced by the current status (AAP 0.3.2).
        * ``extra.headline_metric`` (:data:`EXTRA_HEADLINE_METRIC`) is ``headline``: the key
          of the metric the card leads with (AAP 0.1.2, "Every card leads with one headline
          number, and its full evidence follows"). It is written only here and by
          :meth:`Evidence.with_headline`, so the selection always names a metric of the
          evidence; with ``headline=None`` no key is written.

        Args:
            required_datasets: The dataset names the rule read (``db.models.ops.DATASETS``).
            crm_scope: Which CRM dataset ``hubspot.*`` names resolve to; ``linkedin`` for
                LinkedIn-attributed evidence, ``google`` for the Google-to-CRM link.
            metrics: Every number the reason cites.
            baselines: The values the metrics were compared against.
            sample: The sample size against its minimum.
            test: The statistical test, when one was used.
            threshold_keys: The dotted threshold keys the rule applied.
            extra: Rule-specific detail; it must not set ``headline_metric`` itself.
            headline: The key of the one metric in ``metrics`` the item's card leads with,
                the number its action rests on; ``None`` for an item that is not rendered as
                a card (white-paper table rows), or whose headline precedence appends with
                :meth:`Evidence.with_headline` once every hold is final (C0). Required, so
                every rule decides.

        Returns:
            The frozen evidence.

        Raises:
            ValueError: For an unknown dataset name or duplicate metric keys; when
                ``headline`` is not the key of a metric in ``metrics``, naming it and the
                available keys; and when ``extra`` already sets ``headline_metric``.
            ConfigError: When a threshold key is missing from the configuration.
        """
        details: dict[str, Any] = {} if extra is None else dict(extra)
        if EXTRA_HEADLINE_METRIC in details:
            raise ValueError(
                f"extra must not set {EXTRA_HEADLINE_METRIC!r}; pass the metric key as "
                "headline= so it is checked against the metrics"
            )
        if headline is not None:
            details[EXTRA_HEADLINE_METRIC] = headline
        statuses: list[DatasetStatus] = []
        provenance: dict[int, ProvenanceEntry] = {}
        for name in dict.fromkeys(required_datasets):
            ref = self.dataset_ref_for(name, crm_scope=crm_scope)
            row = None if ref is None else self.freshness_for(name, ref.dataset_id)
            statuses.append(
                row.status(boundary=self._coverage_boundary(name))
                if row is not None
                else DatasetStatus(name, "unknown")
            )
            if ref is not None and ref.dataset_id not in provenance:
                provenance[ref.dataset_id] = ref.provenance()
        evidence = Evidence(
            window=self.window.to_json(),
            metrics=tuple(metrics),
            baselines=tuple(baselines),
            sample=sample,
            test=test,
            thresholds=self.thresholds.subset(*threshold_keys),
            datasets=tuple(statuses),
            provenance=tuple(provenance.values()),
            extra=details,
        )
        # Checked on the constructed evidence, whose metrics are type-checked and unique.
        keys = [metric.key for metric in evidence.metrics]
        if headline is not None and headline not in keys:
            raise ValueError(
                f"headline metric {headline!r} is not one of the evidence metrics {keys}"
            )
        return evidence


# ---------------------------------------------------------------------------
# The Google-to-CRM link (AAP 0.3.3, 0.5.4.1)
# ---------------------------------------------------------------------------


def google_link_gap(ctx: AnalysisContext) -> str | None:
    """Return why Google items have no CRM link, or ``None`` when the link is verified.

    AAP 0.3.3 and 0.5.4.1: the Google-to-CRM link exists only when both
    ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` and ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`` are
    configured and confirmed against HubSpot's active primary dataset
    (:attr:`AnalysisContext.google_crm`). Without it the Google pipeline link is unavailable:
    every Google item carries ``no_crm_link`` (:func:`no_crm_link_flag`), no Google Qualified
    Lead count or pipeline veto is computed, and a LinkedIn-derived subset never stands in.
    Every rule reads the gap here, so one definition decides it for every Google item.

    Args:
        ctx: The analysis context.

    Returns:
        ``"no active HubSpot dataset"`` when the run has no Google CRM dataset; while the link
        is unavailable, the binding's own reason (for example
        "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured"), else "the Google-to-CRM link is
        not verified"; ``None`` once the link is verified.
    """
    crm = ctx.google_crm
    if crm is None:
        return "no active HubSpot dataset"
    link = crm.bindings.google
    if link.available:
        return None
    return link.reason or "the Google-to-CRM link is not verified"


def no_crm_link_flag(reason: str) -> Flag:
    """Return the ``no_crm_link`` flag a Google item carries without the verified CRM link.

    AAP 0.3.3 and 0.5.4.1. ``reason`` is :func:`google_link_gap`'s, so every Google item's
    badge reads "No CRM link for Google campaigns: {reason}".

    Args:
        reason: Why the link is missing, from :func:`google_link_gap`.

    Raises:
        ValueError: When ``reason`` is blank.
    """
    _require_text(reason, "no_crm_link reason")
    return Flag(FLAG_NO_CRM_LINK, f"No CRM link for Google campaigns: {reason}")
