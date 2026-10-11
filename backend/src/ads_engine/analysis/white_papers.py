"""Rules W1 and W2 and the White papers asset rows, one currency cohort at a time.

AAP 0.5.4.7 ("Metrics per asset", "Never blended", "Completeness and floors", "Actions and
ranking"), 0.5.4.2 (G1: W1 names only the campaigns the guard leaves unblocked) and 0.5.5 (the
white-paper table) for FR-8. Every number is computed by :mod:`ads_engine.analysis.attribution`;
this module only decides each asset row's action and builds its item:

* **Insufficient data** (rule ``W``): when a cohort's assets together have fewer than
  ``w.cohort_min_total_ql`` Qualified Leads, the Qualified Lead count is unavailable, or no
  asset reaches ``w.median_min_ql`` QL, the cohort median cost per Qualified Lead is too
  unstable (or undefined) to compare against, so every row of the cohort shows "Insufficient
  data" with its numbers.
* **W1 Double down**: QL >= ``w.min_ql`` and cost per QL <= ``w.double_down_ratio`` x the cohort
  median. The item names the campaigns whose spend feeds the asset, minus every campaign G1
  blocks (:func:`ads_engine.analysis.metrics.spend_increase_blocked`); when none remain W1 is
  withheld ("every contributing campaign is blocked by G1") and the row shows "No change
  warranted".
* **W2 Retire or rework**: QL = 0 and spend >= ``w.retire_spend_multiple`` x the cohort median,
  over a window of at least ``w.retire_min_window_days``, with the QL stage available, no
  contact-share floor (``attribution.floor_share``) and no contact with unknown CRM evidence.
  When the spend multiple is met but a guard fails, W2 is withheld with that guard's reason and
  the row shows "No change warranted". The comparison needs a positive cohort median: at a
  median of 0, W2 is withheld ("spend-to-median comparison unavailable") and the row shows "No
  change warranted".
* **Otherwise** (rule ``W``): "No change warranted", with its numbers.

Every asset row of every cohort therefore yields exactly one item, so the White papers table
lists every asset. Items carry no decision subject ("Mark done" only), sit in the group
:func:`~ads_engine.analysis.context.cohort_group` of their currency (titled with the currency)
and depend (P1) on every campaign whose spend feeds the row.

Never blended (FR-8): MQL and Qualified Lead are separate metrics; no metric, total or rank key
combines them. LinkedIn leads are labelled a platform count. Cost per lead is cost per
Qualified Lead only. Nothing is totalled or compared across currencies, and across assets only
spend is totalled ("counts overlap across assets").

Cross-asset view: each item's ``evidence.extra.cohort`` carries its cohort's spend total, the
any-touch overlap note and the median the row was compared against. It rides on the rows rather
than on a section finding, because the AAP 0.5.3 finding codes are a closed set with no code
for it; the attribution findings (``pipeline_unavailable``, ``skipped_stage``) pass through.

Purity: reads only the :class:`~ads_engine.analysis.context.AnalysisContext`; no database,
network, settings or logging access.

Example:
    >>> result = evaluate(ctx)  # doctest: +SKIP
    >>> [(item.rule_id, item.action) for item in result.candidates][:2]  # doctest: +SKIP
    [('W1', 'Double down: increase spend on Pipeline Report - Lead gen'),
     ('W', 'No change warranted')]
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final

from ads_engine.analysis.attribution import (
    BOOKED_LABEL,
    OTHER_MEETINGS_LABEL,
    AssetRow,
    AttributionTable,
    Cohort,
    CostValue,
    CountValue,
    attribution_for,
    bounded_decimal,
)
from ads_engine.analysis.context import (
    W1,
    W2,
    WHITE_PAPERS,
    AnalysisContext,
    Baseline,
    Candidate,
    DecisionSubject,
    Finding,
    Metric,
    RuleResult,
    Sample,
    W,
    Withheld,
    cohort_group,
)
from ads_engine.analysis.metrics import G1_THRESHOLD_KEYS, spend_increase_blocked
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.decisions import minor_unit_exponent, round_to_minor_unit

__all__ = [
    "ACTION_DOUBLE_DOWN_PREFIX",
    "ACTION_INSUFFICIENT_DATA",
    "ACTION_NO_CHANGE",
    "ACTION_RETIRE",
    "METRIC_KEYS",
    "REQUIRED_DATASETS",
    "SECTION_DATASETS",
    "THRESHOLD_KEYS",
    "W1_ALL_BLOCKED_REASON",
    "evaluate",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACTION_DOUBLE_DOWN_PREFIX: Final = "Double down: increase spend on "
"""W1 headline prefix; the unblocked campaign names follow, comma-separated."""

ACTION_RETIRE: Final = "Retire or rework this white paper"
"""W2 headline."""

ACTION_NO_CHANGE: Final = "No change warranted"
"""Rule ``W`` headline of a row that meets neither W1 nor W2."""

ACTION_INSUFFICIENT_DATA: Final = "Insufficient data"
"""Rule ``W`` headline of every row of a cohort whose median cost per QL cannot be trusted."""

W1_ALL_BLOCKED_REASON: Final = "every contributing campaign is blocked by G1"
"""The withheld reason of a W1 whose every contributing campaign G1 blocks (AAP 0.5.4.2)."""

REQUIRED_DATASETS: Final[tuple[str, ...]] = (
    "linkedin.analytics_daily",
    "linkedin.leads",
    "linkedin.entities",
    "hubspot.contacts",
    "hubspot.associations",
    "hubspot.deal_search",
)
"""The datasets every white-paper item reads; ``hubspot.*`` resolve with ``crm_scope="linkedin"``.

