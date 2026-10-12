"""Campaigns to pause or launch: rules C1, C1b, C2 (Scale or Hold), C3a and C3b.

AAP 0.5.4.4 (the C1-C3b table; group order Discrepancies, Pause, Scale, Launch ideas), 0.5.4.1
(window, outcome by source, efficiency, expected outcomes, budget history and gates, the CRM
pipeline link and veto), 0.5.4.2 (G1 targets) and 0.3.2 (segment windows), serving FR-5 and the
user's directives: "Campaigns spending steadily with no output over a real time window → pause
candidates", "Campaigns hitting their budget cap consistently with good output → scale
candidates" and "New audience or campaign ideas suggested only when there's a specific data gap
supporting them". C0 (discrepancies) lives in ``discrepancy.py``.

Rules, groups and ranking:

* **C1 Pause** (group ``pause``, by spend descending): an active campaign spending on most days
  of a real window with zero outcomes although an average campaign would have produced several.
* **C1b Pipeline pause** (LinkedIn, group ``pause``): matured leads whose contacts reach neither
  Qualified Lead nor a meeting, judged only on fully known CRM evidence.
* **C2 Scale or Hold** (group ``scale``, by cost per outcome ascending): a cap-constrained,
  efficient budget subject. The pipeline veto decides between "Scale" (with a B1 item carrying
  the amount) and "Hold". :func:`scale_outcomes` is the single evaluation ``budget.py`` reads
  for B1, so C2 and B1 can never disagree.
* **C3a / C3b Launch ideas** (group ``launch``): an underused but proven LinkedIn segment, or a
  converting Google search-term cluster no keyword, ad group or campaign covers. No other rule
  produces launch ideas.

The module is pure: it reads only the :class:`~ads_engine.analysis.context.AnalysisContext` and
performs no I/O. Every threshold comes from ``config/thresholds.yaml`` through ``ctx.thresholds``
and is copied into ``evidence.thresholds``; every reason sentence is filled from the item's own
evidence values. Comparisons stay within one source, objective group and currency. A cost per
outcome or account baseline that may rest on a zero or unreported count (C1b's baseline here,
and every budget card's in ``budget.py``) is recorded through :func:`cost_per_outcome_value`:
"—" over a known 0 outcomes, unavailable over unreported ones. C1 needs a finite baseline, and
C2 one plus at least ``scale.min_outcomes`` outcomes, so the costs they show are always finite.

Example:
    >>> result = evaluate(ctx)  # doctest: +SKIP
    >>> [item.action for item in result.candidates]  # doctest: +SKIP
    ['Pause', 'Scale', "Create ad group or campaign for 'hubspot alternative'"]
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal
from typing import Any, Final, Literal

from ads_engine.analysis.attribution import (
    AttributionTable,
    VetoPopulation,
    attribution_for,
    shared_budget_veto,
)
from ads_engine.analysis.context import (
    B1,
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
    OBJ_LINKEDIN_LEAD_GEN,
    OUTCOME_LABELS,
    AnalysisContext,
    Baseline,
    CampaignInfo,
    Candidate,
    CrmScope,
    DecisionSubject,
    EntityRef,
    Evidence,
    Finding,
    Flag,
    LeadInfo,
    Metric,
    Number,
    RuleResult,
    Sample,
    SegmentRow,
    Source,
    SubjectKey,
    Withheld,
    google_link_gap,
    no_crm_link_flag,
    segment_external_id,
    term_cluster_external_id,
)
from ads_engine.analysis.metrics import (
    BUDGET_THRESHOLD_KEYS,
    BudgetHistory,
    GateResult,
    baseline_cpo,
    budget_history,
    budget_subjects,
    cap_constrained,
    cost_per_outcome,
    expected_outcomes,
    native_day_zone,
    spend_increase_blocked,
    subject_baseline,
    subject_baseline_outcomes,
    subject_campaigns,
    subject_objective_groups,
    subject_totals,
    window_totals,
)
from ads_engine.analysis.segment_labels import segment_value_label
from ads_engine.analysis.text import TermCluster, cluster_terms, tokenize
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.decisions import minor_unit_exponent, round_to_minor_unit

__all__ = [
    "C1B_THRESHOLD_KEYS",
    "C1_THRESHOLD_KEYS",
    "C2_THRESHOLD_KEYS",
    "C3A_THRESHOLD_KEYS",
    "C3B_THRESHOLD_KEYS",
    "HOLD_ACTION",
    "PAUSE_ACTION",
    "PIPELINE_PAUSE_ACTION",
    "SCALE_ACTION",
    "SCALE_CACHE_KEY",
    "SECTION_DATASETS",
    "VETO_THRESHOLD_KEYS",
    "ScaleEntry",
    "ScaleOutcome",
    "ScaleWithheld",
    "cost_per_outcome_value",
    "evaluate",
    "scale_outcomes",
]

# ---------------------------------------------------------------------------
# Threshold keys, actions and cache keys
# ---------------------------------------------------------------------------

C1_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "c1.min_window_days",
    "c1.min_spend_day_share",
    "c1.min_expected_outcomes",
    "window.sustained_min_days",
)
"""Every key C1 applies, copied into ``evidence.thresholds``."""

VETO_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "crm.veto_min_leads",
    "crm.lead_maturity_days",
    "attribution.floor_share",
    "crm.meeting_outcome_exclusions",
)
"""The keys of the CRM pipeline veto (AAP 0.5.4.1), added wherever veto evidence is shown.

``crm.meeting_outcome_exclusions`` decides which meetings the population's meeting count
includes (C1b's meetings = 0 gate and the meetings shown beside the veto);
``crm.booking_sources`` does not enter that count.
"""

C1B_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "c1b.min_window_days",
    *VETO_THRESHOLD_KEYS,
    "window.sustained_min_days",
)
"""Every key C1b applies; its eligible-contact minimum reuses ``crm.veto_min_leads``, and its
meetings = 0 gate counts only meetings outside ``crm.meeting_outcome_exclusions``."""

C2_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "scale.min_outcomes",
    "scale.max_cpa_ratio",
    "b1.step",
    "window.sustained_min_days",
    *BUDGET_THRESHOLD_KEYS,
)
"""Every key C2 (and B1 eligibility) applies; the veto keys are added when a veto is read."""

C3A_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "c3a.min_leads",
    "c3a.max_cpl_ratio",
    "c3a.max_impression_share",
    "window.presets",
)
"""Every key C3a applies, including the preset windows its segment data exists for."""

C3B_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "c3b.jaccard",
    "c3b.min_terms",
    "c3b.min_conversions",
    "c3b.head_token_share",
)
"""Every key C3b applies."""

PAUSE_ACTION: Final = "Pause"
"""C1's action; "Record decision" is prefilled with status ``paused``."""

PIPELINE_PAUSE_ACTION: Final = "Pause or rework targeting: leads are not becoming Qualified Leads"
"""C1b's action."""

SCALE_ACTION: Final = "Scale"
"""C2's action when the veto passes; the linked B1 item carries the amount."""

HOLD_ACTION: Final = "Hold: platform outcomes are not reaching Qualified Lead"
"""C2's action when the pipeline veto fires; no B1 item exists for the subject."""

SCALE_CACHE_KEY: Final = "scale_outcomes"
"""``ctx.cache`` key of :func:`scale_outcomes`, shared by C2 here and B1 in ``budget.py``."""

_SETTINGS_CACHE_KEY: Final = "campaigns.settings"
"""``ctx.cache`` key of the run's :class:`_Settings`."""

_CAMPAIGN_GROUP_KEY: Final = "campaign_group"
"""``evidence.extra`` key of a LinkedIn campaign card's campaign group (AAP 0.5.2)."""

_LEADS_BY_CAMPAIGN_CACHE_KEY: Final = "campaigns.leads_by_campaign"
"""``ctx.cache`` key of the run's window leads grouped by campaign (:func:`_leads_by_campaign`)."""

_YOUNG_COHORT_CACHE_KEY: Final = "campaigns.young_cohort"
"""``ctx.cache`` key of the run's :func:`_young_cohort` results, by campaign and maturity days."""

_EXACT: Final = Context(prec=60)
"""Arithmetic context for quantizing evidence values without overflow or rounding surprises."""

_SIX_PLACES: Final = Decimal("0.000001")
"""Ratios, costs, money amounts and outcome counts in evidence keep six places (:func:`_q`)."""

_FOUR_PLACES: Final = Decimal("0.0001")
"""Shares (unresolved, unknown evidence, impression share, ratios) keep four places: 0.01%."""

_ONE_PLACE: Final = Decimal("0.1")
"""Expected outcomes and percentages render with one decimal place in reason sentences."""

_ZERO: Final = Decimal(0)

_DASH: Final = "—"
"""A cost per outcome whose known outcome count is 0 (AAP 0.5.4.3, 0.5.4.7, 0.5.5): a zero
denominator, never a finite value and never "unavailable", which is reserved for unreported
outcomes."""

_FACET_LABELS: Final[Mapping[str, str]] = {
    "job_function": "job function",
    "seniority": "seniority",
    "industry": "industry",
}
"""Display labels of the LinkedIn demographic facets stored in ``fact_segment_window``."""

_CHECK_UNITS: Final[Mapping[str, str]] = {
    "spending_days": "spending days",
    "capped_days": "capped days",
    "capped_share": "capped days",
}
"""Units of the cap-gate checks; ``known_budget_days`` takes the history's own label."""

_HISTORY_MINIMUMS: Final[frozenset[str]] = frozenset({"known_budget_days", "spending_days"})
"""Cap-gate checks that measure history length ("short history", AAP 0.5.4.1)."""

_METRICS_DATASETS: Final[Mapping[str, str]] = {
    "google": "google.metrics_daily",
    "linkedin": "linkedin.analytics_daily",
}
"""The daily metrics dataset each ad source's spend and outcomes come from."""

_CRM_DATASETS: Final[Mapping[CrmScope, tuple[str, ...]]] = {
    "linkedin": ("linkedin.leads", "hubspot.contacts", "hubspot.associations"),
    "google": ("hubspot.google_contacts", "hubspot.contacts", "hubspot.associations"),
}
"""The datasets a pipeline population reads, per CRM scope (the effective CRM dataset)."""


# ---------------------------------------------------------------------------
# Settings and formatting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Settings:
    """The run's campaign-rule thresholds, read once from ``config/thresholds.yaml``.

    There are no code defaults: a missing or mistyped key raises ``ConfigError``.
    """

    c1_min_window: int
    c1_spend_share: Decimal
    c1_min_expected: Decimal
    c1b_min_window: int
    sustained_min: int
    veto_min: int
    maturity_days: int
    floor_share: Decimal
    scale_min_outcomes: Decimal
    scale_max_ratio: Decimal
    b1_step: Decimal
    c3a_min_leads: int
    c3a_max_cpl_ratio: Decimal
    c3a_max_share: Decimal
    c3b_jaccard: Decimal
    c3b_min_terms: int
    c3b_min_conversions: Decimal
    c3b_head_share: Decimal

    @classmethod
    def of(cls, ctx: AnalysisContext) -> _Settings:
        """Return the context's settings, read once per run and memoised in ``ctx.cache``."""
        cached = ctx.cache.get(_SETTINGS_CACHE_KEY)
        if isinstance(cached, _Settings):
            return cached
        settings = cls.from_thresholds(ctx.thresholds)
        ctx.cache[_SETTINGS_CACHE_KEY] = settings
        return settings

    @classmethod
    def from_thresholds(cls, thresholds: Thresholds) -> _Settings:
        """Read every campaign-rule threshold.

        Raises:
            ConfigError: When a key is missing or has the wrong type.
        """
        return cls(
            c1_min_window=thresholds.int("c1.min_window_days"),
            c1_spend_share=thresholds.ratio("c1.min_spend_day_share"),
            c1_min_expected=thresholds.ratio("c1.min_expected_outcomes"),
            c1b_min_window=thresholds.int("c1b.min_window_days"),
            sustained_min=thresholds.int("window.sustained_min_days"),
            veto_min=thresholds.int("crm.veto_min_leads"),
            maturity_days=thresholds.int("crm.lead_maturity_days"),
            floor_share=thresholds.ratio("attribution.floor_share"),
            scale_min_outcomes=thresholds.ratio("scale.min_outcomes"),
            scale_max_ratio=thresholds.ratio("scale.max_cpa_ratio"),
            b1_step=thresholds.ratio("b1.step"),
            c3a_min_leads=thresholds.int("c3a.min_leads"),
            c3a_max_cpl_ratio=thresholds.ratio("c3a.max_cpl_ratio"),
            c3a_max_share=thresholds.ratio("c3a.max_impression_share"),
            c3b_jaccard=thresholds.ratio("c3b.jaccard"),
            c3b_min_terms=thresholds.int("c3b.min_terms"),
            c3b_min_conversions=thresholds.ratio("c3b.min_conversions"),
            c3b_head_share=thresholds.ratio("c3b.head_token_share"),
        )


def _q(value: Decimal) -> Decimal:
    """Record a ratio, cost, money amount or outcome count in evidence with six places, half even.

    Every money amount and ``Decimal`` outcome count a campaign card records goes through here,
    as every budget card's does in ``budget.py``, so one item and one metric key never mix
    ``200``, ``240.00`` and ``398.000000``: a Google budget (``micros / 1,000,000``), a
    LinkedIn one (``numeric(18,6)``) and a proposal rounded to the currency's minor unit all
    read at six places (``240.00`` is recorded as ``240.000000``, the same value), and a C2
    "Scale" names the same amounts its B1 item does. Amounts of at most six places keep their
    exact value; the gates, comparisons and rank keys read the exact values, never these.
    """
    return value.quantize(_SIX_PLACES, rounding=ROUND_HALF_EVEN, context=_EXACT)


def _q_optional(value: Decimal | None) -> Decimal | None:
    """:func:`_q` of an amount or outcome count that may be unavailable; ``None`` stays ``None``."""
    return None if value is None else _q(value)


def cost_per_outcome_value(
    cost: Decimal | None, outcomes: Decimal | int | None
) -> Decimal | str | None:
    """Return a cost per outcome for evidence (AAP 0.5.4.3, 0.5.4.7, 0.5.5 zero denominators).

    The one rendering of a cost per outcome, or of the account baseline, that campaign cards
    here and budget cards in ``budget.py`` record. ``cost`` is the exact ``spend ÷ outcomes``
    (:func:`~ads_engine.analysis.metrics.cost_per_outcome`) and ``outcomes`` the count it
    divides by. A known count of 0 gives "—", a zero denominator shown as such and never
    flagged unavailable; an unreported count (``None``) gives ``None``, which
    :meth:`Metric.to_json` marks unavailable (AAP 0.11.1: absent values are ``None``).
    Otherwise the cost at six places, or ``None`` when no cost was computed. Only the
    displayed value is built here: every comparison and gate keeps its exact ``Decimal``.
    """
    if outcomes is None:
        return None
    if outcomes == 0:
        return _DASH
    return None if cost is None else _q(cost)


def _share(value: Decimal) -> Decimal:
    """Record a share or ratio in evidence with four places, half even: 2/22 → ``0.0909``.

    The gates compare the exact value; evidence shows it at a hundredth of a percent.
    """
    return value.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN, context=_EXACT)


