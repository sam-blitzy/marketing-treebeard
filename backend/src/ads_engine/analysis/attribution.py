"""White-paper attribution chain and the CRM pipeline-veto population.

AAP 0.5.1 ("Attribution": ``build_attribution(ctx) -> AttributionTable``), 0.5.4.7 (the white
paper attribution chain), 0.5.4.1 ("CRM pipeline link") and 0.3.3 (effective CRM dataset;
association fetch and target-read status). Every number the White papers section shows, and
the matured population behind the C1b pause and the C2 pipeline veto, is computed here; the
rule modules (``white_papers.py``, ``campaigns.py``) only decide actions from it.

The chain, one hop at a time (AAP 0.5.4.7):

* **Asset resolution**, one precedence for spend and for leads: the creative's override first,
  otherwise the asset whose form list holds the canonical form URN (the creative's
  call-to-action form for spend, the lead's own form for a lead), so every form version maps
  to the same asset.
* **Cohorts**: rows are partitioned by the LinkedIn account currency. Every asset row,
  per-creative row, total and median sits within one currency; an asset whose creatives span
  currencies gets one row per currency, and nothing is summed across currencies. Event
  ownership is settled per asset before that partition, so each event of an asset sits in
  exactly one of its currency rows (see **Credit**).
* **Causal check**: the anchor of a (contact, asset) pair is the contact's earliest touch
  (lead ``submittedAt``) with that asset in the window, whatever the touch's currency. MQL
  and QL count only when the bound stage date is at or after the anchor; a meeting counts as
  booked only when its record creation (``hs_createdate``, the booking proxy, labelled
  "booked (record created)") and its start are both at or after the anchor, its outcome is
  not excluded and its source is a booking source; a deal counts only when it was created at
  or after the anchor. Earlier events are excluded and reported.
* **Credit**: every event is credited once per asset to the creative of the latest touch at
  or before the event, across every contact the event is associated with and every currency
  row of the asset; that touch selects the currency row as well as the creative within it. A
  touch without a creative is credited to the "unknown creative" row. An event excluded by the
  causal check, and a contact that skipped a stage, is reported once per asset, in the row of
  its anchor's currency (for a meeting or deal, the earliest anchor among its contacts).
  Per-creative rows partition each row's events, which :func:`build_attribution` verifies on
  every row.
* **Floors**: the contact share (unmatched leads), the unknown CRM evidence share (matched
  contacts whose refresh failed or was never recorded, or whose associations or target reads
  failed), the creative share (leads without a creative) and the portal-wide orphan-deal
  share, each over the scope actually assessed, against ``attribution.floor_share``.

Never blended (FR-8): MQL and Qualified Lead are separate fields everywhere; no field, total
or sort key combines them. LinkedIn platform leads are a separate platform count and are never
summed with CRM counts. Cost per lead is computed against Qualified Leads only.

Dataset isolation (decision 8): LinkedIn-attributed evidence reads ``ctx.linkedin_crm``, the
effective CRM dataset (the companion while LinkedIn is simulated); the Google veto population
reads ``ctx.google_crm``, HubSpot's active primary, and only when the Google-to-CRM link is
verified. A LinkedIn-derived subset never stands in for a Google population.

Value conventions (decision 6): money and shares are ``Decimal``; instants are tz-aware. A
missing value is ``None`` and renders "unavailable", never 0. Downstream CRM events count as
of run time (``ctx.now``): an event instant after the run is not counted.

Purity: imports only :mod:`ads_engine.analysis.context`, :mod:`ads_engine.analysis.thresholds`
(types), :mod:`ads_engine.analysis.window` (the UTC boundary name and native day bounds) and
the standard library. No database, network, settings or logging access.

Example:
    >>> table = attribution_for(ctx)  # doctest: +SKIP
    >>> row = table.cohorts[0].rows[0]  # doctest: +SKIP
    >>> row.mql.render(), row.ql.render(), row.cost_per_ql.render()  # doctest: +SKIP
    ('≥ 4', '≥ 3', '≤ 412.50')
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from types import MappingProxyType
from typing import Any, Final

from ads_engine.analysis.context import (
    ENTITY_CONTENT_ASSET,
    FINDING_PIPELINE_UNAVAILABLE,
    FINDING_SKIPPED_STAGE,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_CODES,
    FLAG_CREATIVE_FLOOR,
    FLAG_DEAL_FLOOR,
    FLAG_MEETING_TIME_PROXY,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    WHITE_PAPERS,
    AnalysisContext,
    CampaignInfo,
    ContactInfo,
    CrmData,
    EntityRef,
    Finding,
    Flag,
    LeadInfo,
    MeetingInfo,
    Source,
    content_asset_external_id,
    json_value,
)
from ads_engine.analysis.thresholds import ContentAsset, ResolvedBinding, StageBinding
from ads_engine.analysis.window import UTC_BOUNDARY, AnalysisWindow

__all__ = [
    "ATTRIBUTION_THRESHOLD_KEYS",
    "BOOKED_LABEL",
    "CACHE_KEY",
    "ENTRY_LIFECYCLE_STAGES",
    "NO_MEETINGS_NOTE",
    "NO_QL_NOTE",
    "OTHER_MEETINGS_LABEL",
    "OVERLAP_NOTE",
    "UNKNOWN_CREATIVE_LABEL",
    "AssetRow",
    "AttributionTable",
    "Cohort",
    "CostValue",
    "CountValue",
    "CreditRow",
    "OrphanDealShare",
    "VetoPopulation",
    "attribution_for",
    "bounded_decimal",
    "build_attribution",
    "contact_evidence_known",
    "shared_budget_veto",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_KEY: Final = "attribution"
"""The ``AnalysisContext.cache`` key under which :func:`attribution_for` memoises the table."""

UNKNOWN_CREATIVE_LABEL: Final = "unknown creative"
"""Label of the per-creative row that holds events credited to a touch without a creative."""

OVERLAP_NOTE: Final = "counts overlap across assets"
"""Cross-asset note: a contact who touched two assets is credited to both (any-touch)."""

BOOKED_LABEL: Final = "booked (record created)"
"""Label of the booked-meetings count: booking time is proxied by the record's creation."""

OTHER_MEETINGS_LABEL: Final = "Other meetings (ordering unverified)"
"""Label of meetings from non-booking sources, excluded from cost per meeting."""

NO_QL_NOTE: Final = "no QL yet"
"""Note of a cost per Qualified Lead whose known denominator is 0 (rendered "—")."""

NO_MEETINGS_NOTE: Final = "no meetings yet"
"""Note of a cost per meeting booked whose known denominator is 0 (rendered "—")."""

ENTRY_LIFECYCLE_STAGES: Final[frozenset[str]] = frozenset({"subscriber", "lead"})
"""Lifecycle stages that precede every bound stage.

AAP 0.5.4.7 "Skipped stages": a contact whose bound date is empty is not counted, "even if its
current stage is later". The portal's stage order is not part of the verified bindings, so a
contact is read as having skipped the stage when its bound date is empty while its current
``lifecyclestage`` is set and is not one of HubSpot's entry stages (``subscriber``, ``lead``),
which no bound stage can precede.
"""

ATTRIBUTION_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "attribution.floor_share",
    "crm.lead_maturity_days",
    "crm.veto_min_leads",
    "crm.booking_sources",
    "crm.meeting_outcome_exclusions",
    "w.median_min_ql",
)
"""Every threshold key this module applies, for rules to copy into ``evidence.thresholds``."""

_NO_CRM_REASON: Final = "no CRM dataset for LinkedIn-attributed evidence"
_REFRESH_FAILED_REASON: Final = "contact refresh failed; stage dates not current"
_REFRESH_NOT_RECORDED_REASON: Final = "contact refresh not recorded; stage dates not current"
_GOOGLE_CONTACTS_DATASET: Final = "hubspot.google_contacts"
_NO_SPEND_NOTE: Final = "spend is attributed only to known creatives"
_QL_UNAVAILABLE_NOTE: Final = "Qualified Lead count unavailable"
_MEETINGS_UNAVAILABLE_NOTE: Final = "meeting count unavailable"
_ORDERING_UNVERIFIED: Final = "(ordering unverified)"
"""The qualifier of :data:`OTHER_MEETINGS_LABEL`, repeated by the ``meeting_time_proxy`` flag."""
_HUNDRED: Final = Decimal(100)
_PERCENT_PLACES: Final = Decimal("0.1")
_STORED_PLACES: Final = Decimal("0.000001")
"""Finest stored money precision (``numeric(18, 6)``); derived values are bounded to it."""
_STORED_EXPONENT: Final = -6
"""The exponent of :data:`_STORED_PLACES`."""

type _CreativeKey = tuple[int | None, str | None]
"""A per-creative row key: ``(creative id, creative URN)``; ``(None, None)`` is unknown."""

_UNKNOWN_CREATIVE: Final[_CreativeKey] = (None, None)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


def _percent(share: Decimal) -> str:
    """Render a share in [0, 1] as a percentage with at most one decimal: ``0.1`` → ``"10"``."""
    value = (share * _HUNDRED).quantize(_PERCENT_PLACES, rounding=ROUND_HALF_EVEN).normalize()
    return format(value, "f")


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """Return ``"1 contact"`` / ``"2 contacts"``: the count with its noun agreeing in number."""
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _share(part: int, whole: int) -> Decimal | None:
    """Return ``part / whole`` as an exact ``Decimal``; ``None`` when ``whole`` is 0."""
    if whole <= 0:
        return None
    return Decimal(part) / Decimal(whole)


def bounded_decimal(value: Decimal) -> Decimal:
    """Bound a derived amount or share to the stored money precision without padding it.

    The one precision rule of white-paper evidence: a value finer than six decimals
    (``numeric(18, 6)``) is rounded half-even to six, and a coarser one is returned unchanged,
    so every place that cites a number cites the same value. Display text and every threshold
    comparison keep using the exact value; only serialisation is bounded.

    Args:
        value: The exact amount or share, for example a cost quotient.

    Returns:
        ``value`` quantized to six decimals when its exponent is below -6, else ``value``
        itself (non-finite values included).

    Example:
        >>> bounded_decimal(Decimal(2766) / Decimal(31))
        Decimal('89.225806')
        >>> bounded_decimal(Decimal("10.00"))
        Decimal('10.00')
    """
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < _STORED_EXPONENT:
        return value.quantize(_STORED_PLACES, rounding=ROUND_HALF_EVEN)
    return value


def _bounded_json(value: Decimal | None) -> Any:
    """Render an optional derived amount or share as JSON, bounded by :func:`bounded_decimal`."""
    return json_value(None if value is None else bounded_decimal(value))


@dataclass(frozen=True, slots=True)
class CountValue:
    """A CRM count that may be a floor or unavailable (AAP 0.5.4.7).

    Attributes:
        value: The known count; ``None`` exactly when ``unavailable``.
        floor: Whether the true count may be higher (a stated floor, rendered "≥ N").
        unavailable: Whether the count cannot be computed (rendered "unavailable", never 0).
        reason: Why the count is unavailable, for example the missing stage.

    Raises:
        ValueError: On construction, when ``value`` is missing without ``unavailable``, an
            unavailable count carries a value, no reason or a floor, or the value is negative.
    """

    value: int | None
    floor: bool = False
    unavailable: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        """Enforce the known/unavailable invariants."""
        if self.unavailable:
            if self.value is not None or self.floor:
                raise ValueError("an unavailable count carries no value and no floor")
            if not self.reason:
                raise ValueError("an unavailable count must state its reason")
            return
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise ValueError(f"a known count must be an int, found {self.value!r}")
        if self.value < 0:
            raise ValueError(f"a count cannot be negative, found {self.value}")

    @classmethod
    def missing(cls, reason: str) -> CountValue:
        """Return an unavailable count with its reason."""
        return cls(None, unavailable=True, reason=reason)

    def render(self) -> str:
        """Render the count: ``"unavailable"``, ``"≥ N"`` under a floor, else ``"N"``."""
        if self.unavailable or self.value is None:
            return "unavailable"
        return f"≥ {self.value}" if self.floor else str(self.value)

    def to_json(self) -> dict[str, Any]:
        """Return ``{value, floor, unavailable, reason, display}``."""
        return {
            "value": self.value,
            "floor": self.floor,
            "unavailable": self.unavailable,
            "reason": self.reason,
            "display": self.render(),
        }


