"""Budget changes: rules B1, B2, B3 and B4.

AAP 0.5.4.5 (card fields, shared budgets, the B1-B4 table), 0.5.4.1 (budget history,
utilization, daily predicates, history gates, short history) and 0.5.4.2 (G1; the B3 headroom
allocation) for FR-6, answering the user's directives: "Recommendations based on a sustained
multi-day pattern, not a single day — the tool should show the window it used, not just the
number"; "Utilization (spend vs. cap) shown alongside efficiency (cost per outcome) — never
recommend increasing spend on high-utilization + poor-efficiency campaigns"; "Weekend/weekday
or other time-based patterns surfaced only when there's enough sample size to trust them, with
the sample size shown".

Rules (all in the single ``changes`` group, ranked by window spend descending):

* **B1 Increase**: one item per C2 "Scale" outcome of
  :func:`~ads_engine.analysis.campaigns.scale_outcomes`, the single evaluation C2 and B1 share,
  so they can never disagree. No B1 exists under a veto "Hold". The amount is the latest
  effective daily budget x (1 + ``b1.step``), rounded to the currency's minor unit.
* **B2 Do not increase**: the high-utilization gate passes and efficiency is poor
  (:func:`~ads_engine.analysis.metrics.assess_b2`). G1 in ``precedence.py`` blocks every
  increase on such a target through
  :func:`~ads_engine.analysis.metrics.spend_increase_blocked`; a C1 pause can still appear.
* **B3 Under-utilized**: the under-utilized gate passes; the proposal is the
  ``b3.spend_percentile`` of daily spend over known-budget (or budget-active) days x
  ``b3.headroom``. ``precedence.py`` allocates the freed amount to surviving B1 receivers and
  renders each allocation with :func:`render_b3_allocation` and each receiver's funding with
  :func:`render_b1_funding`.
* **B4 Weekday / weekend pattern**: a two-sample Poisson rate test of outcomes per unit of
  spend, run only on unweighted event counts and only above the sample minimums.

Every budget card shows, side by side (AAP 0.5.4.5): utilization (the window percentage plus
every day count of 0.5.4.1 observed against required), efficiency (cost per outcome against
the account baseline), outcomes and CRM Qualified Leads where linked, the daily budget and the
proposed amount, and the exact window with each source's day boundary
(:func:`budget_evidence`). Patterns below their minimums are withheld with observed against
required and never become items (P6).

Purity: reads only the :class:`~ads_engine.analysis.context.AnalysisContext`; no database,
network, settings or logging access. Never imports ``precedence`` or ``runner``, which import
this module's renderers.

Example:
    >>> result = evaluate(ctx)  # doctest: +SKIP
    >>> [(item.rule_id, item.action) for item in result.candidates]  # doctest: +SKIP
    [('B1', 'Raise daily budget from USD 83.33 to USD 100.00'),
     ('B3', 'Lower daily budget to USD 88.00')]
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal
from typing import Any, Final

# The CRM Qualified Lead count every budget card shows "where linked" (AAP 0.5.4.5) is the
# veto population that attribution builds; reading it from the one memoised attribution table
# keeps B1-B4 numbers identical to the Campaigns section's. This import goes beyond the file
# plan's dependency list because the AAP's card requirement outranks that list; attribution
# imports only context and thresholds, so no import cycle arises.
from ads_engine.analysis.attribution import VetoPopulation, attribution_for
from ads_engine.analysis.campaigns import (
    VETO_THRESHOLD_KEYS,
    ScaleOutcome,
    ScaleWithheld,
    scale_outcomes,
)
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
    OUTCOME_LABELS,
    AnalysisContext,
    Baseline,
    CampaignInfo,
    Candidate,
    CrmScope,
    DayFact,
    DecisionSubject,
    EntityRef,
    Evidence,
    Finding,
    Flag,
    Metric,
    Number,
    RuleResult,
    Sample,
    Source,
    SubjectKey,
    TestResult,
    Withheld,
)
from ads_engine.analysis.metrics import (
    B2_THRESHOLD_KEYS,
    BUDGET_THRESHOLD_KEYS,
    BudgetHistory,
    GateResult,
    assess_b2,
    baseline_cpo,
    budget_history,
    budget_subjects,
    cost_per_outcome,
    counting_contract,
    subject_baseline,
    subject_campaigns,
    subject_objective_groups,
    subject_totals,
    under_utilized,
    window_totals,
)
from ads_engine.analysis.stats import RateTest, percentile, poisson_rate_test
from ads_engine.analysis.thresholds import THRESHOLDS_FILE, ConfigError, Thresholds
from ads_engine.decisions import minor_unit_exponent, round_to_minor_unit

__all__ = [
    "B1_THRESHOLD_KEYS",
    "B2_RULE_THRESHOLD_KEYS",
    "B3_THRESHOLD_KEYS",
    "B4_TEST_NAME",
    "B4_THRESHOLD_KEYS",
    "CONTRACT_UNKNOWN_REASON",
    "FRACTIONAL_REASON",
    "NO_EXPOSURE_REASON",
    "WEIGHTED_REASON",
    "BudgetCrm",
    "budget_crm",
    "budget_evidence",
    "evaluate",
    "render_b1_funding",
    "render_b3_allocation",
]

# ---------------------------------------------------------------------------
# Threshold keys, fixed texts and cache keys
# ---------------------------------------------------------------------------

B1_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "b1.step",
    "scale.min_outcomes",
    "scale.max_cpa_ratio",
    "window.sustained_min_days",
    *BUDGET_THRESHOLD_KEYS,
)
"""Every key B1 applies: its step, the C2 eligibility it shares, and the ``budget.*`` gates."""

B2_RULE_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    *B2_THRESHOLD_KEYS,
    "b1.step",
    "window.sustained_min_days",
    *BUDGET_THRESHOLD_KEYS,
)
"""Every key B2 applies.

``b2.poor_efficiency_ratio`` and ``b2.min_expected_outcomes`` are the predicate's own keys
(:data:`~ads_engine.analysis.metrics.B2_THRESHOLD_KEYS`); ``b1.step`` sizes the "lower by 20%"
proposal, so the B2 decrease and the B1 increase are the same configured step.
"""

B3_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "b3.spend_percentile",
    "b3.headroom",
    "window.sustained_min_days",
    *BUDGET_THRESHOLD_KEYS,
)
"""Every key B3 applies, including the ``budget.under_utilization`` predicate."""

B4_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "b4.min_weekend_days",
    "b4.min_weekdays",
    "b4.min_outcomes",
    "b4.alpha",
    "window.sustained_min_days",
)
"""Every key B4 applies."""

B4_TEST_NAME: Final = "two-sample Poisson rate test"
"""``evidence.test.name`` of a B4 item."""

WEIGHTED_REASON: Final = "weighted attribution: rate test not valid"
"""B4 withheld: an included Google conversion action was data-driven or external on a day."""

CONTRACT_UNKNOWN_REASON: Final = "conversion counting contract unknown"
"""B4 withheld: some day's conversion-action settings are not whole-day known."""

FRACTIONAL_REASON: Final = "fractional conversions"
"""B4 withheld: the last-click contract holds but a daily value is fractional (modelled)."""

NO_EXPOSURE_REASON: Final = "no exposure"
"""B4 withheld: zero spend on the weekend or weekday side, so no rate exists to compare."""

_SETTINGS_CACHE_KEY: Final = "budget.settings"
"""``ctx.cache`` key of the run's :class:`_Settings`."""

_EXACT: Final = Context(prec=60)
"""Arithmetic context for quantizing evidence values without overflow or rounding surprises."""

_SIX_PLACES: Final = Decimal("0.000001")
"""Costs and rates in evidence keep six decimal places, like ``analysis/stats.py`` outputs."""

_FOUR_PLACES: Final = Decimal("0.0001")
"""Shares and utilization keep four places: a hundredth of a percent."""

_ONE_PLACE: Final = Decimal("0.1")
"""Percentages render with at most one decimal place in reason sentences."""

_ZERO: Final = Decimal(0)

_HUNDRED: Final = Decimal(100)

_WEEKEND: Final[frozenset[int]] = frozenset({5, 6})
"""``date.weekday()`` of Saturday and Sunday, read on each source's native dates."""

_METRICS_DATASETS: Final[Mapping[str, str]] = {
    "google": "google.metrics_daily",
    "linkedin": "linkedin.analytics_daily",
}
"""The daily metrics dataset each ad source's spend and outcomes come from."""

_CRM_DATASETS: Final[Mapping[CrmScope, tuple[str, ...]]] = {
    "linkedin": ("linkedin.leads", "hubspot.contacts", "hubspot.associations"),
    "google": ("hubspot.google_contacts", "hubspot.contacts", "hubspot.associations"),
}
"""The datasets a CRM Qualified Lead count reads, per CRM scope (the effective CRM dataset)."""

_HISTORY_MINIMUMS: Final[frozenset[str]] = frozenset({"known_budget_days", "spending_days"})
"""Gate checks that measure history length: their shortfall is a "short history" (P6)."""

_CHECK_UNITS: Final[Mapping[str, str]] = {
    "spending_days": "spending days",
    "spending_share": "spending days",
    "capped_days": "capped days",
    "capped_share": "capped days",
    "high_utilization_days": "high-utilization days",
    "under_utilized_days": "under-utilized days",
}
"""Units of the gate checks; ``known_budget_days`` takes the history's own label."""