def _q_floor(value: Decimal) -> Decimal:
    """Record a gated sample with six places, rounded down.

    A sample shown against its minimum never displays above the value that was compared, so
    a withheld "2.9999995 of 3" can never read "3.000000 of 3".
    """
    return value.quantize(_SIX_PLACES, rounding=ROUND_FLOOR, context=_EXACT)


def _money(amount: Decimal | int | str | None, currency: str) -> str:
    """Render an amount at the currency's minor unit: ``USD 1,400.00``; ``unavailable``.

    Without a known currency the amount renders at two places with no code. A pre-rendered
    string (such as ``n/a``) is returned as it is.
    """
    if amount is None:
        return "unavailable"
    if isinstance(amount, str):
        return amount
    places = minor_unit_exponent(currency)
    rendered = format(round_to_minor_unit(Decimal(amount), currency), f",.{places}f")
    return f"{currency} {rendered}" if currency.strip() else rendered


def _one_place(value: Decimal) -> str:
    """Render one decimal place, rounded down, so 2.95 expected outcomes read "2.9"."""
    return format(value.quantize(_ONE_PLACE, rounding=ROUND_FLOOR, context=_EXACT), "f")


def _percent(share: Decimal) -> str:
    """Render a share as a percentage with at most one decimal place: 0.1304 → ``13%``."""
    scaled = (share * 100).quantize(_ONE_PLACE, rounding=ROUND_HALF_EVEN, context=_EXACT)
    return f"{format(scaled.normalize(_EXACT), 'f')}%"


def _count(value: int | Decimal | str | None) -> str:
    """Render a count without trailing zeros (``6``, ``0.5``); ``unavailable`` for ``None``.

    A pre-rendered string (such as ``n/a``) is returned as it is.
    """
    if value is None:
        return "unavailable"
    if isinstance(value, Decimal):
        return format(value.normalize(_EXACT), "f")
    return str(value)


def _singular(label: str) -> str:
    """The singular of an outcome label: ``conversions`` → ``conversion``."""
    return label[:-1] if label.endswith("s") else label


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


# ---------------------------------------------------------------------------
# Entities, subjects and datasets
# ---------------------------------------------------------------------------


def _campaign_ref(campaign: CampaignInfo) -> EntityRef:
    return EntityRef(
        campaign.source, ENTITY_CAMPAIGN, campaign.id, campaign.external_id, campaign.name
    )


def _campaign_group_extra(ctx: AnalysisContext, campaign: CampaignInfo) -> dict[str, Any]:
    """The ``evidence.extra`` entry naming a LinkedIn campaign's campaign group.

    AAP 0.5.2: a campaign group is "shown in campaign evidence", while decisions and
    discrepancies cover campaigns and budgets only. The group is therefore evidence alone,
    never a decision subject, a P1 dependency or a G1 target. Google campaigns have no
    campaign groups, so a Google campaign gets ``{}``.

    Returns:
        ``{"campaign_group": {state, id, external_id, name, status, daily_budget,
        total_budget, currency}}``. ``state`` is ``loaded`` (the group's identity, name, raw
        LinkedIn status and reported budgets, in its account's currency; a budget the group
        does not set stays ``None``, never 0), ``not_loaded`` (the campaign names a group the
        context does not hold: the id is kept and every other field is ``None``, unavailable)
        or ``ungrouped`` (the campaign belongs to no group; every field is ``None``).
    """
    if campaign.source != "linkedin":
        return {}
    group_id = campaign.campaign_group_id
    group = None if group_id is None else ctx.campaign_groups.get(group_id)
    if group is None:
        return {
            _CAMPAIGN_GROUP_KEY: {
                "state": "ungrouped" if group_id is None else "not_loaded",
                "id": group_id,
                "external_id": None,
                "name": None,
                "status": None,
                "daily_budget": None,
                "total_budget": None,
                "currency": None,
            }
        }
    account = ctx.accounts.get(group.account_id)
    currency: str | None = None
    if account is not None:
        currency = account.currency
    elif group.account_id == campaign.account_id:
        currency = campaign.currency
    return {
        _CAMPAIGN_GROUP_KEY: {
            "state": "loaded",
            "id": group.id,
            "external_id": group.external_id,
            "name": group.name,
            "status": group.status,
            "daily_budget": group.daily_budget,
            "total_budget": group.total_budget,
            "currency": currency,
        }
    }


def _subject_ref(ctx: AnalysisContext, subject: SubjectKey) -> EntityRef:
    """The entity reference of a budget subject: a campaign, or a Google shared budget."""
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return _campaign_ref(ctx.campaigns[subject_id])
    budget = ctx.budgets[subject_id]
    return EntityRef("google", ENTITY_BUDGET, budget.id, budget.external_id, budget.name)


def _subject_currency(ctx: AnalysisContext, subject: SubjectKey) -> str:
    """The currency a subject's amounts are in; amounts never cross currencies."""
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return ctx.account_currency(subject_id)
    return ctx.budgets[subject_id].currency


def _depends_on(
    ctx: AnalysisContext, campaign_ids: Iterable[int], subjects: Iterable[SubjectKey] = ()
) -> tuple[DecisionSubject, ...]:
    """The subjects whose unresolved discrepancy holds an item (P1).

    These are ``subjects``, then each campaign and the budget it draws from, de-duplicated in
    that order. Precedence expands the list further (keywords, creatives, shared budgets).
    """
    ordered: list[DecisionSubject] = [DecisionSubject.of(key) for key in subjects]
    for campaign_id in campaign_ids:
        ordered.append(DecisionSubject("campaign", campaign_id))
        campaign = ctx.campaigns.get(campaign_id)
        if campaign is not None and campaign.budget_id is not None:
            ordered.append(DecisionSubject("budget", campaign.budget_id))
    return tuple(dict.fromkeys(ordered))


def _ad_datasets(source: Source) -> tuple[str, str]:
    return (f"{source}.entities", _METRICS_DATASETS[source])


def _active_campaigns(ctx: AnalysisContext, source: Source | None = None) -> list[CampaignInfo]:
    """The context's active ad campaigns, of one source when given, by id."""
    return sorted(
        (
            campaign
            for campaign in ctx.campaigns.values()
            if campaign.status == "active"
            and campaign.source in _METRICS_DATASETS
            and (source is None or campaign.source == source)
        ),
        key=lambda campaign: campaign.id,
    )


# ---------------------------------------------------------------------------
# CRM pipeline evidence (AAP 0.5.4.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CrmView:
    """The pipeline evidence one item reads.

    Attributes:
        veto: The matured population and veto; ``None`` without a CRM link.
        scope: Which CRM dataset its ``hubspot.*`` datasets resolve to; ``None`` without one.
        flags: ``no_crm_link`` (Google without a verified link), or the population's
            ``pipeline_unavailable``, ``attribution_floor`` and ``unknown_crm_evidence``.
    """

    veto: VetoPopulation | None
    scope: CrmScope | None
    flags: tuple[Flag, ...] = ()

    @property
    def datasets(self) -> tuple[str, ...]:
        """The CRM datasets the evidence reads; empty without a population."""
        if self.veto is None or self.scope is None:
            return ()
        return _CRM_DATASETS[self.scope]


def _veto_flags(veto: VetoPopulation) -> tuple[Flag, ...]:
    """The flags explaining why a population's pipeline cannot be judged."""
    flags: list[Flag] = []
    if not veto.available:
        flags.append(Flag(FLAG_PIPELINE_UNAVAILABLE, f"Pipeline unavailable: {veto.reason}"))
    if veto.attribution_floor and veto.unresolved_share is not None:
        flags.append(
            Flag(
                FLAG_ATTRIBUTION_FLOOR,
                f"Floor: {_percent(veto.unresolved_share)} of matured leads unmatched in HubSpot",
            )
        )
    if veto.unknown_evidence:
        flags.append(
            Flag(
                FLAG_UNKNOWN_CRM_EVIDENCE,
                f"Unknown CRM evidence for {veto.unknown_evidence_contacts} of "
                f"{veto.eligible_contacts} eligible contacts",
            )
        )
    return tuple(flags)


def _campaign_crm(
    ctx: AnalysisContext, attribution: AttributionTable, campaign: CampaignInfo
) -> _CrmView:
    """The pipeline evidence of one campaign: LinkedIn lead attribution or the Google link."""
    if campaign.source == "linkedin":
        veto = attribution.veto.get(campaign.id)
        return _CrmView(veto, "linkedin", () if veto is None else _veto_flags(veto))
    gap = google_link_gap(ctx)
    if gap is not None:
        return _CrmView(None, None, (no_crm_link_flag(gap),))
    veto = attribution.veto.get(campaign.id)
    return _CrmView(veto, "google", () if veto is None else _veto_flags(veto))


def _subject_crm(
    ctx: AnalysisContext,
    attribution: AttributionTable,
    subject: SubjectKey,
    history: BudgetHistory,
) -> _CrmView:
    """The pipeline evidence of a budget subject (C2): a campaign's, or a shared budget's.

    A shared budget's population is the Google touch-date contacts of its members on their
    member days only (:func:`~ads_engine.analysis.attribution.shared_budget_veto`), the days
    its spend and outcomes are aggregated over (AAP 0.5.4.5), dated in the zone its history
    classified them in. A contact touched before a campaign joined the budget or after it left
    never enters its veto, and one veto serves the C2 item and its B1.
    """
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return _campaign_crm(ctx, attribution, ctx.campaigns[subject_id])
    gap = google_link_gap(ctx)
    if gap is not None:
        return _CrmView(None, None, (no_crm_link_flag(gap),))
    veto = shared_budget_veto(ctx, history.members, native_day_zone(ctx, "google"))
    return _CrmView(veto, "google", () if veto is None else _veto_flags(veto))


def _leads_by_campaign(ctx: AnalysisContext) -> Mapping[int, tuple[LeadInfo, ...]]:
    """The run's window leads grouped by campaign id, each group in ``ctx.leads`` order.

    Built in one pass over ``ctx.leads`` and memoised in ``ctx.cache``, so reading one
    campaign's leads is a lookup rather than a scan of the whole window population.
    """
    cached = ctx.cache.get(_LEADS_BY_CAMPAIGN_CACHE_KEY)
    if isinstance(cached, Mapping):
        return cached
    grouped: dict[int, list[LeadInfo]] = {}
    for lead in ctx.leads:
        grouped.setdefault(lead.campaign_id, []).append(lead)
    by_campaign = {campaign_id: tuple(leads) for campaign_id, leads in grouped.items()}
    ctx.cache[_LEADS_BY_CAMPAIGN_CACHE_KEY] = by_campaign
    return by_campaign