@dataclass(frozen=True, slots=True)
class CostValue:
    """A cost per outcome within one currency (AAP 0.5.4.7).

    When the known denominator is 0 the cost is ``None`` with a note ("no QL yet", "no
    meetings yet") and renders "—": never a finite value and never a "≤ X" ceiling. Under a
    floor on the denominator with a known denominator above 0 the cost is a ceiling ("≤ X"),
    because the true denominator can only be larger.

    Attributes:
        value: Spend divided by the known denominator; ``None`` when not computable.
        ceiling: Whether ``value`` is an upper bound.
        note: Why the value is missing.
        unavailable: Whether the denominator itself is unavailable (rendered "unavailable").

    Raises:
        ValueError: On construction, for a ceiling without a value, a missing value without a
            note, or a negative value.
    """

    value: Decimal | None
    ceiling: bool = False
    note: str | None = None
    unavailable: bool = False

    def __post_init__(self) -> None:
        """Enforce the value/ceiling/note invariants."""
        if self.value is None:
            if self.ceiling:
                raise ValueError("a cost without a value cannot be a ceiling")
            if not self.note:
                raise ValueError("a cost without a value must carry a note")
            return
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise ValueError(f"a cost must be a finite Decimal, found {self.value!r}")
        if self.value < 0:
            raise ValueError(f"a cost cannot be negative, found {self.value}")
        if self.unavailable:
            raise ValueError("an unavailable cost carries no value")

    def render(self, places: int = 2) -> str:
        """Render the cost: ``"unavailable"``, ``"—"``, ``"≤ X"`` or ``"X"`` to ``places``."""
        if self.unavailable:
            return "unavailable"
        if self.value is None:
            return "—"
        shown = format(self.value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_EVEN), "f")
        return f"≤ {shown}" if self.ceiling else shown

    def to_json(self) -> dict[str, Any]:
        """Return ``{value, ceiling, note, unavailable, display}``, ``value`` to six decimals.

        ``value`` is bounded by :func:`bounded_decimal`, the precision every white-paper
        metric cites; ``display`` renders the exact value.
        """
        return {
            "value": _bounded_json(self.value),
            "ceiling": self.ceiling,
            "note": self.note,
            "unavailable": self.unavailable,
            "display": self.render(),
        }


@dataclass(frozen=True, slots=True)
class CreditRow:
    """One creative's share of an asset row (AAP 0.5.4.7 "Credit and de-duplication").

    Rows partition the asset's spend, leads and events: each sums to the asset total. The
    ``unknown creative`` row (``creative_id`` and ``creative_urn`` both ``None``) holds the
    leads without a creative and the events credited to their touches; it carries no spend,
    because spend always belongs to a creative.

    Attributes:
        creative_id: The creative's internal id, when known.
        creative_urn: The creative's URN, when known.
        label: The creative's headline, else its URN; "unknown creative" when unknown.
        spend: Window spend of the creative (LinkedIn UTC days), in the row's currency.
        leads: LinkedIn lead-form responses from this creative (a platform count).
        mql: MQL entries credited to this creative.
        ql: Qualified Lead entries credited to this creative.
        meetings_booked: Booked meetings credited to this creative.
        other_meetings: Meetings with ordering unverified credited to this creative.
        deals: Deals created after the touch, credited to this creative.
        cost_per_ql: ``spend / ql`` within the row's currency.
        cost_per_meeting: ``spend / meetings_booked`` (other meetings excluded).
    """

    creative_id: int | None
    creative_urn: str | None
    label: str
    spend: Decimal
    leads: int
    mql: CountValue
    ql: CountValue
    meetings_booked: CountValue
    other_meetings: CountValue
    deals: CountValue
    cost_per_ql: CostValue
    cost_per_meeting: CostValue

    @property
    def is_unknown(self) -> bool:
        """Whether this is the "unknown creative" row."""
        return self.creative_id is None and self.creative_urn is None

    def to_json(self) -> dict[str, Any]:
        """Return the row as plain JSON for ``evidence.extra.rows``; MQL and QL stay separate."""
        return {
            "creative_id": self.creative_id,
            "creative_urn": self.creative_urn,
            "label": self.label,
            "spend": json_value(self.spend),
            "leads": self.leads,
            "mql": self.mql.to_json(),
            "ql": self.ql.to_json(),
            "meetings_booked": self.meetings_booked.to_json(),
            "other_meetings": self.other_meetings.to_json(),
            "deals": self.deals.to_json(),
            "cost_per_ql": self.cost_per_ql.to_json(),
            "cost_per_meeting": self.cost_per_meeting.to_json(),
        }


@dataclass(frozen=True, slots=True)
class AssetRow:
    """One white paper within one currency cohort (AAP 0.5.4.7 "Metrics per asset").

    The CRM counts (MQL, QL, meetings, deals, and the excluded and skipped counts) are this
    row's share of the asset's events, each counted once per asset across its currency rows:
    in the row of the touch it is credited to, or, when excluded or skipped, of its anchor.

    Attributes:
        slug: The asset's registry slug.
        title: The asset's display title.
        currency: The cohort currency; every amount of the row is in it.
        spend: Window spend of the creatives resolving to the asset.
        linkedin_leads: Lead-form responses resolving to the asset: a LinkedIn platform
            count, labelled as such and never summed with CRM counts.
        mql: Contacts entering the bound MQL stage at or after their anchor.
        ql: Contacts entering the bound Qualified Lead stage at or after their anchor,
            observed independently of MQL.
        meetings_booked: Booked meetings after the touch ("booked (record created)").
        other_meetings: "Other meetings (ordering unverified)", excluded from cost per meeting.
        deals_after_touch: Deals created at or after the anchor.
        cost_per_ql: ``spend / ql``.
        cost_per_meeting: ``spend / meetings_booked``.
        contact_share: Leads not matched in HubSpot ÷ leads; ``None`` without leads.
        unknown_evidence_share: Matched contacts with unknown CRM evidence ÷ matched contacts;
            ``None`` without matched contacts.
        creative_share: Leads without a creative ÷ leads; ``None`` without leads.
        already_mql_before: Contacts already MQL before their touch (excluded, reported).
        already_ql_before: Contacts already QL before their touch (excluded, reported).
        skipped_mql: Contacts that skipped the MQL stage (not counted, reported).
        skipped_ql: Contacts that skipped the QL stage (not counted, reported).
        campaign_ids: Campaigns whose creatives' window spend resolves to the asset, the W1
            targets and the P1 dependencies of the row.
        creative_rows: The per-creative partition, by URN with "unknown creative" last.
        flags: Flag codes of the row, in :data:`~ads_engine.analysis.context.FLAG_CODES` order.
        mql_label: The bound MQL stage label for the column header, "MQL ({label})".
        ql_label: The bound Qualified Lead stage label.
        unmatched_leads: Leads whose match is not ``matched`` (the contact-share numerator).
        matched_contacts: Distinct matched contacts (the unknown-evidence denominator).
        unknown_evidence_contacts: Matched contacts with unknown CRM evidence.
        leads_without_creative: Leads without a creative (the creative-share numerator).
        meetings_before_touch: Meetings excluded because they precede the anchor.
        deals_before_touch: Deals excluded because they were created before the anchor.
        flag_messages: The message of each flag code in :attr:`flags`.
    """

    slug: str
    title: str
    currency: str
    spend: Decimal
    linkedin_leads: int
    mql: CountValue
    ql: CountValue
    meetings_booked: CountValue
    other_meetings: CountValue
    deals_after_touch: CountValue
    cost_per_ql: CostValue
    cost_per_meeting: CostValue
    contact_share: Decimal | None
    unknown_evidence_share: Decimal | None
    creative_share: Decimal | None
    already_mql_before: int
    already_ql_before: int
    skipped_mql: int
    skipped_ql: int
    campaign_ids: tuple[int, ...]
    creative_rows: tuple[CreditRow, ...]
    flags: tuple[str, ...]
    mql_label: str | None
    ql_label: str | None = None
    unmatched_leads: int = 0
    matched_contacts: int = 0
    unknown_evidence_contacts: int = 0
    leads_without_creative: int = 0
    meetings_before_touch: int = 0
    deals_before_touch: int = 0
    flag_messages: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze the collections and require a message for every flag."""
        object.__setattr__(self, "campaign_ids", tuple(self.campaign_ids))
        object.__setattr__(self, "creative_rows", tuple(self.creative_rows))
        object.__setattr__(self, "flags", tuple(self.flags))
        messages = MappingProxyType(dict(self.flag_messages))
        missing = [code for code in self.flags if code not in messages]
        if missing:
            raise ValueError(f"AssetRow {self.slug}: flags without a message: {missing}")
        object.__setattr__(self, "flag_messages", messages)

    @property
    def external_id(self) -> str:
        """The row's stable entity key within its cohort: ``{slug}|{CUR}``."""
        return content_asset_external_id(self.slug, self.currency)

    @property
    def entity(self) -> EntityRef:
        """The row as an :class:`EntityRef` of type ``content_asset`` (no internal id)."""
        return EntityRef("linkedin", ENTITY_CONTENT_ASSET, None, self.external_id, self.title)

    def has_flag(self, code: str) -> bool:
        """Return whether the row carries the flag ``code``."""
        return code in self.flags

    def to_flags(self) -> tuple[Flag, ...]:
        """Return the row's flags as :class:`Flag` badges with their messages."""
        return tuple(Flag(code, self.flag_messages[code]) for code in self.flags)

    def to_json(self) -> dict[str, Any]:
        """Return the row as plain JSON; MQL and QL are separate keys, never combined.

        Costs and shares are bounded to six decimals by :func:`bounded_decimal`.
        """
        return {
            "slug": self.slug,
            "title": self.title,
            "currency": self.currency,
            "spend": json_value(self.spend),
            "linkedin_leads": self.linkedin_leads,
            "mql": self.mql.to_json(),
            "ql": self.ql.to_json(),
            "meetings_booked": self.meetings_booked.to_json(),
            "other_meetings": self.other_meetings.to_json(),
            "deals_after_touch": self.deals_after_touch.to_json(),
            "cost_per_ql": self.cost_per_ql.to_json(),
            "cost_per_meeting": self.cost_per_meeting.to_json(),
            "contact_share": _bounded_json(self.contact_share),
            "unknown_evidence_share": _bounded_json(self.unknown_evidence_share),
            "creative_share": _bounded_json(self.creative_share),
            "already_mql_before": self.already_mql_before,
            "already_ql_before": self.already_ql_before,
            "skipped_mql": self.skipped_mql,
            "skipped_ql": self.skipped_ql,
            "unmatched_leads": self.unmatched_leads,
            "matched_contacts": self.matched_contacts,
            "unknown_evidence_contacts": self.unknown_evidence_contacts,
            "leads_without_creative": self.leads_without_creative,
            "meetings_before_touch": self.meetings_before_touch,
            "deals_before_touch": self.deals_before_touch,
            "campaign_ids": list(self.campaign_ids),
            "mql_label": self.mql_label,
            "ql_label": self.ql_label,
            "flags": [flag.to_json() for flag in self.to_flags()],
            "creative_rows": [row.to_json() for row in self.creative_rows],
        }