_PREDICATES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("capped_days", "capped", "capped_days", "Capped days"),
    ("high_utilization_days", "high", "high_utilization_days", "High-utilization days"),
    ("under_utilized_days", "under", "under_utilized_days", "Under-utilized days"),
)
"""``(gate check, BudgetHistory count, metric key, label)`` of each rule's daily predicate.

The share of known-budget days meeting the predicate is recorded as ``{metric key}_share``.

A gate's checks name its predicate: the cap-constrained gate (C2/B1) counts capped days, the
high-utilization gate (B2/G1) high-utilization days, the under-utilized gate (B3)
under-utilized days.
"""


# ---------------------------------------------------------------------------
# Settings and formatting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Settings:
    """The run's budget-rule thresholds, read once from ``config/thresholds.yaml``.

    There are no code defaults: a missing or mistyped key raises ``ConfigError``.
    """

    b1_step: Decimal
    scale_max_ratio: Decimal
    poor_ratio: Decimal
    b3_quantile: Decimal
    b3_percentile: Decimal
    b3_headroom: Decimal
    b4_min_weekend_days: int
    b4_min_weekdays: int
    b4_min_outcomes: int
    b4_alpha: Decimal
    sustained_min: int
    floor_share: Decimal

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
        """Read every budget-rule threshold.

        ``b3.spend_percentile`` is configured as a percent (90) and applied as a fraction.

        Raises:
            ConfigError: When a key is missing or has the wrong type.
                Also when ``b3.spend_percentile`` lies outside 0-100 or ``b4.alpha`` outside
                (0, 1).
        """
        percent = thresholds.ratio("b3.spend_percentile")
        if percent > _HUNDRED:
            raise ConfigError(
                THRESHOLDS_FILE, "b3.spend_percentile", f"must lie in 0-100, found {percent}"
            )
        alpha = thresholds.ratio("b4.alpha")
        if not _ZERO < alpha < 1:
            raise ConfigError(
                THRESHOLDS_FILE, "b4.alpha", f"must lie strictly between 0 and 1, found {alpha}"
            )
        return cls(
            b1_step=thresholds.ratio("b1.step"),
            scale_max_ratio=thresholds.ratio("scale.max_cpa_ratio"),
            poor_ratio=thresholds.ratio("b2.poor_efficiency_ratio"),
            b3_quantile=percent / _HUNDRED,
            b3_percentile=percent,
            b3_headroom=thresholds.ratio("b3.headroom"),
            b4_min_weekend_days=thresholds.int("b4.min_weekend_days"),
            b4_min_weekdays=thresholds.int("b4.min_weekdays"),
            b4_min_outcomes=thresholds.int("b4.min_outcomes"),
            b4_alpha=alpha,
            sustained_min=thresholds.int("window.sustained_min_days"),
            floor_share=thresholds.ratio("attribution.floor_share"),
        )


def _q(value: Decimal) -> Decimal:
    """Record a cost or rate in evidence with six places, half even."""
    return value.quantize(_SIX_PLACES, rounding=ROUND_HALF_EVEN, context=_EXACT)


def _share(value: Decimal) -> Decimal:
    """Record a share or utilization in evidence with four places, half even: 0.3214."""
    return value.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN, context=_EXACT)


def _money(amount: Decimal | int | str | None, currency: str) -> str:
    """Render an amount at the currency's minor unit: ``USD 1,400.00``; ``unavailable``.

    Without a known currency the amount renders at two places with no code. A pre-rendered
    string (such as ``n/a``) is returned as it is.
    """
    if amount is None:
        return "unavailable"
    if isinstance(amount, str):
        return amount
    code = currency.strip().upper()
    places = minor_unit_exponent(code)
    rendered = format(round_to_minor_unit(Decimal(amount), code), f",.{places}f")
    return f"{code} {rendered}" if code else rendered


def _cost(amount: Decimal | int | str | None, currency: str) -> str:
    """Render a cost per outcome; "—" when the outcome count is zero (no finite cost)."""
    return "—" if amount is None else _money(amount, currency)


def _percent(share: Decimal) -> str:
    """Render a share as a percentage with at most one decimal place: 0.1304 → ``13%``."""
    scaled = (share * _HUNDRED).quantize(_ONE_PLACE, rounding=ROUND_HALF_EVEN, context=_EXACT)
    return f"{format(scaled.normalize(_EXACT), 'f')}%"


def _number(value: Decimal | int | str | None) -> str:
    """Render a count or multiple without trailing zeros (``6``, ``1.1``); ``unavailable``."""
    if value is None:
        return "unavailable"
    if isinstance(value, Decimal):
        return format(value.normalize(_EXACT), "f")
    return str(value)


def _p_value(value: Decimal) -> str:
    """Render ``p = 0.0123``, or ``p < 0.0001`` below the four places shown.

    Four places, rounded down, so a p-value judged against alpha never reads below it.
    """
    if value < _FOUR_PLACES:
        return f"p < {format(_FOUR_PLACES, 'f')}"
    return f"p = {format(value.quantize(_FOUR_PLACES, rounding=ROUND_FLOOR, context=_EXACT), 'f')}"


def _singular(label: str) -> str:
    """The singular of an outcome label: ``conversions`` → ``conversion``."""
    return label[:-1] if label.endswith("s") else label


def _require_amount(value: object, what: str) -> Decimal:
    """Require a finite, non-negative ``Decimal`` amount.

    Raises:
        TypeError: When ``value`` is not a ``Decimal``.
        ValueError: When it is not finite or is negative.
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"{what} must be a Decimal, found {value!r}")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{what} must be a finite, non-negative amount, found {value}")
    return value


# ---------------------------------------------------------------------------
# Renderers used by precedence.py for the B3 headroom allocation (AAP 0.5.4.2)
# ---------------------------------------------------------------------------


def render_b1_funding(
    increase: Decimal, allocations: Sequence[tuple[str, Decimal]], currency: str
) -> str:
    """Render how a B1 receiver's recommended increase is funded (AAP 0.5.4.2).

    A reallocation is funding for the receiver's B1 increase, never an increase of its own,
    so neither the B1 card nor a B3 card can be read as adding to the other: "Raise by
    {increase}: {Σ allocations} funded by reallocation from {donors}, {remainder} from new
    budget". Donors are named once each, in allocation order; with no allocation the "from
    {donors}" clause is omitted.

    Two donors giving 300 and 100 into an increase of 400 read "Raise by USD 400.00: USD
    400.00 funded by reallocation from Donor A, Donor B, USD 0.00 from new budget".

    Example:
        >>> render_b1_funding(Decimal("400.00"), [], "USD")
        'Raise by USD 400.00: USD 0.00 funded by reallocation, USD 400.00 from new budget'

    Args:
        increase: The B1 increase (``extra.increase``): proposed minus current daily budget.
        allocations: ``(donor name, amount)`` per B3 allocation into this receiver.
        currency: The receiver's currency; donors share it (allocations never cross
            currencies).

    Returns:
        The funding sentence, amounts at the currency's minor unit.

    Raises:
        TypeError: When an amount is not a ``Decimal``.
        ValueError: When an amount is negative or not finite, a donor name is blank, or the
            allocations exceed the increase, which the B3 allocation rule never allows.
    """
    total_increase = _require_amount(increase, "increase")
    funded = _ZERO
    donors: list[str] = []
    for name, amount in allocations:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"a donor name must be a non-blank string, found {name!r}")
        funded += _require_amount(amount, f"allocation from {name}")
        donors.append(name.strip())
    if funded > total_increase:
        raise ValueError(
            f"allocations of {funded} exceed the B1 increase of {total_increase}; the B3 "
            "allocation never funds more than a receiver's increase"
        )
    named = ", ".join(dict.fromkeys(donors))
    source = f" from {named}" if named else ""
    return (
        f"Raise by {_money(total_increase, currency)}: {_money(funded, currency)} funded by "
        f"reallocation{source}, {_money(total_increase - funded, currency)} from new budget"
    )


def render_b3_allocation(allocation: Decimal, receiver_name: str, currency: str) -> str:
    """Render one B3 allocation as funding for a named B1 increase (AAP 0.5.4.2).

    Example:
        >>> render_b3_allocation(Decimal("300.00"), "Receiver - Lead gen", "USD")
        "Move USD 300.00 to fund Receiver - Lead gen's recommended increase (B1)"

    Args:
        allocation: The amount moved from the donor to the receiver.
        receiver_name: The receiver's display name.
        currency: The shared currency of donor and receiver.

    Returns:
        "Move {allocation} to fund {receiver}'s recommended increase (B1)".

    Raises:
        TypeError: When ``allocation`` is not a ``Decimal``.
        ValueError: When it is negative or not finite, or the receiver name is blank.
    """
    amount = _require_amount(allocation, "allocation")
    if not isinstance(receiver_name, str) or not receiver_name.strip():
        raise ValueError(f"receiver_name must be a non-blank string, found {receiver_name!r}")
    return (
        f"Move {_money(amount, currency)} to fund {receiver_name.strip()}'s recommended "
        "increase (B1)"
    )


# ---------------------------------------------------------------------------
# Entities, subjects and datasets
# ---------------------------------------------------------------------------


def _campaign_ref(campaign: CampaignInfo) -> EntityRef:
    """The entity reference of a campaign."""
    return EntityRef(
        campaign.source, ENTITY_CAMPAIGN, campaign.id, campaign.external_id, campaign.name
    )


def _subject_ref(ctx: AnalysisContext, subject: SubjectKey) -> EntityRef:
    """The entity reference of a budget subject: a campaign, or a Google shared budget."""
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return _campaign_ref(ctx.campaigns[subject_id])
    budget = ctx.budgets[subject_id]
    return EntityRef("google", ENTITY_BUDGET, budget.id, budget.external_id, budget.name)


def _subject_source(ctx: AnalysisContext, subject: SubjectKey) -> Source:
    """The ad source of a budget subject; shared budgets are Google only."""
    subject_type, subject_id = subject
    return ctx.campaigns[subject_id].source if subject_type == "campaign" else "google"


def _subject_currency(ctx: AnalysisContext, subject: SubjectKey) -> str:
    """The currency a subject's amounts are in; amounts never cross currencies."""
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return ctx.account_currency(subject_id)
    return ctx.budgets[subject_id].currency