def _young_cohort(
    ctx: AnalysisContext, settings: _Settings, campaign_id: int
) -> tuple[int, int | None]:
    """Leads too young for the veto, and the Qualified Leads their contacts already reached.

    AAP 0.5.4.1: outcomes of leads younger than ``crm.lead_maturity_days`` never enter the
    veto. They are shown beside it, labelled as not judged, so a Hold card makes visible that
    newer leads may already be converting. QL counts only contacts that are not already
    eligible through a matured lead, at or after their earliest young touch and by the run.

    The campaign's leads come from :func:`_leads_by_campaign`, one grouping of ``ctx.leads``
    per run. The result is memoised in ``ctx.cache`` per campaign id and
    ``crm.lead_maturity_days``, so every item of one campaign reuses one evaluation.

    Returns:
        ``(young leads, their contacts' QL)``; QL is ``None`` without a CRM dataset or a
        resolved Qualified Lead stage, never 0.
    """
    key = (campaign_id, settings.maturity_days)
    cached = ctx.cache.get(_YOUNG_COHORT_CACHE_KEY)
    if isinstance(cached, dict):
        memo: dict[tuple[int, int], tuple[int, int | None]] = cached
    else:
        memo = {}
        ctx.cache[_YOUNG_COHORT_CACHE_KEY] = memo
    cohort = memo.get(key)
    if cohort is None:
        cohort = _young_cohort_counts(ctx, settings, _leads_by_campaign(ctx).get(campaign_id, ()))
        memo[key] = cohort
    return cohort


def _young_cohort_counts(
    ctx: AnalysisContext, settings: _Settings, leads: Sequence[LeadInfo]
) -> tuple[int, int | None]:
    """Compute :func:`_young_cohort` from one campaign's window leads, without the memo."""
    cutoff = ctx.window.utc_bounds()[1] - timedelta(days=settings.maturity_days)
    young = [lead for lead in leads if lead.submitted_at > cutoff]
    crm = ctx.linkedin_crm
    if crm is None or not crm.bindings.ql.available:
        return len(young), None
    matured_contacts = {
        lead.contact_hs_id for lead in leads if lead.submitted_at <= cutoff and lead.matched
    }
    touches: dict[str, datetime] = {}
    for lead in young:
        hs_id = lead.contact_hs_id
        if not lead.matched or hs_id is None or hs_id in matured_contacts:
            continue
        earlier = touches.get(hs_id)
        if earlier is None or lead.submitted_at < earlier:
            touches[hs_id] = lead.submitted_at
    reached = 0
    for hs_id, touch in touches.items():
        contact = crm.contacts.get(hs_id)
        at = None if contact is None else contact.ql_at
        if at is not None and touch <= at <= ctx.now:
            reached += 1
    return len(young), reached


def _veto_metrics(
    ctx: AnalysisContext, settings: _Settings, attribution: AttributionTable, view: _CrmView
) -> list[Metric]:
    """The pipeline counts, side by side and never blended (AAP 0.5.4.1, 0.5.4.4).

    Leads, matured, matched and unresolved matured leads, eligible contacts, MQL, QL and
    meetings each get their own metric, followed by the young, not-judged cohort and the
    Qualified Leads it already reached. For Google, leads are every touch-date contact in the
    window (or on the shared budget's member days) and the young cohort is the population's
    own (``young_leads``, ``young_ql``); for LinkedIn it comes from the window leads
    (:func:`_young_cohort`).
    """
    veto = view.veto
    if veto is None:
        return []
    google = veto.source == "google"
    lead_unit = "contacts" if google else "leads"
    mql_label = "MQL" if google or not attribution.mql_label else f"MQL ({attribution.mql_label})"
    unknown_share = (
        Decimal(veto.unknown_evidence_contacts) / Decimal(veto.eligible_contacts)
        if veto.eligible_contacts
        else None
    )
    metrics = [
        Metric(
            "leads",
            "Google touch-date contacts" if google else "LinkedIn leads (platform)",
            veto.leads,
            lead_unit,
        ),
        Metric(
            "matured_leads",
            f"Matured {lead_unit} (at least {settings.maturity_days} days before window end)",
            veto.matured_leads,
            lead_unit,
        ),
        Metric("matched_leads", f"Matched matured {lead_unit}", veto.matched_leads, lead_unit),
        Metric("unresolved", f"Unresolved matured {lead_unit}", veto.unresolved_leads, lead_unit),
        Metric(
            "unresolved_share",
            f"Unresolved share of matured {lead_unit}",
            None if veto.unresolved_share is None else _share(veto.unresolved_share),
            "ratio",
        ),
        Metric("eligible_contacts", "Eligible contacts", veto.eligible_contacts, "contacts"),
        Metric(
            "unknown_evidence_share",
            "Share of eligible contacts with unknown CRM evidence",
            None if unknown_share is None else _share(unknown_share),
            "ratio",
        ),
        Metric("mql", mql_label, veto.mql, "contacts"),
        Metric("ql", "Qualified Leads", veto.ql, "contacts"),
        Metric("meetings", "Meetings", veto.meetings, "meetings"),
    ]
    if google:
        metrics.append(
            Metric(
                "young_leads",
                f"Touch-date contacts younger than {settings.maturity_days} days (not judged)",
                veto.young_leads,
                "contacts",
            )
        )
        metrics.append(
            Metric(
                "young_ql",
                "Qualified Leads from those contacts (not judged)",
                veto.young_ql,
                "contacts",
            )
        )
    else:
        young, young_ql = _young_cohort(ctx, settings, veto.campaign_id)
        metrics.append(
            Metric(
                "young_leads",
                f"Leads younger than {settings.maturity_days} days (not judged)",
                young,
                "leads",
            )
        )
        metrics.append(
            Metric(
                "young_ql", "Qualified Leads from those leads (not judged)", young_ql, "contacts"
            )
        )
    return metrics


# ---------------------------------------------------------------------------
# C2 and B1 eligibility: scale_outcomes (AAP 0.5.4.4, 0.5.4.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScaleOutcome:
    """One cap-constrained, efficient budget subject, and what the pipeline veto made of it.

    ``kind == "scale"`` yields a C2 "Scale" item here and a B1 increase in ``budget.py``;
    ``kind == "hold"`` yields a C2 "Hold" item and no B1, because the veto fired. Both
    readers take this one evaluation, so C2 and B1 can never disagree.

    Attributes:
        subject: The budget subject: ``("campaign", id)`` or a shared ``("budget", id)``.
        entity: The subject's entity reference.
        kind: ``scale`` or ``hold``.
        history: The subject's budget-day history over the window.
        gate: The passed cap-constrained gate, with every count observed against required.
        outcomes: Window outcomes (for a shared budget, over member days).
        cpo: Window cost per outcome.
        baseline: The account baseline cost per outcome it was compared with.
        spend: Window spend (for a shared budget, over member days).
        current_daily_budget: The latest effective daily budget.
        proposed_daily_budget: ``current x (1 + b1.step)`` at the currency's minor unit.
        veto: The pipeline population judged; ``None`` without a CRM link.
        crm_scope: The CRM dataset the veto read; ``None`` without one.
        flags: ``no_crm_link``, ``pipeline_unavailable``, ``attribution_floor``,
            ``unknown_crm_evidence`` and, for a shared budget, ``shared_budget``.
        members: The campaigns the subject's spend comes from (a campaign is its own member;
            a shared budget's are those with member days in the window plus those drawing
            from it now), for the evidence and the item's dependencies.
        receivers: The campaigns a spend increase on the subject lands on now: the campaign
            itself, or the campaigns drawing from the shared budget now. A campaign that left
            the budget keeps its member days in ``members`` but is no longer raised by it.
        currency: The currency of every amount.
        g1_blocked: Whether G1 blocks a spend increase on the subject or a receiver
            (:func:`~ads_engine.analysis.metrics.spend_increase_blocked`). Only a ``scale``
            entry can be blocked: its C2 "Scale" and B1 items are still built, declaring their
            targets, so the precedence G1 post-filter removes them and counts ``g1_blocked``.
    """

    subject: SubjectKey
    entity: EntityRef
    kind: Literal["scale", "hold"]
    history: BudgetHistory
    gate: GateResult
    outcomes: Decimal
    cpo: Decimal
    baseline: Decimal | None
    spend: Decimal
    current_daily_budget: Decimal
    proposed_daily_budget: Decimal
    veto: VetoPopulation | None
    crm_scope: CrmScope | None
    flags: tuple[Flag, ...]
    members: tuple[int, ...]
    receivers: tuple[int, ...]
    currency: str
    g1_blocked: bool = False

    @property
    def shared(self) -> bool:
        """Whether the subject is a Google explicitly shared budget."""
        return self.subject[0] == "budget"

    @property
    def decision_subject(self) -> DecisionSubject:
        """The subject a "Record decision" writes: the shared budget itself when shared."""
        return DecisionSubject.of(self.subject)

    @property
    def increases_spend_on(self) -> tuple[DecisionSubject, ...]:
        """The G1 targets of a Scale (and its B1): the subject plus every receiver campaign.

        AAP 0.5.4.2: a candidate declares the campaigns and budgets whose spend it would
        raise, so a campaign that has left a shared budget is no target of its increase.
        """
        targets = [self.decision_subject]
        targets.extend(DecisionSubject("campaign", cid) for cid in self.receivers)
        return tuple(dict.fromkeys(targets))


@dataclass(frozen=True, slots=True)
class ScaleWithheld:
    """An efficient budget subject whose scale evidence falls short of a minimum (P6).

    Converted to one C2 ``Withheld`` here and one B1 ``Withheld`` in ``budget.py``.

    Attributes:
        subject: The budget subject.
        entity: Its entity reference.
        reason: The shortfall, for example ``known-budget days 4 of 7 required``.
        observed: The first shortfall's observed value; ``None`` when not a count.
        required: Its required value; ``None`` when not a count.
        unit: The unit of ``observed`` and ``required``.
        history: The budget history, when the gate was evaluated.
        gate: The failed or passed gate, when evaluated.
        members: The campaigns the subject's spend comes from.
        kind: Always ``withheld``, so entries can be told apart without ``isinstance``.
    """

    subject: SubjectKey
    entity: EntityRef
    reason: str
    observed: Decimal | int | None
    required: Decimal | int | None
    unit: str
    history: BudgetHistory | None = None
    gate: GateResult | None = None
    members: tuple[int, ...] = ()
    kind: Literal["withheld"] = "withheld"

    def to_withheld(self, rule_id: str, section: str) -> Withheld:
        """The withheld line of ``rule_id`` (C2 or B1) in ``section``."""
        return Withheld(
            rule_id=rule_id,
            section=section,
            entity=self.entity,
            reason=self.reason,
            observed=self.observed,
            required=self.required,
            unit=self.unit,
        )


ScaleEntry = ScaleOutcome | ScaleWithheld
"""One entry of :func:`scale_outcomes`: an eligible subject, or a withheld one."""


@dataclass(frozen=True, slots=True)
class _ScaleMemo:
    """The memoised :func:`scale_outcomes` of one context."""

    entries: tuple[ScaleEntry, ...]