@dataclass(frozen=True, slots=True)
class Cohort:
    """The asset rows of one currency (AAP 0.5.4.7 "Currency cohorts").

    Only spend is totalled across assets: a contact who touched two assets is credited to both
    (any-touch), so cross-asset MQL, QL, meeting or deal totals would double count and none is
    exposed. Nothing is summed or compared across currencies.

    Attributes:
        currency: The cohort currency.
        rows: The cohort's asset rows, by slug.
        spend_total: Σ row spend, in ``currency``.
        total_ql: Σ row QL, the input of W's "Insufficient data" gate ("a cohort's assets
            together have fewer than 3 QL"); not a display total. ``None`` when QL is
            unavailable.
        median_cost_per_ql: Median cost per QL over the rows with at least ``w.median_min_ql``
            QL; ``None`` when no row qualifies.
        overlap_note: "counts overlap across assets".
    """

    currency: str
    rows: tuple[AssetRow, ...]
    spend_total: Decimal
    total_ql: int | None
    median_cost_per_ql: Decimal | None
    overlap_note: str = OVERLAP_NOTE

    def __post_init__(self) -> None:
        """Freeze the rows and require every row in the cohort's currency."""
        object.__setattr__(self, "rows", tuple(self.rows))
        foreign = [row.slug for row in self.rows if row.currency != self.currency]
        if foreign:
            raise ValueError(f"Cohort {self.currency}: rows in another currency: {foreign}")

    def to_json(self) -> dict[str, Any]:
        """Return the cohort as plain JSON (spend total only, with the overlap note).

        The median is bounded to six decimals by :func:`bounded_decimal`.
        """
        return {
            "currency": self.currency,
            "spend_total": json_value(self.spend_total),
            "median_cost_per_ql": _bounded_json(self.median_cost_per_ql),
            "overlap_note": self.overlap_note,
            "rows": [row.to_json() for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class OrphanDealShare:
    """The portal-wide orphan-deal share (AAP 0.5.4.7 "Completeness and floors").

    Measured over the portal deals created from window start to run time: a deal whose
    deal-to-contact fetch is ``ok_empty`` is an orphan; one whose fetch is ``ok`` with every
    target read is linked; anything else is not assessed.

    Attributes:
        assessed: Deals with a known deal-to-contact fetch.
        orphans: Assessed deals with no associated contact.
        not_assessed: Deals whose fetch or target reads are unknown.
        share: ``orphans / assessed``; ``None`` when unknown or nothing was assessed.
        unknown: Whether the share is unknown: the deal search is incomplete or some deals were
            not assessed.
        label: The share as a percentage, or when unknown "unknown (N deals not assessed)"
            after a complete search, "unknown (deal search incomplete)" or "unknown (deal
            search incomplete; at least N deals not assessed)" after an incomplete one ("deal
            search completeness unknown" when its completeness is unknown), and "unknown (no
            CRM dataset for LinkedIn-attributed evidence)" without a CRM dataset; "1 deal" for
            N = 1.
        reason: Why the share is unknown, when it is.
    """

    assessed: int
    orphans: int
    not_assessed: int
    share: Decimal | None
    unknown: bool
    label: str
    reason: str | None = None

    def is_floor(self, floor_share: Decimal) -> bool:
        """Return whether deal counts are floors: the share is unknown or at least the floor."""
        return self.unknown or (self.share is not None and self.share >= floor_share)

    def to_json(self) -> dict[str, Any]:
        """Return the share as plain JSON, ``share`` bounded to six decimals.

        ``share`` is bounded by :func:`bounded_decimal`; :meth:`is_floor` and ``label`` use the
        exact share.
        """
        return {
            "assessed": self.assessed,
            "orphans": self.orphans,
            "not_assessed": self.not_assessed,
            "share": _bounded_json(self.share),
            "unknown": self.unknown,
            "label": self.label,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class VetoPopulation:
    """One campaign's matured CRM population and pipeline veto (AAP 0.5.4.1).

    The population is defined before matching: the campaign's window leads whose touch is at
    least ``crm.lead_maturity_days`` before the window end. Unresolved leads stay in it, so the
    unresolved share's denominator never shrinks. QL, MQL and meetings are counted only for the
    eligible contacts (the distinct contacts behind matched matured leads, each at its earliest
    matured touch) and only for events at or after that touch; younger leads never enter.

    Attributes:
        source: ``linkedin`` (lead attribution) or ``google`` (verified Google-to-CRM link).
        campaign_id: The campaign.
        matured_leads: Matured leads (Google: matured touch-date contacts, one lead each).
        matched_leads: Matured leads matched to a contact.
        unresolved_leads: Matured leads ``pending``, ``no_match``, ``no_email``, ``error`` or
            without a match row.
        eligible_contacts: Distinct contacts behind the matched matured leads.
        unresolved_share: ``unresolved_leads / matured_leads``; ``None`` without matured leads.
        unknown_evidence_contacts: Eligible contacts whose refresh is not a recorded success
            (failed or never recorded), or whose contact-to-meeting or contact-to-deal fetch,
            or target reads failed; every eligible contact when the population's refresh is
            unknown (Google touch-date discovery).
        mql: MQL entries of eligible contacts after their touch; ``None`` when unavailable.
        ql: Qualified Lead entries likewise; ``None`` when unavailable.
        meetings: Meetings of eligible contacts after their touch, booked and ordering-
            unverified alike, outcome exclusions applied; ``None`` without a CRM dataset.
        available: Whether the pipeline can be judged; ``False`` means "pipeline unavailable".
        reason: Why the pipeline is unavailable.
        veto_fires: ``available`` and at least ``crm.veto_min_leads`` eligible contacts with
            QL 0.
        leads: Every window lead of the campaign (LinkedIn), matured or not; for Google,
            every touch-date contact admitted to the window (or to a shared budget's member
            days), matured or not, once each.
        attribution_floor: Whether the unresolved share reaches ``attribution.floor_share``.
        young_leads: Google: the touch-date contacts whose earliest admitted touch is younger
            than ``crm.lead_maturity_days``, shown beside the veto and never judged. ``None``
            for LinkedIn, whose young cohort the campaign rules derive from window leads.
        young_ql: Google: those young contacts that reached Qualified Lead at or after their
            touch and by the run; ``None`` when the Qualified Lead binding is unavailable
            (never 0 then), and ``None`` for LinkedIn.
    """

    source: Source
    campaign_id: int
    matured_leads: int
    matched_leads: int
    unresolved_leads: int
    eligible_contacts: int
    unresolved_share: Decimal | None
    unknown_evidence_contacts: int
    mql: int | None
    ql: int | None
    meetings: int | None
    available: bool
    reason: str | None
    veto_fires: bool
    leads: int = 0
    attribution_floor: bool = False
    young_leads: int | None = None
    young_ql: int | None = None

    def __post_init__(self) -> None:
        """Require a reason whenever the pipeline is unavailable."""
        if not self.available and not self.reason:
            raise ValueError(f"VetoPopulation {self.campaign_id}: unavailable without a reason")
        if self.veto_fires and not self.available:
            raise ValueError(f"VetoPopulation {self.campaign_id}: veto fired while unavailable")

    @property
    def unknown_evidence(self) -> bool:
        """Whether any eligible contact's CRM evidence is unknown."""
        return self.unknown_evidence_contacts > 0

    def to_json(self) -> dict[str, Any]:
        """Return the population as plain JSON; MQL, QL and meetings stay separate."""
        return {
            "source": self.source,
            "campaign_id": self.campaign_id,
            "leads": self.leads,
            "matured_leads": self.matured_leads,
            "matched_leads": self.matched_leads,
            "unresolved_leads": self.unresolved_leads,
            "eligible_contacts": self.eligible_contacts,
            "unresolved_share": json_value(self.unresolved_share),
            "unknown_evidence_contacts": self.unknown_evidence_contacts,
            "mql": self.mql,
            "ql": self.ql,
            "meetings": self.meetings,
            "available": self.available,
            "reason": self.reason,
            "veto_fires": self.veto_fires,
            "attribution_floor": self.attribution_floor,
            "young_leads": self.young_leads,
            "young_ql": self.young_ql,
        }


@dataclass(frozen=True, slots=True)
class AttributionTable:
    """Everything :func:`build_attribution` derives for one run.

    Attributes:
        cohorts: One cohort per currency, ordered by currency.
        orphan_deals: The portal-wide orphan-deal share of the effective CRM dataset.
        veto: Veto populations by campaign id: every LinkedIn campaign, plus every Google
            campaign when the Google-to-CRM link is verified.
        findings: ``skipped_stage`` and ``pipeline_unavailable`` findings of the White papers
            section.
        ql_available: Whether the Qualified Lead binding of the effective CRM dataset resolved.
        mql_available: Whether the MQL binding resolved.
        mql_label: The bound MQL stage label, for the column header.
        ql_label: The bound Qualified Lead stage label.
    """

    cohorts: tuple[Cohort, ...]
    orphan_deals: OrphanDealShare
    veto: Mapping[int, VetoPopulation]
    findings: tuple[Finding, ...]
    ql_available: bool
    mql_available: bool
    mql_label: str | None = None
    ql_label: str | None = None

    def __post_init__(self) -> None:
        """Freeze the collections."""
        object.__setattr__(self, "cohorts", tuple(self.cohorts))
        object.__setattr__(self, "veto", MappingProxyType(dict(self.veto)))
        object.__setattr__(self, "findings", tuple(self.findings))

    def rows(self) -> tuple[AssetRow, ...]:
        """Return every asset row, cohort by cohort."""
        return tuple(row for cohort in self.cohorts for row in cohort.rows)

    def cohort(self, currency: str) -> Cohort | None:
        """Return the cohort of ``currency``, when it has one."""
        code = currency.strip().upper()
        return next((cohort for cohort in self.cohorts if cohort.currency == code), None)


# ---------------------------------------------------------------------------
# Internal working types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Settings:
    """The thresholds this module applies, read once per build (no code defaults)."""

    floor_share: Decimal
    maturity: timedelta
    veto_min: int
    booking_sources: frozenset[str]
    exclusions: frozenset[str]
    median_min_ql: int

    @classmethod
    def of(cls, ctx: AnalysisContext) -> _Settings:
        """Read :data:`ATTRIBUTION_THRESHOLD_KEYS` from ``ctx.thresholds``.

        Raises:
            ConfigError: When a key is missing from the configuration.
        """
        thresholds = ctx.thresholds
        return cls(
            floor_share=thresholds.ratio("attribution.floor_share"),
            maturity=timedelta(days=thresholds.int("crm.lead_maturity_days")),
            veto_min=thresholds.int("crm.veto_min_leads"),
            booking_sources=frozenset(
                _code(value) for value in thresholds.strings("crm.booking_sources")
            ),
            exclusions=frozenset(
                _code(value) for value in thresholds.strings("crm.meeting_outcome_exclusions")
            ),
            median_min_ql=thresholds.int("w.median_min_ql"),
        )


@dataclass(frozen=True, slots=True, order=True)
class _Touch:
    """One matched lead of a contact: its ``submittedAt``, its creative and its row's currency.

    Touches order by instant, then lead id. The currency names the (asset, currency) row the
    lead belongs to, so the touch an event is credited to selects that row as well as the
    creative within it.
    """

    at: datetime
    lead_id: int
    creative: _CreativeKey = field(compare=False)
    currency: str = field(compare=False)


@dataclass(slots=True)
class _Tally:
    """Events credited to one creative of one asset row."""

    mql: int = 0
    ql: int = 0
    booked: int = 0
    other: int = 0
    deals: int = 0


@dataclass(slots=True)
class _RowInput:
    """What resolves to one (asset, currency) row before any CRM evidence is read.

    Spend and leads belong to exactly one row; CRM events are owned per asset, across all of
    its rows (:func:`_asset_events`).
    """

    asset: ContentAsset
    currency: str
    spend: dict[_CreativeKey, Decimal] = field(default_factory=dict)
    campaign_ids: set[int] = field(default_factory=set)
    leads: list[tuple[LeadInfo, _CreativeKey]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _StageCount:
    """One stage's outcome within one currency row of an asset (see :func:`_count_stage`)."""

    counted: int
    before: int
    skipped: int


def _code(value: str | None) -> str:
    """Normalise a HubSpot enumeration value (meeting source, outcome) for comparison."""
    return (value or "").strip().upper()


def _currency(ctx: AnalysisContext, campaign_id: int) -> str:
    """Return the normalised account currency of a campaign.

    Raises:
        KeyError: When the campaign is not in the context; every lead and creative of the
            active LinkedIn dataset belongs to one of its campaigns.
    """
    return ctx.account_currency(campaign_id).strip().upper()


def _latest_touch(touches: Sequence[_Touch], at: datetime) -> _Touch | None:
    """Return the latest touch at or before ``at`` from ascending ``touches``."""
    latest: _Touch | None = None
    for touch in touches:
        if touch.at > at:
            break
        latest = touch
    return latest


def _credit_touch(
    touches: Mapping[str, Sequence[_Touch]], contacts: Iterable[str], at: datetime
) -> _Touch | None:
    """Return the latest touch at or before ``at`` across ``contacts`` (multi-contact events)."""
    candidates = [
        touch for contact in contacts if (touch := _latest_touch(touches[contact], at)) is not None
    ]
    return max(candidates) if candidates else None


def _meeting_start(meeting: MeetingInfo) -> datetime | None:
    """Return the meeting's start: ``hs_meeting_start_time``, else ``hs_timestamp``.

    HubSpot sets ``hs_timestamp`` to the meeting's start for meetings, so it stands in only when
    the start property is empty.
    """
    return meeting.start_at if meeting.start_at is not None else meeting.meeting_timestamp


def _refresh_problem(lead: LeadInfo) -> str | None:
    """Return why a lead's record of its contact's latest refresh is not a success, if it is not.

    AAP 0.5.4.1 counts a contact's evidence as known only when its refresh succeeded, and
    AAP 0.3.3 records that success as ``contact_read_status = ok``. Only an explicit ``ok`` is
    a success: ``error`` is a failed refresh and an absent status is a refresh never recorded;
    in both cases the stored stage dates are not current.
    """
    if lead.contact_read_status == "ok":
        return None
    if lead.contact_read_status == "error":
        return _REFRESH_FAILED_REASON
    return _REFRESH_NOT_RECORDED_REASON


def _refresh_failures(leads: Iterable[LeadInfo]) -> Mapping[str, str]:
    """Return, by contact, why its refresh is unknown, over every matched lead of the contact.

    A contact is listed when any of its matched leads records a refresh other than ``ok``
    (:func:`_refresh_problem`). When its leads disagree, a failed refresh outranks one not
    recorded, whatever the lead order, so the reason is deterministic. Contacts are listed in
    sorted order.
    """
    reasons: dict[str, str] = {}
    for lead in leads:
        if not lead.matched or lead.contact_hs_id is None:
            continue
        problem = _refresh_problem(lead)
        if problem is not None and reasons.get(lead.contact_hs_id) != _REFRESH_FAILED_REASON:
            reasons[lead.contact_hs_id] = problem
    return MappingProxyType(dict(sorted(reasons.items())))


def _read_evidence_problem(
    crm: CrmData, hs_id: str, contact: ContactInfo | None, *, refresh: str | None
) -> str | None:
    """Return why a contact's CRM evidence is unknown, or ``None`` when it is known.

    AAP 0.5.4.7 / 0.3.3: the evidence is known only when the contact refresh succeeded, the
    contact-to-meeting and contact-to-deal fetches are ``ok`` or ``ok_empty``, and every
    associated meeting and deal was read. ``ok_empty`` is a genuine zero; an associated but
    unread meeting or deal is never treated as absent.

    Args:
        crm: The CRM dataset the evidence reads.
        hs_id: The contact's HubSpot id.
        contact: The contact's stored record; ``None`` when it was never read.
        refresh: Why the contact's refresh is not a recorded success
            (:func:`_refresh_failures`); ``None`` when it succeeded.
    """
    if refresh is not None:
        return refresh
    if contact is None:
        return "contact record not read"
    readers: tuple[tuple[str, Mapping[str, object]], ...] = (
        ("meeting", crm.meetings),
        ("deal", crm.deals),
    )
    for to_type, records in readers:
        fetch = crm.fetch("contact", hs_id, to_type)
        if fetch is None:
            return f"contact-to-{to_type} associations not fetched"
        if fetch.status in ("error", "partial"):
            return f"contact-to-{to_type} association fetch {fetch.status}"
        if not fetch.known:
            return f"{to_type} target reads {fetch.target_read_status or 'not recorded'}"
        unread = [
            target for target in crm.targets("contact", hs_id, to_type) if target not in records
        ]
        if unread:
            return f"{len(unread)} associated {to_type} record(s) not read"
    return None


def contact_evidence_known(crm: CrmData, hs_id: str, lead: LeadInfo) -> tuple[bool, str | None]:
    """Return whether one matched contact's CRM evidence is known, with the reason when not.

    AAP 0.5.4.7 "Unknown CRM evidence share": a contact's evidence is unknown when its contact
    refresh is not a recorded success (``contact_read_status`` ``error``, or not recorded at
    all, so its stage dates are not current; AAP 0.5.4.1 requires the refresh to have
    succeeded); a stage property (the MQL or Qualified Lead binding) is unavailable; its
    contact-to-meeting or contact-to-deal fetch is missing, ``error`` or ``partial``; or that
    fetch is ``ok`` but its target reads are ``partial`` or ``error``, so an associated meeting
    or deal was not read. An ``ok_empty`` fetch is a genuine zero.

    The asset rows and veto populations use the per-contact part of this check (refresh,
    associations, target reads); an unavailable binding is portal-wide and makes its count
    ``unavailable`` instead (see :func:`build_attribution`).

    Args:
        crm: The CRM dataset the evidence reads (the effective CRM dataset for LinkedIn).
        hs_id: The contact's HubSpot id.
        lead: A matched lead of the contact, carrying the contact's refresh status.

    Returns:
        ``(True, None)`` when known, else ``(False, reason)``.
    """
    refresh = _refresh_problem(lead)
    if refresh is not None:
        return False, refresh
    for name, binding in (("MQL", crm.bindings.mql), ("Qualified Lead", crm.bindings.ql)):
        if not binding.available:
            return False, f"{name} stage unavailable: {binding.reason}"
    problem = _read_evidence_problem(crm, hs_id, crm.contacts.get(hs_id), refresh=None)
    return problem is None, problem


# ---------------------------------------------------------------------------
# Asset resolution and cohort partition (AAP 0.5.4.7 "Identification", "Cohort")
# ---------------------------------------------------------------------------


def _lead_creative(ctx: AnalysisContext, by_urn: Mapping[str, int], lead: LeadInfo) -> _CreativeKey:
    """Return the per-creative key of a lead's ``associatedEntity`` creative."""
    if lead.creative_id is not None:
        info = ctx.creatives.get(lead.creative_id)
        return lead.creative_id, (info.urn if info is not None else lead.creative_urn)
    if lead.creative_urn:
        return by_urn.get(lead.creative_urn), lead.creative_urn
    return _UNKNOWN_CREATIVE


def _collect_rows(ctx: AnalysisContext) -> dict[tuple[str, str], _RowInput]:
    """Resolve every creative's spend and every lead to an (asset, currency) row.

    One precedence serves spend and leads: the creative's override, otherwise the asset whose
    form list holds the canonical form URN, the creative's call-to-action form for spend and
    the lead's own form for a lead. Spend and leads that resolve to no asset attribute to no
    white paper. Spend is the window spend of each creative on LinkedIn UTC days
    (``ctx.creative_totals``); leads are the stored lead-form responses submitted within
    ``ctx.window.utc_bounds()`` (test leads are never stored).

    The (asset, currency) rows partition spend and leads only. CRM events are not split here:
    :func:`build_attribution` groups an asset's rows and counts its events once across them
    (:func:`_asset_events`), so an event shared by touches in two currencies, or a contact
    with leads in both, is never counted in both rows.
    """
    by_urn = {info.urn: info.id for info in ctx.creatives.values()}
    inputs: dict[tuple[str, str], _RowInput] = {}

    def entry(asset: ContentAsset, currency: str) -> _RowInput:
        key = (asset.slug, currency)
        found = inputs.get(key)
        if found is None:
            found = inputs[key] = _RowInput(asset, currency)
        return found

    for totals in sorted(ctx.creative_totals.values(), key=lambda item: item.creative_id):
        creative = ctx.creatives.get(totals.creative_id)
        if creative is None:
            continue
        asset = ctx.resolve_asset(creative_urn=creative.urn, form_urn=creative.lead_form_urn)
        if asset is None:
            continue
        row = entry(asset, _currency(ctx, totals.campaign_id))
        key: _CreativeKey = (creative.id, creative.urn)
        row.spend[key] = row.spend.get(key, Decimal(0)) + totals.cost
        if totals.cost > 0:
            row.campaign_ids.add(totals.campaign_id)

    for lead in sorted(ctx.leads, key=lambda item: (item.submitted_at, item.id)):
        creative_key = _lead_creative(ctx, by_urn, lead)
        asset = ctx.resolve_asset(creative_urn=creative_key[1], form_urn=lead.lead_form_urn)
        if asset is None:
            continue
        entry(asset, _currency(ctx, lead.campaign_id)).leads.append((lead, creative_key))
    return inputs


# ---------------------------------------------------------------------------
# Orphan deals (AAP 0.5.4.7 "Completeness and floors")
# ---------------------------------------------------------------------------


def _orphan_deals(ctx: AnalysisContext, crm: CrmData | None) -> OrphanDealShare:
    """Return the orphan-deal share over the portal deals created from window start to run time.

    A deal is an orphan when its deal-to-contact fetch is ``ok_empty`` and linked when it is
    ``ok`` with its target reads ``ok``; any other deal is not assessed. The share is measured
    over the assessed deals. It is unknown when the deal search did not cover the range
    completely (``CrmData.deal_search_complete``, or a ``hubspot.deal_search`` freshness row of
    the dataset that is not ``complete``) or when any deal was not assessed.

    The unknown label is "unknown (N deals not assessed)" after a complete search. Deals an
    incomplete search never returned are not counted in N, so the label then states the search
    instead: "unknown (deal search incomplete)", or "unknown (deal search incomplete; at least
    N deals not assessed)" when N > 0, with "deal search completeness unknown" in place of
    "deal search incomplete" when the freshness row's completeness is ``unknown``. Without a
    CRM dataset it is "unknown (no CRM dataset for LinkedIn-attributed evidence)". Each count
    agrees with its noun: "1 deal not assessed", "2 deals not assessed".
    """
    if crm is None:
        return OrphanDealShare(0, 0, 0, None, True, f"unknown ({_NO_CRM_REASON})", _NO_CRM_REASON)
    assessed = orphans = not_assessed = 0
    for deal_id in dict.fromkeys(crm.window_deal_ids):
        fetch = crm.fetch("deal", deal_id, "contact")
        if fetch is None or not fetch.known:
            not_assessed += 1
            continue
        assessed += 1
        if fetch.is_empty:
            orphans += 1
    reasons: list[str] = []
    search_gap: str | None = None
    freshness = ctx.freshness_for("hubspot.deal_search", crm.dataset.dataset_id)
    if not crm.deal_search_complete:
        reasons.append("deal search did not cover the range completely")
        search_gap = "deal search incomplete"
    elif freshness is not None and not freshness.is_complete:
        detail = f": {freshness.reason}" if freshness.reason else ""
        reasons.append(f"deal search {freshness.completeness}{detail}")
        search_gap = (
            "deal search completeness unknown"
            if freshness.completeness == "unknown"
            else "deal search incomplete"
        )
    if not_assessed:
        fetches = _plural(not_assessed, "deal-to-contact fetch", "deal-to-contact fetches")
        reasons.append(f"{fetches} unknown")
    unknown = bool(reasons)
    share = _share(orphans, assessed)
    if search_gap is not None:
        # Deals the search never returned are absent from ``window_deal_ids``, so the count of
        # deals not assessed is only a lower bound and is stated as one, never as a bare 0.
        label = (
            f"unknown ({search_gap}; at least {_plural(not_assessed, 'deal')} not assessed)"
            if not_assessed
            else f"unknown ({search_gap})"
        )
    elif unknown:
        label = f"unknown ({_plural(not_assessed, 'deal')} not assessed)"
    elif share is None:
        label = "no deals created in range"
    else:
        label = f"{_percent(share)}% ({orphans} of {_plural(assessed, 'deal')} without a contact)"
    return OrphanDealShare(
        assessed=assessed,
        orphans=orphans,
        not_assessed=not_assessed,
        share=share,
        unknown=unknown,
        label=label,
        reason="; ".join(reasons) or None,
    )


# ---------------------------------------------------------------------------
# Causal check and credit (AAP 0.5.4.7 "Causal ordering check", "Credit")
# ---------------------------------------------------------------------------


def _skipped_stage(contact: ContactInfo) -> bool:
    """Whether a contact with an empty bound date skipped the stage (see entry stages)."""
    stage = (contact.lifecycle_stage or "").strip().lower()
    return bool(stage) and stage not in ENTRY_LIFECYCLE_STAGES


def _bump(counts: dict[str, int], currency: str) -> None:
    """Add one to the count of ``currency``'s row."""
    counts[currency] = counts.get(currency, 0) + 1


def _count_stage(
    *,
    crm: CrmData,
    getter: Callable[[ContactInfo], datetime | None],
    touches: Mapping[str, Sequence[_Touch]],
    refresh_unknown: Mapping[str, str],
    now: datetime,
    credit: Callable[[_Touch], None],
) -> dict[str, _StageCount]:
    """Count one bound stage over an asset's contacts once per asset; return it by currency.

    ``touches`` holds each contact's touches with the asset across all of the asset's currency
    rows, so a contact with leads in two currencies is still one contact of the asset. A
    contact counts once when its bound date is at or after its anchor (earliest touch, whatever
    its currency) and not after the run; the entry is credited to its latest touch at or before
    the date, whose currency is the row it counts in and whose creative it is credited to. A
    date before the anchor is "already MQL/QL before this touch" (excluded, reported). An empty
    date is never replaced by another date: it is not counted, and is reported as skipped when
    the contact's current stage is past the entry stages. Each excluded or skipped contact is
    reported once, in the row of its anchor's currency. A contact whose refresh is not a
    recorded success (``refresh_unknown``: failed or not recorded) keeps its previous stage
    dates, which still count (stage-entry dates never move), as a floor through the
    unknown-evidence share; it is not reported as skipped because its current stage is not
    known.
    """
    counted: dict[str, int] = {}
    before: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for hs_id in sorted(touches):
        contact = crm.contacts.get(hs_id)
        if contact is None:
            continue
        contact_touches = touches[hs_id]
        anchor = contact_touches[0]
        at = getter(contact)
        if at is None:
            if hs_id not in refresh_unknown and _skipped_stage(contact):
                _bump(skipped, anchor.currency)
            continue
        if at < anchor.at:
            _bump(before, anchor.currency)
            continue
        if at > now:
            # Downstream events count as of run time; a later instant is not yet an event.
            continue
        touch = _latest_touch(contact_touches, at)
        if touch is not None:
            credit(touch)
            _bump(counted, touch.currency)
    return {
        currency: _StageCount(
            counted.get(currency, 0), before.get(currency, 0), skipped.get(currency, 0)
        )
        for currency in sorted(counted.keys() | before.keys() | skipped.keys())
    }


def _associated(crm: CrmData, contacts: Iterable[str], to_type: str) -> dict[str, list[str]]:
    """Return target id to the given contacts associated with it, both sorted.

    Only verified edges are returned (AAP 0.3.3, 0.5.4.7): a contact's stored edges to
    ``to_type`` enter only when its current contact-to-``to_type`` fetch is known evidence
    (:attr:`AssociationFetchInfo.known`), that is ``ok`` with every target read. A stored edge
    whose fetch is missing, ``error`` or ``partial``, or ``ok`` with target reads ``partial``,
    ``error`` or unrecorded, may no longer exist, so the event it links is never a proven lower
    bound; an ``ok_empty`` fetch found no association, so an edge stored under it is stale.
    A contact left out for an unknown fetch still counts as unknown evidence
    (:func:`_read_evidence_problem`), which keeps its row's counts floors and its pipeline
    unavailable.
    """
    linked: dict[str, list[str]] = {}
    for hs_id in sorted(contacts):
        outcome = crm.fetch("contact", hs_id, to_type)
        if outcome is None or not outcome.known or outcome.is_empty:
            continue
        for target in crm.targets("contact", hs_id, to_type):
            members = linked.setdefault(target, [])
            if hs_id not in members:
                members.append(hs_id)
    return dict(sorted(linked.items()))


@dataclass(frozen=True, slots=True)
class _MeetingCount:
    """A row's meetings: booked after touch, ordering unverified, and preceding the anchor."""

    booked: int
    other: int
    before: int


def _count_meetings(
    *,
    crm: CrmData,
    settings: _Settings,
    touches: Mapping[str, Sequence[_Touch]],
    now: datetime,
    credit: Callable[[_Touch, bool], None],
) -> dict[str, _MeetingCount]:
    """Count an asset's meetings once per distinct meeting, with the causal check, by currency.

    HubSpot records no booking instant, so booking time is proxied by the record's creation
    (``hs_createdate``, "booked (record created)"). A meeting associated with a contact passes
    the causal check for that contact when its creation and its start are both at or after the
    contact's anchor; a meeting created after the touch but starting before it fails. Meetings
    whose outcome is in ``crm.meeting_outcome_exclusions`` are never counted, in either column.
    A passing meeting from a ``crm.booking_sources`` source is booked; one from any other
    source is "Other meetings (ordering unverified)", whose creation may postdate the real
    booking, and is excluded from cost per meeting. A meeting without a creation or start
    instant cannot pass the check and is not counted.

    Each meeting counts once per asset however many of the asset's contacts it is associated
    with, in whichever currencies they touched it: it is credited to the latest touch across
    the passing contacts at or before the meeting's earlier instant (creation or start), and
    counts in that touch's currency row, under its creative. A meeting that precedes every
    associated contact's anchor is reported once, in the currency row of the earliest anchor
    among those contacts. Only associations whose current fetch and target reads succeeded
    are counted (:func:`_associated`).
    """
    booked: dict[str, int] = {}
    other: dict[str, int] = {}
    before: dict[str, int] = {}
    for meeting_id, contacts in _associated(crm, touches, "meeting").items():
        meeting = crm.meetings.get(meeting_id)
        if meeting is None or _code(meeting.outcome) in settings.exclusions:
            continue
        created, start = meeting.created_at, _meeting_start(meeting)
        if created is None or start is None or created > now:
            continue
        passing = [hs for hs in contacts if touches[hs][0].at <= min(created, start)]
        if not passing:
            _bump(before, min(touches[hs][0] for hs in contacts).currency)
            continue
        touch = _credit_touch(touches, passing, min(created, start))
        if touch is None:
            continue
        is_booked = _code(meeting.meeting_source) in settings.booking_sources
        credit(touch, is_booked)
        _bump(booked if is_booked else other, touch.currency)
    return {
        currency: _MeetingCount(
            booked.get(currency, 0), other.get(currency, 0), before.get(currency, 0)
        )
        for currency in sorted(booked.keys() | other.keys() | before.keys())
    }


def _count_deals(
    *,
    crm: CrmData,
    touches: Mapping[str, Sequence[_Touch]],
    now: datetime,
    credit: Callable[[_Touch], None],
) -> dict[str, tuple[int, int]]:
    """Count an asset's deals created at or after the touch; ``(counted, before)`` by currency.

    The user's causal check exactly: a deal counts only when its ``createdate`` is at or after
    the anchor of a contact it is associated with. Each deal counts once per asset, whatever
    the currencies its contacts touched the asset in, credited to the latest touch across
    those contacts at or before its creation; it counts in that touch's currency row, under
    its creative. A deal created before every associated contact's anchor is reported once, in
    the currency row of the earliest anchor among those contacts. Only associations whose
    current fetch and target reads succeeded are counted (:func:`_associated`).
    """
    counted: dict[str, int] = {}
    before: dict[str, int] = {}
    for deal_id, contacts in _associated(crm, touches, "deal").items():
        deal = crm.deals.get(deal_id)
        if deal is None or deal.created_at is None or deal.created_at > now:
            continue
        created = deal.created_at
        passing = [hs for hs in contacts if touches[hs][0].at <= created]
        if not passing:
            _bump(before, min(touches[hs][0] for hs in contacts).currency)
            continue
        touch = _credit_touch(touches, passing, created)
        if touch is None:
            continue
        credit(touch)
        _bump(counted, touch.currency)
    return {
        currency: (counted.get(currency, 0), before.get(currency, 0))
        for currency in sorted(counted.keys() | before.keys())
    }


@dataclass(slots=True)
class _RowEvents:
    """One (asset, currency) row's share of its asset's CRM events.

    Filled by :func:`_asset_events`, which counts each event once per asset: a counted event
    sits in the row of the touch it is credited to, under that touch's creative
    (``tallies``); an excluded or skipped one sits in the row of its anchor's currency.
    """

    tallies: dict[_CreativeKey, _Tally] = field(default_factory=dict)
    mql: _StageCount = field(default_factory=lambda: _StageCount(0, 0, 0))
    ql: _StageCount = field(default_factory=lambda: _StageCount(0, 0, 0))
    meetings: _MeetingCount = field(default_factory=lambda: _MeetingCount(0, 0, 0))
    deals: int = 0
    deals_before: int = 0


def _asset_events(
    *,
    crm: CrmData | None,
    settings: _Settings,
    bindings: _Bindings,
    refresh_unknown: Mapping[str, str],
    now: datetime,
    entries: Sequence[_RowInput],
) -> dict[str, _RowEvents]:
    """Count one asset's CRM events once across its currency rows; return them by currency.

    AAP 0.5.4.7 "Credit and de-duplication": every event (MQL, QL, meeting, deal) is credited
    once per asset to the creative of the latest touch at or before it, and a multi-contact
    event to the latest qualifying touch across its contacts. ``entries`` are the asset's
    (asset, currency) rows; each contact's touches are gathered across all of them, so its
    anchor is its earliest touch with the asset whatever the currency, and it contributes at
    most one MQL and one QL to the asset. The touch an event is credited to selects its
    currency row and the creative within it, so no event is counted in two currency rows.
    Excluded events and skipped-stage contacts are reported once, in the row of the anchor's
    currency. Counts of an unavailable stage binding, and every count without a CRM dataset,
    stay zero here; :func:`_build_row` renders them unavailable.
    """
    touches: dict[str, list[_Touch]] = {}
    for entry in entries:
        for lead, key in entry.leads:
            if lead.matched and lead.contact_hs_id is not None:
                touches.setdefault(lead.contact_hs_id, []).append(
                    _Touch(lead.submitted_at, lead.id, key, entry.currency)
                )
    for contact_touches in touches.values():
        contact_touches.sort()
    rows = {entry.currency: _RowEvents() for entry in entries}
    if crm is None:
        return rows

    def tally(touch: _Touch) -> _Tally:
        tallies = rows[touch.currency].tallies
        found = tallies.get(touch.creative)
        if found is None:
            found = tallies[touch.creative] = _Tally()
        return found

    def credit_mql(touch: _Touch) -> None:
        tally(touch).mql += 1

    def credit_ql(touch: _Touch) -> None:
        tally(touch).ql += 1

    def credit_meeting(touch: _Touch, booked: bool) -> None:
        if booked:
            tally(touch).booked += 1
        else:
            tally(touch).other += 1

    def credit_deal(touch: _Touch) -> None:
        tally(touch).deals += 1

    if bindings.mql_available:
        mql = _count_stage(
            crm=crm,
            getter=lambda contact: contact.mql_at,
            touches=touches,
            refresh_unknown=refresh_unknown,
            now=now,
            credit=credit_mql,
        )
        for currency, stage in mql.items():
            rows[currency].mql = stage
    if bindings.ql_available:
        ql = _count_stage(
            crm=crm,
            getter=lambda contact: contact.ql_at,
            touches=touches,
            refresh_unknown=refresh_unknown,
            now=now,
            credit=credit_ql,
        )
        for currency, stage in ql.items():
            rows[currency].ql = stage
    meetings = _count_meetings(
        crm=crm, settings=settings, touches=touches, now=now, credit=credit_meeting
    )
    for currency, meeting_count in meetings.items():
        rows[currency].meetings = meeting_count
    deals = _count_deals(crm=crm, touches=touches, now=now, credit=credit_deal)
    for currency, (counted, before) in deals.items():
        rows[currency].deals, rows[currency].deals_before = counted, before
    return rows


# ---------------------------------------------------------------------------
# Asset rows (AAP 0.5.4.7 "Metrics per asset", "Completeness and floors")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Bindings:
    """The stage bindings of the effective CRM dataset, with their unavailability reasons."""

    mql: ResolvedBinding | None
    ql: ResolvedBinding | None
    mql_reason: str | None
    ql_reason: str | None
    mql_label: str | None
    ql_label: str | None

    @classmethod
    def of(cls, ctx: AnalysisContext, crm: CrmData | None) -> _Bindings:
        """Read the bindings; without a CRM dataset both counts are unavailable."""
        mql = None if crm is None else crm.bindings.mql
        ql = None if crm is None else crm.bindings.ql
        return cls(
            mql=mql,
            ql=ql,
            mql_reason=_binding_reason(mql),
            ql_reason=_binding_reason(ql),
            mql_label=_stage_label(mql, ctx.thresholds.mapping.mql),
            ql_label=_stage_label(ql, ctx.thresholds.mapping.ql),
        )

    @property
    def mql_available(self) -> bool:
        """Whether the MQL count can be computed."""
        return self.mql_reason is None

    @property
    def ql_available(self) -> bool:
        """Whether the Qualified Lead count can be computed."""
        return self.ql_reason is None


def _binding_reason(binding: ResolvedBinding | None) -> str | None:
    """Return why a binding is unavailable, or ``None`` when it resolved."""
    if binding is None:
        return _NO_CRM_REASON
    if binding.available:
        return None
    return binding.reason or "stage binding not resolved"


def _stage_label(resolved: ResolvedBinding | None, configured: StageBinding) -> str | None:
    """Return the label that heads a stage column: the resolved label, else the configured one.

    In ``property`` mode with no configured label, the bound property name stands in.
    """
    if resolved is not None and resolved.label:
        return resolved.label
    if configured.stage_labels:
        return configured.stage_labels[0]
    if resolved is not None and resolved.property:
        return resolved.property
    return configured.property


def _count_value(value: int, *, reason: str | None, floor: bool) -> CountValue:
    """Return a known count with its floor, or the unavailable count when ``reason`` is set."""
    if reason is not None:
        return CountValue.missing(reason)
    return CountValue(value, floor=floor)


def _cost(spend: Decimal, count: CountValue, *, zero_note: str, unavailable_note: str) -> CostValue:
    """Return ``spend / count`` within one currency (AAP 0.5.4.7 costs).

    An unavailable denominator gives an unavailable cost; a known denominator of 0 gives "—"
    with ``zero_note``, never a finite value or a ceiling; under a floor the cost is a ceiling
    ("≤ X") because the true denominator can only be larger.
    """
    if count.unavailable or count.value is None:
        return CostValue(None, note=unavailable_note, unavailable=True)
    if count.value == 0:
        return CostValue(None, note=zero_note)
    return CostValue(spend / Decimal(count.value), ceiling=count.floor)


def _creative_label(ctx: AnalysisContext, key: _CreativeKey) -> str:
    """Return a per-creative row's label: headline, else URN, else ``Creative #{id}``."""
    creative_id, urn = key
    if creative_id is None and urn is None:
        return UNKNOWN_CREATIVE_LABEL
    info = None if creative_id is None else ctx.creatives.get(creative_id)
    if info is not None and info.headline and info.headline.strip():
        return info.headline.strip()
    if urn:
        return urn
    return f"Creative #{creative_id}"


def _creative_sort_key(key: _CreativeKey) -> tuple[bool, str, int]:
    """Order per-creative rows by URN, with the "unknown creative" row last."""
    creative_id, urn = key
    return key == _UNKNOWN_CREATIVE, urn or "", -1 if creative_id is None else creative_id


def _check_partition(row: AssetRow) -> None:
    """Verify that the per-creative rows partition the asset row's spend, leads and events.

    Raises:
        AssertionError: When any per-creative column does not sum to the asset total; this
            would mean an event was credited twice or not at all.
    """
    columns: tuple[tuple[str, Callable[[CreditRow], object], object], ...] = (
        ("spend", lambda credit: credit.spend, row.spend),
        ("leads", lambda credit: credit.leads, row.linkedin_leads),
        ("mql", lambda credit: credit.mql.value, row.mql.value),
        ("ql", lambda credit: credit.ql.value, row.ql.value),
        ("meetings_booked", lambda credit: credit.meetings_booked.value, row.meetings_booked.value),
        ("other_meetings", lambda credit: credit.other_meetings.value, row.other_meetings.value),
        ("deals", lambda credit: credit.deals.value, row.deals_after_touch.value),
    )
    # An explicit raise rather than ``assert`` (ruff S101): the invariant must hold under -O too.
    for name, read, total in columns:
        values = [read(credit) for credit in row.creative_rows]
        if total is None:
            if any(value is not None for value in values):
                raise AssertionError(f"{row.external_id}: {name} unavailable but credited")
            continue
        summed: Decimal | int = sum(
            (value for value in values if isinstance(value, Decimal | int)), Decimal(0)
        )
        if any(value is None for value in values) or summed != total:
            raise AssertionError(
                f"{row.external_id}: per-creative {name} sums to {summed}, asset total {total}"
            )


def _build_row(
    ctx: AnalysisContext,
    crm: CrmData | None,
    settings: _Settings,
    bindings: _Bindings,
    orphan: OrphanDealShare,
    refresh_unknown: Mapping[str, str],
    entry: _RowInput,
    events: _RowEvents,
) -> tuple[AssetRow, list[Finding]]:
    """Build one (asset, currency) row with its per-creative partition and findings.

    The row's CRM counts are ``events``, its share of the asset's events as counted once per
    asset across the asset's currency rows (:func:`_asset_events`), so no event of the asset
    appears in two rows. Spend, LinkedIn leads, the contact, creative and unknown-evidence
    shares and the matched contacts describe this row's own leads and their contacts.
    """
    leads = entry.leads
    lead_counts: dict[_CreativeKey, int] = {}
    contacts: set[str] = set()
    for lead, key in leads:
        lead_counts[key] = lead_counts.get(key, 0) + 1
        if lead.matched and lead.contact_hs_id is not None:
            contacts.add(lead.contact_hs_id)

    unmatched = sum(1 for lead, _key in leads if not lead.matched)
    no_creative = lead_counts.get(_UNKNOWN_CREATIVE, 0)
    tallies = events.tallies
    mql, ql, meetings = events.mql, events.ql, events.meetings
    deals, deals_before = events.deals, events.deals_before

    unknown_contacts = 0
    if crm is not None:
        # The unknown-evidence share counts per-contact read failures (a refresh failed or not
        # recorded, associations, target reads). An unresolved stage binding is portal-wide:
        # its count is reported as unavailable instead, so it never turns a fully read count
        # into a "≥ N" floor.
        unknown_contacts = sum(
            1
            for hs_id in sorted(contacts)
            if _read_evidence_problem(
                crm, hs_id, crm.contacts.get(hs_id), refresh=refresh_unknown.get(hs_id)
            )
            is not None
        )

    contact_share = _share(unmatched, len(leads))
    unknown_share = _share(unknown_contacts, len(contacts))
    creative_share = _share(no_creative, len(leads))
    contact_floor = contact_share is not None and contact_share >= settings.floor_share
    unknown_floor = unknown_share is not None and unknown_share >= settings.floor_share
    creative_floor = creative_share is not None and creative_share >= settings.floor_share
    deal_floor = orphan.is_floor(settings.floor_share)
    counts_floor = contact_floor or unknown_floor
    crm_reason = _NO_CRM_REASON if crm is None else None

    spend = sum(entry.spend.values(), Decimal(0))
    mql_count = _count_value(mql.counted, reason=bindings.mql_reason, floor=counts_floor)
    ql_count = _count_value(ql.counted, reason=bindings.ql_reason, floor=counts_floor)
    booked_count = _count_value(meetings.booked, reason=crm_reason, floor=counts_floor)
    other_count = _count_value(meetings.other, reason=crm_reason, floor=counts_floor)
    deal_count = _count_value(deals, reason=crm_reason, floor=counts_floor or deal_floor)

    creative_rows: list[CreditRow] = []
    keys = sorted(set(entry.spend) | set(lead_counts) | set(tallies), key=_creative_sort_key)
    for key in keys:
        counts = tallies.get(key, _Tally())
        creative_spend = entry.spend.get(key, Decimal(0))
        unknown_row = key == _UNKNOWN_CREATIVE
        row_floor = counts_floor or (creative_floor and not unknown_row)
        row_ql = _count_value(counts.ql, reason=bindings.ql_reason, floor=row_floor)
        row_booked = _count_value(counts.booked, reason=crm_reason, floor=row_floor)
        if unknown_row:
            cost_per_ql = _cost_without_spend(row_ql, _QL_UNAVAILABLE_NOTE)
            cost_per_meeting = _cost_without_spend(row_booked, _MEETINGS_UNAVAILABLE_NOTE)
        else:
            cost_per_ql = _cost(
                creative_spend,
                row_ql,
                zero_note=NO_QL_NOTE,
                unavailable_note=_QL_UNAVAILABLE_NOTE,
            )
            cost_per_meeting = _cost(
                creative_spend,
                row_booked,
                zero_note=NO_MEETINGS_NOTE,
                unavailable_note=_MEETINGS_UNAVAILABLE_NOTE,
            )
        creative_rows.append(
            CreditRow(
                creative_id=key[0],
                creative_urn=key[1],
                label=_creative_label(ctx, key),
                spend=creative_spend,
                leads=lead_counts.get(key, 0),
                mql=_count_value(counts.mql, reason=bindings.mql_reason, floor=row_floor),
                ql=row_ql,
                meetings_booked=row_booked,
                other_meetings=_count_value(counts.other, reason=crm_reason, floor=row_floor),
                deals=_count_value(counts.deals, reason=crm_reason, floor=row_floor or deal_floor),
                cost_per_ql=cost_per_ql,
                cost_per_meeting=cost_per_meeting,
            )
        )

    messages: dict[str, str] = {}
    if contact_floor and contact_share is not None:
        messages[FLAG_ATTRIBUTION_FLOOR] = (
            f"Floor: {_percent(contact_share)}% of leads unmatched in HubSpot"
        )
    if unknown_floor and unknown_share is not None:
        messages[FLAG_UNKNOWN_CRM_EVIDENCE] = (
            f"Floor: CRM evidence unknown for {_percent(unknown_share)}% of matched contacts "
            f"({unknown_contacts} of {len(contacts)})"
        )
    if creative_floor and creative_share is not None:
        messages[FLAG_CREATIVE_FLOOR] = (
            f"Per-creative breakdown is a floor: {_percent(creative_share)}% of leads have no "
            "creative"
        )
    if deal_floor:
        messages[FLAG_DEAL_FLOOR] = f"Deal counts are a floor: orphan-deal share {orphan.label}"
    if meetings.other > 0:
        # Noun and pronoun agree with the count: "1 other meeting … it is", "2 … they are".
        excluded = "it is" if meetings.other == 1 else "they are"
        messages[FLAG_MEETING_TIME_PROXY] = (
            f"{_plural(meetings.other, 'other meeting')} {_ORDERING_UNVERIFIED}: booking time is "
            f"proxied by record creation, so {excluded} excluded from cost per meeting"
        )
    if bindings.ql_reason is not None:
        messages[FLAG_PIPELINE_UNAVAILABLE] = (
            f"Qualified Lead count unavailable: {bindings.ql_reason}"
        )
    flags = tuple(code for code in FLAG_CODES if code in messages)

    row = AssetRow(
        slug=entry.asset.slug,
        title=entry.asset.title,
        currency=entry.currency,
        spend=spend,
        linkedin_leads=len(leads),
        mql=mql_count,
        ql=ql_count,
        meetings_booked=booked_count,
        other_meetings=other_count,
        deals_after_touch=deal_count,
        cost_per_ql=_cost(
            spend, ql_count, zero_note=NO_QL_NOTE, unavailable_note=_QL_UNAVAILABLE_NOTE
        ),
        cost_per_meeting=_cost(
            spend,
            booked_count,
            zero_note=NO_MEETINGS_NOTE,
            unavailable_note=_MEETINGS_UNAVAILABLE_NOTE,
        ),
        contact_share=contact_share,
        unknown_evidence_share=unknown_share,
        creative_share=creative_share,
        already_mql_before=mql.before,
        already_ql_before=ql.before,
        skipped_mql=mql.skipped,
        skipped_ql=ql.skipped,
        campaign_ids=tuple(sorted(entry.campaign_ids)),
        creative_rows=tuple(creative_rows),
        flags=flags,
        mql_label=bindings.mql_label,
        ql_label=bindings.ql_label,
        unmatched_leads=unmatched,
        matched_contacts=len(contacts),
        unknown_evidence_contacts=unknown_contacts,
        leads_without_creative=no_creative,
        meetings_before_touch=meetings.before,
        deals_before_touch=deals_before,
        flag_messages=messages,
    )
    _check_partition(row)
    return row, _skipped_findings(row, bindings)


def _cost_without_spend(count: CountValue, unavailable_note: str) -> CostValue:
    """Return the cost of the "unknown creative" row, which holds no spend.

    Spend always belongs to a creative, so a cost on this row would divide zero spend by its
    events and mislead; it renders "—" with the reason, or "unavailable" with the count.
    """
    if count.unavailable:
        return CostValue(None, note=unavailable_note, unavailable=True)
    return CostValue(None, note=_NO_SPEND_NOTE)


def _stage_heading(name: str, label: str | None) -> str:
    """Return the heading a stage finding opens with: ``MQL (First meeting booked)``.

    The bound label is parenthesised only when it differs from the stage name (compared
    stripped and case-insensitively), so a Qualified Lead count bound to the "Qualified Lead"
    stage reads "Qualified Lead", never "Qualified Lead (Qualified Lead)".
    """
    text = (label or "").strip()
    if not text or text.casefold() == name.strip().casefold():
        return name
    return f"{name} ({text})"


def _skipped_findings(row: AssetRow, bindings: _Bindings) -> list[Finding]:
    """Return the row's ``skipped_stage`` findings, one per stage with skipped contacts.

    Each reads "{heading}: N contacts skipped this stage (not counted)" (AAP 0.5.4.7 "Skipped
    stages"), with "1 contact" for a single contact and the heading of :func:`_stage_heading`.
    """
    findings: list[Finding] = []
    stages: tuple[tuple[str, str | None, ResolvedBinding | None, int], ...] = (
        ("MQL", bindings.mql_label, bindings.mql, row.skipped_mql),
        ("Qualified Lead", bindings.ql_label, bindings.ql, row.skipped_ql),
    )
    for name, label, binding, skipped in stages:
        if skipped <= 0:
            continue
        heading = _stage_heading(name, label)
        findings.append(
            Finding(
                FINDING_SKIPPED_STAGE,
                WHITE_PAPERS,
                row.entity,
                f"{heading}: {_plural(skipped, 'contact')} skipped this stage (not counted)",
                {
                    "stage": name,
                    "label": label,
                    "property": None if binding is None else binding.property,
                    "skipped": skipped,
                    "asset": row.slug,
                    "currency": row.currency,
                },
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Veto population (AAP 0.5.4.1 "CRM pipeline link")
# ---------------------------------------------------------------------------


def _veto_stage(
    contacts: Mapping[str, ContactInfo],
    touches: Mapping[str, datetime],
    getter: Callable[[ContactInfo], datetime | None],
    now: datetime,
) -> int:
    """Count eligible contacts whose bound stage date is at or after their touch, by the run."""
    counted = 0
    for hs_id, touch in touches.items():
        contact = contacts.get(hs_id)
        at = None if contact is None else getter(contact)
        if at is not None and touch <= at <= now:
            counted += 1
    return counted


def _veto_meetings(
    crm: CrmData, touches: Mapping[str, datetime], settings: _Settings, now: datetime
) -> int:
    """Count distinct meetings of eligible contacts after their touch, for C1b and the veto.

    Booked and ordering-unverified meetings both count, with ``crm.meeting_outcome_exclusions``
    applied, and a meeting counts unless its creation or start is provably before the touch.
    Counting every plausible meeting is the conservative reading for a pause: a pipeline pause
    (meetings = 0) must never rest on a meeting excluded only for want of booking evidence.
    Only associations whose current fetch and target reads succeeded are counted
    (:func:`_associated`); a contact whose fetch or target reads did not succeed makes the
    pipeline unavailable instead, so no pause rests on the meetings that leaves out.
    """
    counted = 0
    for meeting_id, contacts in _associated(crm, touches, "meeting").items():
        meeting = crm.meetings.get(meeting_id)
        if meeting is None or _code(meeting.outcome) in settings.exclusions:
            continue
        created, start = meeting.created_at, _meeting_start(meeting)
        if created is not None and created > now:
            continue
        if any(
            (created is None or created >= touches[hs]) and (start is None or start >= touches[hs])
            for hs in contacts
        ):
            counted += 1
    return counted


def _judge(
    *,
    source: Source,
    campaign_id: int,
    crm: CrmData | None,
    contacts: Mapping[str, ContactInfo],
    touches: Mapping[str, datetime],
    refresh_unknown: Mapping[str, str],
    settings: _Settings,
    now: datetime,
    leads: int,
    matured: int,
    matched: int,
    refresh_problem: str | None = None,
    young: Mapping[str, datetime] | None = None,
) -> VetoPopulation:
    """Evaluate one campaign's pipeline veto over its eligible contacts (AAP 0.5.4.1).

    The pipeline is unavailable when there is no CRM dataset, the Qualified Lead stage did not
    resolve, any eligible contact's evidence is unknown (its refresh, its contact-to-meeting
    and contact-to-deal fetches, and the target reads of those meetings and deals must all
    have succeeded; a contact in ``refresh_unknown``, whose refresh failed or was never
    recorded, is unknown), or the unresolved share reaches ``attribution.floor_share``. The MQL
    binding does not enter the veto, which judges Qualified Leads and meetings only. The veto
    fires when the pipeline is available, eligible contacts reach ``crm.veto_min_leads``
    (counted in contacts after de-duplication) and their Qualified Lead count is 0.

    ``refresh_problem`` names a refresh that is unknown for the whole population (Google
    touch-date discovery, :func:`_google_refresh_problem`): every eligible contact's evidence
    is then unknown, and the pipeline is unavailable with that reason even when it has no
    eligible contact, because the refresh may have missed some.

    ``young`` holds the Google touch-date contacts too young for the veto, each at its
    earliest admitted touch. They never enter it: they are only counted beside it as
    ``young_leads``, with the Qualified Leads they reached at or after that touch and by the
    run as ``young_ql`` (``None`` without a resolved Qualified Lead stage). ``None``
    (LinkedIn) leaves both counts ``None``.
    """
    young_leads = None if young is None else len(young)
    unresolved = matured - matched
    share = _share(unresolved, matured)
    floor = share is not None and share >= settings.floor_share
    eligible = len(touches)
    if crm is None:
        return VetoPopulation(
            source=source,
            campaign_id=campaign_id,
            matured_leads=matured,
            matched_leads=matched,
            unresolved_leads=unresolved,
            eligible_contacts=eligible,
            unresolved_share=share,
            unknown_evidence_contacts=0,
            mql=None,
            ql=None,
            meetings=None,
            available=False,
            reason=_NO_CRM_REASON,
            veto_fires=False,
            leads=leads,
            attribution_floor=floor,
            young_leads=young_leads,
        )
    # Veto evidence is the AAP 0.5.4.1 list (refresh, fetches, target reads); the MQL binding
    # is not part of it, because the veto judges Qualified Leads and meetings only.
    unknown = (
        eligible
        if refresh_problem is not None
        else sum(
            1
            for hs_id in touches
            if _read_evidence_problem(
                crm, hs_id, contacts.get(hs_id), refresh=refresh_unknown.get(hs_id)
            )
            is not None
        )
    )
    mql_binding, ql_binding = crm.bindings.mql, crm.bindings.ql
    mql = (
        _veto_stage(contacts, touches, lambda contact: contact.mql_at, now)
        if mql_binding.available
        else None
    )
    ql = (
        _veto_stage(contacts, touches, lambda contact: contact.ql_at, now)
        if ql_binding.available
        else None
    )
    young_ql = (
        _veto_stage(contacts, young, lambda contact: contact.ql_at, now)
        if young is not None and ql_binding.available
        else None
    )
    reason: str | None = None
    if not ql_binding.available:
        reason = f"Qualified Lead stage unavailable: {_binding_reason(ql_binding)}"
    elif refresh_problem is not None:
        reason = refresh_problem
    elif unknown:
        reason = f"CRM evidence unknown for {unknown} of {eligible} eligible contacts"
    elif floor and share is not None:
        reason = (
            f"{_percent(share)}% of matured leads unresolved in HubSpot "
            f"(floor {_percent(settings.floor_share)}%)"
        )
    available = reason is None
    return VetoPopulation(
        source=source,
        campaign_id=campaign_id,
        matured_leads=matured,
        matched_leads=matched,
        unresolved_leads=unresolved,
        eligible_contacts=eligible,
        unresolved_share=share,
        unknown_evidence_contacts=unknown,
        mql=mql,
        ql=ql,
        meetings=_veto_meetings(crm, touches, settings, now),
        available=available,
        reason=reason,
        veto_fires=available and eligible >= settings.veto_min and ql == 0,
        leads=leads,
        attribution_floor=floor,
        young_leads=young_leads,
        young_ql=young_ql,
    )


def _linkedin_veto(
    ctx: AnalysisContext, settings: _Settings, refresh_unknown: Mapping[str, str]
) -> dict[int, VetoPopulation]:
    """Return the matured population of every LinkedIn campaign, from ``ctx.linkedin_crm``.

    The matured population is defined before matching: the campaign's window leads submitted
    at least ``crm.lead_maturity_days`` before the window end (``utc_bounds()[1]``). Each lead
    resolves from its match row in the effective CRM dataset; unresolved leads (``pending``,
    ``no_match``, ``no_email``, ``error`` or no row) stay in the population. Eligible contacts
    are the distinct contacts behind the matched matured leads, each at its earliest matured
    touch, so a contact with repeat leads counts once among contacts but once per lead in the
    unresolved share's denominator. A contact in ``refresh_unknown`` (any of its matched leads
    records a refresh other than ``ok``) has unknown evidence.
    """
    crm = ctx.linkedin_crm
    cutoff = ctx.window.utc_bounds()[1] - settings.maturity
    by_campaign: dict[int, list[LeadInfo]] = {}
    for lead in ctx.leads:
        by_campaign.setdefault(lead.campaign_id, []).append(lead)
    contacts: Mapping[str, ContactInfo] = {} if crm is None else crm.contacts
    populations: dict[int, VetoPopulation] = {}
    for campaign in _campaigns(ctx, "linkedin"):
        leads = by_campaign.get(campaign.id, [])
        matured = [lead for lead in leads if lead.submitted_at <= cutoff]
        touches: dict[str, datetime] = {}
        matched = 0
        for lead in matured:
            if not lead.matched or lead.contact_hs_id is None:
                continue
            matched += 1
            earlier = touches.get(lead.contact_hs_id)
            if earlier is None or lead.submitted_at < earlier:
                touches[lead.contact_hs_id] = lead.submitted_at
        populations[campaign.id] = _judge(
            source="linkedin",
            campaign_id=campaign.id,
            crm=crm,
            contacts=contacts,
            touches=dict(sorted(touches.items())),
            refresh_unknown=refresh_unknown,
            settings=settings,
            now=ctx.now,
            leads=len(leads),
            matured=len(matured),
            matched=matched,
        )
    return populations


def _google_window_instants(ctx: AnalysisContext) -> tuple[datetime, datetime]:
    """Return the window's instant range in Google's native day: ``[start 00:00, end + 1)``.

    AAP 0.5.4.1 applies the calendar window to each source's own days, and Google days end at
    midnight in the account's time zone. The boundary is the window's Google boundary, else
    the Google account's time zone, else UTC: the precedence ``metrics.load_context`` selects
    the Google-touch contacts with, so the loaded and the judged populations agree.
    """
    window = ctx.window
    boundary = next((item.boundary for item in window.source_days if item.source == "google"), None)
    account_zone = next(
        (account.timezone for account in ctx.accounts.values() if account.source == "google"),
        None,
    )
    zone = boundary or account_zone or UTC_BOUNDARY
    lower, _ = window.day_bounds(window.start, zone)
    _, upper = window.day_bounds(window.end, zone)
    return lower, upper


def _google_refresh_problem(ctx: AnalysisContext, crm: CrmData) -> str | None:
    """Return why the Google-touch contacts' refresh is unknown, or ``None`` when it is known.

    AAP 0.5.4.1 judges the pipeline only when every eligible contact's refresh succeeded.
    Google-touch contacts carry no per-contact refresh status: the touch-date discovery search
    finds them and the CRM sync re-reads every stored one by id with the matched contacts
    (AAP 0.3.3). ``crm``'s own ``hubspot.google_contacts`` freshness row records both, and a
    contact not read makes it ``incomplete``. A failed attempt (such as a transient error that
    stopped the discovery or cost a Google-touch read) moves only ``last_attempt_at`` and the
    reason, so a contact it did not read keeps its stored stage dates. The refresh is known
    only when that row exists, has succeeded, did not fail on a later attempt, is ``complete``
    and covers every window date. No other dataset's row stands in, so a LinkedIn (companion)
    dataset never vouches for the Google population.
    """
    row = ctx.freshness_for(_GOOGLE_CONTACTS_DATASET, crm.dataset.dataset_id)
    prefix = f"Google contact refresh unknown: {_GOOGLE_CONTACTS_DATASET}"
    if row is None:
        return f"{prefix} has no freshness record"
    detail = f": {row.reason}" if row.reason else ""
    if row.last_success_at is None:
        return f"{prefix} never succeeded{detail}"
    if row.last_attempt_at is not None and row.last_attempt_at > row.last_success_at:
        return f"{prefix} last attempt failed{detail}"
    if not row.is_complete:
        return f"{prefix} {row.completeness}{detail}"
    start, end = ctx.window.start, ctx.window.end
    if (
        row.covered_from is None
        or row.covered_through is None
        or row.covered_from > start
        or row.covered_through < end
    ):
        covered = (
            "no dates"
            if row.covered_from is None or row.covered_through is None
            else f"{row.covered_from.isoformat()} to {row.covered_through.isoformat()}"
        )
        return f"{prefix} covers {covered}, not the window {start.isoformat()} to {end.isoformat()}"
    return None


def _split_by_maturity(
    touches: Mapping[str, datetime], cutoff: datetime
) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """Split contacts at their earliest admitted touch: matured by ``cutoff``, else young.

    Both mappings are ordered by contact id. A contact with a matured and a younger touch is
    therefore matured, at its earliest touch, and never young.
    """
    ordered = sorted(touches.items())
    return (
        {hs_id: at for hs_id, at in ordered if at <= cutoff},
        {hs_id: at for hs_id, at in ordered if at > cutoff},
    )


def _google_veto(ctx: AnalysisContext, settings: _Settings) -> dict[int, VetoPopulation]:
    """Return the matured population of every Google campaign, from ``ctx.google_crm``.

    Only when the Google-to-CRM link is configured and verified (``bindings.google``):
    otherwise there is no Google entry and Google items carry ``no_crm_link``; a
    LinkedIn-derived subset never stands in. The campaign's touch-date contacts are those
    found by touch-date discovery whose campaign property equals the campaign's external id
    and whose touch date lies in the window, each once, at its earliest window touch; they are
    the population's ``leads``. The population is those whose earliest touch is matured; each
    counts as one matched lead at that touch. The rest are ``young_leads``, never judged, shown
    with the Qualified Leads they reached at or after their touch and by the run
    (``young_ql``). The window and the maturity cutoff are Google-native
    (:func:`_google_window_instants`): a touch is in the window when it falls in
    ``[start 00:00, end + 1 day 00:00)`` of the Google account's day, and matured when it is
    at least ``crm.lead_maturity_days`` before that upper instant.

    The ``hubspot.google_contacts`` row records the discovery and the by-id refresh of every
    stored Google-touch contact; a contact not read makes it ``incomplete``. When that refresh
    is unknown (:func:`_google_refresh_problem`), every Google population is unavailable with
    that reason and its eligible contacts count as unknown evidence. Otherwise each eligible
    contact is judged on its own reads (contact record, associations, target reads);
    discovered contacts carry no per-contact refresh status.
    """
    crm = ctx.google_crm
    if crm is None or not crm.bindings.google.available:
        return {}
    refresh_problem = _google_refresh_problem(ctx, crm)
    lower, upper = _google_window_instants(ctx)
    cutoff = upper - settings.maturity
    by_ref: dict[str, dict[str, datetime]] = {}
    found: dict[str, ContactInfo] = {}
    found_young: dict[str, ContactInfo] = {}
    for contact in crm.google_contacts:
        ref, touch = (contact.google_campaign_ref or "").strip(), contact.google_touch_at
        if not ref or touch is None or not lower <= touch < upper:
            continue
        (found if touch <= cutoff else found_young).setdefault(contact.hs_id, contact)
        touches = by_ref.setdefault(ref, {})
        earlier = touches.get(contact.hs_id)
        if earlier is None or touch < earlier:
            touches[contact.hs_id] = touch
    # A matured touch's record outranks a younger one's, so the judged population reads
    # exactly the records it would read without the young cohort.
    contacts: dict[str, ContactInfo] = {**crm.contacts, **found_young, **found}
    populations: dict[int, VetoPopulation] = {}
    for campaign in _campaigns(ctx, "google"):
        admitted = by_ref.get(campaign.external_id.strip(), {})
        touches, young = _split_by_maturity(admitted, cutoff)
        populations[campaign.id] = _judge(
            source="google",
            campaign_id=campaign.id,
            crm=crm,
            contacts=contacts,
            touches=touches,
            refresh_unknown=MappingProxyType({}),
            settings=settings,
            now=ctx.now,
            leads=len(admitted),
            matured=len(touches),
            matched=len(touches),
            refresh_problem=refresh_problem,
            young=young,
        )
    return populations


def _on_member_day(days: tuple[Sequence[datetime], Sequence[datetime]], touch: datetime) -> bool:
    """Whether ``touch`` lies in a member day, given the days' ascending starts and ends."""
    starts, ends = days
    index = bisect_right(starts, touch) - 1
    return index >= 0 and touch < ends[index]


def shared_budget_veto(
    ctx: AnalysisContext, member_days: Mapping[int, Sequence[date]], zone: str
) -> VetoPopulation | None:
    """Return a Google shared budget's matured population over its members' member days.

    AAP 0.5.4.5 aggregates a shared budget's spend and outcomes over member days only, so its
    pipeline veto (AAP 0.5.4.1) is judged on the same Google native days. The population is
    the touch-date contacts of ``ctx.google_crm`` whose campaign property equals a member
    campaign's external id and whose touch instant lies in one of that campaign's member days
    in the window: native day ``d`` is ``[d 00:00, d + 1 day 00:00)`` in ``zone``, the Google
    account zone the member days were classified in, compared as instants (HubSpot events are
    instants). A touch is matured when it is at or before the native window end
    (``window.end + 1 day 00:00`` in ``zone``) minus ``crm.lead_maturity_days``. A touch
    before a campaign joined the budget or after it left therefore never enters, and no UTC
    bound moves a touch in or out of the first or last native day. Every contact with an
    admitted touch is one of the population's ``leads``, once, at its earliest admitted touch,
    even when found through several members; those whose earliest touch is matured form the
    judged population, each one matched lead, and the rest are ``young_leads``, never judged,
    shown with the Qualified Leads they reached at or after their touch and by the run
    (``young_ql``). QL, MQL and meetings count only at or after that touch and by the run,
    and a meeting of contacts of two members counts once. Known evidence, the floor and the
    veto follow the per-campaign population.

    The population comes from the same touch-date discovery as the per-campaign Google veto
    (:func:`_google_veto`), so its refresh is judged the same way. The
    ``hubspot.google_contacts`` row records the discovery and the by-id refresh of every
    stored Google-touch contact, and a contact not read makes it ``incomplete``. When that
    refresh is unknown (:func:`_google_refresh_problem`), the population is unavailable with
    that reason and its eligible contacts count as unknown evidence, even with none admitted,
    because the refresh may have missed some. Otherwise each eligible contact is judged on its
    own reads; discovered contacts carry no per-contact refresh status.

    Args:
        ctx: The run's analysis context.
        member_days: Each member campaign's native member days, for example
            :attr:`~ads_engine.analysis.metrics.BudgetHistory.members`; a date outside the
            window is not a member day of this run.
        zone: The IANA zone of those dates
            (:func:`~ads_engine.analysis.metrics.native_day_zone` for ``google``).

    Returns:
        The population, keyed by the lowest member campaign id; ``None`` without a verified
        Google-to-CRM link (no Google veto is evaluated) or without a member Google campaign
        in the context.

    Raises:
        ConfigError: When a threshold in :data:`ATTRIBUTION_THRESHOLD_KEYS` is missing.
        ValueError: When ``zone`` is not a loadable IANA zone.
    """
    crm = ctx.google_crm
    if crm is None or not crm.bindings.google.available:
        return None
    by_ref: dict[str, list[tuple[datetime, datetime]]] = {}
    members: list[int] = []
    for campaign_id in sorted(member_days):
        campaign = ctx.campaigns.get(campaign_id)
        if campaign is None or campaign.source != "google":
            continue
        members.append(campaign_id)
        bounds = by_ref.setdefault(campaign.external_id.strip(), [])
        bounds.extend(
            AnalysisWindow.day_bounds(day, zone)
            for day in set(member_days[campaign_id])
            if ctx.window.contains(day)
        )
    if not members:
        return None
    days: dict[str, tuple[list[datetime], list[datetime]]] = {}
    for ref, spans in by_ref.items():
        ordered = sorted(spans)
        days[ref] = ([start for start, _ in ordered], [end for _, end in ordered])
    settings = _Settings.of(ctx)
    # Member-day spans are window dates, so they bound a touch to the native window; maturity
    # is measured back from that window's native end, never from its UTC end.
    cutoff = AnalysisWindow.day_bounds(ctx.window.end, zone)[1] - settings.maturity
    touches: dict[str, datetime] = {}
    found: dict[str, ContactInfo] = {}
    found_young: dict[str, ContactInfo] = {}
    for contact in crm.google_contacts:
        ref, touch = (contact.google_campaign_ref or "").strip(), contact.google_touch_at
        if not ref or touch is None:
            continue
        member = days.get(ref)
        if member is None or not _on_member_day(member, touch):
            continue
        (found if touch <= cutoff else found_young).setdefault(contact.hs_id, contact)
        earlier = touches.get(contact.hs_id)
        if earlier is None or touch < earlier:
            touches[contact.hs_id] = touch
    eligible, young = _split_by_maturity(touches, cutoff)
    return _judge(
        source="google",
        campaign_id=members[0],
        crm=crm,
        # A matured touch's record outranks a younger one's, as in the per-campaign population.
        contacts={**crm.contacts, **found_young, **found},
        touches=eligible,
        refresh_unknown=MappingProxyType({}),
        settings=settings,
        now=ctx.now,
        leads=len(touches),
        matured=len(eligible),
        matched=len(eligible),
        refresh_problem=_google_refresh_problem(ctx, crm),
        young=young,
    )


def _campaigns(ctx: AnalysisContext, source: Source) -> list[CampaignInfo]:
    return sorted(
        (campaign for campaign in ctx.campaigns.values() if campaign.source == source),
        key=lambda campaign: campaign.id,
    )


# ---------------------------------------------------------------------------
# Cohorts and entry points
# ---------------------------------------------------------------------------


def _median(values: Sequence[Decimal]) -> Decimal | None:
    """Return the median of ``values``; the mean of the middle two for an even count."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _cohort(currency: str, rows: Sequence[AssetRow], settings: _Settings) -> Cohort:
    """Assemble one currency cohort: spend total, the QL gate input and the median cost/QL."""
    ordered = tuple(sorted(rows, key=lambda row: row.slug))
    ql_values = [row.ql.value for row in ordered]
    total_ql = (
        None
        if any(value is None for value in ql_values)
        else sum(value for value in ql_values if value is not None)
    )
    costs = [
        row.cost_per_ql.value
        for row in ordered
        if row.ql.value is not None
        and row.ql.value >= settings.median_min_ql
        and row.cost_per_ql.value is not None
    ]
    return Cohort(
        currency=currency,
        rows=ordered,
        spend_total=sum((row.spend for row in ordered), Decimal(0)),
        total_ql=total_ql,
        median_cost_per_ql=_median(costs),
    )


def _binding_findings(bindings: _Bindings) -> list[Finding]:
    """Return a ``pipeline_unavailable`` finding naming each stage that did not resolve."""
    findings: list[Finding] = []
    stages: tuple[tuple[str, str | None, str | None, ResolvedBinding | None], ...] = (
        ("MQL", bindings.mql_label, bindings.mql_reason, bindings.mql),
        ("Qualified Lead", bindings.ql_label, bindings.ql_reason, bindings.ql),
    )
    for name, label, reason, binding in stages:
        if reason is None:
            continue
        heading = _stage_heading(name, label)
        findings.append(
            Finding(
                FINDING_PIPELINE_UNAVAILABLE,
                WHITE_PAPERS,
                None,
                f"{heading} count unavailable: {reason}",
                {
                    "stage": name,
                    "label": label,
                    "property": None if binding is None else binding.property,
                    "reason": reason,
                },
            )
        )
    return findings


def build_attribution(ctx: AnalysisContext) -> AttributionTable:
    """Build the white-paper attribution table and the CRM veto populations for one run.

    AAP 0.5.4.7 and 0.5.4.1. Pure and deterministic: cohorts by currency, rows by slug,
    per-creative rows by URN with "unknown creative" last, findings in that order after the
    stage-binding findings. Each asset's CRM events are counted once across its currency rows
    (:func:`_asset_events`) before the rows are built, so the currency partition never
    duplicates an event. LinkedIn-attributed evidence reads ``ctx.linkedin_crm`` (the
    effective CRM dataset); the Google veto reads ``ctx.google_crm``.

    Args:
        ctx: The run's analysis context.

    Returns:
        The attribution table. An empty context gives no cohorts and no veto populations.

    Raises:
        ConfigError: When a threshold in :data:`ATTRIBUTION_THRESHOLD_KEYS` is missing.
        KeyError: When a lead's or creative's campaign is not in the context, so its currency
            cohort cannot be determined.
        AssertionError: When the per-creative rows of an asset do not partition its totals.
    """
    settings = _Settings.of(ctx)
    crm = ctx.linkedin_crm
    bindings = _Bindings.of(ctx, crm)
    refresh_unknown = _refresh_failures(ctx.leads)
    orphan = _orphan_deals(ctx, crm)

    by_asset: dict[str, list[_RowInput]] = {}
    for entry in _collect_rows(ctx).values():
        by_asset.setdefault(entry.asset.slug, []).append(entry)
    by_currency: dict[str, list[AssetRow]] = {}
    row_findings: dict[tuple[str, str], list[Finding]] = {}
    for entries in by_asset.values():
        events = _asset_events(
            crm=crm,
            settings=settings,
            bindings=bindings,
            refresh_unknown=refresh_unknown,
            now=ctx.now,
            entries=entries,
        )
        for entry in entries:
            row, found = _build_row(
                ctx,
                crm,
                settings,
                bindings,
                orphan,
                refresh_unknown,
                entry,
                events[entry.currency],
            )
            by_currency.setdefault(entry.currency, []).append(row)
            row_findings[(entry.asset.slug, entry.currency)] = found
    cohorts = tuple(
        _cohort(currency, rows, settings) for currency, rows in sorted(by_currency.items())
    )

    findings: list[Finding] = _binding_findings(bindings) if cohorts else []
    for cohort in cohorts:
        for row in cohort.rows:
            findings.extend(row_findings[(row.slug, row.currency)])

    veto = {**_linkedin_veto(ctx, settings, refresh_unknown), **_google_veto(ctx, settings)}
    return AttributionTable(
        cohorts=cohorts,
        orphan_deals=orphan,
        veto=dict(sorted(veto.items())),
        findings=tuple(findings),
        ql_available=bindings.ql_available,
        mql_available=bindings.mql_available,
        mql_label=bindings.mql_label,
        ql_label=bindings.ql_label,
    )


def attribution_for(ctx: AnalysisContext) -> AttributionTable:
    """Return :func:`build_attribution` for ``ctx``, computed once per run.

    The table is memoised in ``ctx.cache[CACHE_KEY]`` so ``white_papers``, ``campaigns`` and
    ``precedence`` share one computation of the same frozen inputs.
    """
    cached = ctx.cache.get(CACHE_KEY)
    if isinstance(cached, AttributionTable):
        return cached
    table = build_attribution(ctx)
    ctx.cache[CACHE_KEY] = table
    return table