def _subject_active(ctx: AnalysisContext, subject: SubjectKey) -> bool:
    """Whether a subject is active now: the campaign, or for a shared budget a drawing member.

    A budget change on a paused or ended campaign changes nothing the platform serves, so
    B2 and B3 judge only active subjects, as C1 and C2 do.
    """
    subject_type, subject_id = subject
    if subject_type == "campaign":
        campaign = ctx.campaigns.get(subject_id)
        return campaign is not None and campaign.status == "active"
    return any(
        campaign.status == "active" and campaign.budget_id == subject_id
        for campaign in ctx.campaigns.values()
    )


def _outcome_label(ctx: AnalysisContext, members: Iterable[int]) -> str:
    """The outcome label of a subject's members (one objective group), e.g. ``leads``."""
    for cid in members:
        campaign = ctx.campaigns.get(cid)
        if campaign is not None:
            return OUTCOME_LABELS[campaign.objective_group]
    return "outcomes"


def _depends_on(
    ctx: AnalysisContext, campaign_ids: Iterable[int], subjects: Iterable[SubjectKey] = ()
) -> tuple[DecisionSubject, ...]:
    """The subjects whose unresolved discrepancy holds an item (P1).

    ``subjects`` first, then each campaign and the budget it draws from, de-duplicated in
    that order; ``precedence.py`` expands the list further.
    """
    ordered: list[DecisionSubject] = [DecisionSubject.of(key) for key in subjects]
    for campaign_id in campaign_ids:
        ordered.append(DecisionSubject("campaign", campaign_id))
        campaign = ctx.campaigns.get(campaign_id)
        if campaign is not None and campaign.budget_id is not None:
            ordered.append(DecisionSubject("budget", campaign.budget_id))
    return tuple(dict.fromkeys(ordered))


def _ad_datasets(source: Source) -> tuple[str, str]:
    """An ad source's entity and daily metrics datasets."""
    return (f"{source}.entities", _METRICS_DATASETS[source])


def _required(*groups: Iterable[str]) -> tuple[str, ...]:
    """Dataset names in order, repeats dropped."""
    return tuple(dict.fromkeys(name for group in groups for name in group))


def _shared_flag(member_count: int) -> Flag:
    """The ``shared_budget`` flag of a Google explicitly shared budget subject."""
    return Flag(
        FLAG_SHARED_BUDGET,
        f"Shared budget across {member_count} campaigns: decided at budget level",
    )


def _day_boundary(ctx: AnalysisContext, source: Source) -> str:
    """The native day boundary ``source`` used in this run's window.

    Falls back to the Google account's zone (else UTC) when the window does not list the
    source, so evidence always names the boundary its dates were read on.
    """
    try:
        return ctx.window.boundary("google" if source == "google" else "linkedin")
    except KeyError:
        if source == "google":
            for account in sorted(ctx.accounts.values(), key=lambda acct: acct.id):
                if account.source == "google" and account.timezone:
                    return account.timezone
        return "UTC"


# ---------------------------------------------------------------------------
# CRM Qualified Leads where linked (AAP 0.5.4.1 "CRM pipeline link", 0.5.4.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetCrm:
    """The CRM evidence a budget card shows: Qualified Leads of the subject's veto population.

    Attributes:
        scope: Which CRM dataset the counts read (``linkedin``: the effective CRM dataset of
            the LinkedIn dataset; ``google``: HubSpot's primary through the verified
            Google-to-CRM link); ``None`` when the subject has no CRM link.
        eligible_contacts: Distinct contacts behind the matched matured leads; ``None`` when
            no population exists.
        ql: Qualified Lead entries of those contacts after their touch; ``None`` when the
            Qualified Lead stage is unavailable, never 0.
        flags: ``no_crm_link`` (Google without a verified link), or the population's
            ``pipeline_unavailable``, ``attribution_floor`` and ``unknown_crm_evidence``.
    """

    scope: CrmScope | None
    eligible_contacts: int | None
    ql: int | None
    flags: tuple[Flag, ...] = ()

    @property
    def linked(self) -> bool:
        """Whether a population exists, so the card shows eligible contacts and QL."""
        return self.scope is not None and self.eligible_contacts is not None

    @property
    def datasets(self) -> tuple[str, ...]:
        """The CRM datasets the counts read; empty without a population."""
        if self.scope is None or not self.linked:
            return ()
        return _CRM_DATASETS[self.scope]

    @classmethod
    def from_veto(
        cls, veto: VetoPopulation | None, scope: CrmScope | None, flags: Sequence[Flag] = ()
    ) -> BudgetCrm:
        """Build the card's CRM evidence from a veto population (for example a C2 outcome's)."""
        if veto is None or scope is None:
            return cls(None, None, None, tuple(flags))
        return cls(scope, veto.eligible_contacts, veto.ql, tuple(flags))


def _google_link_gap(ctx: AnalysisContext) -> str | None:
    """Why Google subjects have no CRM link, or ``None`` when the link is verified (AAP 0.3.3)."""
    crm = ctx.google_crm
    if crm is None:
        return "no active HubSpot dataset"
    link = crm.bindings.google
    if link.available:
        return None
    return link.reason or "the Google-to-CRM link is not verified"


def _no_crm_link_flag(reason: str) -> Flag:
    """The ``no_crm_link`` flag of a Google item."""
    return Flag(FLAG_NO_CRM_LINK, f"No CRM link for Google campaigns: {reason}")


def _population_flags(
    ctx: AnalysisContext,
    settings: _Settings,
    populations: Sequence[VetoPopulation],
) -> tuple[Flag, ...]:
    """The flags explaining why a (combined) population's counts cannot be fully trusted.

    One population keeps its own floor verdict, exactly as the Campaigns section shows it.
    Several populations (a shared budget's members) pool the unresolved share (unresolved ÷
    matured leads), so the floor is judged on all of the members' leads together.
    """
    flags: list[Flag] = []
    unavailable = next((item for item in populations if not item.available), None)
    if unavailable is not None:
        prefix = f"{ctx.campaign_name(unavailable.campaign_id)}: " if len(populations) > 1 else ""
        flags.append(
            Flag(FLAG_PIPELINE_UNAVAILABLE, f"Pipeline unavailable: {prefix}{unavailable.reason}")
        )
    share: Decimal | None
    if len(populations) == 1:
        single = populations[0]
        share = single.unresolved_share if single.attribution_floor else None
    else:
        matured = sum(item.matured_leads for item in populations)
        unresolved = sum(item.unresolved_leads for item in populations)
        pooled = Decimal(unresolved) / Decimal(matured) if matured else None
        share = pooled if pooled is not None and pooled >= settings.floor_share else None
    if share is not None:
        flags.append(
            Flag(
                FLAG_ATTRIBUTION_FLOOR,
                f"Floor: {_percent(share)} of matured leads unmatched in HubSpot",
            )
        )
    unknown = sum(population.unknown_evidence_contacts for population in populations)
    if unknown:
        eligible = sum(population.eligible_contacts for population in populations)
        flags.append(
            Flag(
                FLAG_UNKNOWN_CRM_EVIDENCE,
                f"Unknown CRM evidence for {unknown} of {eligible} eligible contacts",
            )
        )
    return tuple(flags)


def _sum_known(values: Iterable[int | None]) -> int | None:
    """Sum counts, or ``None`` when any is unavailable: an unknown part is never zero."""
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _combined(
    ctx: AnalysisContext,
    settings: _Settings,
    scope: CrmScope,
    populations: Sequence[VetoPopulation],
) -> BudgetCrm:
    """The CRM evidence of one or more populations of one scope.

    Counts are summed, which is exact for a Google shared budget: each touch-date contact
    carries one campaign reference, so no contact sits in two members' populations.
    """
    if not populations:
        return BudgetCrm(None, None, None)
    return BudgetCrm(
        scope,
        sum(population.eligible_contacts for population in populations),
        _sum_known(population.ql for population in populations),
        _population_flags(ctx, settings, populations),
    )


def budget_crm(ctx: AnalysisContext, subject: SubjectKey) -> BudgetCrm:
    """Return the CRM Qualified Lead evidence of a budget subject or campaign (AAP 0.5.4.1).

    * A LinkedIn campaign links through lead attribution: its veto population in the
      effective CRM dataset (the companion while LinkedIn is simulated).
    * A Google campaign links only through the verified Google-to-CRM link; otherwise the
      result carries ``no_crm_link`` and no Google QL count is computed, and a
      LinkedIn-derived subset never stands in.
    * A Google shared budget combines its member campaigns' populations
      (:func:`~ads_engine.analysis.metrics.subject_campaigns`).

    The populations come from the run's one memoised attribution table, so these counts
    equal the Campaigns section's. A budget card shows them; no rule here evaluates a veto.

    Raises:
        KeyError: When the subject is not in the context.
        ConfigError: When ``attribution.floor_share`` is missing.
    """
    settings = _Settings.of(ctx)
    subject_type, subject_id = subject
    if subject_type == "campaign":
        campaign = ctx.campaigns[subject_id]
        members: tuple[int, ...] = (campaign.id,)
        source: Source = campaign.source
    else:
        if subject_id not in ctx.budgets:
            raise KeyError(f"budget {subject_id} is not in the analysis context")
        members = subject_campaigns(ctx, subject)
        source = "google"
    scope: CrmScope = "linkedin" if source == "linkedin" else "google"
    if scope == "google":
        gap = _google_link_gap(ctx)
        if gap is not None:
            return BudgetCrm(None, None, None, (_no_crm_link_flag(gap),))
    veto = attribution_for(ctx).veto
    populations = [veto[cid] for cid in members if cid in veto]
    return _combined(ctx, settings, scope, populations)