def scale_outcomes(ctx: AnalysisContext) -> tuple[ScaleEntry, ...]:
    """Return the C2 and B1 evaluation of every active budget subject, in subject order.

    AAP 0.5.4.4 C2 and 0.5.4.5 B1, one evaluation for both. Eligibility (identical to B1):

    * the subject is active and has good output: window outcomes ≥ ``scale.min_outcomes``
      and cost per outcome ≤ ``scale.max_cpa_ratio`` x the baseline for its source and
      objective group;
    * the window is at least ``window.sustained_min_days`` (a sustained-pattern rule);
    * it is cap-constrained (:func:`~ads_engine.analysis.metrics.cap_constrained`: at least
      ``budget.min_known_days`` known-budget active days, ``budget.min_spending_days``
      spending days, and capped days ≥ ``budget.min_capped_days`` and ≥
      ``budget.sustained_share`` of the known-budget active days);
    * G1 does not block a spend increase on it or on a campaign the increase would raise now
      (:attr:`ScaleOutcome.receivers`;
      :func:`~ads_engine.analysis.metrics.spend_increase_blocked`).

    The pipeline veto is evaluated separately: when it fires the entry is a ``hold``, and
    otherwise a ``scale``, flagged ``pipeline_unavailable`` or ``no_crm_link`` when the veto
    could not be judged.

    G1 is a post-filter, not applied here: a blocked subject that would otherwise scale stays
    a ``scale`` entry with :attr:`ScaleOutcome.g1_blocked`, so its C2 "Scale" and B1 items
    reach ``precedence.py``, which removes them and records them in the run's G1-blocked
    suppression count. A blocked subject never yields a ``hold``: it is not eligible, and a
    Hold declares no spend increase the post-filter could remove.

    An efficient subject whose window, history or outcomes fall short becomes a
    :class:`ScaleWithheld` with observed against required, as does one whose objective groups
    or currencies are mixed (efficiency "n/a"). A cap gate failure is withheld when the history
    is short or at least one day was capped; a full history with no capped day is simply not
    cap-constrained. Subjects without good output yield nothing.

    Memoised in ``ctx.cache["scale_outcomes"]``: a second call returns the identical tuple.

    Threshold keys: ``scale.min_outcomes``, ``scale.max_cpa_ratio``, ``b1.step``,
    ``window.sustained_min_days``, the ``budget.*`` gate keys and, through the veto,
    ``crm.veto_min_leads``, ``crm.lead_maturity_days``, ``attribution.floor_share`` and
    ``crm.meeting_outcome_exclusions``.
    """
    cached = ctx.cache.get(SCALE_CACHE_KEY)
    if isinstance(cached, _ScaleMemo):
        return cached.entries
    settings = _Settings.of(ctx)
    attribution = attribution_for(ctx)
    blocked = spend_increase_blocked(ctx)
    entries: list[ScaleEntry] = []
    for subject in budget_subjects(ctx):
        entry = _scale_entry(ctx, settings, attribution, blocked, subject)
        if entry is not None:
            entries.append(entry)
    memo = _ScaleMemo(tuple(entries))
    ctx.cache[SCALE_CACHE_KEY] = memo
    return memo.entries


def _subject_active(ctx: AnalysisContext, subject: SubjectKey, members: Sequence[int]) -> bool:
    """Whether a subject is active now: the campaign, or for a shared budget any member."""
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return ctx.campaigns[subject_id].status == "active"
    return any(
        ctx.campaigns[cid].status == "active" and ctx.campaigns[cid].budget_id == subject_id
        for cid in members
        if cid in ctx.campaigns
    )


def _spend_receivers(ctx: AnalysisContext, subject: SubjectKey) -> tuple[int, ...]:
    """The campaigns a spend increase on a budget subject lands on now, ascending.

    AAP 0.5.4.2: G1 targets are the campaigns and budgets whose spend an increase would
    raise. A campaign subject raises only itself; a shared budget raises the campaigns drawing
    from it now. A campaign that left the budget mid-window keeps its member days in the
    evidence, but a raise of this budget no longer reaches it, so its own G1 block (for
    example B2 on the budget it moved to) cannot suppress this increase.
    """
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return (subject_id,)
    return tuple(sorted(c.id for c in ctx.campaigns.values() if c.budget_id == subject_id))


def _outcome_label(ctx: AnalysisContext, members: Sequence[int]) -> str:
    """The outcome label of a subject's members (one objective group), e.g. ``leads``."""
    for cid in members:
        campaign = ctx.campaigns.get(cid)
        if campaign is not None:
            return OUTCOME_LABELS[campaign.objective_group]
    return "outcomes"


def _scale_entry(
    ctx: AnalysisContext,
    settings: _Settings,
    attribution: AttributionTable,
    blocked: frozenset[SubjectKey],
    subject: SubjectKey,
) -> ScaleEntry | None:
    """Evaluate one budget subject for C2 and B1; ``None`` when it is no scale candidate."""
    members = subject_campaigns(ctx, subject)
    if not _subject_active(ctx, subject, members):
        return None
    entity = _subject_ref(ctx, subject)
    totals = subject_totals(ctx, subject)
    if totals is None:
        groups = subject_objective_groups(ctx, subject)
        if len(groups) > 1:
            return ScaleWithheld(
                subject,
                entity,
                f"members span {len(groups)} objective groups: efficiency n/a",
                len(groups),
                1,
                "objective groups",
                members=members,
            )
        return ScaleWithheld(
            subject,
            entity,
            "members span more than one currency: efficiency n/a",
            None,
            None,
            "currencies",
            members=members,
        )
    baseline = subject_baseline(ctx, subject)
    cpo = cost_per_outcome(totals.spend, totals.outcomes)
    if (
        totals.outcomes is None
        or cpo is None
        or baseline is None
        or cpo > settings.scale_max_ratio * baseline
    ):
        return None
    if not ctx.window.sustained_ok:
        return ScaleWithheld(
            subject,
            entity,
            f"window {_days(ctx.window.days)} of {settings.sustained_min} required",
            ctx.window.days,
            settings.sustained_min,
            "days",
            members=members,
        )
    history = budget_history(ctx, subject)
    gate = cap_constrained(history, ctx.thresholds)
    if not gate.passed:
        return _gate_withheld(subject, entity, history, gate, members)
    if totals.outcomes < settings.scale_min_outcomes:
        label = _outcome_label(ctx, members)
        return ScaleWithheld(
            subject,
            entity,
            f"{label} {_count(totals.outcomes)} of {_count(settings.scale_min_outcomes)} required",
            _q_floor(totals.outcomes),
            settings.scale_min_outcomes,
            label,
            history=history,
            gate=gate,
            members=members,
        )
    current = history.latest_daily_budget
    if current is None:
        return None
    currency = _subject_currency(ctx, subject)
    view = _subject_crm(ctx, attribution, subject, history)
    flags = list(view.flags)
    if subject[0] == "budget":
        flags.append(
            Flag(
                FLAG_SHARED_BUDGET,
                f"Shared budget across {len(members)} campaigns: decided at budget level",
            )
        )
    hold = view.veto is not None and view.veto.veto_fires
    receivers = _spend_receivers(ctx, subject)
    targets: list[SubjectKey] = [subject, *(("campaign", cid) for cid in receivers)]
    g1_blocked = any(target in blocked for target in targets)
    if hold and g1_blocked:
        # Not eligible, and a Hold declares no spend increase the G1 post-filter could remove.
        return None
    return ScaleOutcome(
        subject=subject,
        entity=entity,
        kind="hold" if hold else "scale",
        history=history,
        gate=gate,
        outcomes=totals.outcomes,
        cpo=cpo,
        baseline=baseline,
        spend=totals.spend,
        current_daily_budget=current,
        proposed_daily_budget=round_to_minor_unit(current * (1 + settings.b1_step), currency),
        veto=view.veto,
        crm_scope=view.scope,
        flags=tuple(flags),
        members=members,
        receivers=receivers,
        currency=currency,
        g1_blocked=g1_blocked,
    )


def _gate_withheld(
    subject: SubjectKey,
    entity: EntityRef,
    history: BudgetHistory,
    gate: GateResult,
    members: tuple[int, ...],
) -> ScaleWithheld | None:
    """The withheld line of a failed cap gate, or ``None`` when the subject is simply uncapped.

    A short history (known-budget or spending days below their minimums) or at least one
    capped day is a pattern below its minimum (P6) and is withheld with observed against
    required. A full history with no capped day is not cap-constrained at all. The sample is
    the check the first shortfall states, so it carries the requirement that clause names.
    """
    short = [
        key
        for key in _HISTORY_MINIMUMS
        if key in gate.counts and gate.counts[key][0] < gate.counts[key][1]
    ]
    if not short and history.capped == 0:
        return None
    first = gate.shortfall_checks[0]
    observed, required = gate.counts[first]
    unit = history.known_label if first == "known_budget_days" else _CHECK_UNITS[first]
    return ScaleWithheld(
        subject,
        entity,
        gate.describe(),
        observed,
        required,
        unit,
        history=history,
        gate=gate,
        members=members,
    )


# ---------------------------------------------------------------------------
# Shared item plumbing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Notes:
    """The section findings collected while a run's campaign rules are evaluated.

    Attributes:
        pipeline: Per campaign entity key, the entity and the unavailable or floored
            population an item or withheld line referred to; one finding each.
        no_crm_link: The Google link gap, once any Google item carries ``no_crm_link``.
        no_crm_items: How many Google items the rules built carry ``no_crm_link``.
            ``precedence.py`` recounts the finding's ``items`` over the section's stored
            items carrying the flag, C0 included, after G1, P2 and feedback suppression.
    """

    pipeline: dict[str, tuple[EntityRef, VetoPopulation, tuple[Flag, ...]]] = field(
        default_factory=dict
    )
    no_crm_link: str | None = None
    no_crm_items: int = 0

    def note_view(self, ctx: AnalysisContext, entity: EntityRef, view: _CrmView) -> None:
        """Record what an item's pipeline evidence implies for the section's findings."""
        if any(flag.code == FLAG_NO_CRM_LINK for flag in view.flags):
            self.no_crm_link = google_link_gap(ctx)
            self.no_crm_items += 1
        veto = view.veto
        if veto is not None and (not veto.available or veto.attribution_floor):
            self.pipeline.setdefault(entity.key, (entity, veto, view.flags))

    def findings(self) -> list[Finding]:
        """One ``pipeline_unavailable`` finding per noted campaign, then ``no_crm_link``."""
        found: list[Finding] = []
        for key in sorted(self.pipeline):
            entity, veto, flags = self.pipeline[key]
            reason = veto.reason or "attribution floor reached"
            found.append(
                Finding(
                    FINDING_PIPELINE_UNAVAILABLE,
                    CAMPAIGNS,
                    entity,
                    f"Pipeline unavailable for {entity.name}: {reason}",
                    {
                        "reason": reason,
                        "flags": [flag.code for flag in flags],
                        "flag_messages": [flag.message for flag in flags],
                        "matured_leads": veto.matured_leads,
                        "unresolved_leads": veto.unresolved_leads,
                        "unresolved_share": (
                            None if veto.unresolved_share is None else _share(veto.unresolved_share)
                        ),
                        "eligible_contacts": veto.eligible_contacts,
                        "unknown_evidence_contacts": veto.unknown_evidence_contacts,
                        "unknown_evidence_share": (
                            _share(
                                Decimal(veto.unknown_evidence_contacts)
                                / Decimal(veto.eligible_contacts)
                            )
                            if veto.eligible_contacts
                            else None
                        ),
                    },
                )
            )
        if self.no_crm_link is not None:
            found.append(
                Finding(
                    FINDING_NO_CRM_LINK,
                    CAMPAIGNS,
                    None,
                    f"No CRM link for Google campaigns: {self.no_crm_link}. No Google Qualified "
                    "Lead counts or pipeline veto are computed.",
                    {"reason": self.no_crm_link, "items": self.no_crm_items},
                )
            )
        return found


def _value(evidence: Evidence, key: str) -> Decimal | int | str | None:
    return evidence.metric(key).value


def _money_of(evidence: Evidence, key: str) -> str:
    """One of an item's own money metrics, rendered in its currency."""
    metric = evidence.metric(key)
    return _money(metric.value, metric.currency or "")


def _required(*groups: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for group in groups for name in group))


# ---------------------------------------------------------------------------
# C2 Scale or Hold (AAP 0.5.4.4)
# ---------------------------------------------------------------------------


def _day_metrics(history: BudgetHistory, currency: str) -> list[Metric]:
    """Utilization and every day count of 0.5.4.1, beside the daily budget."""
    utilization: Decimal | str = (
        "n/a" if history.window_utilization is None else _share(history.window_utilization)
    )
    return [
        Metric(
            "daily_budget",
            "Latest effective daily budget",
            _q_optional(history.latest_daily_budget),
            "currency",
            currency,
        ),
        Metric("window_utilization", "Window utilization (spend vs cap)", utilization, "ratio"),
        Metric("known_days", history.known_label.capitalize(), history.known_active, "days"),
        Metric("spending_days", "Spending days", history.spending, "days"),
        Metric("capped_days", "Capped days", history.capped, "days"),
        Metric("idle_days", "Known idle days", history.idle, "days"),
        Metric(
            "changed_intraday_days",
            "Changed-intraday days (unknown budget)",
            history.changed_intraday,
            "days",
        ),
        Metric("unobserved_days", "Unobserved days (unknown budget)", history.unobserved, "days"),
    ]


def _day_extra(history: BudgetHistory, gate: GateResult) -> dict[str, Any]:
    """The day lists (ISO dates) and the gate's observed-against-required counts, for the card.

    Dates are native dates of the subject's source, stored as ISO strings exactly as the
    evidence JSON renders them.
    """

    def dates(klass: str | None = None, *, idle: bool = False) -> tuple[str, ...]:
        return tuple(
            day.date.isoformat()
            for day in history.days
            if (day.idle if idle else day.klass == klass)
        )

    return {
        "gate": {
            key: {"observed": observed, "required": required}
            for key, (observed, required) in gate.counts.items()
        },
        "observation_assumption": history.assumption,
        "idle_dates": dates(idle=True),
        "changed_intraday_dates": dates("changed_intraday"),
        "unobserved_dates": dates("unobserved"),
    }