That is the effective CRM dataset of the LinkedIn dataset in use: the companion while LinkedIn
is simulated, HubSpot's active primary otherwise (AAP 0.3.3), so an item's ``stale`` and
``incomplete`` flags derive from the CRM data its counts were actually read from.
"""

SECTION_DATASETS: Final[tuple[str, ...]] = REQUIRED_DATASETS
"""Every dataset the White papers section reads: exactly :data:`REQUIRED_DATASETS`.

Precedence judges these over the run's window when the section stores no item, so an empty
section says which of its datasets do not cover the window (AAP 0.5.5 "Each section shows an
EmptyState with the reason").
"""

THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "w.min_ql",
    "w.median_min_ql",
    "w.double_down_ratio",
    "w.retire_spend_multiple",
    "w.retire_min_window_days",
    "w.cohort_min_total_ql",
    "attribution.floor_share",
    "crm.booking_sources",
    "crm.meeting_outcome_exclusions",
)
"""Every threshold the rows depend on, copied into each item's ``evidence.thresholds``.

The ``w.*`` keys decide the action; ``attribution.floor_share`` sets the floors and the W2
contact-share guard; ``crm.booking_sources`` and ``crm.meeting_outcome_exclusions`` decide
which meetings count as booked, the denominator of cost per meeting. An item whose decision
ran G1 over at least one contributing campaign (the W1 double down, and the "No change
warranted" row whose W1 is withheld because G1 blocks every contributing campaign) also
carries :data:`~ads_engine.analysis.metrics.G1_THRESHOLD_KEYS`, the controls that selected or
left out its spend targets.
"""

METRIC_KEYS: Final[tuple[str, ...]] = (
    "spend",
    "linkedin_leads",
    "mql",
    "ql",
    "meetings_booked",
    "other_meetings",
    "deals_after_touch",
    "cost_per_ql",
    "cost_per_meeting",
)
"""Evidence metric keys in the White papers table's column order (AAP 0.5.5).

``mql`` and ``ql`` are separate metrics: no key, label or value combines them (FR-8).
"""

_CRM_SCOPE: Final = "linkedin"
"""LinkedIn-attributed evidence reads the effective CRM dataset (AAP 0.3.3)."""

_RATIO_PLACES: Final = Decimal("0.0001")
"""Precision of a displayed ratio or multiple; trailing zeros are dropped (``10``, ``2.5``)."""

_HUNDRED: Final = Decimal(100)
_PERCENT_PLACES: Final = Decimal("0.1")
_DASH: Final = "—"
"""A cost whose known denominator is 0 (AAP 0.5.4.7): never a finite value or a ceiling."""

_RANK_WITH_QL: Final = 0
"""Rank class of rows with QL >= 1: ranked by cost per QL, ascending."""

_RANK_WITHOUT_QL: Final = 1
"""Rank class of rows with QL = 0 (or unavailable): ranked by spend, descending."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Settings:
    """The W thresholds, read once per evaluation; there are no code defaults."""

    min_ql: int
    median_min_ql: int
    double_down_ratio: Decimal
    retire_multiple: Decimal
    retire_min_days: int
    cohort_min_total_ql: int
    floor_share: Decimal

    @classmethod
    def of(cls, thresholds: Thresholds) -> _Settings:
        """Read the ``w.*`` keys and ``attribution.floor_share``.

        Raises:
            ConfigError: When a key is missing or has the wrong type.
        """
        return cls(
            min_ql=thresholds.int("w.min_ql"),
            median_min_ql=thresholds.int("w.median_min_ql"),
            double_down_ratio=thresholds.ratio("w.double_down_ratio"),
            retire_multiple=thresholds.ratio("w.retire_spend_multiple"),
            retire_min_days=thresholds.int("w.retire_min_window_days"),
            cohort_min_total_ql=thresholds.int("w.cohort_min_total_ql"),
            floor_share=thresholds.ratio("attribution.floor_share"),
        )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _bounded(value: Decimal) -> Decimal:
    """Bound a derived amount to the stored money precision without padding coarser values.

    ``Decimal("10.00")`` stays ``10.00``; ``2766 / 31`` becomes ``89.225806``, so evidence never
    carries a 28-digit quotient. It delegates to
    :func:`~ads_engine.analysis.attribution.bounded_decimal`, the rule the attribution JSON in
    ``extra.costs``, ``extra.rows`` and ``extra.orphan_deals`` applies, so a metric and its
    detail always cite one value.
    """
    return bounded_decimal(value)


def _plain(value: Decimal) -> str:
    """Render a ratio or multiple without trailing zeros: ``0.80`` -> ``"0.8"``."""
    return format(_plain_decimal(value), "f")


def _plain_decimal(value: Decimal) -> Decimal:
    """Round a ratio or multiple to four decimals and drop trailing zeros (``10.0000`` -> ``10``).

    Display only: every threshold comparison is made on exact values.
    """
    rounded = value.quantize(_RATIO_PLACES, rounding=ROUND_HALF_EVEN)
    return rounded.normalize() if rounded else Decimal(0)


def _percent(share: Decimal) -> str:
    """Render a share in [0, 1] as a percentage with at most one decimal: ``0.1`` -> ``"10"``."""
    value = (share * _HUNDRED).quantize(_PERCENT_PLACES, rounding=ROUND_HALF_EVEN)
    return format(value.normalize() if value else Decimal(0), "f")


def _money(amount: Decimal, currency: str) -> str:
    """Render an amount with its currency at the currency's minor unit: ``USD 2,766.00``.

    The places are the ISO 4217 minor unit of :func:`ads_engine.decisions.minor_unit_exponent`,
    the unit decided amounts are validated to. The amount is rounded by
    :func:`ads_engine.decisions.round_to_minor_unit` (half up: a tie rounds away from zero) and
    grouped in thousands, as the campaign and budget reason texts render money and as the web
    client's ``formatMoney`` renders the same stored value in the table cell, so a white-paper
    reason and its cell read alike: a cost of ``89.225`` is ``USD 89.23`` in both.

    Example:
        >>> [_money(Decimal("30"), code) for code in ("USD", "JPY", "KWD", "IQD", "AFN")]
        ['USD 30.00', 'JPY 30', 'KWD 30.000', 'IQD 30.000', 'AFN 30.00']
        >>> _money(Decimal("2766"), "USD"), _money(Decimal("89.225"), "USD")
        ('USD 2,766.00', 'USD 89.23')
    """
    places = minor_unit_exponent(currency)
    rendered = format(round_to_minor_unit(amount, currency), f",.{places}f")
    return f"{currency} {rendered}"


def _cost_text(cost: CostValue, currency: str) -> str:
    """Render a cost for a reason sentence: ``USD 10.00``, ``≤ USD 10.00``, ``— (no QL yet)``."""
    if cost.unavailable:
        return "unavailable"
    if cost.value is None:
        return f"{_DASH} ({cost.note})" if cost.note else _DASH
    text = _money(cost.value, currency)
    return f"≤ {text}" if cost.ceiling else text


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """Return ``"1 contact"`` / ``"2 contacts"``."""
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _count_text(count: CountValue, singular: str) -> str:
    """Render a CRM count with its noun, keeping a floor: ``≥ 5 Qualified Leads``."""
    noun = singular if count.value == 1 else f"{singular}s"
    return f"{count.render()} {noun}"


def _mql_label(row: AssetRow) -> str:
    """Return the MQL column header with its bound stage label: ``MQL (First meeting booked)``.

    A label that only repeats the stage name ("MQL", in any case) is not parenthesised.
    """
    label = (row.mql_label or "").strip()
    return f"MQL ({label})" if label and label.casefold() != "mql" else "MQL"


# ---------------------------------------------------------------------------
# Evidence: metrics in the table's column order, never blending MQL and QL
# ---------------------------------------------------------------------------


def _count_metric(key: str, label: str, count: CountValue, unit: str) -> Metric:
    """Return a CRM count as a metric: ``None`` when unavailable, ``floor`` under a floor."""
    if count.unavailable or count.value is None:
        return Metric(key, label, None, unit)
    return Metric(key, label, count.value, unit, floor=count.floor)


def _cost_metric(key: str, label: str, cost: CostValue, currency: str) -> Metric:
    """Return a cost as a metric within one currency (AAP 0.5.4.7 costs).

    An unavailable denominator gives ``None`` ("unavailable"); a known denominator of 0 gives
    "—" (its note, "no QL yet" or "no meetings yet", is in ``extra.costs``); a cost over a
    floored denominator is a ceiling, rendered "≤ X" through ``floor``.
    """
    if cost.unavailable:
        return Metric(key, label, None, "currency", currency=currency)
    if cost.value is None:
        return Metric(key, label, _DASH, "currency", currency=currency)
    return Metric(
        key, label, _bounded(cost.value), "currency", currency=currency, floor=cost.ceiling
    )


def _row_metrics(row: AssetRow) -> tuple[Metric, ...]:
    """Return the row's metrics in :data:`METRIC_KEYS` order (AAP 0.5.5 column order)."""
    currency = row.currency
    return (
        Metric("spend", "Spend", row.spend, "currency", currency=currency),
        Metric("linkedin_leads", "LinkedIn leads (platform)", row.linkedin_leads, "leads"),
        _count_metric("mql", _mql_label(row), row.mql, "contacts"),
        _count_metric("ql", "Qualified Lead", row.ql, "contacts"),
        _count_metric(
            "meetings_booked", f"Meetings {BOOKED_LABEL}", row.meetings_booked, "meetings"
        ),
        _count_metric("other_meetings", OTHER_MEETINGS_LABEL, row.other_meetings, "meetings"),
        _count_metric("deals_after_touch", "Deals after touch", row.deals_after_touch, "deals"),
        _cost_metric("cost_per_ql", "Cost per Qualified Lead", row.cost_per_ql, currency),
        _cost_metric("cost_per_meeting", "Cost per meeting booked", row.cost_per_meeting, currency),
    )


def _share_json(share: Decimal | None, part: int, whole: int | None) -> dict[str, Any]:
    """Return one completeness share (``None`` without a denominator) with its counts."""
    return {"share": None if share is None else _bounded(share), "count": part, "of": whole}


def _row_extra(ctx: AnalysisContext, table: AttributionTable, row: AssetRow) -> dict[str, Any]:
    """Return the row detail every white-paper item carries in ``evidence.extra``.

    ``rows`` is the per-creative partition (including "unknown creative"), ``shares`` the
    contact, unknown-evidence and creative shares, ``orphan_deals`` the portal orphan-deal
    share (or its "unknown (…)" label), ``already_before`` and ``skipped`` the events
    the causal check and the stage binding excluded, and ``costs`` each cost with its note.
    """
    return {
        "asset": {"slug": row.slug, "title": row.title, "currency": row.currency},
        "mql_label": row.mql_label,
        "ql_label": row.ql_label,
        "rows": [credit.to_json() for credit in row.creative_rows],
        "shares": {
            "contact": _share_json(row.contact_share, row.unmatched_leads, row.linkedin_leads),
            "unknown_evidence": _share_json(
                row.unknown_evidence_share, row.unknown_evidence_contacts, row.matched_contacts
            ),
            "creative": _share_json(
                row.creative_share, row.leads_without_creative, row.linkedin_leads
            ),
        },
        "orphan_deals": table.orphan_deals.to_json(),
        "already_before": {
            "mql": row.already_mql_before,
            "ql": row.already_ql_before,
            "meetings": row.meetings_before_touch,
            "deals": row.deals_before_touch,
        },
        "skipped": {"mql": row.skipped_mql, "ql": row.skipped_ql},
        "costs": {
            "cost_per_ql": row.cost_per_ql.to_json(),
            "cost_per_meeting": row.cost_per_meeting.to_json(),
        },
        "campaigns": [
            {"id": campaign_id, "name": ctx.campaign_name(campaign_id)}
            for campaign_id in row.campaign_ids
        ],
    }


def _cohort_extra(cohort: Cohort, settings: _Settings) -> dict[str, Any]:
    """Return the cross-asset view of a cohort, carried by each of its rows.

    Only spend is totalled across assets, within the cohort's currency: a contact who touched
    two assets is credited to both (any-touch), so no MQL, QL, meeting or deal total is shown,
    and the note says so (AAP 0.5.4.7 "Credit and de-duplication"). ``retire_spend_floor`` (W2's
    spend threshold) is set only for a positive median: at a median of 0 W2's spend-to-median
    comparison is unavailable, so no floor of 0 is shown.
    """
    median = cohort.median_cost_per_ql
    view: dict[str, Any] = {
        "currency": cohort.currency,
        "assets": len(cohort.rows),
        "spend_total": cohort.spend_total,
        "overlap_note": cohort.overlap_note,
        "median_cost_per_ql": None,
        "double_down_ceiling": None,
        "retire_spend_floor": None,
    }
    if median is not None:
        view["median_cost_per_ql"] = _bounded(median)
        view["double_down_ceiling"] = _bounded(settings.double_down_ratio * median)
        if median > 0:
            view["retire_spend_floor"] = _bounded(settings.retire_multiple * median)
    return view


# ---------------------------------------------------------------------------
# Decisions: Insufficient data, W1, W2, No change warranted
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Decision:
    """One asset row's outcome: its rule, headline and reason, and what it adds to evidence.

    Attributes:
        rule_id: ``W1``, ``W2`` or ``W``.
        action: The headline.
        reason: The reason sentence, filled only from numbers the item's evidence carries.
        increases: The campaigns a W1 double down raises spend on (``increases_spend_on``).
        sample: The sample of a gate the decision passed, against its minimum; ``None`` when
            no gate passed. A gate the row falls short of is never its sample (P6): the
            shortfall goes to ``extra`` and, where the pattern is otherwise met, to
            ``withheld``.
        withheld: The W1 or W2 patterns withheld for this row, with their reasons.
        extra: Decision detail merged into ``evidence.extra``.
        threshold_keys: Keys the decision applied beyond :data:`THRESHOLD_KEYS`:
            :data:`~ads_engine.analysis.metrics.G1_THRESHOLD_KEYS` when G1 judged at least one
            contributing campaign.
    """

    rule_id: str
    action: str
    reason: str
    increases: tuple[int, ...] = ()
    sample: Sample | None = None
    withheld: tuple[Withheld, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)
    threshold_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Insufficiency:
    """Why a cohort's median cost per Qualified Lead cannot be compared against.

    Attributes:
        reason: The reason sentence shared by every row of the cohort.
        withheld_reason: The reason of the cohort's withheld W1/W2 line.
        total_ql: The cohort's QL across its assets (the gate input); ``None`` when unavailable.
        required: ``w.cohort_min_total_ql``.
    """

    reason: str
    withheld_reason: str
    total_ql: int | None
    required: int

    @property
    def cohort_sample(self) -> dict[str, Any]:
        """The cohort QL against its minimum, for ``evidence.extra.cohort_sample``.

        Observational only: a cohort that falls short of the minimum is a failed gate, so its
        rows carry no ``evidence.sample`` (P6); ``observed`` is ``None`` when the QL count is
        unavailable.
        """
        return {
            "observed": self.total_ql,
            "required": self.required,
            "unit": "Qualified Leads across the cohort's assets",
        }


def _insufficiency(cohort: Cohort, settings: _Settings) -> _Insufficiency | None:
    """Return why the cohort is "Insufficient data", or ``None`` when W1 and W2 can be judged.

    AAP 0.5.4.7: "when a cohort's assets together have fewer than 3 QL, the median is too
    unstable to compare against". The cohort QL total is only this gate's input, never a
    display total (counts overlap across assets). An unavailable QL count, or a cohort in which
    no asset reaches ``w.median_min_ql`` QL, leaves no median at all and is treated the same way.
    """
    currency = cohort.currency
    required = settings.cohort_min_total_ql
    if cohort.total_ql is None:
        cause = next(
            (row.ql.reason for row in cohort.rows if row.ql.unavailable and row.ql.reason), None
        )
        detail = f" ({cause})" if cause else ""
        return _Insufficiency(
            reason=(
                f"The Qualified Lead count is unavailable{detail}, so no {currency} white paper "
                "can be compared on cost per Qualified Lead."
            ),
            withheld_reason=(
                f"W1 and W2 not evaluated for the {currency} cohort: Qualified Lead count "
                f"unavailable{detail}"
            ),
            total_ql=None,
            required=required,
        )
    if cohort.total_ql < required:
        have = _plural(cohort.total_ql, "Qualified Lead")
        if any(row.ql.floor for row in cohort.rows):
            # A sum that includes a floor is itself a floor: the true total may be higher.
            have = f"≥ {have}"
        return _Insufficiency(
            reason=(
                f"The {currency} cohort's white papers have {have} together, fewer than the "
                f"{required} needed for a stable median cost per Qualified Lead."
            ),
            withheld_reason=(
                f"W1 and W2 not evaluated for the {currency} cohort: {have} of {required} "
                "required for a stable median cost per Qualified Lead"
            ),
            total_ql=cohort.total_ql,
            required=required,
        )
    if cohort.median_cost_per_ql is None:
        least = _plural(settings.median_min_ql, "Qualified Lead")
        return _Insufficiency(
            reason=(
                f"No {currency} white paper has at least {least}, so the cohort has no median "
                "cost per Qualified Lead."
            ),
            withheld_reason=(
                f"W1 and W2 not evaluated for the {currency} cohort: no asset with at least {least}"
            ),
            total_ql=cohort.total_ql,
            required=required,
        )
    return None


def _blocked_campaigns(ctx: AnalysisContext, campaign_ids: tuple[int, ...]) -> frozenset[int]:
    """Return the campaigns among ``campaign_ids`` that G1 blocks from any spend increase.

    A campaign is blocked when it is in :func:`spend_increase_blocked` itself (an own-budget
    B2 target, or a member of a blocked shared budget) or when the budget it draws from is.
    """
    blocked = spend_increase_blocked(ctx)
    result: set[int] = set()
    for campaign_id in campaign_ids:
        campaign = ctx.campaigns.get(campaign_id)
        budget_id = None if campaign is None else campaign.budget_id
        if ("campaign", campaign_id) in blocked or (
            budget_id is not None and ("budget", budget_id) in blocked
        ):
            result.add(campaign_id)
    return frozenset(result)


def _campaign_refs(ctx: AnalysisContext, campaign_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    return [{"id": cid, "name": ctx.campaign_name(cid)} for cid in campaign_ids]


def _no_change(
    reason: str,
    *,
    sample: Sample | None = None,
    withheld: tuple[Withheld, ...] = (),
    extra: dict[str, Any] | None = None,
    threshold_keys: tuple[str, ...] = (),
) -> _Decision:
    """Return the rule ``W`` "No change warranted" outcome."""
    detail = {} if extra is None else extra
    if withheld:
        detail = {**detail, "withheld_reasons": [item.reason for item in withheld]}
    return _Decision(
        W,
        ACTION_NO_CHANGE,
        reason,
        sample=sample,
        withheld=withheld,
        extra=detail,
        threshold_keys=threshold_keys,
    )


def _decide_with_ql(
    ctx: AnalysisContext, row: AssetRow, median: Decimal, ql: int, settings: _Settings
) -> _Decision:
    """Decide a row with at least one Qualified Lead: W1 Double down, or No change warranted.

    W1 needs QL >= ``w.min_ql`` and cost per QL <= ``w.double_down_ratio`` x the cohort
    median. Under a contact floor QL is a floor and cost per QL a ceiling, so both only move
    further in W1's favour; the floor flags travel with the item. G1 then removes every
    contributing campaign it blocks; with none left W1 is withheld.

    Below ``w.min_ql`` the row is "No change warranted" with no ``evidence.sample`` (P6): the
    shortfall is in ``extra.w1_sample``, and when the cost pattern holds W1 is withheld with
    the QL observed and required. Every other outcome passed the QL gate and carries it as its
    sample.
    """
    currency = row.currency
    cost = row.cost_per_ql
    cost_text = _cost_text(cost, currency)
    median_text = _money(median, currency)
    ceiling = settings.double_down_ratio * median
    ceiling_text = (
        f"{_money(ceiling, currency)} ({_plain(settings.double_down_ratio)} x the {currency} "
        f"cohort median of {median_text})"
    )
    ql_text = _count_text(row.ql, "Qualified Lead")
    if ql < settings.min_ql:
        # P6: a QL count below W1's minimum is a failed gate, never the item's sample. The
        # shortfall is kept in extra, and W1 is withheld with it when the cost pattern holds.
        below_minimum: tuple[Withheld, ...] = ()
        if cost.value is not None and cost.value <= ceiling:
            below_minimum = (
                Withheld(
                    rule_id=W1,
                    section=WHITE_PAPERS,
                    entity=row.entity,
                    reason=f"{ql_text} of the {settings.min_ql} W1 needs",
                    observed=ql,
                    required=settings.min_ql,
                    unit="Qualified Leads",
                ),
            )
        return _no_change(
            f"{ql_text}, below the {settings.min_ql} W1 needs; cost per Qualified Lead "
            f"{cost_text} against the {currency} cohort median of {median_text}.",
            withheld=below_minimum,
            extra={
                "w1_sample": {
                    "observed": ql,
                    "required": settings.min_ql,
                    "unit": "Qualified Leads",
                }
            },
        )
    sample = Sample(ql, settings.min_ql, "Qualified Leads")
    if cost.value is None or cost.value > ceiling:
        # A ceiling ("≤ X", under a floor) above the threshold does not prove the true cost is.
        verdict = "is not shown to be within" if cost.ceiling else "is above"
        return _no_change(
            f"Cost per Qualified Lead {cost_text} {verdict} the double-down ceiling of "
            f"{ceiling_text}, with {ql_text}.",
            sample=sample,
        )

    blocked = _blocked_campaigns(ctx, row.campaign_ids)
    targets = tuple(cid for cid in row.campaign_ids if cid not in blocked)
    left_out = tuple(cid for cid in row.campaign_ids if cid in blocked)
    extra: dict[str, Any] = {
        "double_down_campaigns": _campaign_refs(ctx, targets),
        "blocked_campaigns": _campaign_refs(ctx, left_out),
    }
    qualifies = (
        f"Cost per Qualified Lead {cost_text} is at or below the double-down ceiling of "
        f"{ceiling_text}, with {ql_text} (W1 needs {settings.min_ql})"
    )
    blocked_names = ", ".join(ctx.campaign_name(cid) for cid in left_out)
    # G1 judged the contributing campaigns, so the keys that selected or excluded the spend
    # targets are part of the evidence; with no contributing campaign G1 judged none.
    g1_keys = G1_THRESHOLD_KEYS if row.campaign_ids else ()
    if not targets:
        why = (
            W1_ALL_BLOCKED_REASON
            if row.campaign_ids
            else "no campaign with window spend feeds this white paper"
        )
        withheld = Withheld(
            rule_id=W1,
            section=WHITE_PAPERS,
            entity=row.entity,
            reason=why,
            observed=0,
            required=1,
            unit="unblocked campaigns",
        )
        listed = f" ({blocked_names})" if blocked_names else ""
        return _no_change(
            f"{qualifies}, but W1 is withheld: {why}{listed}.",
            sample=sample,
            withheld=(withheld,),
            extra=extra,
            threshold_keys=g1_keys,
        )
    reason = f"{qualifies}."
    if blocked_names:
        reason += f" Left out because G1 blocks spend increases on them: {blocked_names}."
    action = ACTION_DOUBLE_DOWN_PREFIX + ", ".join(ctx.campaign_name(cid) for cid in targets)
    return _Decision(
        W1,
        action,
        reason,
        increases=targets,
        sample=sample,
        extra=extra,
        threshold_keys=g1_keys,
    )


def _w2_guards(ctx: AnalysisContext, row: AssetRow, settings: _Settings) -> tuple[Withheld, ...]:
    """Return one withheld W2 line per failing guard, each with its own sample.

    AAP 0.5.4.7 W2: the window is at least ``w.retire_min_window_days``; the QL stage is
    available; there is no contact-share floor (``attribution.floor_share``); and the unknown
    CRM evidence share is 0. A zero under a floor or under unknown evidence may be an artefact
    of unmatched contacts or unread associations, so retiring on it is never recommended.
    """
    guards: list[Withheld] = []

    def withhold(
        reason: str, observed: int | Decimal | None, required: int | Decimal | None, unit: str
    ) -> None:
        guards.append(Withheld(W2, WHITE_PAPERS, row.entity, reason, observed, required, unit))

    days = ctx.window.days
    if days < settings.retire_min_days:
        withhold(
            f"window of {_plural(days, 'day')} is below the "
            f"{_plural(settings.retire_min_days, 'day')} W2 needs",
            days,
            settings.retire_min_days,
            "days",
        )
    if row.ql.unavailable:
        withhold(
            f"Qualified Lead stage unavailable: {row.ql.reason or 'stage binding not resolved'}",
            None,
            None,
            "Qualified Leads",
        )
    share = row.contact_share
    if share is not None and share >= settings.floor_share:
        withhold(
            f"Floor: {_percent(share)}% of leads unmatched in HubSpot (W2 needs below "
            f"{_percent(settings.floor_share)}%)",
            _bounded(share),
            settings.floor_share,
            "share of leads unmatched",
        )
    unknown = row.unknown_evidence_contacts
    if unknown > 0:
        withhold(
            f"unknown CRM evidence on {_plural(unknown, 'contact')}",
            unknown,
            0,
            "contacts with unknown CRM evidence",
        )
    return tuple(guards)


def _decide_without_ql(
    ctx: AnalysisContext, row: AssetRow, median: Decimal, settings: _Settings
) -> _Decision:
    """Decide a row with no Qualified Lead: W2 Retire or rework, or No change warranted.

    W2 needs spend >= ``w.retire_spend_multiple`` x the cohort median cost per QL, compared
    exactly as ``spend >= multiple x median`` so no rounded quotient decides it, and every
    :func:`_w2_guards` guard to hold. A row without spend is never retired.

    The comparison needs a positive cohort median. A median of 0 (the cohort's assets with QL
    had no window spend) leaves spend / median undefined, so W2 is withheld with "spend-to-median
    comparison unavailable" (observed unavailable, required ``w.retire_spend_multiple``): no
    ratio and no retirement threshold are shown, and the guards are not evaluated.
    """
    currency = row.currency
    spend = row.spend
    spend_text = _money(spend, currency)
    median_text = _money(median, currency)
    multiple_text = _plain(settings.retire_multiple)
    ratio = None if median <= 0 else _plain_decimal(spend / median)
    extra: dict[str, Any] = {"spend_to_median": ratio}
    if spend <= 0:
        return _no_change("No spend and no Qualified Lead in the window.", extra=extra)
    if ratio is None:
        # The median is 0: "spend >= multiple x 0" would retire any paid row, so W2's
        # spend-to-median comparison is reported unavailable instead of judged.
        why = (
            f"spend-to-median comparison unavailable: the {currency} cohort median cost per "
            f"Qualified Lead is {median_text}"
        )
        unavailable = Withheld(
            rule_id=W2,
            section=WHITE_PAPERS,
            entity=row.entity,
            reason=why,
            observed=None,
            required=settings.retire_multiple,
            unit="x cohort median cost per Qualified Lead",
        )
        return _no_change(
            f"No Qualified Lead yet after {spend_text} of spend, but W2 is withheld: {why}.",
            withheld=(unavailable,),
            extra=extra,
        )
    retire_at = settings.retire_multiple * median
    ratio_text = (
        f"{format(ratio, 'f')} x the {currency} cohort median cost per Qualified Lead of "
        f"{median_text}"
    )
    if spend < retire_at:
        return _no_change(
            f"No Qualified Lead yet; spend {spend_text} is {ratio_text}, below the "
            f"{multiple_text} x ({_money(retire_at, currency)}) at which W2 retires.",
            extra=extra,
        )
    guards = _w2_guards(ctx, row, settings)
    if guards:
        reasons = "; ".join(item.reason for item in guards)
        return _no_change(
            f"No Qualified Lead after {spend_text} of spend, {ratio_text}, but W2 is withheld: "
            f"{reasons}.",
            withheld=guards,
            extra=extra,
        )
    return _Decision(
        W2,
        ACTION_RETIRE,
        (
            f"No Qualified Lead after {spend_text} of spend over "
            f"{_plural(ctx.window.days, 'day')}: {ratio_text} (W2 retires at {multiple_text} x, "
            f"{_money(retire_at, currency)})."
        ),
        sample=Sample(ctx.window.days, settings.retire_min_days, "days"),
        extra=extra,
    )


def _decide(ctx: AnalysisContext, row: AssetRow, cohort: Cohort, settings: _Settings) -> _Decision:
    """Decide one row of a cohort whose median cost per Qualified Lead can be compared."""
    median = cohort.median_cost_per_ql
    ql = row.ql.value
    if median is None or ql is None:
        # Unreachable after _insufficiency (a known cohort total means every row's QL is known,
        # and a median exists), kept so a contract change cannot fabricate a comparison.
        return _no_change(
            f"Cost per Qualified Lead {_cost_text(row.cost_per_ql, row.currency)} cannot be "
            f"compared: the {cohort.currency} cohort has no median cost per Qualified Lead."
        )
    if ql >= 1:
        return _decide_with_ql(ctx, row, median, ql, settings)
    return _decide_without_ql(ctx, row, median, settings)


# ---------------------------------------------------------------------------
# Items, findings and the entry point
# ---------------------------------------------------------------------------


def _rank_key(row: AssetRow) -> tuple[int, Decimal]:
    """Rank within a cohort: QL >= 1 by cost per QL ascending, then zero-QL by spend descending.

    AAP 0.5.4.7 "Ranking". The key uses Qualified Leads only, never MQL. A row whose QL is
    unavailable has no cost per QL and ranks with the zero-QL rows, by spend.
    """
    ql = row.ql.value
    cost = row.cost_per_ql.value
    if ql is not None and ql >= 1 and cost is not None:
        return (_RANK_WITH_QL, cost)
    return (_RANK_WITHOUT_QL, -row.spend)


def _candidate(
    ctx: AnalysisContext,
    table: AttributionTable,
    cohort: Cohort,
    row: AssetRow,
    settings: _Settings,
    decision: _Decision,
) -> Candidate:
    """Build one asset row's item: its metrics in column order, the row detail and its flags.

    ``decision`` holds the row's rule, decided by :func:`evaluate`: an action rule, ``W1``
    Double down or ``W2`` Retire or rework, or rule ``W`` for a row that shows "No change
    warranted" or, in a cohort whose median cannot be compared against, "Insufficient data".
    Only a W1 item raises spend (``increases_spend_on``), on the campaigns G1 left unblocked.

    Threshold keys: :data:`THRESHOLD_KEYS` (the ``w.*`` decision keys, the attribution floor
    ``attribution.floor_share`` and the ``crm.*`` booked-meeting keys), plus
    :data:`~ads_engine.analysis.metrics.G1_THRESHOLD_KEYS` when the decision ran G1 over a
    contributing campaign.

    Rationale (AAP 0.5.4.7): every comparison stays within one currency cohort, so the item sits
    in its currency's group with that cohort's median, when it has one, as its baseline, and no
    amount of another currency is mixed in. Counts and costs keep the floors attribution
    computed ("≥ N", "≤ X") with their flags, so a partial CRM match is never shown as exact.
    """
    median = cohort.median_cost_per_ql
    baselines = (
        ()
        if median is None
        else (
            Baseline(
                "cohort_median_cost_per_ql",
                f"Cohort median cost per Qualified Lead ({cohort.currency})",
                _bounded(median),
            ),
        )
    )
    evidence = ctx.make_evidence(
        required_datasets=REQUIRED_DATASETS,
        crm_scope=_CRM_SCOPE,
        metrics=_row_metrics(row),
        baselines=baselines,
        sample=decision.sample,
        threshold_keys=tuple(dict.fromkeys((*THRESHOLD_KEYS, *decision.threshold_keys))),
        extra={
            **_row_extra(ctx, table, row),
            # The cross-asset view rides on every row of its cohort: AAP 0.5.3 defines a closed
            # set of finding codes with none for it, so it is not emitted as a section finding.
            "cohort": _cohort_extra(cohort, settings),
            **decision.extra,
        },
        # White-paper items render as table rows whose columns AAP 0.5.5 fixes, not as cards.
        headline=None,
    )
    return Candidate(
        rule_id=decision.rule_id,
        section=WHITE_PAPERS,
        group=cohort_group(row.currency),
        entity_ref=row.entity,
        action=decision.action,
        reason=decision.reason,
        evidence=evidence,
        rank_key=_rank_key(row),
        flags=row.to_flags(),
        required_datasets=REQUIRED_DATASETS,
        increases_spend_on=tuple(DecisionSubject("campaign", cid) for cid in decision.increases),
        # White-paper items offer "Mark done", never "Record decision" (AAP 0.5.5).
        decision_subject=None,
        # P1: a white paper row whose spend comes from a discrepant campaign is held.
        depends_on=tuple(DecisionSubject("campaign", cid) for cid in row.campaign_ids),
    )


def _section_finding(finding: Finding) -> Finding:
    return finding if finding.section == WHITE_PAPERS else replace(finding, section=WHITE_PAPERS)


def evaluate(ctx: AnalysisContext) -> RuleResult:
    """Rules W1 and W2: one action per white paper per currency cohort (AAP 0.5.4.7).

    For each cohort of :func:`~ads_engine.analysis.attribution.attribution_for` and each of its
    asset rows, exactly one item:

    * **Insufficient data** (``W``) for every row of a cohort whose assets together have fewer
      than ``w.cohort_min_total_ql`` (3) QL, whose QL count is unavailable, or with no asset at
      ``w.median_min_ql`` (1) QL: the median is too unstable, or undefined, to compare against.
      One cohort-wide withheld line states the shortfall, and each row carries it in
      ``extra.cohort_sample``, never as its ``evidence.sample`` (P6).
    * **W1 Double down** when QL >= ``w.min_ql`` (3) and cost per QL <= ``w.double_down_ratio``
      (0.8) x the cohort median: a white paper well below the cohort's typical cost per
      Qualified Lead earns more spend. It names the contributing campaigns G1 leaves unblocked
      (``increases_spend_on``); with none left W1 is withheld ("every contributing campaign is
      blocked by G1") and the row is "No change warranted".
    * **W2 Retire or rework** when QL = 0 and spend >= ``w.retire_spend_multiple`` (3) x the
      cohort median over a window of >= ``w.retire_min_window_days`` (28) days, with the QL
      stage available, no contact-share floor (``attribution.floor_share``, 10%) and no unknown
      CRM evidence: three typical Qualified Leads' worth of spend with none to show. A failing
      guard withholds W2 with its reason. The comparison needs a positive cohort median: a
      median of 0 withholds W2 ("spend-to-median comparison unavailable").
    * **No change warranted** (``W``) otherwise.

    Each item's evidence shows, in the table's column order, spend, LinkedIn leads (platform),
    MQL (headed with the bound stage label), Qualified Lead, meetings booked, other meetings
    (ordering unverified), deals after touch, cost per Qualified Lead and cost per meeting
    booked, with floors and unavailable counts as attribution computed them; ``extra.rows``
    holds the per-creative partition and ``baselines`` the cohort median. MQL and QL are never
    combined. Items rank by cost per QL ascending, then zero-QL rows by spend descending.

    Threshold keys: :data:`THRESHOLD_KEYS`, copied into every item's ``evidence.thresholds``,
    plus :data:`~ads_engine.analysis.metrics.G1_THRESHOLD_KEYS` (the ``budget.*`` gate keys,
    ``b2.poor_efficiency_ratio`` and ``b2.min_expected_outcomes``) on the items whose decision
    ran G1 over a contributing campaign: the W1 double down and the row whose W1 G1 withholds.

    Args:
        ctx: The analysis context.

    Returns:
        One candidate per asset row, cohort by cohort (by currency) in rank order; the
        attribution findings (``pipeline_unavailable``, ``skipped_stage``) of the White papers
        section; and the withheld W1, W2 and cohort lines. No suppression counts.

    Raises:
        ConfigError: When a threshold key is missing from the configuration.
    """
    settings = _Settings.of(ctx.thresholds)
    table = attribution_for(ctx)
    candidates: list[Candidate] = []
    withheld: list[Withheld] = []
    for cohort in table.cohorts:
        insufficient = _insufficiency(cohort, settings)
        if insufficient is not None:
            withheld.append(
                Withheld(
                    rule_id=W,
                    section=WHITE_PAPERS,
                    entity=None,
                    reason=insufficient.withheld_reason,
                    observed=insufficient.total_ql,
                    required=insufficient.required,
                    unit="Qualified Leads",
                )
            )
        items: list[Candidate] = []
        for row in cohort.rows:
            if insufficient is not None:
                # P6: the cohort shortfall is observational on each row (no failed sample);
                # the cohort-wide withheld line above lists it.
                decision = _Decision(
                    W,
                    ACTION_INSUFFICIENT_DATA,
                    insufficient.reason,
                    extra={"cohort_sample": insufficient.cohort_sample},
                )
            else:
                decision = _decide(ctx, row, cohort, settings)
            items.append(_candidate(ctx, table, cohort, row, settings, decision))
            withheld.extend(decision.withheld)
        candidates.extend(sorted(items, key=lambda item: item.sort_key))
    return RuleResult(
        candidates=tuple(candidates),
        findings=tuple(_section_finding(finding) for finding in table.findings),
        withheld=tuple(withheld),
    )