# ---------------------------------------------------------------------------
# Shared card content (AAP 0.5.4.5 "Every budget item shows these side by side")
# ---------------------------------------------------------------------------


def _predicate(gate: GateResult) -> tuple[str, str, str, str] | None:
    """The daily predicate a gate counts (capped, high-utilization or under-utilized days)."""
    for entry in _PREDICATES:
        if entry[0] in gate.counts:
            return entry
    return None


def _predicate_count(history: BudgetHistory, attribute: str) -> int:
    """A history's count of days meeting one daily predicate."""
    counts = {"capped": history.capped, "high": history.high, "under": history.under}
    return counts[attribute]


def _dates(history: BudgetHistory, *klasses: str) -> tuple[str, ...]:
    """ISO native dates of the history's days in ``klasses``, ascending."""
    return tuple(day.date.isoformat() for day in history.days if day.klass in klasses)


def _members_extra(ctx: AnalysisContext, subject: SubjectKey) -> list[dict[str, Any]]:
    """Every member campaign of a shared budget with its member days in the window."""
    history = budget_history(ctx, subject)
    return [
        {
            "campaign_id": cid,
            "name": ctx.campaign_name(cid),
            "member_days": len(history.members.get(cid, ())),
        }
        for cid in subject_campaigns(ctx, subject)
    ]