def _members_extra(ctx: AnalysisContext, outcome: ScaleOutcome) -> list[dict[str, Any]]:
    """Every member campaign of a shared budget with its member days in the window."""
    return [
        {
            "campaign_id": cid,
            "name": ctx.campaign_name(cid),
            "member_days": len(outcome.history.members.get(cid, ())),
        }
        for cid in outcome.members
    ]


def _c2_candidate(
    ctx: AnalysisContext,
    settings: _Settings,
    attribution: AttributionTable,
    outcome: ScaleOutcome,
    notes: _Notes,
) -> Candidate:
    """The C2 "Scale" or "Hold" item of one :class:`ScaleOutcome`.

    The card shows spend, outcomes, expected outcomes (spend ÷ the account baseline cost per
    outcome, AAP 0.5.4.1; ``None``, unavailable, without a baseline), cost per outcome, the
    day counts of the cap gate and the veto counts (AAP 0.5.5: "Pause and scale cards show
    spend, outcomes, expected outcomes and CRM QL"). A shared budget lists its members in
    ``extra.members``; a LinkedIn campaign subject names its campaign group in
    ``extra.campaign_group`` (:func:`_campaign_group_extra`), and a Google campaign has none.

    Rule C2 Scale or Hold (AAP 0.5.4.4), judged by :func:`scale_outcomes` and emitted by
    :func:`_c2`. Eligibility is the same evaluation as B1: the cap-constrained gate
    (``budget.*``), at least ``scale.min_outcomes`` (5) outcomes, cost per outcome at most
    ``scale.max_cpa_ratio`` (1.0) x the baseline, and G1 not blocking (applied by the
    precedence post-filter; a blocked Scale records ``extra.g1_blocked``). A "Scale" links
    its B1 item in ``extra.b1_link``, whose amount is the daily budget raised by ``b1.step``;
    a "Hold" means the pipeline veto fired, so it declares no spend increase and has no B1.

    Threshold keys: :data:`C2_THRESHOLD_KEYS`, plus :data:`VETO_THRESHOLD_KEYS` when the
    outcome carries a veto population (always for a Hold).

    Rationale: eligibility and veto are evaluated separately, so the Hold is always emitted
    when the veto fires: a budget capped with good platform output is not scaled while its
    eligible contacts reach no Qualified Lead.
    """
    history = outcome.history
    currency = outcome.currency
    label = _outcome_label(ctx, outcome.members)
    scale = outcome.kind == "scale"
    view = _CrmView(outcome.veto, outcome.crm_scope, outcome.flags)
    notes.note_view(ctx, outcome.entity, view)
    expected = expected_outcomes(outcome.spend, outcome.baseline)
    # Every amount and outcome count is recorded at six places (:func:`_q`), as the B1 item
    # records them, so the card and ``b1_link`` name its amounts exactly; the proposal keeps
    # its minor-unit value. The exact values stay the inputs of every gate and the rank key.
    current = _q(outcome.current_daily_budget)
    proposed = _q(outcome.proposed_daily_budget)
    metrics = [
        Metric("spend", "Window spend", _q(outcome.spend), "currency", currency),
        Metric("window_days", "Window days", ctx.window.days, "days"),
        Metric("outcomes", f"Window {label}", _q(outcome.outcomes), label),
        Metric(
            "expected_outcomes",
            f"Expected {label} at the account baseline",
            None if expected is None else _q(expected),
            label,
        ),
        Metric(
            "cost_per_outcome",
            f"Cost per {_singular(label)}",
            _q(outcome.cpo),
            "currency",
            currency,
        ),
        *_day_metrics(history, currency),
    ]
    if scale:
        metrics.append(
            Metric(
                "proposed_daily_budget",
                "Proposed daily budget (B1)",
                proposed,
                "currency",
                currency,
            )
        )
    metrics.extend(_veto_metrics(ctx, settings, attribution, view))
    baseline = None if outcome.baseline is None else _q(outcome.baseline)
    ceiling = None if outcome.baseline is None else _q(settings.scale_max_ratio * outcome.baseline)
    extra: dict[str, Any] = {
        "outcome": "Scale" if scale else "Hold",
        "b1": scale,
        **_day_extra(history, outcome.gate),
    }
    if scale:
        extra["b1_link"] = {
            "rule_id": B1,
            "subject": {"type": outcome.subject[0], "id": outcome.subject[1]},
            "current_daily_budget": current,
            "proposed_daily_budget": proposed,
        }
    if outcome.shared:
        extra["members"] = _members_extra(ctx, outcome)
    else:
        extra.update(_campaign_group_extra(ctx, ctx.campaigns[outcome.subject[1]]))
    if outcome.veto is not None:
        extra["matured_ql"] = outcome.veto.ql
    if outcome.g1_blocked:
        extra["g1_blocked"] = True
    datasets = _required(_ad_datasets(outcome.entity.source), view.datasets)
    evidence = ctx.make_evidence(
        required_datasets=datasets,
        crm_scope=outcome.crm_scope,
        metrics=metrics,
        baselines=(
            Baseline(
                "baseline_cost_per_outcome",
                f"Account cost per {_singular(label)} (same source and objective)",
                baseline,
                "currency",
                currency,
            ),
            Baseline(
                "max_cost_per_outcome",
                "Highest cost per outcome allowed to scale",
                ceiling,
                "currency",
                currency,
            ),
        ),
        sample=Sample(history.known_active, history.config.min_known_days, history.known_label),
        threshold_keys=(
            C2_THRESHOLD_KEYS
            if outcome.veto is None
            else (*C2_THRESHOLD_KEYS, *VETO_THRESHOLD_KEYS)
        ),
        extra=extra,
        # Scale rests on the capped budget's utilization; Hold on the Qualified Leads the veto
        # found missing.
        headline="window_utilization" if scale else "ql",
    )
    return Candidate(
        rule_id=C2,
        section=CAMPAIGNS,
        group=GROUP_SCALE,
        entity_ref=outcome.entity,
        action=SCALE_ACTION if scale else HOLD_ACTION,
        reason=_c2_reason(evidence, label, scale, history.known_label),
        evidence=evidence,
        rank_key=(_q(outcome.cpo),),
        flags=outcome.flags,
        required_datasets=datasets,
        increases_spend_on=outcome.increases_spend_on if scale else (),
        decision_subject=outcome.decision_subject if scale else None,
        depends_on=_depends_on(ctx, outcome.members, (outcome.subject,)),
    )


def _c2_reason(evidence: Evidence, label: str, scale: bool, known_label: str) -> str:
    """C2's reason, filled from its evidence only.

    Rule C2 Scale or Hold (AAP 0.5.4.4), for the item :func:`_c2_candidate` builds from
    :func:`scale_outcomes`. The sentence states the capped days out of the known days, the
    outcomes and cost per outcome beside the account baseline when there is one, then either
    the B1 raise of the daily budget ("Scale") or the eligible contacts and Qualified Leads
    the veto found ("Hold").

    Threshold keys: those of the evidence it reads, :data:`C2_THRESHOLD_KEYS` plus
    :data:`VETO_THRESHOLD_KEYS` when a veto was read (always for a Hold).

    Rationale: every number is read back from the item's own evidence, never recomputed, so
    the sentence cannot drift from the metrics and baselines the card shows.
    """
    capped = (
        f"Capped on {_value(evidence, 'capped_days')} of {_value(evidence, 'known_days')} "
        f"{known_label} with {_count(_value(evidence, 'outcomes'))} {label} at "
        f"{_money_of(evidence, 'cost_per_outcome')} per {_singular(label)}"
    )
    baseline = evidence.baselines[0].value
    if baseline is not None:
        capped += f" (account {_money(baseline, evidence.metric('spend').currency or '')})"
    if scale:
        return (
            f"{capped}: raise the daily budget from {_money_of(evidence, 'daily_budget')} to "
            f"{_money_of(evidence, 'proposed_daily_budget')} (B1)."
        )
    return (
        f"{capped}, but {_value(evidence, 'eligible_contacts')} eligible contacts from matured "
        f"leads reached {_count(_value(evidence, 'ql'))} Qualified Leads: hold the budget."
    )


def _c2(
    ctx: AnalysisContext, settings: _Settings, attribution: AttributionTable, notes: _Notes
) -> RuleResult:
    """C2 Scale or Hold: one item per eligible subject of :func:`scale_outcomes`.

    AAP 0.5.4.4 C2. "Scale" links to its B1 item (``extra.b1_link``), which carries the
    amount; its decision affordance writes the subject (the shared budget itself when shared,
    with every member in ``extra.members``) and it declares the subject and the campaigns
    drawing from it now (:attr:`ScaleOutcome.receivers`) as G1 targets. "Hold" ("platform
    outcomes are not reaching Qualified Lead") is emitted whenever the veto fires, offers only
    "Mark done" and raises no spend.

    Threshold keys: ``scale.min_outcomes``, ``scale.max_cpa_ratio``, ``b1.step``,
    ``window.sustained_min_days``, the ``budget.*`` gate keys and the veto keys.

    Rationale: "Campaigns hitting their budget cap consistently with good output → scale
    candidates", judged on known-budget days only, and against real pipeline before scaling.
    """
    candidates: list[Candidate] = []
    withheld: list[Withheld] = []
    for entry in scale_outcomes(ctx):
        if isinstance(entry, ScaleWithheld):
            withheld.append(entry.to_withheld(C2, CAMPAIGNS))
        else:
            candidates.append(_c2_candidate(ctx, settings, attribution, entry, notes))
    return RuleResult(candidates=tuple(candidates), withheld=tuple(withheld))


# ---------------------------------------------------------------------------
# C1 Pause (AAP 0.5.4.4)
# ---------------------------------------------------------------------------


def _ceil(value: Decimal) -> int:
    """The least whole number at or above ``value``."""
    return int(value.to_integral_value(rounding=ROUND_CEILING, context=_EXACT))


def _window_withheld(rule_id: str, days: int, required: int) -> Withheld:
    """A rule withheld for the whole run because the window is too short (P6)."""
    return Withheld(
        rule_id=rule_id,
        section=CAMPAIGNS,
        entity=None,
        reason=f"window {_days(days)} of {required} required",
        observed=days,
        required=required,
        unit="days",
    )


def _c1(
    ctx: AnalysisContext, settings: _Settings, attribution: AttributionTable, notes: _Notes
) -> RuleResult:
    """C1 Pause: an active campaign spending steadily over a real window with no output.

    AAP 0.5.4.4 C1, for both sources. Fires when the window is at least
    ``c1.min_window_days`` (14) days (and the sustained minimum), the campaign spent on at
    least ``c1.min_spend_day_share`` (70%) of the window days, its outcomes are exactly 0,
    and its expected outcomes (spend ÷ the account's baseline cost per outcome for the same
    source and objective group) are at least ``c1.min_expected_outcomes`` (3).

    Rationale: at a Poisson mean of 3 expected outcomes, zero observed has a probability of
    about 5% (e^-3), so a campaign at that spend producing nothing is very unlikely to be
    merely unlucky. A spend-day share below 70% is not "spending steadily".

    Near patterns are withheld with observed against required: zero outcomes on steady spend
    with fewer expected outcomes than the minimum, or enough expected outcomes on too few
    spending days. A short window withholds C1 for the run.
    """
    campaigns = _active_campaigns(ctx)
    if not campaigns:
        return RuleResult()
    days = ctx.window.days
    required_days = max(settings.c1_min_window, settings.sustained_min)
    if days < required_days:
        return RuleResult(withheld=(_window_withheld(C1, days, required_days),))
    candidates: list[Candidate] = []
    withheld: list[Withheld] = []
    for campaign in campaigns:
        totals = window_totals(ctx, (campaign.id,))
        if totals.outcomes is None or totals.outcomes != 0 or totals.spend <= 0:
            continue
        baseline = baseline_cpo(ctx, campaign.account_id, campaign.objective_group)
        exact = expected_outcomes(totals.spend, baseline)
        if exact is None or baseline is None:
            continue
        # Compared at the six places the evidence records: the baseline is a rounded quotient,
        # so an exact 3.0 (1,400 spent at 466.67 per outcome) must not read 2.999999 and miss.
        expected = _q(exact)
        entity = _campaign_ref(campaign)
        label = OUTCOME_LABELS[campaign.objective_group]
        steady = Decimal(totals.spend_days) >= settings.c1_spend_share * Decimal(days)
        if expected < settings.c1_min_expected:
            if steady:
                withheld.append(
                    Withheld(
                        rule_id=C1,
                        section=CAMPAIGNS,
                        entity=entity,
                        reason=(
                            f"expected {label} {_one_place(expected)} of "
                            f"{_count(settings.c1_min_expected)} required"
                        ),
                        observed=expected,
                        required=settings.c1_min_expected,
                        unit=f"expected {label}",
                    )
                )
            continue
        if not steady:
            needed = _ceil(settings.c1_spend_share * Decimal(days))
            withheld.append(
                Withheld(
                    rule_id=C1,
                    section=CAMPAIGNS,
                    entity=entity,
                    reason=(
                        f"spend days {totals.spend_days} of {needed} required "
                        f"({_percent(settings.c1_spend_share)} of {days} window days)"
                    ),
                    observed=totals.spend_days,
                    required=needed,
                    unit="days",
                )
            )
            continue
        candidates.append(
            _c1_candidate(
                ctx,
                settings,
                attribution,
                notes,
                campaign,
                entity,
                totals.spend,
                totals.spend_days,
                expected,
                baseline,
            )
        )
    return RuleResult(candidates=tuple(candidates), withheld=tuple(withheld))


def _c1_candidate(
    ctx: AnalysisContext,
    settings: _Settings,
    attribution: AttributionTable,
    notes: _Notes,
    campaign: CampaignInfo,
    entity: EntityRef,
    spend: Decimal,
    spend_days: int,
    expected: Decimal,
    baseline: Decimal,
) -> Candidate:
    """The C1 "Pause" item of one campaign.

    Shows spend, days with spend, outcomes, expected outcomes and the baseline, plus the
    linked CRM QL. The card has no cost-per-outcome metric for the baseline to pair with, so
    the baseline carries its own ``currency`` unit and the account currency. A LinkedIn
    campaign's card also names its campaign group in ``extra.campaign_group``
    (:func:`_campaign_group_extra`); a Google card has none.

    Rule C1 Pause (AAP 0.5.4.4), for a campaign that passed :func:`_c1`'s gates: a window of
    at least ``c1.min_window_days`` (14) and the sustained minimum, spend on at least
    ``c1.min_spend_day_share`` (70%) of the window days, 0 outcomes, and ``expected`` at
    least ``c1.min_expected_outcomes`` (3).

    Threshold keys: :data:`C1_THRESHOLD_KEYS`, plus :data:`VETO_THRESHOLD_KEYS` when the
    campaign has a pipeline population whose QL is shown.

    Rationale: a 70% spend-day share is what "spending steadily" means, and at a Poisson mean
    of 3 expected outcomes the chance of observing none is about 5%, so the zero is unlikely
    to be bad luck.
    """
    currency = ctx.account_currency(campaign.id)
    label = OUTCOME_LABELS[campaign.objective_group]
    days = ctx.window.days
    view = _campaign_crm(ctx, attribution, campaign)
    notes.note_view(ctx, entity, view)
    metrics = [
        Metric("spend", "Window spend", _q(spend), "currency", currency),
        Metric("spend_days", "Days with spend", spend_days, "days"),
        Metric("window_days", "Window days", days, "days"),
        Metric(
            "spend_day_share",
            "Share of window days with spend",
            _share(Decimal(spend_days) / Decimal(days)),
            "ratio",
        ),
        # The zero outcomes read at six places, as C2's and C1b's ``outcomes`` do (:func:`_q`).
        Metric("outcomes", f"Window {label}", _q(_ZERO), label),
        Metric(
            "expected_outcomes",
            f"Expected {label} at the account baseline",
            expected,
            label,
        ),
    ]
    if view.veto is not None:
        metrics.append(
            Metric(
                "eligible_contacts",
                "Eligible contacts (matured leads)",
                view.veto.eligible_contacts,
                "contacts",
            )
        )
        metrics.append(Metric("ql", "Qualified Leads (CRM)", view.veto.ql, "contacts"))
    datasets = _required(_ad_datasets(campaign.source), view.datasets)
    evidence = ctx.make_evidence(
        required_datasets=datasets,
        crm_scope=view.scope,
        metrics=metrics,
        baselines=(
            Baseline(
                "baseline_cost_per_outcome",
                f"Account cost per {_singular(label)} (same source and objective)",
                _q(baseline),
                "currency",
                currency,
            ),
        ),
        sample=Sample(expected, settings.c1_min_expected, f"expected {label}"),
        threshold_keys=(
            C1_THRESHOLD_KEYS if view.veto is None else (*C1_THRESHOLD_KEYS, *VETO_THRESHOLD_KEYS)
        ),
        extra={"proposed_status": "paused", **_campaign_group_extra(ctx, campaign)},
        # The pause rests on the spend that produced no outcome.
        headline="spend",
    )
    reason = (
        f"Spent {_money_of(evidence, 'spend')} on {_value(evidence, 'spend_days')} of "
        f"{_value(evidence, 'window_days')} days with 0 {label}; at the account's "
        f"{_money(evidence.baselines[0].value, currency)} per {_singular(label)}, "
        f"{_one_place(expected)} {label} were expected."
    )
    return Candidate(
        rule_id=C1,
        section=CAMPAIGNS,
        group=GROUP_PAUSE,
        entity_ref=entity,
        action=PAUSE_ACTION,
        reason=reason,
        evidence=evidence,
        rank_key=(-spend,),
        flags=view.flags,
        required_datasets=datasets,
        increases_spend_on=(),
        decision_subject=DecisionSubject("campaign", campaign.id),
        depends_on=_depends_on(ctx, (campaign.id,)),
    )


# ---------------------------------------------------------------------------
# C1b Pipeline pause, LinkedIn (AAP 0.5.4.4)
# ---------------------------------------------------------------------------

_C1B_DATASETS: Final[tuple[str, ...]] = (
    "linkedin.leads",
    "linkedin.analytics_daily",
    "hubspot.contacts",
    "hubspot.associations",
    "linkedin.entities",
)
"""C1b's datasets; ``hubspot.*`` resolve to the effective CRM dataset (``crm_scope="linkedin"``).

``linkedin.entities`` is read too, for the campaign's status (it must be active).
"""


def _c1b_shortfall(
    settings: _Settings, veto: VetoPopulation
) -> tuple[str, Number | None, Number | None, str] | None:
    """Why C1b cannot judge a zero-QL population, or ``None`` when it can.

    In order: the pipeline is unavailable for a reason other than the evidence below (the
    Qualified Lead stage, no CRM dataset); an attribution floor; unknown CRM evidence; too
    few eligible contacts. A zero under a floor or unknown evidence may be an artefact of
    unmatched contacts or failed association reads, so C1b never fires on it.

    Returns:
        ``(reason, observed, required, unit)`` of the withheld line.
    """
    if veto.ql is None:
        return (
            f"pipeline unavailable: {veto.reason or 'Qualified Lead count unavailable'}",
            None,
            None,
            "contacts",
        )
    if veto.attribution_floor and veto.unresolved_share is not None:
        return (
            f"attribution floor: {_percent(veto.unresolved_share)} of matured leads unresolved "
            f"in HubSpot (floor {_percent(settings.floor_share)})",
            _share(veto.unresolved_share),
            settings.floor_share,
            "unresolved share of matured leads (must be below the floor)",
        )
    if veto.unknown_evidence:
        share = (
            Decimal(veto.unknown_evidence_contacts) / Decimal(veto.eligible_contacts)
            if veto.eligible_contacts
            else Decimal(1)
        )
        return (
            f"unknown CRM evidence for {veto.unknown_evidence_contacts} of "
            f"{veto.eligible_contacts} eligible contacts",
            _share(share),
            0,
            "share of eligible contacts with unknown CRM evidence (must be 0)",
        )
    if not veto.available:
        return (f"pipeline unavailable: {veto.reason}", None, None, "contacts")
    if veto.eligible_contacts < settings.veto_min:
        return (
            f"eligible contacts {veto.eligible_contacts} of {settings.veto_min} required",
            veto.eligible_contacts,
            settings.veto_min,
            "contacts",
        )
    if veto.meetings is None:
        return ("pipeline unavailable: meetings unavailable", None, None, "contacts")
    return None


def _c1b(
    ctx: AnalysisContext, settings: _Settings, attribution: AttributionTable, notes: _Notes
) -> RuleResult:
    """C1b Pipeline pause (LinkedIn): leads that never become Qualified Leads or meetings.

    AAP 0.5.4.4 C1b and 0.5.4.1. Fires on a window of at least ``c1b.min_window_days`` (28)
    when the campaign's matured population has at least ``crm.veto_min_leads`` (20) eligible
    contacts (distinct contacts behind matched leads submitted at least
    ``crm.lead_maturity_days`` before the window end), their Qualified Leads and meetings
    (outcomes in ``crm.meeting_outcome_exclusions`` not counted) are both 0, the pipeline is
    available, the unresolved share is below ``attribution.floor_share`` and no eligible
    contact's CRM evidence is unknown.

    Rationale: platform leads that never reach Qualified Lead are not pipeline. The gates
    exist because a zero under a floor or unknown evidence "may be an artefact of unmatched
    contacts or failed association reads"; such campaigns are withheld, and named in a
    ``pipeline_unavailable`` finding, rather than paused.
    """
    campaigns = _active_campaigns(ctx, "linkedin")
    if not campaigns:
        return RuleResult()
    days = ctx.window.days
    required_days = max(settings.c1b_min_window, settings.sustained_min)
    if days < required_days:
        return RuleResult(withheld=(_window_withheld(C1B, days, required_days),))
    candidates: list[Candidate] = []
    withheld: list[Withheld] = []
    for campaign in campaigns:
        veto = attribution.veto.get(campaign.id)
        if veto is None or veto.matured_leads == 0:
            continue
        if veto.ql not in (None, 0) or veto.meetings not in (None, 0):
            continue
        entity = _campaign_ref(campaign)
        view = _CrmView(veto, "linkedin", _veto_flags(veto))
        shortfall = _c1b_shortfall(settings, veto)
        if shortfall is not None:
            notes.note_view(ctx, entity, view)
            reason, observed, required, unit = shortfall
            withheld.append(Withheld(C1B, CAMPAIGNS, entity, reason, observed, required, unit))
            continue
        candidates.append(_c1b_candidate(ctx, settings, attribution, campaign, entity, view))
    return RuleResult(candidates=tuple(candidates), withheld=tuple(withheld))


def _c1b_candidate(
    ctx: AnalysisContext,
    settings: _Settings,
    attribution: AttributionTable,
    campaign: CampaignInfo,
    entity: EntityRef,
    view: _CrmView,
) -> Candidate:
    """The C1b item of one LinkedIn campaign whose eligible contacts produced no pipeline.

    The card shows window spend, window outcomes (``None``, unavailable, when a window day did
    not report them) and expected outcomes at the account baseline (spend ÷ the account's cost
    per outcome for the same source and objective group, AAP 0.5.4.1; ``None`` without a
    baseline), and records that baseline (AAP 0.5.5: "Pause and scale cards show spend,
    outcomes, expected outcomes and CRM QL"). The baseline reads "—" when the account's known
    outcomes are 0, a zero denominator, and is ``None``, unavailable, only when they are
    unreported (:func:`cost_per_outcome_value`). With no cost-per-outcome metric to pair with,
    the baseline carries its own ``currency`` unit and the account currency. The veto counts
    follow as their own metrics, never blended with the platform outcomes, and
    ``extra.campaign_group`` names the campaign's campaign group (:func:`_campaign_group_extra`).

    Rule C1b Pipeline pause (AAP 0.5.4.4), for a campaign that passed :func:`_c1b` and
    :func:`_c1b_shortfall`: a window of at least ``c1b.min_window_days`` (28) and the
    sustained minimum, at least ``crm.veto_min_leads`` (20) eligible contacts from the
    matured population, Qualified Leads and meetings both 0, the pipeline available, the
    unresolved share below ``attribution.floor_share`` and no eligible contact's CRM evidence
    unknown.

    Threshold keys: :data:`C1B_THRESHOLD_KEYS` (``c1b.min_window_days``,
    :data:`VETO_THRESHOLD_KEYS` and ``window.sustained_min_days``).

    Rationale: platform leads that never reach Qualified Lead or a meeting are not pipeline.
    Under an attribution floor or unknown CRM evidence the zero may be an artefact of
    unmatched contacts or failed association reads, so such a campaign is withheld instead
    and never reaches this item.
    """
    veto = view.veto
    if veto is None:
        raise ValueError(f"C1b needs a pipeline population for campaign {campaign.id}")
    currency = ctx.account_currency(campaign.id)
    totals = window_totals(ctx, (campaign.id,))
    spend = totals.spend
    label = OUTCOME_LABELS[campaign.objective_group]
    baseline = baseline_cpo(ctx, campaign.account_id, campaign.objective_group)
    expected = expected_outcomes(spend, baseline)
    metrics = [
        Metric("spend", "Window spend", _q(spend), "currency", currency),
        Metric("outcomes", f"Window {label}", _q_optional(totals.outcomes), label),
        Metric(
            "expected_outcomes",
            f"Expected {label} at the account baseline",
            None if expected is None else _q(expected),
            label,
        ),
        *_veto_metrics(ctx, settings, attribution, view),
    ]
    evidence = ctx.make_evidence(
        required_datasets=_C1B_DATASETS,
        crm_scope="linkedin",
        metrics=metrics,
        baselines=(
            Baseline(
                "baseline_cost_per_outcome",
                f"Account cost per {_singular(label)} (same source and objective)",
                cost_per_outcome_value(
                    baseline, subject_baseline_outcomes(ctx, ("campaign", campaign.id))
                ),
                "currency",
                currency,
            ),
        ),
        sample=Sample(veto.eligible_contacts, settings.veto_min, "contacts"),
        threshold_keys=C1B_THRESHOLD_KEYS,
        extra={
            "proposed_status": "paused",
            "matured_ql": veto.ql,
            **_campaign_group_extra(ctx, campaign),
        },
        # The pipeline pause rests on the zero Qualified Leads.
        headline="ql",
    )
    reason = (
        f"{_value(evidence, 'eligible_contacts')} eligible contacts from "
        f"{_value(evidence, 'matured_leads')} matured leads reached "
        f"{_count(_value(evidence, 'ql'))} Qualified Leads and "
        f"{_count(_value(evidence, 'meetings'))} meetings "
        f"(MQL {_count(_value(evidence, 'mql'))}) after "
        f"{_money_of(evidence, 'spend')} of spend in the window."
    )
    return Candidate(
        rule_id=C1B,
        section=CAMPAIGNS,
        group=GROUP_PAUSE,
        entity_ref=entity,
        action=PIPELINE_PAUSE_ACTION,
        reason=reason,
        evidence=evidence,
        rank_key=(-spend,),
        flags=view.flags,
        required_datasets=_C1B_DATASETS,
        increases_spend_on=(),
        decision_subject=DecisionSubject("campaign", campaign.id),
        depends_on=_depends_on(ctx, (campaign.id,)),
    )