def budget_evidence(
    ctx: AnalysisContext,
    subject: SubjectKey,
    history: BudgetHistory,
    gate: GateResult,
    *,
    outcomes: Decimal | None,
    cpo: Decimal | None,
    baseline: Decimal | None,
    proposed: Decimal | None = None,
    spend: Decimal | None = None,
    crm: BudgetCrm | None = None,
    metrics: Sequence[Metric] = (),
    baselines: Sequence[Baseline] = (),
    threshold_keys: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> Evidence:
    """Build the evidence every budget card shows side by side (AAP 0.5.4.5, 0.5.4.8).

    * **Utilization**: the window utilization (Σ spend ÷ Σ daily budget over known-budget
      active days; "n/a" without one) plus every day count of AAP 0.5.4.1: known-budget days
      (budget-active days for a shared budget) against ``budget.min_known_days`` in
      ``sample``, spending days and their share, the days meeting this gate's predicate and
      their share, known idle days, changed-intraday days and unobserved days. Each check's
      observed against required is in ``extra.gate``; the dated day lists and
      ``extra.observation_assumption`` ("unchanged across N observations, at most 6 h apart")
      state what the counts rest on.
    * **Efficiency**: cost per outcome beside the account baseline for the same source and
      objective group.
    * **Outcomes** and, where linked, the CRM eligible contacts and Qualified Leads of the
      veto population (:func:`budget_crm`), never blended with platform outcomes.
    * **Amounts**: the latest effective daily budget and, when given, the proposed one.
    * **Window**: the exact window with each source's native day boundary (from
      :meth:`AnalysisContext.make_evidence`) and ``extra.day_boundary`` for the subject's own
      source.

    A shared budget adds ``extra.members``: every member campaign with its member days.
    Lifetime and custom-period budgets never reach this function: they are no budget subject
    (:func:`~ads_engine.analysis.metrics.budget_subjects`), so their utilization stays "n/a".

    Args:
        ctx: The analysis context.
        subject: The budget subject the card is about.
        history: The subject's budget history (``history.subject`` must equal ``subject``).
        gate: The gate the rule applied; its checks name the daily predicate shown.
        outcomes: Window outcomes (member days for a shared budget); ``None`` when unavailable.
        cpo: Window cost per outcome; ``None`` at zero or unavailable outcomes.
        baseline: The account baseline cost per outcome; ``None`` when unavailable.
        proposed: The proposed daily budget, already at the currency's minor unit.
        spend: Window spend; the history's spend (member days only) when omitted.
        crm: The CRM evidence to show; :func:`budget_crm` of the subject when omitted.
        metrics: Rule-specific metrics, appended after the shared ones.
        baselines: Rule-specific baselines, appended after the account baseline.
        threshold_keys: The rule's own threshold keys, added to the ``budget.*`` gate keys
            (and the CRM keys when CRM counts are shown).
        extra: Rule-specific detail, merged over the shared ``extra``.

    Returns:
        The frozen evidence.

    Raises:
        ValueError: When ``history`` belongs to another subject, or a rule metric repeats a
            shared metric's key.
        KeyError: When the subject is not in the context.
        ConfigError: When a threshold key is missing.
    """
    if history.subject != subject:
        raise ValueError(f"history of {history.subject} cannot evidence subject {subject}")
    currency = _subject_currency(ctx, subject)
    source = _subject_source(ctx, subject)
    members = subject_campaigns(ctx, subject)
    label = _outcome_label(ctx, members)
    view = budget_crm(ctx, subject) if crm is None else crm
    known = history.known_active

    def share_of(count: int) -> Decimal | None:
        return None if known == 0 else _share(Decimal(count) / Decimal(known))

    utilization: Decimal | str = (
        "n/a" if history.window_utilization is None else _share(history.window_utilization)
    )
    card: list[Metric] = [
        Metric(
            "spend",
            "Window spend",
            history.window_spend if spend is None else spend,
            "currency",
            currency,
        ),
        Metric("window_days", "Window days", ctx.window.days, "days"),
        Metric("outcomes", f"Window {label}", outcomes, label),
        Metric(
            "cost_per_outcome",
            f"Cost per {_singular(label)}",
            None if cpo is None else _q(cpo),
            "currency",
            currency,
        ),
        Metric(
            "daily_budget",
            "Latest effective daily budget",
            history.latest_daily_budget,
            "currency",
            currency,
        ),
    ]
    if proposed is not None:
        card.append(
            Metric("proposed_daily_budget", "Proposed daily budget", proposed, "currency", currency)
        )
    card.extend(
        (
            Metric("window_utilization", "Window utilization (spend vs cap)", utilization, "ratio"),
            Metric("known_days", history.known_label.capitalize(), known, "days"),
            Metric("spending_days", "Spending days", history.spending, "days"),
            Metric(
                "spending_day_share",
                f"Share of {history.known_label} with spend",
                share_of(history.spending),
                "ratio",
            ),
        )
    )
    predicate = _predicate(gate)
    if predicate is not None:
        _check, attribute, key, predicate_label = predicate
        count = _predicate_count(history, attribute)
        card.append(Metric(key, predicate_label, count, "days"))
        card.append(
            Metric(
                f"{key}_share",
                f"Share of {history.known_label} that are {predicate_label.lower()}",
                share_of(count),
                "ratio",
            )
        )
    card.extend(
        (
            Metric("idle_days", "Known idle days", history.idle, "days"),
            Metric(
                "changed_intraday_days",
                "Changed-intraday days (unknown budget)",
                history.changed_intraday,
                "days",
            ),
            Metric(
                "unobserved_days", "Unobserved days (unknown budget)", history.unobserved, "days"
            ),
        )
    )
    if view.linked:
        card.append(
            Metric(
                "eligible_contacts",
                "Eligible contacts (matured leads)",
                view.eligible_contacts,
                "contacts",
            )
        )
        card.append(Metric("ql", "Qualified Leads (CRM)", view.ql, "contacts"))
    card.extend(metrics)

    shared_extra: dict[str, Any] = {
        "subject": {"type": subject[0], "id": subject[1]},
        "subject_type": subject[0],
        "shared": history.shared,
        "currency": currency,
        "objective_groups": tuple(sorted(subject_objective_groups(ctx, subject))),
        "gate": {
            check: {"observed": observed, "required": required}
            for check, (observed, required) in gate.counts.items()
        },
        "gate_shortfalls": gate.shortfalls,
        "day_counts": history.counts(),
        "observation_assumption": history.assumption,
        "idle_dates": tuple(day.date.isoformat() for day in history.days if day.idle),
        "changed_intraday_dates": _dates(history, "changed_intraday"),
        "unobserved_dates": _dates(history, "unobserved"),
        "unknown_dates": _dates(history, "changed_intraday", "unobserved"),
        "day_boundary": {"source": source, "boundary": _day_boundary(ctx, source)},
        "current": history.latest_daily_budget,
    }
    if proposed is not None:
        shared_extra["proposed"] = proposed
    if history.shared:
        shared_extra["members"] = _members_extra(ctx, subject)
    return ctx.make_evidence(
        required_datasets=_required(_ad_datasets(source), view.datasets),
        crm_scope=view.scope if view.linked else None,
        metrics=card,
        baselines=(
            Baseline(
                "baseline_cost_per_outcome",
                f"Account cost per {_singular(label)} (same source and objective)",
                None if baseline is None else _q(baseline),
            ),
            *baselines,
        ),
        sample=Sample(known, history.config.min_known_days, history.known_label),
        threshold_keys=_required(
            BUDGET_THRESHOLD_KEYS,
            threshold_keys,
            VETO_THRESHOLD_KEYS if view.linked else (),
        ),
        extra={**shared_extra, **(extra or {})},
    )


def _subject_flags(ctx: AnalysisContext, subject: SubjectKey, view: BudgetCrm) -> tuple[Flag, ...]:
    """A budget item's flags: its CRM flags, plus ``shared_budget`` for a shared budget."""
    flags = list(view.flags)
    if subject[0] == "budget":
        flags.append(_shared_flag(len(subject_campaigns(ctx, subject))))
    return tuple(flags)


def _datasets_of(evidence: Evidence) -> tuple[str, ...]:
    """The dataset names an item read: exactly those its evidence records."""
    return tuple(status.dataset for status in evidence.datasets)


def _value(evidence: Evidence, key: str) -> Decimal | int | str | None:
    """The value of one of an item's own metrics, for its reason sentence."""
    return evidence.metric(key).value


def _money_of(evidence: Evidence, key: str) -> str:
    """One of an item's own money metrics, rendered in its currency."""
    metric = evidence.metric(key)
    return _money(metric.value, metric.currency or "")


def _cost_of(evidence: Evidence, key: str) -> str:
    """One of an item's own cost-per-outcome metrics; "—" when it is unavailable."""
    metric = evidence.metric(key)
    return _cost(metric.value, metric.currency or "")


def _utilization_text(evidence: Evidence) -> str:
    """The window utilization metric as a percentage, or "n/a"."""
    value = _value(evidence, "window_utilization")
    return _percent(value) if isinstance(value, Decimal) else "n/a"


def _baseline_text(evidence: Evidence) -> str:
    """The account baseline cost per outcome in the item's currency; "unavailable" when unknown."""
    currency = evidence.metric("spend").currency or ""
    return _money(evidence.baselines[0].value, currency)


def _ordinal(value: Decimal) -> str:
    """Render a percentile rank as an ordinal: 90 → ``90th``, 1 → ``1st``, 92.5 → ``92.5th``."""
    text = _number(value)
    if value != value.to_integral_value():
        return f"{text}th"
    whole = int(value)
    if 10 <= whole % 100 <= 20:
        return f"{text}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(whole % 10, "th")
    return f"{text}{suffix}"


# ---------------------------------------------------------------------------
# Withheld lines (P6)
# ---------------------------------------------------------------------------


def _window_withheld(rule_id: str, days: int, required: int) -> Withheld:
    """A rule withheld for the whole run: the window is below ``window.sustained_min_days``."""
    return Withheld(
        rule_id=rule_id,
        section=BUDGET,
        entity=None,
        reason=f"window {days} days of {required} required",
        observed=days,
        required=required,
        unit="days",
    )


def _history_short(gate: GateResult) -> bool:
    """Whether the gate failed on history length: known-budget or spending days (AAP 0.5.4.1)."""
    return any(
        check in gate.counts and gate.counts[check][0] < gate.counts[check][1]
        for check in _HISTORY_MINIMUMS
    )


def _gate_withheld(
    rule_id: str, entity: EntityRef, history: BudgetHistory, gate: GateResult
) -> Withheld:
    """The withheld line of a failed gate: every shortfall, the first one as the sample.

    Raises:
        ValueError: When the gate has no shortfall.
    """
    for check, (observed, required) in gate.counts.items():
        if observed < required:
            unit = (
                history.known_label
                if check == "known_budget_days"
                else _CHECK_UNITS.get(check, "days")
            )
            return Withheld(
                rule_id=rule_id,
                section=BUDGET,
                entity=entity,
                reason=gate.describe(),
                observed=observed,
                required=required,
                unit=unit,
            )
    raise ValueError(f"{rule_id}: a passed gate has no shortfall to withhold")


def _mixed_withheld(rule_id: str, entity: EntityRef, groups: frozenset[str]) -> Withheld:
    """A shared budget whose members span objective groups or currencies: efficiency "n/a".

    AAP 0.5.4.5 "Mixed objective groups": B1, B2 and B3 are withheld for that budget. The
    wording matches C2's (and so B1's) line from ``scale_outcomes``.
    """
    if len(groups) > 1:
        return Withheld(
            rule_id=rule_id,
            section=BUDGET,
            entity=entity,
            reason=f"members span {len(groups)} objective groups: efficiency n/a",
            observed=len(groups),
            required=1,
            unit="objective groups",
        )
    return Withheld(
        rule_id=rule_id,
        section=BUDGET,
        entity=entity,
        reason="members span more than one currency: efficiency n/a",
        observed=None,
        required=None,
        unit="currencies",
    )


# ---------------------------------------------------------------------------
# B1 Increase (AAP 0.5.4.5 B1 row, 0.5.4.4 C2)
# ---------------------------------------------------------------------------


def _b1(ctx: AnalysisContext, settings: _Settings) -> RuleResult:
    """B1 Increase: one item per C2 "Scale" outcome; never under a veto "Hold".

    AAP 0.5.4.5 B1. Eligibility is C2's, read from the single shared evaluation
    :func:`~ads_engine.analysis.campaigns.scale_outcomes`: cap-constrained (at least
    ``budget.min_known_days`` known-budget days, ``budget.min_spending_days`` spending days,
    capped days ≥ ``budget.min_capped_days`` and ≥ ``budget.sustained_share``), outcomes ≥
    ``scale.min_outcomes``, cost per outcome ≤ ``scale.max_cpa_ratio`` x baseline, a window of
    at least ``window.sustained_min_days``, and G1 not blocking (a blocked subject's B1 is
    still built, declaring its targets, so the precedence post-filter removes and counts it).
    The proposal is the latest effective daily budget x (1 + ``b1.step``) at the currency's
    minor unit. Shortfalls of an efficient subject become B1 withheld lines with C2's text.

    Threshold keys: :data:`B1_THRESHOLD_KEYS`, plus the CRM keys where CRM counts are shown.

    Rationale: "Campaigns hitting their budget cap consistently with good output → scale
    candidates"; a bounded 20% step lets the next window measure the response.
    """
    candidates: list[Candidate] = []
    withheld: list[Withheld] = []
    for entry in scale_outcomes(ctx):
        if isinstance(entry, ScaleWithheld):
            withheld.append(entry.to_withheld(B1, BUDGET))
        elif entry.kind == "scale":
            candidates.append(_b1_candidate(ctx, settings, entry))
    return RuleResult(candidates=tuple(candidates), withheld=tuple(withheld))


def _b1_candidate(ctx: AnalysisContext, settings: _Settings, outcome: ScaleOutcome) -> Candidate:
    """The B1 item of one C2 "Scale" outcome."""
    current = outcome.current_daily_budget
    proposed = outcome.proposed_daily_budget
    increase = proposed - current
    currency = outcome.currency
    ceiling = None if outcome.baseline is None else _q(settings.scale_max_ratio * outcome.baseline)
    extra: dict[str, Any] = {
        "increase": increase,
        "allocations": (),
        "step": settings.b1_step,
        "outcome": "Scale",
        "c2_link": {
            "rule_id": C2,
            "subject": {"type": outcome.subject[0], "id": outcome.subject[1]},
        },
    }
    if outcome.g1_blocked:
        extra["g1_blocked"] = True
    evidence = budget_evidence(
        ctx,
        outcome.subject,
        outcome.history,
        outcome.gate,
        outcomes=outcome.outcomes,
        cpo=outcome.cpo,
        baseline=outcome.baseline,
        proposed=proposed,
        spend=outcome.spend,
        crm=BudgetCrm.from_veto(outcome.veto, outcome.crm_scope, outcome.flags),
        metrics=(Metric("increase", "Recommended increase", increase, "currency", currency),),
        baselines=(
            Baseline("max_cost_per_outcome", "Highest cost per outcome allowed to scale", ceiling),
        ),
        threshold_keys=B1_THRESHOLD_KEYS,
        extra=extra,
    )
    label = _outcome_label(ctx, outcome.members)
    step = evidence.extra["step"]
    reason = (
        f"Capped on {_value(evidence, 'capped_days')} of {_value(evidence, 'known_days')} "
        f"{outcome.history.known_label} (window utilization {_utilization_text(evidence)}) "
        f"with {_number(_value(evidence, 'outcomes'))} {label} at "
        f"{_cost_of(evidence, 'cost_per_outcome')} per {_singular(label)} (account "
        f"{_baseline_text(evidence)}): raise the daily budget by {_percent(step)} from "
        f"{_money_of(evidence, 'daily_budget')} to {_money_of(evidence, 'proposed_daily_budget')}."
    )
    return Candidate(
        rule_id=B1,
        section=BUDGET,
        group=GROUP_CHANGES,
        entity_ref=outcome.entity,
        action=(
            f"Raise daily budget from {_money_of(evidence, 'daily_budget')} to "
            f"{_money_of(evidence, 'proposed_daily_budget')}"
        ),
        reason=reason,
        evidence=evidence,
        rank_key=(-outcome.spend,),
        flags=outcome.flags,
        required_datasets=_datasets_of(evidence),
        increases_spend_on=outcome.increases_spend_on,
        decision_subject=outcome.decision_subject,
        depends_on=_depends_on(ctx, outcome.members, (outcome.subject,)),
    )


# ---------------------------------------------------------------------------
# B2 Do not increase and B3 Under-utilized (AAP 0.5.4.5 B2 and B3 rows)
# ---------------------------------------------------------------------------


def _b2_b3(ctx: AnalysisContext, settings: _Settings) -> RuleResult:
    """B2 and B3 over every active budget subject (campaign daily budgets, shared budgets).

    Both are sustained-pattern rules: below ``window.sustained_min_days`` each is withheld
    for the run with observed against required days. A shared budget whose members span
    objective groups or currencies has efficiency "n/a", so both are withheld for it.
    Lifetime and custom-period budgets are no budget subject and never reach here.
    """
    subjects = [subject for subject in budget_subjects(ctx) if _subject_active(ctx, subject)]
    if not subjects:
        return RuleResult()
    window = ctx.window
    if not window.sustained_ok:
        return RuleResult(
            withheld=(
                _window_withheld(B2, window.days, settings.sustained_min),
                _window_withheld(B3, window.days, settings.sustained_min),
            )
        )
    b2_items: list[Candidate] = []
    b3_items: list[Candidate] = []
    withheld: list[Withheld] = []
    for subject in subjects:
        entity = _subject_ref(ctx, subject)
        if subject_totals(ctx, subject) is None:
            groups = subject_objective_groups(ctx, subject)
            withheld.append(_mixed_withheld(B2, entity, groups))
            withheld.append(_mixed_withheld(B3, entity, groups))
            continue
        history = budget_history(ctx, subject)
        for found, items in (
            (_b2(ctx, settings, subject, entity, history), b2_items),
            (_b3(ctx, settings, subject, entity, history), b3_items),
        ):
            if isinstance(found, Candidate):
                items.append(found)
            elif isinstance(found, Withheld):
                withheld.append(found)
    return RuleResult(candidates=(*b2_items, *b3_items), withheld=tuple(withheld))


def _b2(
    ctx: AnalysisContext,
    settings: _Settings,
    subject: SubjectKey,
    entity: EntityRef,
    history: BudgetHistory,
) -> Candidate | Withheld | None:
    """B2 Do not increase: high utilization with poor efficiency.

    AAP 0.5.4.5 B2. Fires when :func:`~ads_engine.analysis.metrics.assess_b2` meets: the
    high-utilization gate (at least ``budget.min_known_days`` (7) known-budget active days, at
    least ``budget.min_spending_days`` (5) spending days making up ≥ ``budget.sustained_share``
    (70%) of them, and high-utilization days (u_d ≥ ``budget.high_utilization``, 90%) on ≥ 70%
    of them), and poor efficiency: cost per outcome ≥ ``b2.poor_efficiency_ratio`` (1.5) x
    baseline (branch ``poor_efficiency``), or 0 outcomes with expected outcomes ≥
    ``b2.min_expected_outcomes`` (3) (branch ``zero_outcomes``, which P2 suppresses when the
    source records no outcome at all). The zero-outcome minimum is the predicate's own
    ``b2.*`` key, the one G1 applies too, rather than ``c1.min_expected_outcomes``.

    The item prefills "Record decision" with the daily budget lowered by ``b1.step`` (20%) at
    the currency's minor unit and declares no spend increase; G1 in ``precedence.py`` blocks
    every increase on the target, and a C1 pause can still appear for the same campaign.
    When efficiency is poor but the history is short (known-budget or spending days below
    their minimums), B2 is withheld with every shortfall, for example "spending days 4 of 5
    required". A full history that is simply not high-utilization yields nothing.

    Rationale: "never recommend increasing spend on high-utilization + poor-efficiency
    campaigns": the budget is already spent, and spent badly.
    """
    assessment = assess_b2(ctx, subject)
    if not assessment.meets:
        if assessment.branch is not None and _history_short(assessment.gate):
            return _gate_withheld(B2, entity, history, assessment.gate)
        return None
    currency = _subject_currency(ctx, subject)
    current = history.latest_daily_budget
    proposed = (
        None if current is None else round_to_minor_unit(current * (1 - settings.b1_step), currency)
    )
    view = budget_crm(ctx, subject)
    label = _outcome_label(ctx, subject_campaigns(ctx, subject))
    metrics: list[Metric] = []
    if assessment.expected is not None:
        metrics.append(
            Metric(
                "expected_outcomes",
                f"Expected {label} at the account baseline",
                _q(assessment.expected),
                label,
            )
        )
    poor_at = None if assessment.baseline is None else _q(settings.poor_ratio * assessment.baseline)
    evidence = budget_evidence(
        ctx,
        subject,
        history,
        assessment.gate,
        outcomes=assessment.outcomes,
        cpo=assessment.cpo,
        baseline=assessment.baseline,
        proposed=proposed,
        spend=assessment.spend,
        crm=view,
        metrics=metrics,
        baselines=(
            Baseline(
                "poor_efficiency_cost_per_outcome",
                "Cost per outcome at which efficiency is poor",
                poor_at,
            ),
        ),
        threshold_keys=B2_RULE_THRESHOLD_KEYS,
        extra={
            "branch": assessment.branch,
            "decrease_step": settings.b1_step,
            "poor_efficiency_ratio": settings.poor_ratio,
        },
    )
    step = _percent(evidence.extra["decrease_step"])
    high = (
        f"High utilization on {_value(evidence, 'high_utilization_days')} of "
        f"{_value(evidence, 'known_days')} {history.known_label} (window utilization "
        f"{_utilization_text(evidence)})"
    )
    singular = _singular(label)
    if evidence.extra["branch"] == "zero_outcomes":
        efficiency = (
            f"with 0 {label} where {_number(_value(evidence, 'expected_outcomes'))} were "
            f"expected at the account's {_baseline_text(evidence)} per {singular}"
        )
    else:
        efficiency = (
            f"at {_cost_of(evidence, 'cost_per_outcome')} per {singular}, at least "
            f"{_number(evidence.extra['poor_efficiency_ratio'])}x the account's "
            f"{_baseline_text(evidence)}"
        )
    advice = "do not increase"
    if proposed is not None:
        advice += (
            f"; lowering the daily budget by {step} takes it from "
            f"{_money_of(evidence, 'daily_budget')} to "
            f"{_money_of(evidence, 'proposed_daily_budget')}"
        )
    members = subject_campaigns(ctx, subject)
    return Candidate(
        rule_id=B2,
        section=BUDGET,
        group=GROUP_CHANGES,
        entity_ref=entity,
        action=f"Do not increase. Lower daily budget by {step} or fix efficiency first",
        reason=f"{high} {efficiency}: {advice}.",
        evidence=evidence,
        rank_key=(-assessment.spend,),
        flags=_subject_flags(ctx, subject, view),
        required_datasets=_datasets_of(evidence),
        increases_spend_on=(),
        decision_subject=DecisionSubject.of(subject),
        depends_on=_depends_on(ctx, members, (subject,)),
    )


def _b3(
    ctx: AnalysisContext,
    settings: _Settings,
    subject: SubjectKey,
    entity: EntityRef,
    history: BudgetHistory,
) -> Candidate | Withheld | None:
    """B3 Under-utilized: lower the daily budget toward what the subject actually spends.

    AAP 0.5.4.5 B3. Fires when the under-utilized gate passes
    (:func:`~ads_engine.analysis.metrics.under_utilized`: at least ``budget.min_known_days``
    (7) known-budget active days, at least ``budget.min_spending_days`` (5) spending days, and
    under-utilized days (u_d ≤ ``budget.under_utilization``, 50%, known idle days included)
    on ≥ ``budget.sustained_share`` (70%) of them). The proposal is the
    ``b3.spend_percentile`` (90th) percentile of daily spend over the known-budget (or
    budget-active) days x ``b3.headroom`` (1.1), at the currency's minor unit; the item exists
    only when that is above 0 and below the current daily budget. ``extra.freed`` (current -
    proposed) is what ``precedence.py`` allocates to surviving B1 receivers, filling
    ``extra.allocations`` and rendering each with :func:`render_b3_allocation`; unallocated
    freed amount stays a plain decrease.

    A subject with only idle days and no spending day is idle, never a reallocation donor:
    the spending-day minimum keeps a budget that never serves out of B3, and no withheld line
    is emitted for it. A spending subject with under-utilized days whose history is short
    (known-budget or spending days below their minimums) is withheld with every shortfall,
    for example "known-budget days 6 of 7 required". Unknown days (changed intraday or
    unobserved) are never counted as idle.

    Rationale: an under-spent budget reserves money the subject does not use; the headroom
    above observed delivery keeps the cap from binding on its best days.
    """
    gate = under_utilized(history)
    if not gate.passed:
        if _history_short(gate) and history.spending > 0 and history.under > 0:
            return _gate_withheld(B3, entity, history, gate)
        return None
    current = history.latest_daily_budget
    spends = [day.spend for day in history.days if day.known_active]
    observed = percentile(spends, settings.b3_quantile)
    if current is None or observed is None:
        return None
    currency = _subject_currency(ctx, subject)
    proposed = round_to_minor_unit(observed * settings.b3_headroom, currency)
    if proposed <= 0 or proposed >= current:
        return None
    freed = current - proposed
    totals = subject_totals(ctx, subject)
    if totals is None:
        return None
    baseline = subject_baseline(ctx, subject)
    view = budget_crm(ctx, subject)
    rank = _ordinal(settings.b3_percentile)
    evidence = budget_evidence(
        ctx,
        subject,
        history,
        gate,
        outcomes=totals.outcomes,
        cpo=cost_per_outcome(totals.spend, totals.outcomes),
        baseline=baseline,
        proposed=proposed,
        spend=totals.spend,
        crm=view,
        metrics=(
            Metric(
                "spend_percentile",
                f"{rank} percentile of daily spend on {history.known_label}",
                observed,
                "currency",
                currency,
            ),
            Metric("freed", "Freed daily amount", freed, "currency", currency),
        ),
        threshold_keys=B3_THRESHOLD_KEYS,
        extra={
            "freed": freed,
            "allocations": (),
            "percentile": settings.b3_percentile,
            "headroom": settings.b3_headroom,
            "under_utilization": history.config.under_utilization,
        },
    )
    reason = (
        f"Under-utilized on {_value(evidence, 'under_utilized_days')} of "
        f"{_value(evidence, 'known_days')} {history.known_label} (daily utilization at or below "
        f"{_percent(evidence.extra['under_utilization'])}; window utilization "
        f"{_utilization_text(evidence)}); the {rank} percentile of daily spend is "
        f"{_money_of(evidence, 'spend_percentile')} and "
        f"{_number(evidence.extra['headroom'])}x headroom gives "
        f"{_money_of(evidence, 'proposed_daily_budget')}: lower the daily budget from "
        f"{_money_of(evidence, 'daily_budget')} to "
        f"{_money_of(evidence, 'proposed_daily_budget')}, freeing {_money_of(evidence, 'freed')}."
    )
    members = subject_campaigns(ctx, subject)
    return Candidate(
        rule_id=B3,
        section=BUDGET,
        group=GROUP_CHANGES,
        entity_ref=entity,
        action=f"Lower daily budget to {_money_of(evidence, 'proposed_daily_budget')}",
        reason=reason,
        evidence=evidence,
        rank_key=(-totals.spend,),
        flags=_subject_flags(ctx, subject, view),
        required_datasets=_datasets_of(evidence),
        increases_spend_on=(),
        decision_subject=DecisionSubject.of(subject),
        depends_on=_depends_on(ctx, members, (subject,)),
    )


# ---------------------------------------------------------------------------
# B4 Weekday / weekend pattern (AAP 0.5.4.5 B4 row)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Side:
    """One side (weekend or weekday) of a campaign's window days.

    Attributes:
        spend: Spend over the side's days.
        outcomes: Outcomes over the side's days, an unweighted event count.
        spend_days: Native dates with spend > 0.
    """

    spend: Decimal
    outcomes: int
    spend_days: int


def _side(days: Sequence[DayFact], group: str) -> _Side:
    """Total one side's days; outcomes are whole by the time this is called."""
    outcomes = sum((day.outcome(group) or _ZERO for day in days), _ZERO)
    return _Side(
        spend=sum((day.cost for day in days), _ZERO),
        outcomes=int(outcomes),
        spend_days=sum(1 for day in days if day.cost > 0),
    )


def _b4_withheld(
    entity: EntityRef, reason: str, observed: Number | None, required: Number | None, unit: str
) -> Withheld:
    """One B4 withheld line for a campaign."""
    return Withheld(
        rule_id=B4,
        section=BUDGET,
        entity=entity,
        reason=reason,
        observed=observed,
        required=required,
        unit=unit,
    )


def _counting_validity(
    ctx: AnalysisContext, campaign: CampaignInfo, entity: EntityRef, days: Sequence[DayFact]
) -> str | Withheld:
    """Establish that a campaign's outcomes are unweighted event counts, before any test.

    LinkedIn ``oneClickLeads`` and ``externalWebsiteConversions`` are counts by definition.
    Google conversions need the verified counting contract on every window day
    (:func:`~ads_engine.analysis.metrics.counting_contract`): every included conversion action
    whole-day known with last-click attribution. Integrality of every daily value is an
    additional check on top of the contract, never a substitute for it, and a weighted or
    fractional value is never rounded into the test.

    Returns:
        The contract text for the evidence, or the withheld line naming why the rate test is
        not valid: weighted attribution first (it is definitive), then an unknown contract,
        then fractional daily values.
    """
    label = OUTCOME_LABELS[campaign.objective_group]
    if campaign.source != "google":
        return f"LinkedIn {label} are unweighted event counts"
    contract = counting_contract(ctx)
    total = len(contract)
    weighted = sorted(day for day, kind in contract.items() if kind == "weighted")
    unknown = sorted(day for day, kind in contract.items() if kind == "unknown")
    last_click = total - len(weighted) - len(unknown)
    if weighted:
        return _b4_withheld(
            entity,
            f"{WEIGHTED_REASON} (data-driven or external attribution on {len(weighted)} of "
            f"{total} days, first {weighted[0].isoformat()})",
            last_click,
            total,
            "last-click days",
        )
    if unknown or total == 0:
        first = f", first {unknown[0].isoformat()}" if unknown else ""
        return _b4_withheld(
            entity,
            f"{CONTRACT_UNKNOWN_REASON} ({len(unknown)} of {total} days not whole-day known"
            f"{first})",
            last_click,
            total,
            "last-click days",
        )
    fractional = sorted(
        day.date
        for day in days
        if (value := day.outcome(campaign.objective_group)) is not None
        and value != value.to_integral_value()
    )
    if fractional:
        return _b4_withheld(
            entity,
            f"{FRACTIONAL_REASON} on {len(fractional)} of {len(days)} days (first "
            f"{fractional[0].isoformat()}): rate test not valid",
            len(days) - len(fractional),
            len(days),
            "whole-number days",
        )
    return f"all included actions last-click on all {total} days"


def _b4(ctx: AnalysisContext, settings: _Settings) -> RuleResult:
    """B4 Weekday / weekend pattern, per active campaign.

    AAP 0.5.4.5 B4. Each campaign's window days are split on its source's native dates
    (Google in the account's time zone, LinkedIn UTC; Saturday and Sunday are the weekend),
    and the evidence names that boundary. In order:

    1. **Exposure**: zero spend on either side is withheld "no exposure".
    2. **Count validity** (:func:`_counting_validity`): "weighted attribution: rate test not
       valid", "conversion counting contract unknown" or "fractional conversions".
    3. **Sample minimums**: at least ``b4.min_weekend_days`` (4) weekend days and
       ``b4.min_weekdays`` (10) weekdays with spend, and ``b4.min_outcomes`` (10) outcomes in
       total; shortfalls are withheld with their sample sizes, for example "3 weekend days of
       4 required; 6 outcomes of 10 required".
    4. **Test**: the two-sample Poisson rate test of outcomes per unit of spend
       (:func:`~ads_engine.analysis.stats.poisson_rate_test`); the pattern is an item when
       p < ``b4.alpha`` (0.05), and otherwise withheld with its p-value and sample sizes.

    Items offer only "Mark done" (no decision subject) and raise no spend.

    Rationale: "Weekend/weekday or other time-based patterns surfaced only when there's enough
    sample size to trust them, with the sample size shown"; the Poisson test assumes whole,
    unweighted events, so weighted conversions are withheld rather than rounded.
    """
    campaigns = sorted(
        (
            campaign
            for campaign in ctx.campaigns.values()
            if campaign.status == "active" and campaign.source in _METRICS_DATASETS
        ),
        key=lambda campaign: campaign.id,
    )
    if not campaigns:
        return RuleResult()
    window = ctx.window
    if not window.sustained_ok:
        return RuleResult(withheld=(_window_withheld(B4, window.days, settings.sustained_min),))
    candidates: list[Candidate] = []
    withheld: list[Withheld] = []
    for campaign in campaigns:
        found = _b4_campaign(ctx, settings, campaign)
        if isinstance(found, Candidate):
            candidates.append(found)
        elif isinstance(found, Withheld):
            withheld.append(found)
    return RuleResult(candidates=tuple(candidates), withheld=tuple(withheld))


def _b4_campaign(
    ctx: AnalysisContext, settings: _Settings, campaign: CampaignInfo
) -> Candidate | Withheld | None:
    """Evaluate B4 for one campaign; ``None`` when it has no spend or reports no outcome."""
    window = ctx.window
    days = [
        day
        for day in ctx.campaign_days.get(campaign.id, ())
        if window.start <= day.date <= window.end
    ]
    group = campaign.objective_group
    if not days or sum((day.cost for day in days), _ZERO) <= 0:
        return None
    reported = [day for day in days if day.outcome(group) is not None]
    if not reported:
        return None
    entity = _campaign_ref(campaign)
    weekend_days = [day for day in days if day.date.weekday() in _WEEKEND]
    weekday_days = [day for day in days if day.date.weekday() not in _WEEKEND]
    for name, side_days in (("weekend", weekend_days), ("weekday", weekday_days)):
        if sum((day.cost for day in side_days), _ZERO) <= 0:
            return _b4_withheld(
                entity,
                f"{NO_EXPOSURE_REASON}: zero {name} spend in the window",
                0,
                1,
                f"{name} days with spend",
            )
    label = OUTCOME_LABELS[group]
    if len(reported) < len(days):
        return _b4_withheld(
            entity,
            f"{label} unavailable on {len(days) - len(reported)} of {len(days)} days",
            len(reported),
            len(days),
            f"days with {label} reported",
        )
    validity = _counting_validity(ctx, campaign, entity, days)
    if isinstance(validity, Withheld):
        return validity
    weekend = _side(weekend_days, group)
    weekday = _side(weekday_days, group)
    total = weekend.outcomes + weekday.outcomes
    checks = (
        (weekend.spend_days, settings.b4_min_weekend_days, "weekend days"),
        (weekday.spend_days, settings.b4_min_weekdays, "weekdays"),
        (total, settings.b4_min_outcomes, "outcomes"),
    )
    shortfalls = [check for check in checks if check[0] < check[1]]
    if shortfalls:
        observed, required, unit = shortfalls[0]
        return _b4_withheld(
            entity,
            "; ".join(f"{have} {what} of {need} required" for have, need, what in shortfalls),
            observed,
            required,
            unit,
        )
    test = poisson_rate_test(weekend.outcomes, weekend.spend, weekday.outcomes, weekday.spend)
    if test is None:
        return _b4_withheld(
            entity,
            "rate test not estimable: no outcomes on either side",
            total,
            settings.b4_min_outcomes,
            "outcomes",
        )
    if test.p_value >= settings.b4_alpha:
        return _b4_withheld(
            entity,
            f"weekend and weekday rates not distinguishable ({B4_TEST_NAME} "
            f"{_p_value(test.p_value)}, alpha {_number(settings.b4_alpha)}): "
            f"{weekend.spend_days} weekend days, {weekday.spend_days} weekdays, {total} outcomes",
            total,
            settings.b4_min_outcomes,
            "outcomes",
        )
    return _b4_candidate(ctx, settings, campaign, entity, (weekend, weekday), test, validity)


def _b4_budget_metrics(
    ctx: AnalysisContext, campaign: CampaignInfo, currency: str
) -> tuple[Metric, Metric]:
    """The daily budget and window utilization shown beside a B4 pattern.

    A campaign with its own daily budget shows its budget history's latest effective budget
    and utilization; a shared-budget member or a lifetime budget shows utilization "n/a",
    because it has no daily cap of its own.
    """
    subject: SubjectKey = ("campaign", campaign.id)
    if subject in budget_subjects(ctx):
        history = budget_history(ctx, subject)
        utilization: Decimal | str = (
            "n/a" if history.window_utilization is None else _share(history.window_utilization)
        )
        daily = history.latest_daily_budget
    else:
        utilization = "n/a"
        daily = campaign.daily_budget
    return (
        Metric("daily_budget", "Latest effective daily budget", daily, "currency", currency),
        Metric("window_utilization", "Window utilization (spend vs cap)", utilization, "ratio"),
    )


def _b4_candidate(
    ctx: AnalysisContext,
    settings: _Settings,
    campaign: CampaignInfo,
    entity: EntityRef,
    sides: tuple[_Side, _Side],
    test: RateTest,
    contract: str,
) -> Candidate:
    """The B4 item of one campaign whose weekend and weekday rates differ (p < alpha)."""
    weekend, weekday = sides
    currency = ctx.account_currency(campaign.id)
    label = OUTCOME_LABELS[campaign.objective_group]
    singular = _singular(label)
    totals = window_totals(ctx, (campaign.id,))
    baseline = baseline_cpo(ctx, campaign.account_id, campaign.objective_group)
    cpo = cost_per_outcome(totals.spend, totals.outcomes)
    weekend_cpo = cost_per_outcome(weekend.spend, Decimal(weekend.outcomes))
    weekday_cpo = cost_per_outcome(weekday.spend, Decimal(weekday.outcomes))
    view = budget_crm(ctx, ("campaign", campaign.id))
    boundary = _day_boundary(ctx, campaign.source)
    source_label = "Google" if campaign.source == "google" else "LinkedIn"
    metrics: list[Metric] = [
        Metric("spend", "Window spend", totals.spend, "currency", currency),
        Metric("window_days", "Window days", ctx.window.days, "days"),
        Metric("outcomes", f"Window {label}", totals.outcomes, label),
        Metric(
            "cost_per_outcome",
            f"Cost per {singular}",
            None if cpo is None else _q(cpo),
            "currency",
            currency,
        ),
        Metric("weekend_days", "Weekend days with spend", weekend.spend_days, "days"),
        Metric("weekdays", "Weekdays with spend", weekday.spend_days, "days"),
        Metric("weekend_spend", "Weekend spend", weekend.spend, "currency", currency),
        Metric("weekday_spend", "Weekday spend", weekday.spend, "currency", currency),
        Metric("weekend_outcomes", f"Weekend {label}", weekend.outcomes, label),
        Metric("weekday_outcomes", f"Weekday {label}", weekday.outcomes, label),
        Metric(
            "weekend_cpo",
            f"Weekend cost per {singular}",
            None if weekend_cpo is None else _q(weekend_cpo),
            "currency",
            currency,
        ),
        Metric(
            "weekday_cpo",
            f"Weekday cost per {singular}",
            None if weekday_cpo is None else _q(weekday_cpo),
            "currency",
            currency,
        ),
        Metric("weekend_rate", f"Weekend {label} per unit of spend", test.rate_a, "rate"),
        Metric("weekday_rate", f"Weekday {label} per unit of spend", test.rate_b, "rate"),
        *_b4_budget_metrics(ctx, campaign, currency),
    ]
    if view.linked:
        metrics.append(
            Metric(
                "eligible_contacts",
                "Eligible contacts (matured leads)",
                view.eligible_contacts,
                "contacts",
            )
        )
        metrics.append(Metric("ql", "Qualified Leads (CRM)", view.ql, "contacts"))
    total = weekend.outcomes + weekday.outcomes
    evidence = ctx.make_evidence(
        required_datasets=_required(_ad_datasets(campaign.source), view.datasets),
        crm_scope=view.scope if view.linked else None,
        metrics=metrics,
        baselines=(
            Baseline(
                "baseline_cost_per_outcome",
                f"Account cost per {singular} (same source and objective)",
                None if baseline is None else _q(baseline),
            ),
        ),
        sample=Sample(total, settings.b4_min_outcomes, "outcomes"),
        test=TestResult(B4_TEST_NAME, test.p_value, None, settings.b4_alpha),
        threshold_keys=_required(B4_THRESHOLD_KEYS, VETO_THRESHOLD_KEYS if view.linked else ()),
        extra={
            "contract": contract,
            "method": test.method,
            "day_boundary": {"source": campaign.source, "boundary": boundary},
            "weekend_definition": (
                f"Saturday and Sunday on {source_label} native dates ({boundary})"
            ),
            "gate": {
                "weekend_days": {
                    "observed": weekend.spend_days,
                    "required": settings.b4_min_weekend_days,
                },
                "weekdays": {
                    "observed": weekday.spend_days,
                    "required": settings.b4_min_weekdays,
                },
                "outcomes": {"observed": total, "required": settings.b4_min_outcomes},
            },
        },
    )
    weekend_text = _cost_of(evidence, "weekend_cpo")
    weekday_text = _cost_of(evidence, "weekday_cpo")
    recorded = evidence.test
    p_value = test.p_value if recorded is None or recorded.p_value is None else recorded.p_value
    alpha = settings.b4_alpha if recorded is None else recorded.alpha
    reason = (
        f"Weekend cost per {singular} {weekend_text} over {_value(evidence, 'weekend_days')} "
        f"weekend days vs weekday {weekday_text} over {_value(evidence, 'weekdays')} weekdays "
        f"({_value(evidence, 'weekend_outcomes')} weekend and "
        f"{_value(evidence, 'weekday_outcomes')} weekday {label}; {B4_TEST_NAME} "
        f"{_p_value(p_value)}, below alpha {_number(alpha)}); "
        f"{source_label} days end at midnight {boundary}; {evidence.extra['contract']}."
    )
    return Candidate(
        rule_id=B4,
        section=BUDGET,
        group=GROUP_CHANGES,
        entity_ref=entity,
        action=(
            f"Adjust weekend schedule or bids: weekend cost/outcome {weekend_text} vs weekday "
            f"{weekday_text}"
        ),
        reason=reason,
        evidence=evidence,
        rank_key=(-totals.spend,),
        flags=view.flags,
        required_datasets=_datasets_of(evidence),
        increases_spend_on=(),
        decision_subject=None,
        depends_on=_depends_on(ctx, (campaign.id,)),
    )


# ---------------------------------------------------------------------------
# Section findings and evaluate
# ---------------------------------------------------------------------------


def _findings(ctx: AnalysisContext, result: RuleResult) -> tuple[Finding, ...]:
    """The Budget section's ``no_crm_link`` finding, once any Google item lacks the CRM link.

    AAP 0.3.3: without the verified Google-to-CRM link no Google Qualified Lead count is
    computed for budget cards, and a LinkedIn-derived subset never stands in.
    """
    flagged = sum(1 for item in result.candidates if item.has_flag(FLAG_NO_CRM_LINK))
    gap = _google_link_gap(ctx)
    if not flagged or gap is None:
        return ()
    return (
        Finding(
            FINDING_NO_CRM_LINK,
            BUDGET,
            None,
            f"No CRM link for Google campaigns: {gap}. Google budget items show no Qualified "
            "Lead counts.",
            {"reason": gap, "items": flagged},
        ),
    )


def evaluate(ctx: AnalysisContext) -> RuleResult:
    """Evaluate the "Budget changes" rules B1, B2, B3 and B4.

    AAP 0.5.4.5. Every item sits in the ``changes`` group and is ranked by window spend,
    descending (``rank_key = (-window spend,)``; ties by entity name, then key, in
    ``precedence.py``); withheld patterns follow the items. Items come in rule order: B1 (one
    per C2 "Scale", carrying the amount and declaring its G1 targets), B2 (do not increase;
    prefilled 20% decrease), B3 (under-utilized; ``extra.freed`` for the allocation) and B4
    (weekday / weekend; "Mark done" only).

    ``precedence.py`` then applies G1 (removing any increase on a target that meets B2,
    counted as ``g1_blocked``), allocates B3 freed amounts to surviving B1 receivers with
    :func:`render_b3_allocation` and :func:`render_b1_funding`, applies P1-P8 and feedback,
    and ranks.

    Findings: ``no_crm_link`` once any Google budget item lacks the CRM link.

    Threshold keys: :data:`B1_THRESHOLD_KEYS`, :data:`B2_RULE_THRESHOLD_KEYS`,
    :data:`B3_THRESHOLD_KEYS`, :data:`B4_THRESHOLD_KEYS` and, where CRM counts are shown, the
    CRM veto keys, each copied into the evidence of the items that applied it.

    Rationale: every budget recommendation rests on a sustained multi-day pattern and shows
    its window, its utilization beside its efficiency, and its sample against its minimum.

    Raises:
        ConfigError: When a threshold key is missing or mistyped.
    """
    settings = _Settings.of(ctx)
    result = RuleResult().merge(_b1(ctx, settings), _b2_b3(ctx, settings), _b4(ctx, settings))
    findings = _findings(ctx, result)
    if not findings:
        return result
    return result.merge(RuleResult(findings=findings))