# ---------------------------------------------------------------------------
# C3a Launch: underused proven segment, LinkedIn (AAP 0.5.4.4, 0.3.2)
# ---------------------------------------------------------------------------

_C3A_DATASETS: Final[tuple[str, ...]] = (
    "linkedin.segments",
    "linkedin.analytics_daily",
    "linkedin.entities",
)
"""C3a's datasets: the preset-window segment aggregates and the campaign's daily facts.

``linkedin.entities`` is read too, for the campaign's status (it must be active), objective
group, name and external id.
"""


def _segment_window_gap(ctx: AnalysisContext) -> tuple[str, str] | None:
    """Why no segment data matches the run window, or ``None`` when it does (AAP 0.3.2).

    Returns:
        ``(case, label)``: ``not_yet_available`` for a preset ending on the default end that
        has not been fetched for that end, or ``preset_windows_only`` for any other window.
        The ``not_yet_available`` label's "latest available ends {date}" is the latest end
        among the fetched windows of the same preset length, or ``none`` when that preset has
        never been fetched; other presets' windows never stand in for it. Another window's
        segments are never reused.
    """
    window = ctx.window
    if window.is_segment_window():
        if (window.start, window.end) in ctx.segment_windows:
            return None
        ends = [end for start, end in ctx.segment_windows if (end - start).days + 1 == window.days]
        latest = max(ends).isoformat() if ends else "none"
        return (
            "not_yet_available",
            f"Segment data for the {window.days}-day window ending {window.end.isoformat()} "
            f"not yet available; latest available ends {latest}",
        )
    presets = ", ".join(str(days) for days in window.presets)
    return (
        "preset_windows_only",
        f"Segment data exists only for the preset windows {presets} ending "
        f"{window.default_end.isoformat()}",
    )


def _c3a(ctx: AnalysisContext, settings: _Settings) -> RuleResult:
    """C3a Launch: an underused but proven demographic segment inside a LinkedIn campaign.

    AAP 0.5.4.4 C3a. For a lead-gen campaign's segment (job function, seniority or industry)
    it fires when segment leads ≥ ``c3a.min_leads`` (5), segment cost per lead ≤
    ``c3a.max_cpl_ratio`` (0.8) x the campaign's cost per lead (window spend ÷ leads), and the
    segment's share of the campaign's window impressions is below
    ``c3a.max_impression_share`` (15%). A pivot without a lead metric never fires.

    LinkedIn approximates and privacy-thresholds segment figures, so they exist only as
    aggregates of the preset windows (``window.presets``) ending on the default window end.
    C3a runs only when the run window is exactly such a fetched window; otherwise it records
    one ``segment_window_unavailable`` finding and a withheld line with the label for its
    case, and never reuses another window's segments.

    Rationale: "an underused but proven-good segment" is the data gap the user named; the
    cost and share minimums keep it to segments that convert clearly better and receive
    little delivery. A segment meeting both but below the lead minimum is withheld.

    A campaign with zero window spend but leads has a zero cost per lead, so it gives no cost
    baseline: the segment-to-campaign ratio is undefined and never fabricated. A zero-cost
    segment that meets the share and lead minimums is then withheld as "no exposure" (0 of 1
    campaign days with spend); a segment with any cost is dearer than the campaign and does
    not fire.
    """
    campaigns = {campaign.id: campaign for campaign in _active_campaigns(ctx, "linkedin")}
    if not campaigns:
        return RuleResult()
    gap = _segment_window_gap(ctx)
    if gap is not None:
        case, label = gap
        window = ctx.window
        finding = Finding(
            FINDING_SEGMENT_WINDOW_UNAVAILABLE,
            CAMPAIGNS,
            None,
            label,
            {
                "case": case,
                "window": {"start": window.start, "end": window.end, "days": window.days},
                "default_end": window.default_end,
                "presets": window.presets,
                "fetched_windows": [
                    {"start": start, "end": end} for start, end in sorted(ctx.segment_windows)
                ],
            },
        )
        withheld = Withheld(C3A, CAMPAIGNS, None, label, None, None, "segment windows")
        return RuleResult(findings=(finding,), withheld=(withheld,))
    candidates: list[Candidate] = []
    withheld_lines: list[Withheld] = []
    totals_cache: dict[int, tuple[Decimal, Decimal | None, int]] = {}
    for row in sorted(ctx.segments, key=lambda item: (item.campaign_id, item.facet, item.value)):
        campaign = campaigns.get(row.campaign_id)
        if campaign is None or campaign.objective_group != OBJ_LINKEDIN_LEAD_GEN:
            continue
        if (row.window_start, row.window_end) != (ctx.window.start, ctx.window.end):
            continue
        if row.leads is None or row.cost is None or row.impressions is None or row.leads <= 0:
            continue
        if campaign.id not in totals_cache:
            totals = window_totals(ctx, (campaign.id,))
            totals_cache[campaign.id] = (totals.spend, totals.outcomes, totals.impressions)
        spend, leads, impressions = totals_cache[campaign.id]
        campaign_cpl = cost_per_outcome(spend, leads)
        if campaign_cpl is None or impressions <= 0:
            continue
        segment_cpl = row.cost / Decimal(row.leads)
        share = Decimal(row.impressions) / Decimal(impressions)
        if segment_cpl > settings.c3a_max_cpl_ratio * campaign_cpl:
            continue
        if share >= settings.c3a_max_share:
            continue
        entity = _segment_ref(campaign, row)
        if row.leads < settings.c3a_min_leads:
            withheld_lines.append(
                Withheld(
                    C3A,
                    CAMPAIGNS,
                    entity,
                    f"segment leads {row.leads} of {settings.c3a_min_leads} required",
                    row.leads,
                    settings.c3a_min_leads,
                    "leads",
                )
            )
            continue
        if campaign_cpl <= 0:
            withheld_lines.append(
                Withheld(
                    C3A,
                    CAMPAIGNS,
                    entity,
                    "no exposure: zero campaign spend in the window, so segment cost per lead "
                    "cannot be compared",
                    0,
                    1,
                    "campaign days with spend",
                )
            )
            continue
        candidates.append(
            _c3a_candidate(ctx, campaign, row, entity, spend, leads, impressions, campaign_cpl)
        )
    return RuleResult(candidates=tuple(candidates), withheld=tuple(withheld_lines))


def _segment_ref(campaign: CampaignInfo, row: SegmentRow) -> EntityRef:
    """The entity reference of one demographic segment of a campaign.

    The key stays the stored value (LinkedIn's URN), so feedback keys never change; the name
    shows LinkedIn's standardized name for it (:func:`segment_value_label`).
    """
    facet = _FACET_LABELS.get(row.facet, row.facet)
    return EntityRef(
        "linkedin",
        ENTITY_SEGMENT,
        None,
        segment_external_id(campaign.external_id, row.facet, row.value),
        f"{campaign.name}: {facet} {segment_value_label(row.facet, row.value)}",
    )


def _c3a_candidate(
    ctx: AnalysisContext,
    campaign: CampaignInfo,
    row: SegmentRow,
    entity: EntityRef,
    spend: Decimal,
    leads: Decimal | None,
    impressions: int,
    campaign_cpl: Decimal,
) -> Candidate:
    """The C3a launch idea of one segment.

    The card shows the gap evidence (segment against campaign leads, cost per lead and
    impressions) and names the source campaign's campaign group in ``extra.campaign_group``
    (:func:`_campaign_group_extra`).

    ``campaign_cpl`` must be positive: it divides the segment's cost per lead, so
    :func:`_c3a` withholds a zero-spend campaign's segments before calling this.

    The action and reason name the segment by LinkedIn's standardized name for its stored URN
    (:func:`segment_value_label`; the URN itself when LinkedIn names none). ``extra.value``
    keeps the stored URN, the segment's identity, and ``extra.value_label`` carries the name.
    """
    settings = _Settings.of(ctx)
    currency = ctx.account_currency(campaign.id)
    facet = _FACET_LABELS.get(row.facet, row.facet)
    value_label = segment_value_label(row.facet, row.value)
    segment_leads = row.leads or 0
    segment_cost = row.cost if row.cost is not None else _ZERO
    segment_impressions = row.impressions or 0
    segment_cpl = segment_cost / Decimal(segment_leads)
    metrics = [
        Metric("segment_leads", "Segment leads", segment_leads, "leads"),
        Metric("segment_spend", "Segment spend", _q(segment_cost), "currency", currency),
        Metric("segment_cpl", "Segment cost per lead", _q(segment_cpl), "currency", currency),
        Metric("campaign_cpl", "Campaign cost per lead", _q(campaign_cpl), "currency", currency),
        Metric(
            "cpl_ratio",
            "Segment cost per lead vs campaign",
            _share(segment_cpl / campaign_cpl),
            "ratio",
        ),
        Metric("segment_impressions", "Segment impressions", segment_impressions, "impressions"),
        Metric("campaign_impressions", "Campaign impressions", impressions, "impressions"),
        Metric(
            "impression_share",
            "Segment share of campaign impressions",
            _share(Decimal(segment_impressions) / Decimal(impressions)),
            "ratio",
        ),
        Metric("campaign_spend", "Campaign window spend", _q(spend), "currency", currency),
        Metric("campaign_leads", "Campaign leads", _q_optional(leads), "leads"),
    ]
    evidence = ctx.make_evidence(
        required_datasets=_C3A_DATASETS,
        crm_scope=None,
        metrics=metrics,
        baselines=(
            Baseline("campaign_cpl", "Campaign cost per lead", _q(campaign_cpl)),
            Baseline(
                "max_segment_cpl",
                "Highest segment cost per lead (c3a.max_cpl_ratio)",
                _q(settings.c3a_max_cpl_ratio * campaign_cpl),
            ),
        ),
        sample=Sample(segment_leads, settings.c3a_min_leads, "leads"),
        threshold_keys=C3A_THRESHOLD_KEYS,
        extra={
            "facet": row.facet,
            "facet_label": facet,
            "value": row.value,
            "value_label": value_label,
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "segment_window": {"start": row.window_start, "end": row.window_end},
            **_campaign_group_extra(ctx, campaign),
        },
        # The segment is proven by its cost per lead against the campaign's.
        headline="segment_cpl",
    )
    share = _value(evidence, "impression_share")
    reason = (
        f"{facet.capitalize()} {value_label} produced {_value(evidence, 'segment_leads')} leads "
        f"at {_money_of(evidence, 'segment_cpl')} per lead, "
        f"{_count(_value(evidence, 'cpl_ratio'))}x the campaign's "
        f"{_money_of(evidence, 'campaign_cpl')}, from "
        f"{_percent(share) if isinstance(share, Decimal) else share} of its impressions."
    )
    return Candidate(
        rule_id=C3A,
        section=CAMPAIGNS,
        group=GROUP_LAUNCH,
        entity_ref=entity,
        action=f"Launch a campaign targeting {facet}: {value_label} with {campaign.name}'s offer",
        reason=reason,
        evidence=evidence,
        rank_key=(-segment_leads,),
        flags=(),
        required_datasets=_C3A_DATASETS,
        increases_spend_on=(),
        decision_subject=None,
        depends_on=_depends_on(ctx, (campaign.id,)),
    )


# ---------------------------------------------------------------------------
# C3b Launch: keyword cluster without a campaign, Google (AAP 0.5.4.4)
# ---------------------------------------------------------------------------

_C3B_DATASETS: Final[tuple[str, ...]] = ("google.search_terms", "google.entities")
"""C3b's datasets: the window search terms and the keyword, ad group and campaign names."""

SECTION_DATASETS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(
        (
            *_ad_datasets("google"),
            *_ad_datasets("linkedin"),
            *_CRM_DATASETS["linkedin"],
            *_C1B_DATASETS,
            *_C3A_DATASETS,
            *_C3B_DATASETS,
        )
    )
)
"""Every dataset the Campaigns section reads, first-seen order.

C0 (``discrepancy.py``) reads each ad source's entities; C1 and C2 read each ad source's
entities and daily metrics plus the LinkedIn pipeline population; C1b, C3a and C3b read their
own datasets. ``hubspot.google_contacts`` is left out: it is read only once the Google-to-CRM
link is verified, so it never gates the section. Precedence judges these over the run's window
when the section stores no item, so an empty section says which of its datasets do not cover
the window (AAP 0.5.5 "Each section shows an EmptyState with the reason").
"""


@dataclass(slots=True)
class _TermTotals:
    """One search term's window totals across the ad groups and keywords it matched."""

    spend: Decimal = _ZERO
    conversions: Decimal = _ZERO
    clicks: int = 0
    campaign_ids: set[int] = field(default_factory=set)


def _term_key(term: str) -> str:
    """A search term with case and whitespace normalised, so one query aggregates once."""
    return " ".join(term.lower().split())


def _coverage_texts(ctx: AnalysisContext) -> list[str]:
    """Every Google keyword text, ad group name and campaign name a cluster is checked against."""
    texts = [keyword.text for keyword in ctx.keywords.values()]
    texts.extend(ad_group.name for ad_group in ctx.ad_groups.values())
    texts.extend(
        campaign.name for campaign in ctx.campaigns.values() if campaign.source == "google"
    )
    return texts


def _coverage_tokens(ctx: AnalysisContext) -> frozenset[str]:
    """The union of the tokens of every coverage text, each text tokenised exactly once.

    A cluster is covered when any of its head tokens is in this union, which is the
    any-token-in-any-text test of :func:`~ads_engine.analysis.text.contains_tokens`: head
    tokens come from the same :func:`~ads_engine.analysis.text.tokenize`, so they are already
    caseless, stopword-free whole tokens. Building the union once keeps the check linear in
    the texts instead of re-tokenising every text for every cluster.
    """
    return frozenset(token for text in _coverage_texts(ctx) for token in tokenize(text))


def _google_currency(ctx: AnalysisContext, campaign_ids: Sequence[int]) -> str:
    """The currency of a cluster's spend.

    It is the first loaded campaign's account currency, else the Google account's; empty when
    neither is loaded, and amounts then render without a code.
    """
    for cid in campaign_ids:
        if cid in ctx.campaigns:
            return ctx.account_currency(cid)
    for account in sorted(ctx.accounts.values(), key=lambda item: item.id):
        if account.source == "google":
            return account.currency
    return ""


def _c3b(ctx: AnalysisContext, settings: _Settings, notes: _Notes) -> RuleResult:
    """C3b Launch: a converting search-term cluster no keyword, ad group or campaign covers.

    AAP 0.5.4.4 C3b. The window's converting search terms (conversions > 0; any fractional
    amount counts) are grouped by single linkage at token Jaccard ≥ ``c3b.jaccard`` (0.5),
    lowercased with stopwords removed. A cluster's head tokens are those present in at least
    ``c3b.head_token_share`` (50%) of its members. It fires for a cluster of at least
    ``c3b.min_terms`` (3) terms with at least ``c3b.min_conversions`` (5) conversions in total
    whose head tokens appear in no keyword text, ad group name or campaign name. Those texts
    are tokenised once per evaluation into one token union (:func:`_coverage_tokens`), and
    each cluster's head tokens are tested against that union.

    A C3b item is a Google item, so without the verified Google-to-CRM link it carries
    ``no_crm_link`` (AAP 0.3.3, 0.5.4.1) and ``notes`` counts it in the section's
    ``no_crm_link`` finding.

    Rationale: "a keyword cluster with no matching campaign" is the data gap the user named.
    Several converting queries sharing words the account never targets is demand the account
    structure does not yet address. A smaller uncovered cluster is withheld with its size.
    """
    by_term: dict[str, _TermTotals] = {}
    for row in ctx.search_terms:
        campaign = ctx.campaigns.get(row.campaign_id)
        if campaign is not None and campaign.source != "google":
            continue
        totals = by_term.setdefault(_term_key(row.search_term), _TermTotals())
        totals.spend += row.cost
        totals.conversions += row.conversions
        totals.clicks += row.clicks
        totals.campaign_ids.add(row.campaign_id)
    converting = sorted(term for term, totals in by_term.items() if totals.conversions > 0)
    if not converting:
        return RuleResult()
    clusters = cluster_terms(
        converting, threshold=settings.c3b_jaccard, min_head_share=settings.c3b_head_share
    )
    covered = _coverage_tokens(ctx)
    gap = google_link_gap(ctx)
    # C3b reads no pipeline evidence; the view carries only the Google link gap.
    crm = _CrmView(None, None, () if gap is None else (no_crm_link_flag(gap),))
    candidates: list[Candidate] = []
    withheld: list[Withheld] = []
    for cluster in clusters:
        if cluster.size < 2 or not cluster.head_tokens:
            continue
        if not covered.isdisjoint(cluster.head_tokens):
            continue
        conversions = sum((by_term[term].conversions for term in cluster.members), _ZERO)
        entity = EntityRef(
            "google",
            ENTITY_TERM_CLUSTER,
            None,
            term_cluster_external_id(cluster.sorted_head),
            f"Search-term cluster '{cluster.label}'",
        )
        if cluster.size < settings.c3b_min_terms:
            withheld.append(
                Withheld(
                    C3B,
                    CAMPAIGNS,
                    entity,
                    f"converting search terms {cluster.size} of {settings.c3b_min_terms} required",
                    cluster.size,
                    settings.c3b_min_terms,
                    "converting search terms",
                )
            )
            continue
        if conversions < settings.c3b_min_conversions:
            withheld.append(
                Withheld(
                    C3B,
                    CAMPAIGNS,
                    entity,
                    f"conversions {_count(conversions)} of "
                    f"{_count(settings.c3b_min_conversions)} required",
                    _q_floor(conversions),
                    settings.c3b_min_conversions,
                    "conversions",
                )
            )
            continue
        notes.note_view(ctx, entity, crm)
        candidates.append(
            _c3b_candidate(ctx, settings, cluster, entity, by_term, conversions, crm.flags)
        )
    return RuleResult(candidates=tuple(candidates), withheld=tuple(withheld))


def _c3b_candidate(
    ctx: AnalysisContext,
    settings: _Settings,
    cluster: TermCluster,
    entity: EntityRef,
    by_term: Mapping[str, _TermTotals],
    conversions: Decimal,
    flags: tuple[Flag, ...],
) -> Candidate:
    """The C3b launch idea of one uncovered converting cluster, carrying ``flags``.

    Rule C3b Launch: keyword cluster without a campaign (AAP 0.5.4.4), for a cluster that
    passed :func:`_c3b`: single linkage at token Jaccard ≥ ``c3b.jaccard`` (0.5), at least
    ``c3b.min_terms`` (3) converting search terms with at least ``c3b.min_conversions`` (5)
    conversions in total, and head tokens (in at least ``c3b.head_token_share`` (50%) of the
    members) that appear in no keyword text, ad group name or campaign name.

    Threshold keys: :data:`C3B_THRESHOLD_KEYS`.

    Rationale: converting queries sharing words that no keyword, ad group or campaign
    targets are proven demand the account structure does not cover, the specific data gap a
    launch idea needs.
    """
    members = sorted(
        cluster.members, key=lambda term: (-by_term[term].conversions, -by_term[term].clicks, term)
    )
    campaign_ids = sorted({cid for term in members for cid in by_term[term].campaign_ids})
    currency = _google_currency(ctx, campaign_ids)
    spend = sum((by_term[term].spend for term in members), _ZERO)
    clicks = sum(by_term[term].clicks for term in members)
    cpc = cost_per_outcome(spend, conversions)
    money_currency = currency or None
    metrics = [
        Metric("members", "Converting search terms", cluster.size, "search terms"),
        Metric("conversions", "Cluster conversions", _q(conversions), "conversions"),
        Metric("spend", "Cluster spend", _q(spend), "currency", money_currency),
        Metric("clicks", "Cluster clicks", clicks, "clicks"),
        Metric(
            "cost_per_conversion",
            "Cluster cost per conversion",
            None if cpc is None else _q(cpc),
            "currency",
            money_currency,
        ),
    ]
    evidence = ctx.make_evidence(
        required_datasets=_C3B_DATASETS,
        crm_scope=None,
        metrics=metrics,
        sample=Sample(_q_floor(conversions), settings.c3b_min_conversions, "conversions"),
        threshold_keys=C3B_THRESHOLD_KEYS,
        extra={
            "head_tokens": cluster.sorted_head,
            "members": [
                {
                    "term": term,
                    "spend": _q(by_term[term].spend),
                    "conversions": _q(by_term[term].conversions),
                    "clicks": by_term[term].clicks,
                }
                for term in members
            ],
            "campaign_ids": campaign_ids,
            "source_label": "from your search-term report",
        },
        # The uncovered cluster is proven by its conversions.
        headline="conversions",
    )
    reason = (
        f"{_value(evidence, 'members')} converting search terms cluster around the words "
        f"'{cluster.label}' with {_count(_value(evidence, 'conversions'))} conversions at "
        f"{_money_of(evidence, 'cost_per_conversion')} per conversion; no keyword, ad group "
        "or campaign name contains those words."
    )
    return Candidate(
        rule_id=C3B,
        section=CAMPAIGNS,
        group=GROUP_LAUNCH,
        entity_ref=entity,
        action=f"Create ad group or campaign for '{cluster.label}'",
        reason=reason,
        evidence=evidence,
        rank_key=(-conversions,),
        flags=flags,
        required_datasets=_C3B_DATASETS,
        increases_spend_on=(),
        decision_subject=None,
        depends_on=_depends_on(ctx, campaign_ids),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def evaluate(ctx: AnalysisContext) -> RuleResult:
    """Evaluate the "Campaigns to pause or launch" rules C1, C1b, C2, C3a and C3b.

    AAP 0.5.4.4. Items come in rule order and are grouped ``pause`` (C1, C1b), ``scale``
    (C2) and ``launch`` (C3a, C3b); ``precedence.py`` ranks them within each group, pins C0
    (from ``discrepancy.py``) first, and applies G1 to the Scale items' declared targets. No
    other rule produces launch ideas.

    Findings: ``segment_window_unavailable`` (C3a), one ``pipeline_unavailable`` per campaign
    whose pipeline could not be judged (QL stage unresolved, unknown CRM evidence or an
    attribution floor) and ``no_crm_link`` once any Google item (C1, C2 or C3b) lacks the
    verified CRM link; here its ``items`` counts the C1, C2 and C3b items built with the flag.
    ``precedence.py`` recounts ``items`` over the section's stored items that carry the flag,
    C0 items from ``discrepancy.py`` and held items included, after G1, P2 and feedback
    suppression.

    Threshold keys: :data:`C1_THRESHOLD_KEYS`, :data:`C1B_THRESHOLD_KEYS`,
    :data:`C2_THRESHOLD_KEYS`, :data:`VETO_THRESHOLD_KEYS`, :data:`C3A_THRESHOLD_KEYS` and
    :data:`C3B_THRESHOLD_KEYS`, each copied into the evidence of the items that applied it.

    Rationale: each rule answers one of the user's directives for this section; every item
    states its numbers and window, and every pattern below its minimum is withheld with
    observed against required rather than shown as an action.

    Raises:
        ConfigError: When a threshold key is missing or mistyped.
    """
    settings = _Settings.of(ctx)
    attribution = attribution_for(ctx)
    notes = _Notes()
    result = RuleResult().merge(
        _c1(ctx, settings, attribution, notes),
        _c1b(ctx, settings, attribution, notes),
        _c2(ctx, settings, attribution, notes),
        _c3a(ctx, settings),
        _c3b(ctx, settings, notes),
    )
    findings = notes.findings()
    if not findings:
        return result
    return result.merge(RuleResult(findings=tuple(findings)))
