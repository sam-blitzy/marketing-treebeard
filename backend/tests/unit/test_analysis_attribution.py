"""Unit tests for ``ads_engine.analysis.attribution``: the white-paper attribution chain.

They pin AAP 0.5.4.7 as :func:`build_attribution` computes it: asset resolution with one
precedence for spend and leads (creative override first, then the canonical form URN);
currency cohorts that never sum or compare across currencies; the causal check per event type
(MQL and QL at or after the contact's earliest touch, a meeting booked only when its record
creation and start are both at or after the anchor with a booking source and no excluded
outcome, a deal created at or after the anchor); credit once per asset to the creative of the
latest prior touch, with per-creative rows that partition the asset totals; the contact,
unknown CRM evidence, creative and orphan-deal floors against ``attribution.floor_share``;
costs against Qualified Leads and booked meetings only; "unavailable" never rendered as 0; and
the stage-binding and skipped-stage findings. Pure: no database and no network. The
pipeline-veto population is pinned by ``test_analysis_attribution_veto.py``.
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from ads_engine.analysis import attribution as attribution_module
from ads_engine.analysis.attribution import (
    ATTRIBUTION_THRESHOLD_KEYS,
    BOOKED_LABEL,
    CACHE_KEY,
    NO_MEETINGS_NOTE,
    NO_QL_NOTE,
    OTHER_MEETINGS_LABEL,
    OVERLAP_NOTE,
    UNKNOWN_CREATIVE_LABEL,
    AssetRow,
    AttributionTable,
    Cohort,
    CostValue,
    CountValue,
    CreditRow,
    OrphanDealShare,
    attribution_for,
    bounded_decimal,
    build_attribution,
    contact_evidence_known,
)
from ads_engine.analysis.context import (
    ENTITY_CONTENT_ASSET,
    FINDING_PIPELINE_UNAVAILABLE,
    FINDING_SKIPPED_STAGE,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_CREATIVE_FLOOR,
    FLAG_DEAL_FLOOR,
    FLAG_MEETING_TIME_PROXY,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    WHITE_PAPERS,
    AccountInfo,
    AnalysisContext,
    AssociationFetchInfo,
    AssociationStatus,
    CampaignInfo,
    Completeness,
    ContactInfo,
    CreativeInfo,
    CreativeTotals,
    CrmData,
    DatasetRef,
    DealInfo,
    Finding,
    Flag,
    Freshness,
    LeadInfo,
    MatchStatus,
    MeetingInfo,
    TargetReadStatus,
    objective_group,
)
from ads_engine.analysis.thresholds import (
    HUBSPOT_MAPPING_FILE,
    THRESHOLDS_FILE,
    CrmBindings,
    GoogleCrmBinding,
    ResolvedBinding,
    Thresholds,
)
from ads_engine.analysis.window import AnalysisWindow, SourceDayInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

# Run time and the default window it gives with a Google account on America/New_York:
# 2026-09-07 .. 2026-10-04, whose instant bounds are [09-07 00:00 UTC, 10-05 00:00 UTC).
NOW = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
WINDOW_LOWER = datetime(2026, 9, 7, tzinfo=UTC)
WINDOW_UPPER = datetime(2026, 10, 5, tzinfo=UTC)

LINKEDIN = DatasetRef(12, "linkedin", "live")
HUBSPOT = DatasetRef(13, "hubspot", "live")

# The white paper registry of these tests (content_assets.yaml shape), sorted by slug as
# build_attribution orders rows: benchmarks, pipeline-report, playbook.
REPORT = "pipeline-report"
PLAYBOOK = "playbook"
BENCH = "benchmarks"
FORM_REPORT_1 = "urn:li:leadGenForm:101"
FORM_REPORT_2 = "urn:li:leadGenForm:102"
FORM_PLAYBOOK = "urn:li:leadGenForm:301"
FORM_BENCH = "urn:li:leadGenForm:401"
FORM_UNLISTED = "urn:li:leadGenForm:901"

CR_A = 501
CR_B = 502
CR_EUR = 503
CR_IDLE = 504
CR_OVERRIDE = 601
CR_PLAYBOOK = 602
CR_BENCH = 701
CR_UNLISTED = 801


def _urn(creative_id: int) -> str:
    return f"urn:li:sponsoredCreative:{creative_id}"


REGISTRY: dict[str, Any] = {
    "assets": [
        {
            "slug": REPORT,
            "title": "The Pipeline Report",
            "lead_forms": [FORM_REPORT_1, FORM_REPORT_2],
            "creative_overrides": [],
        },
        {
            "slug": PLAYBOOK,
            "title": "The Playbook",
            "lead_forms": [FORM_PLAYBOOK],
            "creative_overrides": [_urn(CR_OVERRIDE)],
        },
        {"slug": BENCH, "title": "Benchmarks", "lead_forms": [FORM_BENCH]},
    ]
}

ACCOUNT_USD = 1
ACCOUNT_EUR = 2
CAMPAIGN_USD = 11
CAMPAIGN_EUR = 12
CAMPAIGN_USD_IDLE = 13
UNLOADED_CAMPAIGN = 99

MQL_PROPERTY = "hs_v2_date_entered_111"
QL_PROPERTY = "hs_v2_date_entered_222"
MQL_REASON = "stage label 'First meeting booked' not found in lifecyclestage options"
QL_REASON = "stage labels 'Qualified Lead', 'Sales Qualified Lead' not found"
NO_CRM_REASON = "no CRM dataset for LinkedIn-attributed evidence"
DATE_PROPERTY = "date_first_meeting_was_booked"

ROW_KEYS = {
    "slug",
    "title",
    "currency",
    "spend",
    "linkedin_leads",
    "mql",
    "ql",
    "meetings_booked",
    "other_meetings",
    "deals_after_touch",
    "cost_per_ql",
    "cost_per_meeting",
    "contact_share",
    "unknown_evidence_share",
    "creative_share",
    "already_mql_before",
    "already_ql_before",
    "skipped_mql",
    "skipped_ql",
    "unmatched_leads",
    "matched_contacts",
    "unknown_evidence_contacts",
    "leads_without_creative",
    "meetings_before_touch",
    "deals_before_touch",
    "campaign_ids",
    "mql_label",
    "ql_label",
    "flags",
    "creative_rows",
}
CREDIT_KEYS = {
    "creative_id",
    "creative_urn",
    "label",
    "spend",
    "leads",
    "mql",
    "ql",
    "meetings_booked",
    "other_meetings",
    "deals",
    "cost_per_ql",
    "cost_per_meeting",
}


# ---------------------------------------------------------------------------
# Builders (self-contained: no shared conftest)
# ---------------------------------------------------------------------------


def _thresholds(mapping_edit: Mapping[str, Any] | None = None) -> Thresholds:
    """Shipped thresholds and HubSpot mapping with the test registry."""
    trees = {
        name: yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
        for name in (THRESHOLDS_FILE, HUBSPOT_MAPPING_FILE)
    }
    mapping = trees[HUBSPOT_MAPPING_FILE]
    if mapping_edit is not None:
        mapping.update(mapping_edit)
    return Thresholds.from_trees(
        thresholds=trees[THRESHOLDS_FILE],
        content_assets=REGISTRY,
        hubspot_mapping=mapping,
        source_dir=CONFIG_DIR,
    )


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    return _thresholds()


@pytest.fixture(scope="module")
def window(thresholds: Thresholds) -> AnalysisWindow:
    return AnalysisWindow.default(thresholds, SourceDayInfo("America/New_York"), now=NOW)


def _t(day: int, hour: int = 12, minute: int = 0) -> datetime:
    """An instant on 2026-09-{day} UTC, inside the default window."""
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _campaign(campaign_id: int, account_id: int, currency: str) -> CampaignInfo:
    return CampaignInfo(
        id=campaign_id,
        source="linkedin",
        account_id=account_id,
        external_id=str(700000 + campaign_id),
        name=f"LinkedIn campaign {campaign_id}",
        status="active",
        raw_status="ACTIVE",
        objective_or_channel="LEAD_GENERATION",
        objective_group=objective_group("linkedin", "LEAD_GENERATION"),
        campaign_group_id=None,
        budget_id=None,
        daily_budget=Decimal("50.00"),
        total_budget=None,
        budget_period=None,
        currency=currency,
    )


ACCOUNTS = {
    ACCOUNT_USD: AccountInfo(ACCOUNT_USD, "linkedin", "500001", "US account", "USD"),
    # A lower-case account currency: cohorts normalise it to "EUR".
    ACCOUNT_EUR: AccountInfo(ACCOUNT_EUR, "linkedin", "500002", "EU account", "eur"),
}
CAMPAIGNS = {
    CAMPAIGN_USD: _campaign(CAMPAIGN_USD, ACCOUNT_USD, "USD"),
    CAMPAIGN_EUR: _campaign(CAMPAIGN_EUR, ACCOUNT_EUR, "EUR"),
    CAMPAIGN_USD_IDLE: _campaign(CAMPAIGN_USD_IDLE, ACCOUNT_USD, "USD"),
}
CREATIVES = {
    CR_A: CreativeInfo(
        CR_A, CAMPAIGN_USD, _urn(CR_A), headline="Headline A", lead_form_urn=FORM_REPORT_1
    ),
    # A blank headline: the per-creative label falls back to the URN.
    CR_B: CreativeInfo(CR_B, CAMPAIGN_USD, _urn(CR_B), headline="   ", lead_form_urn=FORM_REPORT_2),
    CR_EUR: CreativeInfo(
        CR_EUR, CAMPAIGN_EUR, _urn(CR_EUR), headline=" Headline EUR ", lead_form_urn=FORM_REPORT_1
    ),
    CR_IDLE: CreativeInfo(CR_IDLE, CAMPAIGN_USD_IDLE, _urn(CR_IDLE), lead_form_urn=FORM_REPORT_1),
    # A landing-page creative without a lead-gen form: it resolves through its override only.
    CR_OVERRIDE: CreativeInfo(
        CR_OVERRIDE, CAMPAIGN_USD, _urn(CR_OVERRIDE), headline="Landing page", lead_form_urn=None
    ),
    CR_PLAYBOOK: CreativeInfo(
        CR_PLAYBOOK,
        CAMPAIGN_USD,
        _urn(CR_PLAYBOOK),
        headline="Playbook",
        lead_form_urn=FORM_PLAYBOOK,
    ),
    CR_BENCH: CreativeInfo(
        CR_BENCH, CAMPAIGN_USD, _urn(CR_BENCH), headline="Benchmarks", lead_form_urn=FORM_BENCH
    ),
    CR_UNLISTED: CreativeInfo(
        CR_UNLISTED,
        CAMPAIGN_USD,
        _urn(CR_UNLISTED),
        headline="Unlisted",
        lead_form_urn=FORM_UNLISTED,
    ),
}


def _spend(creative: int, cost: str, campaign: int = CAMPAIGN_USD) -> CreativeTotals:
    return CreativeTotals(
        creative_id=creative,
        campaign_id=campaign,
        cost=Decimal(cost),
        impressions=1000,
        clicks=10,
        form_opens=50,
        leads=5,
        landing_page_clicks=None,
        website_conversions=None,
        delivery_days=20,
    )


def _lead(
    lead_id: int,
    at: datetime,
    *,
    contact: str | None = None,
    creative: int | None = CR_A,
    creative_urn: str | None = None,
    form: str = FORM_REPORT_1,
    campaign: int = CAMPAIGN_USD,
    status: MatchStatus | None = None,
    read_error: bool = False,
) -> LeadInfo:
    """A lead: matched when it names a contact, ``no_match`` otherwise (unless ``status``)."""
    if status is None:
        status = "matched" if contact is not None else "no_match"
    if creative_urn is None and creative is not None:
        creative_urn = _urn(creative)
    return LeadInfo(
        id=lead_id,
        external_id=f"urn:li:leadFormResponse:{lead_id}",
        campaign_id=campaign,
        creative_id=creative,
        creative_urn=creative_urn,
        lead_form_urn=form,
        lead_form_version=1,
        submitted_at=at,
        match_status=status,
        contact_hs_id=contact,
        contact_read_status=None if contact is None else ("error" if read_error else "ok"),
    )


def _fetch(
    object_type: str,
    object_id: str,
    to_type: str,
    status: AssociationStatus,
    target: TargetReadStatus | None = None,
) -> AssociationFetchInfo:
    return AssociationFetchInfo(object_type, object_id, to_type, status, target)


def _bindings(*, mql: bool = True, ql: bool = True) -> CrmBindings:
    return CrmBindings(
        mql=(
            ResolvedBinding(True, MQL_PROPERTY, "111", "First meeting booked")
            if mql
            else ResolvedBinding(False, reason=MQL_REASON)
        ),
        ql=(
            ResolvedBinding(True, QL_PROPERTY, "222", "Qualified Lead")
            if ql
            else ResolvedBinding(False, reason=QL_REASON)
        ),
        google=GoogleCrmBinding(False, reason="HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured"),
        verified_at=NOW,
    )


def _crm(
    contacts: Sequence[ContactInfo] = (),
    *,
    meetings: Sequence[MeetingInfo] = (),
    deals: Sequence[DealInfo] = (),
    contact_meetings: Mapping[str, Sequence[str]] | None = None,
    contact_deals: Mapping[str, Sequence[str]] | None = None,
    fetches: Sequence[AssociationFetchInfo] = (),
    unfetched: Sequence[tuple[str, str, str]] = (),
    window_deal_ids: Sequence[str] = (),
    deal_search_complete: bool = True,
    bindings: CrmBindings | None = None,
) -> CrmData:
    """A CRM dataset whose contacts' associations are fetched and read in full by default.

    Each contact gets a contact-to-meeting and a contact-to-deal fetch: ``ok`` with target
    reads ``ok`` when it has associations, ``ok_empty`` otherwise. ``fetches`` replace those
    outcomes (or add deal-to-contact fetches), and ``unfetched`` removes them.
    """
    associations: dict[tuple[str, str, str], tuple[str, ...]] = {}
    outcomes: dict[tuple[str, str, str], AssociationFetchInfo] = {}
    for contact in contacts:
        for to_type, links in (("meeting", contact_meetings), ("deal", contact_deals)):
            targets = tuple((links or {}).get(contact.hs_id, ()))
            key = ("contact", contact.hs_id, to_type)
            associations[key] = targets
            outcomes[key] = _fetch(*key, "ok", "ok") if targets else _fetch(*key, "ok_empty")
    for fetch in fetches:
        outcomes[(fetch.object_type, fetch.object_id, fetch.to_type)] = fetch
    for key in unfetched:
        outcomes.pop(key, None)
    return CrmData(
        dataset=HUBSPOT,
        bindings=_bindings() if bindings is None else bindings,
        contacts={contact.hs_id: contact for contact in contacts},
        meetings={meeting.hs_id: meeting for meeting in meetings},
        deals={deal.hs_id: deal for deal in deals},
        associations=associations,
        fetches=outcomes,
        window_deal_ids=tuple(window_deal_ids),
        deal_search_complete=deal_search_complete,
    )


def _context(
    thresholds: Thresholds,
    window: AnalysisWindow,
    *,
    crm: CrmData | None,
    leads: Sequence[LeadInfo] = (),
    spend: Sequence[CreativeTotals] = (),
    freshness: Sequence[Freshness] = (),
) -> AnalysisContext:
    return AnalysisContext(
        window=window,
        thresholds=thresholds,
        now=NOW,
        datasets={"linkedin": LINKEDIN, "hubspot": HUBSPOT},
        linkedin_crm=crm,
        accounts=ACCOUNTS,
        campaigns=CAMPAIGNS,
        creatives=CREATIVES,
        creative_totals={item.creative_id: item for item in spend},
        leads=tuple(leads),
        freshness={(row.dataset_id, row.dataset): row for row in freshness},
    )


type MakeContext = Callable[..., AnalysisContext]


@pytest.fixture
def make(thresholds: Thresholds, window: AnalysisWindow) -> MakeContext:
    def build(**changes: Any) -> AnalysisContext:
        return _context(thresholds, window, **changes)

    return build


def _row(table: AttributionTable, slug: str = REPORT, currency: str = "USD") -> AssetRow:
    cohort = table.cohort(currency)
    assert cohort is not None
    return next(row for row in cohort.rows if row.slug == slug)


def _credit(row: AssetRow, creative: int | None) -> CreditRow:
    if creative is None:
        return next(credit for credit in row.creative_rows if credit.is_unknown)
    return next(credit for credit in row.creative_rows if credit.creative_id == creative)


def _assert_partition(row: AssetRow) -> None:
    """Per-creative rows sum to the asset totals: no event credited twice or dropped."""
    credits = row.creative_rows
    assert sum((credit.spend for credit in credits), Decimal(0)) == row.spend
    assert sum(credit.leads for credit in credits) == row.linkedin_leads
    columns: tuple[tuple[CountValue, Callable[[CreditRow], CountValue]], ...] = (
        (row.mql, lambda credit: credit.mql),
        (row.ql, lambda credit: credit.ql),
        (row.meetings_booked, lambda credit: credit.meetings_booked),
        (row.other_meetings, lambda credit: credit.other_meetings),
        (row.deals_after_touch, lambda credit: credit.deals),
    )
    for total, read in columns:
        values = [read(credit).value for credit in credits]
        if total.unavailable:
            assert values == [None] * len(credits)
        else:
            assert sum(value or 0 for value in values) == total.value


def _qualified(
    prefix: str, count: int, *, creative: int | None, form: str, first_id: int
) -> tuple[list[LeadInfo], list[ContactInfo]]:
    """``count`` matched leads, one contact each, every contact a QL one day after its touch."""
    leads: list[LeadInfo] = []
    contacts: list[ContactInfo] = []
    for index in range(count):
        hs_id = f"{prefix}-{index}"
        touch = _t(8) + timedelta(hours=index)
        leads.append(_lead(first_id + index, touch, contact=hs_id, creative=creative, form=form))
        contacts.append(ContactInfo(hs_id, "salesqualifiedlead", ql_at=touch + timedelta(days=1)))
    return leads, contacts


# ---------------------------------------------------------------------------
# Value types: CountValue and CostValue rendering
# ---------------------------------------------------------------------------


def test_count_value_renders_known_floor_and_unavailable() -> None:
    assert CountValue(4).render() == "4"
    assert CountValue(0).render() == "0"
    assert CountValue(3, floor=True).render() == "≥ 3"
    missing = CountValue.missing(MQL_REASON)
    assert missing.render() == "unavailable"
    assert missing.value is None
    assert missing.to_json() == {
        "value": None,
        "floor": False,
        "unavailable": True,
        "reason": MQL_REASON,
        "display": "unavailable",
    }
    assert CountValue(3, floor=True).to_json() == {
        "value": 3,
        "floor": True,
        "unavailable": False,
        "reason": None,
        "display": "≥ 3",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"value": None}, "a known count must be an int"),
        ({"value": True}, "a known count must be an int"),
        ({"value": Decimal(2)}, "a known count must be an int"),
        ({"value": -1}, "a count cannot be negative"),
        ({"value": 2, "unavailable": True, "reason": "x"}, "carries no value and no floor"),
        (
            {"value": None, "unavailable": True, "floor": True, "reason": "x"},
            "carries no value and no floor",
        ),
        ({"value": None, "unavailable": True}, "must state its reason"),
    ],
)
def test_count_value_rejects_contract_violations(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CountValue(**kwargs)


def test_cost_value_renders_value_ceiling_dash_and_unavailable() -> None:
    assert CostValue(Decimal("412.5"), ceiling=True).render() == "≤ 412.50"
    assert CostValue(Decimal(250)).render() == "250.00"
    # Half-even rounding at the display precision.
    assert CostValue(Decimal("12.345")).render() == "12.34"
    assert CostValue(Decimal("12.355")).render() == "12.36"
    assert CostValue(Decimal("7.5")).render(places=0) == "8"
    assert CostValue(None, note=NO_QL_NOTE).render() == "—"
    unavailable = CostValue(None, note="Qualified Lead count unavailable", unavailable=True)
    assert unavailable.render() == "unavailable"
    assert CostValue(Decimal("412.5"), ceiling=True).to_json() == {
        "value": "412.5",
        "ceiling": True,
        "note": None,
        "unavailable": False,
        "display": "≤ 412.50",
    }
    assert CostValue(None, note=NO_MEETINGS_NOTE).to_json() == {
        "value": None,
        "ceiling": False,
        "note": "no meetings yet",
        "unavailable": False,
        "display": "—",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"value": None, "ceiling": True, "note": "x"}, "cannot be a ceiling"),
        ({"value": None}, "must carry a note"),
        ({"value": 1.5}, "must be a finite Decimal"),
        ({"value": Decimal("Infinity")}, "must be a finite Decimal"),
        ({"value": Decimal("NaN")}, "must be a finite Decimal"),
        ({"value": Decimal(-1)}, "cannot be negative"),
        ({"value": Decimal(1), "unavailable": True}, "carries no value"),
    ],
)
def test_cost_value_rejects_contract_violations(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CostValue(**kwargs)


# ---------------------------------------------------------------------------
# Serialised precision: one value per cited number, bounded to numeric(18, 6)
# ---------------------------------------------------------------------------


def _decimal_places(text: str) -> int:
    """Return the number of decimals a serialised ``Decimal`` string carries."""
    exponent = Decimal(text).as_tuple().exponent
    assert isinstance(exponent, int)
    return max(0, -exponent)


def _decimal_strings(value: object) -> list[str]:
    """Every numeric string in a JSON tree (labels, notes and displays are not numbers)."""
    if isinstance(value, Mapping):
        return [
            text
            for key, item in value.items()
            if key not in {"display", "label", "note", "reason", "message", "mql_label", "ql_label"}
            for text in _decimal_strings(item)
        ]
    if isinstance(value, list):
        return [text for item in value for text in _decimal_strings(item)]
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except ArithmeticError:
            return []
        return [value] if parsed.is_finite() else []
    return []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal(2766) / Decimal(31), "89.225806"),
        (Decimal(2) / Decimal(35), "0.057143"),
        (Decimal(1) / Decimal(3), "0.333333"),
        (Decimal(2) / Decimal(3), "0.666667"),
        # Half-even at the seventh decimal: down to an even digit, up to an even digit.
        (Decimal("0.0000005"), "0.000000"),
        (Decimal("0.0000015"), "0.000002"),
        # Six decimals or coarser: returned unchanged, never padded.
        (Decimal("123.456789"), "123.456789"),
        (Decimal("10.00"), "10.00"),
        (Decimal("412.5"), "412.5"),
        (Decimal("1E+2"), "100"),
        (Decimal(0), "0"),
    ],
)
def test_bounded_decimal_rounds_below_six_decimals_half_even_and_never_pads(
    value: Decimal, expected: str
) -> None:
    bounded = bounded_decimal(value)
    assert format(bounded, "f") == expected
    if _decimal_places(format(value, "f")) <= 6:
        assert bounded is value


def test_bounded_decimal_returns_non_finite_values_unchanged() -> None:
    assert bounded_decimal(Decimal("Infinity")) == Decimal("Infinity")
    assert bounded_decimal(Decimal("NaN")).is_nan()


def test_cost_value_json_cites_six_decimals_while_value_and_display_stay_exact() -> None:
    exact = Decimal(1000) / Decimal(13)
    cost = CostValue(exact)
    assert cost.to_json() == {
        "value": "76.923077",
        "ceiling": False,
        "note": None,
        "unavailable": False,
        "display": "76.92",
    }
    assert cost.value == exact
    ceiling = CostValue(exact, ceiling=True)
    assert (ceiling.to_json()["value"], ceiling.to_json()["display"]) == ("76.923077", "≤ 76.92")
    missing = CostValue(None, note=NO_QL_NOTE, unavailable=True)
    assert (missing.to_json()["value"], missing.to_json()["display"]) == (None, "unavailable")


def test_orphan_share_json_is_bounded_while_the_floor_compares_the_exact_share() -> None:
    share = Decimal(2) / Decimal(35)
    orphan = OrphanDealShare(35, 2, 0, share, False, "5.7% (2 of 35 deals without a contact)")
    assert orphan.to_json()["share"] == "0.057143"
    assert orphan.share == share
    # Just below the floor: the serialised share rounds to 0.100000, the floor is not met.
    near = Decimal("0.09999995")
    below = OrphanDealShare(1, 0, 0, near, False, "10% (just below the floor)")
    assert below.to_json()["share"] == "0.100000"
    assert below.is_floor(Decimal("0.10")) is False
    unknown = OrphanDealShare(0, 0, 1, None, True, "unknown (1 deals not assessed)")
    assert unknown.to_json()["share"] is None


def test_asset_row_and_cohort_json_bound_every_quotient_to_six_decimals(make: MakeContext) -> None:
    """Costs, shares and the cohort median serialise at six decimals; internals stay exact.

    Pipeline Report: Headline A with 3 QL on USD 100.00, the blank-headline creative with 4 QL on
    USD 200.00, and 2 unmatched leads without a creative, so cost per QL is 300/7 for the asset,
    100/3 and 50 per creative, and the contact and creative shares are 2/9.
    """
    a_leads, a_contacts = _qualified("a", 3, creative=CR_A, form=FORM_REPORT_1, first_id=1)
    b_leads, b_contacts = _qualified("b", 4, creative=CR_B, form=FORM_REPORT_2, first_id=10)
    unmatched = [_lead(20 + index, _t(9), creative=None) for index in range(2)]
    table = build_attribution(
        make(
            crm=_crm([*a_contacts, *b_contacts]),
            leads=[*a_leads, *b_leads, *unmatched],
            spend=[_spend(CR_A, "100.00"), _spend(CR_B, "200.00")],
        )
    )
    row = _row(table)
    assert row.cost_per_ql.value == Decimal(300) / Decimal(7)
    assert row.contact_share == row.creative_share == Decimal(2) / Decimal(9)
    rendered = row.to_json()
    assert rendered["cost_per_ql"]["value"] == "42.857143"
    assert (rendered["contact_share"], rendered["creative_share"]) == ("0.222222", "0.222222")
    assert rendered["unknown_evidence_share"] == "0"
    credits = {credit["creative_id"]: credit for credit in rendered["creative_rows"]}
    assert credits[CR_A]["cost_per_ql"]["value"] == "33.333333"
    assert credits[CR_B]["cost_per_ql"]["value"] == "50.00"
    assert _credit(row, CR_A).cost_per_ql.value == Decimal(100) / Decimal(3)
    # The display renders the exact value: a ceiling, since the shares are above the floor.
    assert rendered["cost_per_ql"]["display"] == "≤ 42.86"

    (cohort,) = table.cohorts
    assert cohort.median_cost_per_ql == Decimal(300) / Decimal(7)
    assert cohort.to_json()["median_cost_per_ql"] == "42.857143"
    numbers = _decimal_strings(cohort.to_json())
    assert numbers
    assert all(_decimal_places(text) <= 6 for text in numbers)
    third = dataclasses.replace(row, unknown_evidence_share=Decimal(1) / Decimal(7))
    assert third.to_json()["unknown_evidence_share"] == "0.142857"


def test_attribution_threshold_keys_are_the_configured_aap_defaults(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    assert window.utc_bounds() == (WINDOW_LOWER, WINDOW_UPPER)
    assert thresholds.subset(*ATTRIBUTION_THRESHOLD_KEYS) == {
        "attribution.floor_share": 0.1,
        "crm.lead_maturity_days": 14,
        "crm.veto_min_leads": 20,
        "crm.booking_sources": ["MEETINGS_PUBLIC"],
        "crm.meeting_outcome_exclusions": ["CANCELED"],
        "w.median_min_ql": 1,
    }
    assert BOOKED_LABEL == "booked (record created)"
    assert OTHER_MEETINGS_LABEL == "Other meetings (ordering unverified)"
    assert (NO_QL_NOTE, NO_MEETINGS_NOTE) == ("no QL yet", "no meetings yet")


# ---------------------------------------------------------------------------
# Asset resolution (one precedence for spend and for leads)
# ---------------------------------------------------------------------------


def test_spend_resolves_through_the_override_first_then_the_creatives_cta_form(
    make: MakeContext,
) -> None:
    table = build_attribution(
        make(
            crm=_crm(),
            spend=[
                _spend(CR_A, "100.00"),
                _spend(CR_B, "50.00"),
                _spend(CR_IDLE, "0", CAMPAIGN_USD_IDLE),
                _spend(CR_OVERRIDE, "30.00"),
                _spend(CR_PLAYBOOK, "20.00"),
                # Spend that resolves to no asset, and spend of a creative not loaded.
                _spend(CR_UNLISTED, "999.00"),
                _spend(9999, "500.00"),
            ],
        )
    )

    (cohort,) = table.cohorts
    assert cohort.currency == "USD"
    assert [row.slug for row in cohort.rows] == [REPORT, PLAYBOOK]
    assert cohort.spend_total == Decimal("200.00")

    report = _row(table, REPORT)
    assert report.spend == Decimal("150.00")
    # Only creatives with window spend name their campaign (W1 targets, P1 dependencies).
    assert report.campaign_ids == (CAMPAIGN_USD,)
    assert [credit.creative_urn for credit in report.creative_rows] == [
        _urn(CR_A),
        _urn(CR_B),
        _urn(CR_IDLE),
    ]
    assert [credit.label for credit in report.creative_rows] == [
        "Headline A",
        _urn(CR_B),
        _urn(CR_IDLE),
    ]
    assert [credit.spend for credit in report.creative_rows] == [
        Decimal("100.00"),
        Decimal("50.00"),
        Decimal(0),
    ]

    playbook = _row(table, PLAYBOOK)
    assert playbook.spend == Decimal("50.00")
    assert [credit.creative_id for credit in playbook.creative_rows] == [CR_OVERRIDE, CR_PLAYBOOK]
    assert _credit(playbook, CR_OVERRIDE).label == "Landing page"
    for row in cohort.rows:
        _assert_partition(row)


def test_leads_resolve_with_the_same_precedence_as_spend(make: MakeContext) -> None:
    table = build_attribution(
        make(
            crm=_crm(),
            leads=[
                # The override creative wins over the lead's own form.
                _lead(1, _t(10), creative=CR_OVERRIDE, form=FORM_BENCH),
                # No creative: the lead's own form decides, credited to "unknown creative".
                _lead(2, _t(10), creative=None, form=FORM_PLAYBOOK),
                # Another form of the same asset resolves to it too.
                _lead(3, _t(10), creative=CR_A, form=FORM_REPORT_2),
                # For a lead it is the lead's own form, not its creative's call-to-action form.
                _lead(4, _t(10), creative=CR_UNLISTED, form=FORM_BENCH),
                # A form listed under no asset attributes to no white paper.
                _lead(5, _t(10), creative=None, form=FORM_UNLISTED),
            ],
        )
    )

    assert [(row.slug, row.linkedin_leads) for row in table.rows()] == [
        (BENCH, 1),
        (REPORT, 1),
        (PLAYBOOK, 2),
    ]
    playbook = _row(table, PLAYBOOK)
    assert [(credit.creative_id, credit.leads) for credit in playbook.creative_rows] == [
        (CR_OVERRIDE, 1),
        (None, 1),
    ]
    assert playbook.creative_rows[-1].label == UNKNOWN_CREATIVE_LABEL
    assert playbook.leads_without_creative == 1
    assert _credit(_row(table, BENCH), CR_UNLISTED).leads == 1


def test_a_lead_creative_resolves_by_urn_then_falls_back_to_its_id(make: MakeContext) -> None:
    by_urn = _lead(1, _t(10), creative=None, creative_urn=_urn(CR_A))
    unloaded_id = dataclasses.replace(_lead(2, _t(10), creative=999), creative_urn=None)
    unloaded_urn = _lead(3, _t(10), creative=None, creative_urn=_urn(777))
    unloaded_both = _lead(4, _t(10), creative=888)
    table = build_attribution(
        make(
            crm=_crm(),
            leads=[by_urn, unloaded_id, unloaded_urn, unloaded_both],
            spend=[_spend(CR_A, "40.00")],
        )
    )

    row = _row(table)
    keys = [(credit.creative_id, credit.creative_urn) for credit in row.creative_rows]
    assert keys == [
        (999, None),
        (CR_A, _urn(CR_A)),
        (None, _urn(777)),
        (888, _urn(888)),
    ]
    assert [credit.label for credit in row.creative_rows] == [
        "Creative #999",
        "Headline A",
        _urn(777),
        _urn(888),
    ]
    # The lead found by URN shares its creative's row with that creative's spend.
    assert (_credit(row, CR_A).leads, _credit(row, CR_A).spend) == (1, Decimal("40.00"))
    assert row.leads_without_creative == 0
    assert row.creative_share == Decimal(0)
    _assert_partition(row)


def test_a_lead_or_creative_of_a_campaign_outside_the_context_raises(make: MakeContext) -> None:
    with pytest.raises(KeyError):
        build_attribution(make(crm=_crm(), leads=[_lead(1, _t(10), campaign=UNLOADED_CAMPAIGN)]))


def test_an_empty_context_gives_no_cohorts_and_no_findings(make: MakeContext) -> None:
    table = build_attribution(make(crm=_crm(bindings=_bindings(mql=False, ql=False))))
    assert table.cohorts == ()
    assert table.rows() == ()
    # Stage-binding findings belong to a section that has rows.
    assert table.findings == ()
    assert table.ql_available is False
    assert table.mql_available is False
    assert table.orphan_deals.label == "no deals created in range"


# ---------------------------------------------------------------------------
# Currency cohorts and the cross-asset table
# ---------------------------------------------------------------------------


def test_an_asset_spanning_currencies_gets_one_row_per_currency(make: MakeContext) -> None:
    contacts = [
        ContactInfo("usd-1", "salesqualifiedlead", ql_at=_t(12)),
        ContactInfo("eur-1", "salesqualifiedlead", ql_at=_t(13)),
    ]
    table = build_attribution(
        make(
            crm=_crm(contacts),
            leads=[
                _lead(1, _t(10), contact="usd-1", creative=CR_A),
                _lead(2, _t(10), contact="eur-1", creative=CR_EUR, campaign=CAMPAIGN_EUR),
            ],
            spend=[_spend(CR_A, "100.00"), _spend(CR_EUR, "80.00", CAMPAIGN_EUR)],
        )
    )

    assert [cohort.currency for cohort in table.cohorts] == ["EUR", "USD"]
    assert table.cohort(" usd ") is table.cohorts[1]
    assert table.cohort("GBP") is None
    for cohort, spend in zip(table.cohorts, ("80.00", "100.00"), strict=True):
        (row,) = cohort.rows
        assert row.slug == REPORT
        assert row.currency == cohort.currency
        assert row.spend == Decimal(spend)
        assert cohort.spend_total == Decimal(spend)
        assert cohort.total_ql == 1
        assert cohort.median_cost_per_ql == Decimal(spend)
        # Only spend is totalled, within the currency; no CRM count is totalled at all.
        assert set(cohort.to_json()) == {
            "currency",
            "spend_total",
            "median_cost_per_ql",
            "overlap_note",
            "rows",
        }
    eur = _row(table, REPORT, "EUR")
    assert eur.external_id == "pipeline-report|EUR"
    assert _credit(eur, CR_EUR).label == "Headline EUR"
    assert eur.campaign_ids == (CAMPAIGN_EUR,)
    entity = eur.entity
    assert (entity.source, entity.type, entity.id) == ("linkedin", ENTITY_CONTENT_ASSET, None)
    assert (entity.external_id, entity.name) == ("pipeline-report|EUR", "The Pipeline Report")


# Two contacts of one asset in two currencies: c-usd touches in USD on day 10, c-eur in EUR on
# day 12. Events after both touches go to c-eur's later touch; events between them qualify only
# through c-usd; events before both are excluded once, in the row of the earliest anchor (USD).
TWO_CURRENCY_LEADS = (
    _lead(1, _t(10), contact="c-usd", creative=CR_A),
    _lead(2, _t(12), contact="c-eur", creative=CR_EUR, campaign=CAMPAIGN_EUR),
)
TWO_CURRENCY_SPEND = (_spend(CR_A, "100.00"), _spend(CR_EUR, "80.00", CAMPAIGN_EUR))
TWO_CURRENCY_CONTACTS = (ContactInfo("c-usd", "lead"), ContactInfo("c-eur", "lead"))


def test_a_meeting_shared_across_currencies_counts_once_in_the_latest_touchs_row(
    make: MakeContext,
) -> None:
    public = "MEETINGS_PUBLIC"
    meetings = [
        MeetingInfo("m-shared", _t(14), _t(15), meeting_source=public),
        MeetingInfo("m-usd", _t(11), _t(16), meeting_source=public),
        MeetingInfo("m-before", _t(9), _t(9, 13), meeting_source=public),
    ]
    shared = [meeting.hs_id for meeting in meetings]
    crm = _crm(
        TWO_CURRENCY_CONTACTS,
        meetings=meetings,
        contact_meetings={"c-usd": shared, "c-eur": shared},
    )
    table = build_attribution(make(crm=crm, leads=TWO_CURRENCY_LEADS, spend=TWO_CURRENCY_SPEND))

    usd, eur = _row(table, REPORT, "USD"), _row(table, REPORT, "EUR")
    # Three distinct meetings: two booked after a touch and one before, each in one row only.
    assert (usd.meetings_booked.value, usd.meetings_before_touch) == (1, 1)
    assert (eur.meetings_booked.value, eur.meetings_before_touch) == (1, 0)
    # m-shared follows both touches: the latest (c-eur, EUR) owns it; m-usd precedes c-eur's.
    assert _credit(eur, CR_EUR).meetings_booked.value == 1
    assert _credit(usd, CR_A).meetings_booked.value == 1
    # Costs stay within their row's currency.
    assert (usd.cost_per_meeting.render(), eur.cost_per_meeting.render()) == ("100.00", "80.00")
    for row in (usd, eur):
        assert (row.matched_contacts, row.unknown_evidence_contacts) == (1, 0)
        assert row.other_meetings.value == 0
        _assert_partition(row)


def test_a_deal_shared_across_currencies_counts_once_in_the_latest_touchs_row(
    make: MakeContext,
) -> None:
    deals = [DealInfo("d-shared", _t(13)), DealInfo("d-usd", _t(11)), DealInfo("d-before", _t(9))]
    shared = [deal.hs_id for deal in deals]
    crm = _crm(TWO_CURRENCY_CONTACTS, deals=deals, contact_deals={"c-usd": shared, "c-eur": shared})
    table = build_attribution(make(crm=crm, leads=TWO_CURRENCY_LEADS, spend=TWO_CURRENCY_SPEND))

    usd, eur = _row(table, REPORT, "USD"), _row(table, REPORT, "EUR")
    assert (usd.deals_after_touch.value, usd.deals_before_touch) == (1, 1)
    assert (eur.deals_after_touch.value, eur.deals_before_touch) == (1, 0)
    assert (_credit(usd, CR_A).deals.value, _credit(eur, CR_EUR).deals.value) == (1, 1)
    for row in (usd, eur):
        _assert_partition(row)


def test_a_contact_touching_an_asset_in_two_currencies_enters_each_stage_once(
    make: MakeContext,
) -> None:
    contacts = [
        # Anchor EUR (day 10): MQL before it is reported once, in EUR; the QL (day 13) follows
        # the USD touch (day 12) and counts there only.
        ContactInfo("c1", "salesqualifiedlead", mql_at=_t(9), ql_at=_t(13)),
        # Anchor USD (day 10): the QL (day 11) follows the USD touch, the MQL (day 14) the EUR
        # touch (day 12); each counts once, in its own touch's row.
        ContactInfo("c2", "salesqualifiedlead", mql_at=_t(14), ql_at=_t(11)),
        # Skipped both stages: reported once, in the row of its anchor (USD, day 9).
        ContactInfo("c3", "customer"),
    ]
    leads = [
        _lead(1, _t(10), contact="c1", creative=CR_EUR, campaign=CAMPAIGN_EUR),
        _lead(2, _t(12), contact="c1", creative=CR_A),
        _lead(3, _t(10), contact="c2", creative=CR_A),
        _lead(4, _t(12), contact="c2", creative=CR_EUR, campaign=CAMPAIGN_EUR),
        _lead(5, _t(9), contact="c3", creative=CR_A),
        _lead(6, _t(15), contact="c3", creative=CR_EUR, campaign=CAMPAIGN_EUR),
    ]
    table = build_attribution(make(crm=_crm(contacts), leads=leads, spend=TWO_CURRENCY_SPEND))

    usd, eur = _row(table, REPORT, "USD"), _row(table, REPORT, "EUR")
    assert (usd.mql.value, usd.already_mql_before, usd.skipped_mql) == (0, 0, 1)
    assert (eur.mql.value, eur.already_mql_before, eur.skipped_mql) == (1, 1, 0)
    assert (usd.ql.value, usd.already_ql_before, usd.skipped_ql) == (2, 0, 1)
    assert (eur.ql.value, eur.already_ql_before, eur.skipped_ql) == (0, 0, 0)
    # Across the asset: one MQL (c2), two QL (c1, c2), each counted in exactly one row.
    assert (usd.mql.value + eur.mql.value, usd.ql.value + eur.ql.value) == (1, 2)
    assert (_credit(usd, CR_A).mql.value, _credit(usd, CR_A).ql.value) == (0, 2)
    assert (_credit(eur, CR_EUR).mql.value, _credit(eur, CR_EUR).ql.value) == (1, 0)
    for row in (usd, eur):
        # Leads, matched contacts and shares stay the row's own.
        assert (row.linkedin_leads, row.matched_contacts, row.unknown_evidence_contacts) == (
            3,
            3,
            0,
        )
        assert row.flags == ()
        _assert_partition(row)
    assert (usd.cost_per_ql.render(), eur.cost_per_ql.render()) == ("50.00", "—")
    assert eur.cost_per_ql.note == NO_QL_NOTE

    # The skipped contact is reported once, on the USD row only.
    assert [
        (finding.code, finding.entity.external_id if finding.entity else None)
        for finding in table.findings
    ] == [
        (FINDING_SKIPPED_STAGE, "pipeline-report|USD"),
        (FINDING_SKIPPED_STAGE, "pipeline-report|USD"),
    ]

    # Totals and medians stay within each currency; nothing combines the two cohorts.
    assert [cohort.currency for cohort in table.cohorts] == ["EUR", "USD"]
    summary = {
        cohort.currency: (cohort.spend_total, cohort.total_ql, cohort.median_cost_per_ql)
        for cohort in table.cohorts
    }
    assert summary == {
        "EUR": (Decimal("80.00"), 0, None),
        "USD": (Decimal("100.00"), 2, Decimal("50.00")),
    }
    for cohort in table.cohorts:
        assert [row.currency for row in cohort.rows] == [cohort.currency]
        assert set(cohort.to_json()) == {
            "currency",
            "spend_total",
            "median_cost_per_ql",
            "overlap_note",
            "rows",
        }


def test_a_cohort_rejects_a_row_in_another_currency(make: MakeContext) -> None:
    table = build_attribution(make(crm=_crm(), spend=[_spend(CR_A, "10.00")]))
    row = _row(table)
    with pytest.raises(ValueError, match="rows in another currency"):
        Cohort("EUR", (row,), Decimal("10.00"), 0, None)


def test_a_contact_who_touched_two_assets_is_credited_to_both(make: MakeContext) -> None:
    contact = ContactInfo("c1", "salesqualifiedlead", mql_at=_t(13), ql_at=_t(11))
    table = build_attribution(
        make(
            crm=_crm([contact]),
            leads=[
                _lead(1, _t(10), contact="c1", creative=CR_A),
                _lead(2, _t(12), contact="c1", creative=CR_PLAYBOOK, form=FORM_PLAYBOOK),
            ],
        )
    )

    report, playbook = _row(table, REPORT), _row(table, PLAYBOOK)
    # Any-touch: the MQL after both touches counts on both assets.
    assert (report.mql.value, playbook.mql.value) == (1, 1)
    # Each (contact, asset) pair has its own anchor: the QL precedes the playbook touch.
    assert (report.ql.value, report.already_ql_before) == (1, 0)
    assert (playbook.ql.value, playbook.already_ql_before) == (0, 1)
    (cohort,) = table.cohorts
    assert cohort.overlap_note == OVERLAP_NOTE == "counts overlap across assets"
    assert cohort.to_json()["overlap_note"] == OVERLAP_NOTE


# ---------------------------------------------------------------------------
# Causal check: MQL and QL
# ---------------------------------------------------------------------------


def test_stage_entries_count_only_at_or_after_the_contacts_anchor(make: MakeContext) -> None:
    touch = _t(10)
    contacts = [
        ContactInfo("c-on", "salesqualifiedlead", mql_at=touch, ql_at=touch),
        # QL observed on its own bound date while MQL is empty: never derived from MQL.
        ContactInfo("c-after", "salesqualifiedlead", ql_at=touch + timedelta(days=1)),
        ContactInfo(
            "c-before",
            "salesqualifiedlead",
            mql_at=touch - timedelta(days=2),
            ql_at=touch - timedelta(hours=1),
        ),
        # A date after the run is not yet an event.
        ContactInfo(
            "c-future", "lead", mql_at=touch + timedelta(days=2), ql_at=NOW + timedelta(hours=1)
        ),
        ContactInfo("c-lead", "lead"),
        ContactInfo("c-skip", "customer"),
        ContactInfo("c-blank", None),
        ContactInfo("c-sub", " Subscriber "),
    ]
    leads = [
        _lead(index, touch, contact=contact.hs_id) for index, contact in enumerate(contacts, 1)
    ]
    table = build_attribution(make(crm=_crm(contacts), leads=leads, spend=[_spend(CR_A, "300.00")]))

    row = _row(table)
    assert (row.mql.render(), row.already_mql_before, row.skipped_mql) == ("2", 1, 2)
    assert (row.ql.render(), row.already_ql_before, row.skipped_ql) == ("2", 1, 1)
    assert row.cost_per_ql.render() == "150.00"
    assert row.matched_contacts == 8
    assert row.flags == ()

    skipped = [finding for finding in table.findings if finding.code == FINDING_SKIPPED_STAGE]
    assert skipped == list(table.findings)
    assert [finding.to_json() for finding in skipped] == [
        {
            "code": FINDING_SKIPPED_STAGE,
            "section": WHITE_PAPERS,
            "entity": row.entity.to_json(),
            "message": "MQL (First meeting booked): 2 contacts skipped this stage (not counted)",
            "evidence": {
                "stage": "MQL",
                "label": "First meeting booked",
                "property": MQL_PROPERTY,
                "skipped": 2,
                "asset": REPORT,
                "currency": "USD",
            },
        },
        {
            "code": FINDING_SKIPPED_STAGE,
            "section": WHITE_PAPERS,
            "entity": row.entity.to_json(),
            # The QL label equals its stage name, so it is not repeated in parentheses.
            "message": "Qualified Lead: 1 contact skipped this stage (not counted)",
            "evidence": {
                "stage": "Qualified Lead",
                "label": "Qualified Lead",
                "property": QL_PROPERTY,
                "skipped": 1,
                "asset": REPORT,
                "currency": "USD",
            },
        },
    ]


@pytest.mark.parametrize(
    ("label", "heading"),
    [
        ("Qualified Lead", "Qualified Lead"),
        (" qualified LEAD ", "Qualified Lead"),
        ("Sales Qualified Lead", "Qualified Lead (Sales Qualified Lead)"),
    ],
)
@pytest.mark.parametrize(("skipped", "noun"), [(1, "contact"), (3, "contacts")])
def test_a_skipped_stage_heading_omits_a_label_that_repeats_the_stage_name(
    make: MakeContext, label: str, heading: str, skipped: int, noun: str
) -> None:
    touch = _t(10)
    bindings = dataclasses.replace(_bindings(), ql=ResolvedBinding(True, QL_PROPERTY, "222", label))
    contacts = [
        ContactInfo(f"c{index}", "customer", mql_at=touch + timedelta(days=1))
        for index in range(1, skipped + 1)
    ]
    leads = [
        _lead(index, touch, contact=contact.hs_id) for index, contact in enumerate(contacts, 1)
    ]
    table = build_attribution(
        make(crm=_crm(contacts, bindings=bindings), leads=leads, spend=[_spend(CR_A, "90.00")])
    )

    row = _row(table)
    assert (row.skipped_mql, row.skipped_ql) == (0, skipped)
    (finding,) = table.findings
    assert finding.code == FINDING_SKIPPED_STAGE
    assert finding.message == f"{heading}: {skipped} {noun} skipped this stage (not counted)"
    # Only the message changes: the evidence still carries the stage and its bound label.
    assert finding.evidence == {
        "stage": "Qualified Lead",
        "label": label,
        "property": QL_PROPERTY,
        "skipped": skipped,
        "asset": REPORT,
        "currency": "USD",
    }


def test_a_pipeline_unavailable_heading_omits_a_label_that_repeats_the_stage_name(
    make: MakeContext,
) -> None:
    bindings = dataclasses.replace(
        _bindings(), mql=ResolvedBinding(False, reason=MQL_REASON, label="mql")
    )
    contact = ContactInfo("c1", "salesqualifiedlead", ql_at=_t(12))
    table = build_attribution(
        make(crm=_crm([contact], bindings=bindings), leads=[_lead(1, _t(10), contact="c1")])
    )

    # The resolved label "mql" only repeats the stage name, so the heading is the bare name.
    assert table.mql_label == "mql"
    assert [finding.message for finding in table.findings] == [
        f"MQL count unavailable: {MQL_REASON}"
    ]


def test_a_failed_refresh_keeps_stage_dates_but_reports_no_skipped_stage(
    make: MakeContext,
) -> None:
    contacts = [
        ContactInfo("c-stale", "customer", ql_at=_t(12)),
        ContactInfo("c-fresh", "salesqualifiedlead", ql_at=_t(12)),
    ]
    leads = [
        _lead(1, _t(10), contact="c-stale", read_error=True),
        # One failed refresh among the contact's matched leads is enough.
        _lead(2, _t(11), contact="c-stale"),
        _lead(3, _t(10), contact="c-fresh"),
    ]
    table = build_attribution(make(crm=_crm(contacts), leads=leads, spend=[_spend(CR_A, "90")]))

    row = _row(table)
    # Stage-entry dates never move, so the stale contact's QL still counts; its current stage
    # is unknown, so only the fresh contact is reported as having skipped MQL.
    assert row.ql.value == 2
    assert (row.mql.value, row.skipped_mql, row.skipped_ql) == (0, 1, 0)
    assert (row.unknown_evidence_contacts, row.matched_contacts) == (1, 2)
    assert row.unknown_evidence_share == Decimal("0.5")
    assert row.flag_messages[FLAG_UNKNOWN_CRM_EVIDENCE] == (
        "Floor: CRM evidence unknown for 50% of matched contacts (1 of 2)"
    )
    assert (row.ql.render(), row.mql.render()) == ("≥ 2", "≥ 0")
    assert row.cost_per_ql.render() == "≤ 45.00"


def test_a_refresh_never_recorded_is_unknown_evidence_and_a_floor(make: MakeContext) -> None:
    # Only an explicit "ok" records a successful refresh: a matched lead without a refresh
    # status leaves its contact's stage dates not current, exactly like a failed refresh.
    contacts = [
        ContactInfo("c-unrecorded", "customer", ql_at=_t(12)),
        ContactInfo("c-fresh", "salesqualifiedlead", ql_at=_t(12)),
    ]
    unrecorded = dataclasses.replace(
        _lead(1, _t(10), contact="c-unrecorded"), contact_read_status=None
    )
    leads = [unrecorded, _lead(2, _t(10), contact="c-fresh")]
    crm = _crm(contacts)
    table = build_attribution(make(crm=crm, leads=leads, spend=[_spend(CR_A, "90")]))

    assert contact_evidence_known(crm, "c-unrecorded", unrecorded) == (
        False,
        "contact refresh not recorded; stage dates not current",
    )
    row = _row(table)
    assert row.ql.value == 2
    assert (row.mql.value, row.skipped_mql, row.skipped_ql) == (0, 1, 0)
    assert (row.unknown_evidence_contacts, row.matched_contacts) == (1, 2)
    assert row.flag_messages[FLAG_UNKNOWN_CRM_EVIDENCE] == (
        "Floor: CRM evidence unknown for 50% of matched contacts (1 of 2)"
    )
    assert (row.ql.render(), row.cost_per_ql.render()) == ("≥ 2", "≤ 45.00")


def test_repeat_touches_give_one_entry_credited_to_the_latest_prior_touch(
    make: MakeContext,
) -> None:
    contact = ContactInfo("c1", "salesqualifiedlead", mql_at=_t(11), ql_at=_t(13))
    crm = _crm(
        [contact],
        meetings=[MeetingInfo("m1", _t(11, 6), _t(16), meeting_source="MEETINGS_PUBLIC")],
        deals=[DealInfo("d1", _t(15))],
        contact_meetings={"c1": ["m1"]},
        contact_deals={"c1": ["d1"]},
    )
    leads = [
        _lead(1, _t(10), contact="c1", creative=CR_A),
        _lead(2, _t(12), contact="c1", creative=CR_B, form=FORM_REPORT_2),
        _lead(3, _t(14), contact="c1", creative=None),
    ]
    table = build_attribution(
        make(crm=crm, leads=leads, spend=[_spend(CR_A, "60.00"), _spend(CR_B, "40.00")])
    )

    row = _row(table)
    # One MQL and one QL per contact per asset, however many touches.
    assert (row.linkedin_leads, row.mql.value, row.ql.value) == (3, 1, 1)
    assert (row.meetings_booked.value, row.deals_after_touch.value) == (1, 1)
    credit_a, credit_b, unknown = row.creative_rows
    assert (credit_a.creative_id, credit_b.creative_id, unknown.is_unknown) == (CR_A, CR_B, True)
    # The MQL (day 11) and the meeting (created day 11) follow touch A (day 10).
    assert (credit_a.mql.value, credit_a.meetings_booked.value, credit_a.ql.value) == (1, 1, 0)
    # The QL (day 13) follows touch B (day 12).
    assert (credit_b.ql.value, credit_b.mql.value, credit_b.deals.value) == (1, 0, 0)
    # The deal (day 15) follows the creative-less touch (day 14).
    assert (unknown.deals.value, unknown.leads, unknown.spend) == (1, 1, Decimal(0))
    assert unknown.label == UNKNOWN_CREATIVE_LABEL
    assert unknown.cost_per_ql.render() == "—"
    assert unknown.cost_per_ql.note == "spend is attributed only to known creatives"
    assert unknown.cost_per_meeting.render() == "—"
    _assert_partition(row)

    # A third of the leads have no creative: the breakdown, not the asset total, is a floor.
    assert row.creative_share == Decimal(1) / Decimal(3)
    assert row.flag_messages[FLAG_CREATIVE_FLOOR] == (
        "Per-creative breakdown is a floor: 33.3% of leads have no creative"
    )
    assert (row.mql.render(), credit_a.mql.render(), unknown.deals.render()) == ("1", "≥ 1", "1")
    assert credit_a.cost_per_ql.render() == "—"
    assert credit_b.cost_per_ql.render() == "≤ 40.00"


# ---------------------------------------------------------------------------
# Causal check: meetings and deals
# ---------------------------------------------------------------------------


def test_a_meeting_is_booked_only_with_creation_and_start_after_the_anchor(
    make: MakeContext,
) -> None:
    touch = _t(10)
    hour, day = timedelta(hours=1), timedelta(days=1)
    public = "MEETINGS_PUBLIC"
    meetings = [
        # On the touch instant: counted.
        MeetingInfo("m-touch", touch, touch + 3 * day, meeting_source=public),
        # Source codes are normalised before the comparison.
        MeetingInfo("m-lower", touch + hour, touch + 2 * day, meeting_source=" meetings_public "),
        # hs_timestamp stands in for an empty hs_meeting_start_time.
        MeetingInfo("m-ts", touch + 2 * hour, None, touch + 4 * day, meeting_source=public),
        # Other sources pass the same checks but land in "ordering unverified".
        MeetingInfo("m-other", touch + day, touch + 5 * day, meeting_source="CRM_UI"),
        MeetingInfo("m-other-none", touch + day, touch + 6 * day),
        # Excluded outcomes are never counted, in either column or as "before".
        MeetingInfo(
            "m-cancel", touch + day, touch + 2 * day, outcome="canceled", meeting_source=public
        ),
        MeetingInfo("m-cancel-early", touch - day, touch - day, outcome="CANCELED"),
        # Created after the touch but starting before it: excluded and reported.
        MeetingInfo("m-start-before", touch + hour, touch - hour, meeting_source=public),
        MeetingInfo("m-created-before", touch - day, touch + day, meeting_source=public),
        # Without a creation or start instant the check cannot pass.
        MeetingInfo("m-no-created", None, touch + day, meeting_source=public),
        MeetingInfo("m-no-start", touch + hour, None, meeting_source=public),
        # Created after the run: not yet an event.
        MeetingInfo("m-future", NOW + hour, NOW + 2 * day, meeting_source=public),
    ]
    contact = ContactInfo("c1", "salesqualifiedlead")
    crm = _crm(
        [contact],
        meetings=meetings,
        contact_meetings={"c1": [meeting.hs_id for meeting in meetings]},
    )
    table = build_attribution(
        make(crm=crm, leads=[_lead(1, touch, contact="c1")], spend=[_spend(CR_A, "600.00")])
    )

    row = _row(table)
    assert row.meetings_booked.render() == "3"
    assert row.other_meetings.render() == "2"
    assert row.meetings_before_touch == 2
    # Cost per meeting divides by booked meetings only.
    assert row.cost_per_meeting.value == Decimal(200)
    assert row.cost_per_meeting.render() == "200.00"
    assert row.flags == (FLAG_MEETING_TIME_PROXY,)
    assert row.flag_messages[FLAG_MEETING_TIME_PROXY] == (
        "2 other meetings (ordering unverified): booking time is proxied by record creation, "
        "so they are excluded from cost per meeting"
    )
    credit = _credit(row, CR_A)
    assert (credit.meetings_booked.value, credit.other_meetings.value) == (3, 2)
    assert credit.cost_per_meeting.render() == "200.00"
    _assert_partition(row)


def test_one_other_meeting_is_named_in_the_singular(make: MakeContext) -> None:
    touch = _t(10)
    day = timedelta(days=1)
    meeting = MeetingInfo("m-other", touch + day, touch + 2 * day, meeting_source="CRM_UI")
    crm = _crm(
        [ContactInfo("c1", "salesqualifiedlead")],
        meetings=[meeting],
        contact_meetings={"c1": [meeting.hs_id]},
    )
    table = build_attribution(
        make(crm=crm, leads=[_lead(1, touch, contact="c1")], spend=[_spend(CR_A, "60.00")])
    )

    row = _row(table)
    assert (row.meetings_booked.render(), row.other_meetings.render()) == ("0", "1")
    assert row.flags == (FLAG_MEETING_TIME_PROXY,)
    assert row.flag_messages[FLAG_MEETING_TIME_PROXY] == (
        "1 other meeting (ordering unverified): booking time is proxied by record creation, "
        "so it is excluded from cost per meeting"
    )


@pytest.mark.parametrize(
    ("count", "named"),
    [
        (
            1,
            "≥ 1 other meeting (ordering unverified): booking time is proxied by record "
            "creation, so it is excluded from cost per meeting",
        ),
        (
            2,
            "≥ 2 other meetings (ordering unverified): booking time is proxied by record "
            "creation, so they are excluded from cost per meeting",
        ),
    ],
)
def test_a_floored_other_meeting_count_keeps_its_floor_in_the_proxy_flag(
    make: MakeContext, count: int, named: str
) -> None:
    # AAP 0.5.4.7/0.5.5: under a contact floor every count reads "≥ N", and the flag repeats
    # the count exactly as the "Other meetings (ordering unverified)" column shows it.
    touch = _t(10)
    day = timedelta(days=1)
    meetings = [
        MeetingInfo(f"m-other-{index}", touch + day, touch + 2 * day, meeting_source="CRM_UI")
        for index in range(count)
    ]
    crm = _crm(
        [ContactInfo("c1", "salesqualifiedlead")],
        meetings=meetings,
        contact_meetings={"c1": [meeting.hs_id for meeting in meetings]},
    )
    leads = [_lead(1, touch, contact="c1"), _lead(2, touch)]
    table = build_attribution(make(crm=crm, leads=leads, spend=[_spend(CR_A, "60.00")]))

    row = _row(table)
    assert row.contact_share == Decimal("0.5")
    assert row.other_meetings.render() == f"≥ {count}"
    assert row.flags == (FLAG_ATTRIBUTION_FLOOR, FLAG_MEETING_TIME_PROXY)
    assert row.flag_messages[FLAG_MEETING_TIME_PROXY] == named


def test_events_of_several_contacts_count_once_for_the_latest_qualifying_touch(
    make: MakeContext,
) -> None:
    public = "MEETINGS_PUBLIC"
    meetings = [
        MeetingInfo("m-late", _t(14), _t(15), meeting_source=public),
        MeetingInfo("m-mid", _t(11), _t(16), meeting_source=public),
        MeetingInfo("m-early", _t(9), _t(9, 13), meeting_source=public),
    ]
    deals = [DealInfo("d-late", _t(13)), DealInfo("d-mid", _t(11)), DealInfo("d-early", _t(9))]
    both = {"c1": ["m-late", "m-mid", "m-early"], "c2": ["m-late", "m-mid", "m-early"]}
    both_deals = {"c1": ["d-late", "d-mid", "d-early"], "c2": ["d-late", "d-mid", "d-early"]}
    crm = _crm(
        [ContactInfo("c1", "lead"), ContactInfo("c2", "lead")],
        meetings=meetings,
        deals=deals,
        contact_meetings=both,
        contact_deals=both_deals,
    )
    leads = [
        _lead(1, _t(10), contact="c1", creative=CR_A),
        _lead(2, _t(12), contact="c2", creative=CR_B, form=FORM_REPORT_2),
    ]
    table = build_attribution(make(crm=crm, leads=leads, spend=[_spend(CR_A, "10.00")]))

    row = _row(table)
    # Two shared meetings and two shared deals, each counted once, not once per contact.
    assert (row.meetings_booked.value, row.meetings_before_touch) == (2, 1)
    assert (row.deals_after_touch.value, row.deals_before_touch) == (2, 1)
    credit_a, credit_b = _credit(row, CR_A), _credit(row, CR_B)
    # Events after both touches go to the latest touch across the contacts (c2, creative B);
    # events between the touches qualify only through c1 (creative A).
    assert (credit_a.meetings_booked.value, credit_a.deals.value) == (1, 1)
    assert (credit_b.meetings_booked.value, credit_b.deals.value) == (1, 1)
    _assert_partition(row)


def test_a_deal_counts_only_when_created_at_or_after_the_anchor(make: MakeContext) -> None:
    touch = _t(10)
    deals = [
        DealInfo("d-on", touch),
        DealInfo("d-after", touch + timedelta(days=1), stage="appointmentscheduled"),
        DealInfo("d-before", touch - timedelta(seconds=1)),
        DealInfo("d-none", None),
        DealInfo("d-future", NOW + timedelta(minutes=1)),
    ]
    crm = _crm(
        [ContactInfo("c1", "lead")],
        deals=deals,
        contact_deals={"c1": [deal.hs_id for deal in deals]},
    )
    table = build_attribution(make(crm=crm, leads=[_lead(1, touch, contact="c1")]))

    row = _row(table)
    assert (row.deals_after_touch.render(), row.deals_before_touch) == ("2", 1)
    assert _credit(row, CR_A).deals.value == 2
    assert row.unknown_evidence_contacts == 0


# ---------------------------------------------------------------------------
# Costs: cost per Qualified Lead and per booked meeting, within one currency
# ---------------------------------------------------------------------------


def test_costs_divide_spend_by_ql_and_by_booked_meetings(make: MakeContext) -> None:
    leads, contacts = _qualified("ql", 4, creative=CR_A, form=FORM_REPORT_1, first_id=1)
    public = "MEETINGS_PUBLIC"
    meetings = [
        MeetingInfo("m1", _t(20), _t(21), meeting_source=public),
        MeetingInfo("m2", _t(20), _t(22), meeting_source=public),
        MeetingInfo("m3", _t(20), _t(23), meeting_source=public),
        MeetingInfo("m4", _t(20), _t(24), meeting_source="CALENDAR"),
    ]
    crm = _crm(
        contacts,
        meetings=meetings,
        contact_meetings={"ql-0": ["m1", "m2"], "ql-1": ["m3", "m4"]},
    )
    table = build_attribution(make(crm=crm, leads=leads, spend=[_spend(CR_A, "1000.00")]))

    row = _row(table)
    assert row.cost_per_ql.value == Decimal(250)
    assert row.cost_per_ql.render() == "250.00"
    # Spend over booked meetings only; the ordering-unverified meeting is excluded.
    assert row.cost_per_meeting.value == Decimal("1000.00") / Decimal(3)
    assert row.cost_per_meeting.render() == "333.33"
    assert row.cost_per_ql.ceiling is False
    (cohort,) = table.cohorts
    assert cohort.median_cost_per_ql == Decimal(250)


def test_a_zero_denominator_cost_renders_a_dash_with_its_note(make: MakeContext) -> None:
    contact = ContactInfo("c1", "lead")
    table = build_attribution(
        make(
            crm=_crm([contact]),
            leads=[_lead(1, _t(10), contact="c1")],
            spend=[_spend(CR_A, "75.00")],
        )
    )

    row = _row(table)
    assert (row.ql.render(), row.meetings_booked.render()) == ("0", "0")
    assert (row.cost_per_ql.value, row.cost_per_ql.note) == (None, NO_QL_NOTE)
    assert (row.cost_per_meeting.value, row.cost_per_meeting.note) == (None, NO_MEETINGS_NOTE)
    assert (row.cost_per_ql.render(), row.cost_per_meeting.render()) == ("—", "—")
    credit = _credit(row, CR_A)
    assert (credit.cost_per_ql.note, credit.cost_per_meeting.note) == (NO_QL_NOTE, NO_MEETINGS_NOTE)
    (cohort,) = table.cohorts
    assert (cohort.total_ql, cohort.median_cost_per_ql) == (0, None)


# ---------------------------------------------------------------------------
# Floors: contact share, unknown CRM evidence share, creative share
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "no_match", "no_email", "error", None])
def test_the_contact_floor_fires_at_ten_percent_unmatched_leads(
    make: MakeContext, status: MatchStatus | None
) -> None:
    leads, contacts = _qualified("ok", 9, creative=CR_A, form=FORM_REPORT_1, first_id=1)
    unmatched = dataclasses.replace(_lead(10, _t(9)), match_status=status)
    table = build_attribution(
        make(crm=_crm(contacts), leads=[*leads, unmatched], spend=[_spend(CR_A, "900.00")])
    )

    row = _row(table)
    assert (row.contact_share, row.unmatched_leads, row.linkedin_leads) == (Decimal("0.1"), 1, 10)
    assert FLAG_ATTRIBUTION_FLOOR in row.flags
    assert row.flag_messages[FLAG_ATTRIBUTION_FLOOR] == "Floor: 10% of leads unmatched in HubSpot"
    assert row.has_flag(FLAG_ATTRIBUTION_FLOOR)
    assert Flag(FLAG_ATTRIBUTION_FLOOR, row.flag_messages[FLAG_ATTRIBUTION_FLOOR]) in (
        row.to_flags()
    )
    rendered = (
        row.mql.render(),
        row.ql.render(),
        row.meetings_booked.render(),
        row.other_meetings.render(),
        row.deals_after_touch.render(),
    )
    assert rendered == ("≥ 0", "≥ 9", "≥ 0", "≥ 0", "≥ 0")
    # A ceiling only over a known denominator above 0; a zero denominator stays "—".
    assert row.cost_per_ql.render() == "≤ 100.00"
    assert row.cost_per_ql.ceiling is True
    assert row.cost_per_meeting.render() == "—"
    assert row.cost_per_meeting.ceiling is False
    assert _credit(row, CR_A).ql.render() == "≥ 9"


def test_the_contact_floor_does_not_fire_at_nine_point_nine_percent(make: MakeContext) -> None:
    contact = ContactInfo("c1", "salesqualifiedlead", ql_at=_t(25))
    matched = [
        _lead(index, _t(8) + timedelta(minutes=index), contact="c1") for index in range(1, 902)
    ]
    unmatched = [_lead(index, _t(9)) for index in range(902, 1001)]
    table = build_attribution(
        make(crm=_crm([contact]), leads=[*matched, *unmatched], spend=[_spend(CR_A, "901.00")])
    )

    row = _row(table)
    assert (row.linkedin_leads, row.unmatched_leads) == (1000, 99)
    assert row.contact_share == Decimal("0.099")
    assert FLAG_ATTRIBUTION_FLOOR not in row.flags
    # Contacts count once in the matched denominator; their leads count once each.
    assert (row.matched_contacts, row.ql.render(), row.cost_per_ql.render()) == (1, "1", "901.00")


UNKNOWN_EVIDENCE_CASES: list[tuple[str, dict[str, Any], bool, str]] = [
    ("refresh failed", {}, True, "contact refresh failed; stage dates not current"),
    ("contact not read", {"contacts": ()}, False, "contact record not read"),
    (
        "meeting fetch missing",
        {"unfetched": [("contact", "c1", "meeting")]},
        False,
        "contact-to-meeting associations not fetched",
    ),
    (
        "meeting fetch error",
        {"fetches": [_fetch("contact", "c1", "meeting", "error")]},
        False,
        "contact-to-meeting association fetch error",
    ),
    (
        "deal fetch partial",
        {"fetches": [_fetch("contact", "c1", "deal", "partial", "ok")]},
        False,
        "contact-to-deal association fetch partial",
    ),
    (
        "meeting target reads partial",
        {"fetches": [_fetch("contact", "c1", "meeting", "ok", "partial")]},
        False,
        "meeting target reads partial",
    ),
    (
        "deal target reads error",
        {"fetches": [_fetch("contact", "c1", "deal", "ok", "error")]},
        False,
        "deal target reads error",
    ),
    (
        "target reads not recorded",
        {"fetches": [_fetch("contact", "c1", "meeting", "ok")]},
        False,
        "meeting target reads not recorded",
    ),
    (
        "associated deal not read",
        {"contact_deals": {"c1": ["d-unread"]}},
        False,
        "1 associated deal record(s) not read",
    ),
]


@pytest.mark.parametrize(
    ("changes", "read_error", "reason"),
    [case[1:] for case in UNKNOWN_EVIDENCE_CASES],
    ids=[case[0] for case in UNKNOWN_EVIDENCE_CASES],
)
def test_unknown_crm_evidence_is_a_floor_never_a_zero(
    make: MakeContext, changes: dict[str, Any], read_error: bool, reason: str
) -> None:
    fields: dict[str, Any] = {"contacts": [ContactInfo("c1", "lead")], **changes}
    crm = _crm(fields.pop("contacts"), **fields)
    lead = _lead(1, _t(10), contact="c1", read_error=read_error)
    table = build_attribution(make(crm=crm, leads=[lead], spend=[_spend(CR_A, "20.00")]))

    assert contact_evidence_known(crm, "c1", lead) == (False, reason)
    row = _row(table)
    assert (row.unknown_evidence_contacts, row.matched_contacts) == (1, 1)
    assert row.unknown_evidence_share == Decimal(1)
    assert row.flag_messages[FLAG_UNKNOWN_CRM_EVIDENCE] == (
        "Floor: CRM evidence unknown for 100% of matched contacts (1 of 1)"
    )
    assert FLAG_ATTRIBUTION_FLOOR not in row.flags
    # An unread meeting or deal is never counted as absent.
    assert (row.meetings_booked.render(), row.deals_after_touch.render()) == ("≥ 0", "≥ 0")
    assert row.ql.render() == "≥ 0"


def test_an_ok_empty_association_is_a_genuine_zero(make: MakeContext) -> None:
    crm = _crm([ContactInfo("c1", "lead")])
    lead = _lead(1, _t(10), contact="c1")
    table = build_attribution(make(crm=crm, leads=[lead]))

    assert contact_evidence_known(crm, "c1", lead) == (True, None)
    row = _row(table)
    assert (row.unknown_evidence_contacts, row.unknown_evidence_share) == (0, Decimal(0))
    assert (row.meetings_booked.render(), row.deals_after_touch.render()) == ("0", "0")
    assert row.flags == ()


def test_a_fully_read_association_with_every_target_is_known(make: MakeContext) -> None:
    crm = _crm(
        [ContactInfo("c1", "lead")],
        meetings=[MeetingInfo("m1", _t(11), _t(12), meeting_source="MEETINGS_PUBLIC")],
        deals=[DealInfo("d1", _t(12))],
        contact_meetings={"c1": ["m1"]},
        contact_deals={"c1": ["d1"]},
    )
    lead = _lead(1, _t(10), contact="c1")
    assert contact_evidence_known(crm, "c1", lead) == (True, None)
    row = _row(build_attribution(make(crm=crm, leads=[lead])))
    assert (row.meetings_booked.render(), row.deals_after_touch.render()) == ("1", "1")


# A stored edge outlives the fetch that verified it: each case fails the current fetch of one
# target type while the edge and its read meeting or deal are retained.
RETAINED_EDGE_CASES: list[tuple[str, AssociationFetchInfo, str]] = [
    (
        "meeting fetch error",
        _fetch("contact", "c1", "meeting", "error"),
        "contact-to-meeting association fetch error",
    ),
    (
        "meeting fetch partial",
        _fetch("contact", "c1", "meeting", "partial", "ok"),
        "contact-to-meeting association fetch partial",
    ),
    (
        "meeting target reads partial",
        _fetch("contact", "c1", "meeting", "ok", "partial"),
        "meeting target reads partial",
    ),
    (
        "deal fetch error",
        _fetch("contact", "c1", "deal", "error"),
        "contact-to-deal association fetch error",
    ),
    (
        "deal fetch partial",
        _fetch("contact", "c1", "deal", "partial", "ok"),
        "contact-to-deal association fetch partial",
    ),
    (
        "deal target reads partial",
        _fetch("contact", "c1", "deal", "ok", "partial"),
        "deal target reads partial",
    ),
]


@pytest.mark.parametrize(
    ("failed", "reason"),
    [case[1:] for case in RETAINED_EDGE_CASES],
    ids=[case[0] for case in RETAINED_EDGE_CASES],
)
def test_a_retained_edge_under_an_unknown_fetch_is_never_credited(
    make: MakeContext, failed: AssociationFetchInfo, reason: str
) -> None:
    crm = _crm(
        [ContactInfo("c1", "lead")],
        meetings=[MeetingInfo("m1", _t(11), _t(12), meeting_source="MEETINGS_PUBLIC")],
        deals=[DealInfo("d1", _t(12))],
        contact_meetings={"c1": ["m1"]},
        contact_deals={"c1": ["d1"]},
        fetches=[failed],
    )
    lead = _lead(1, _t(10), contact="c1")
    table = build_attribution(make(crm=crm, leads=[lead], spend=[_spend(CR_A, "20.00")]))

    assert contact_evidence_known(crm, "c1", lead) == (False, reason)
    row = _row(table)
    # The contact still counts as unknown evidence, so every count is a floor.
    assert (row.unknown_evidence_contacts, row.unknown_evidence_share) == (1, Decimal(1))
    assert FLAG_UNKNOWN_CRM_EVIDENCE in row.flags
    # The edge under the failed fetch credits nothing; the other, verified edge still does.
    meetings_failed = failed.to_type == "meeting"
    assert row.meetings_booked.render() == ("≥ 0" if meetings_failed else "≥ 1")
    assert row.deals_after_touch.render() == ("≥ 1" if meetings_failed else "≥ 0")
    assert (row.meetings_before_touch, row.deals_before_touch) == (0, 0)
    credit = _credit(row, CR_A)
    assert (credit.meetings_booked.value, credit.deals.value) == (
        (0, 1) if meetings_failed else (1, 0)
    )
    _assert_partition(row)


@pytest.mark.parametrize(
    ("meeting_fetch", "booked", "unknown"),
    [
        (_fetch("contact", "c-0", "meeting", "error"), "0", 1),
        (_fetch("contact", "c-0", "meeting", "ok", "ok"), "1", 0),
    ],
    ids=["error-fetch-not-credited", "ok-fetch-credited"],
)
def test_one_unknown_contact_below_the_floor_share_credits_no_retained_meeting(
    make: MakeContext, meeting_fetch: AssociationFetchInfo, booked: str, unknown: int
) -> None:
    # One contact of 20 (5%, below attribution.floor_share), so the count shows without a
    # floor: a retained meeting under the failed fetch must not make it 1.
    leads = [_lead(index + 1, _t(10), contact=f"c-{index}") for index in range(20)]
    crm = _crm(
        [ContactInfo(f"c-{index}", "lead") for index in range(20)],
        meetings=[MeetingInfo("m1", _t(11), _t(12), meeting_source="MEETINGS_PUBLIC")],
        contact_meetings={"c-0": ["m1"]},
        fetches=[meeting_fetch],
    )
    row = _row(build_attribution(make(crm=crm, leads=leads, spend=[_spend(CR_A, "40.00")])))

    assert (row.unknown_evidence_contacts, row.matched_contacts) == (unknown, 20)
    assert row.unknown_evidence_share == Decimal(unknown) / Decimal(20)
    assert FLAG_UNKNOWN_CRM_EVIDENCE not in row.flags
    assert row.meetings_booked.render() == booked
    assert row.meetings_booked.floor is False


def test_an_event_shared_with_an_unknown_contact_is_credited_through_known_contacts_only(
    make: MakeContext,
) -> None:
    # c2's later touch would win the credit, but c2's meeting and deal fetches failed, so the
    # shared meeting and deal are credited once, through c1's verified edges, to creative A.
    crm = _crm(
        [ContactInfo("c1", "lead"), ContactInfo("c2", "lead")],
        meetings=[MeetingInfo("m1", _t(14), _t(15), meeting_source="MEETINGS_PUBLIC")],
        deals=[DealInfo("d1", _t(14))],
        contact_meetings={"c1": ["m1"], "c2": ["m1"]},
        contact_deals={"c1": ["d1"], "c2": ["d1"]},
        fetches=[
            _fetch("contact", "c2", "meeting", "error"),
            _fetch("contact", "c2", "deal", "partial", "ok"),
        ],
    )
    leads = [
        _lead(1, _t(10), contact="c1", creative=CR_A),
        _lead(2, _t(12), contact="c2", creative=CR_B, form=FORM_REPORT_2),
    ]
    row = _row(build_attribution(make(crm=crm, leads=leads, spend=[_spend(CR_A, "10.00")])))

    assert (row.unknown_evidence_contacts, row.matched_contacts) == (1, 2)
    assert (row.meetings_booked.render(), row.deals_after_touch.render()) == ("≥ 1", "≥ 1")
    credit_a, credit_b = _credit(row, CR_A), _credit(row, CR_B)
    assert (credit_a.meetings_booked.value, credit_a.deals.value) == (1, 1)
    assert (credit_b.meetings_booked.value, credit_b.deals.value) == (0, 0)
    _assert_partition(row)


def test_an_edge_stored_under_an_ok_empty_fetch_is_stale_and_never_credited(
    make: MakeContext,
) -> None:
    # The current fetch found no association: a genuine zero, whatever an older edge says.
    crm = _crm(
        [ContactInfo("c1", "lead")],
        meetings=[MeetingInfo("m1", _t(11), _t(12), meeting_source="MEETINGS_PUBLIC")],
        deals=[DealInfo("d1", _t(12))],
        contact_meetings={"c1": ["m1"]},
        contact_deals={"c1": ["d1"]},
        fetches=[
            _fetch("contact", "c1", "meeting", "ok_empty"),
            _fetch("contact", "c1", "deal", "ok_empty"),
        ],
    )
    lead = _lead(1, _t(10), contact="c1")
    row = _row(build_attribution(make(crm=crm, leads=[lead])))

    assert contact_evidence_known(crm, "c1", lead) == (True, None)
    assert row.unknown_evidence_contacts == 0
    assert (row.meetings_booked.render(), row.deals_after_touch.render()) == ("0", "0")
    assert row.flags == ()


def test_unknown_evidence_below_the_floor_share_is_counted_but_not_flagged(
    make: MakeContext,
) -> None:
    leads, contacts = _qualified("c", 11, creative=CR_A, form=FORM_REPORT_1, first_id=1)
    crm = _crm(contacts, fetches=[_fetch("contact", "c-0", "deal", "error")])
    table = build_attribution(make(crm=crm, leads=leads, spend=[_spend(CR_A, "110.00")]))

    row = _row(table)
    assert (row.unknown_evidence_contacts, row.matched_contacts) == (1, 11)
    assert row.unknown_evidence_share == Decimal(1) / Decimal(11)
    assert FLAG_UNKNOWN_CRM_EVIDENCE not in row.flags
    assert (row.ql.render(), row.cost_per_ql.render()) == ("11", "10.00")


def test_contact_evidence_known_reports_an_unavailable_stage_binding() -> None:
    lead = _lead(1, _t(10), contact="c1")
    contacts = [ContactInfo("c1", "lead")]
    no_mql = _crm(contacts, bindings=_bindings(mql=False))
    no_ql = _crm(contacts, bindings=_bindings(ql=False))
    neither = _crm(contacts, bindings=_bindings(mql=False, ql=False))
    assert contact_evidence_known(no_mql, "c1", lead) == (
        False,
        f"MQL stage unavailable: {MQL_REASON}",
    )
    assert contact_evidence_known(no_ql, "c1", lead) == (
        False,
        f"Qualified Lead stage unavailable: {QL_REASON}",
    )
    assert contact_evidence_known(neither, "c1", lead)[1] == f"MQL stage unavailable: {MQL_REASON}"


@pytest.mark.parametrize(("leads_total", "floored"), [(10, True), (11, False)])
def test_the_creative_floor_labels_only_the_per_creative_breakdown(
    make: MakeContext, leads_total: int, floored: bool
) -> None:
    with_creative, contacts = _qualified(
        "c", leads_total - 1, creative=CR_A, form=FORM_REPORT_1, first_id=1
    )
    without, more = _qualified("n", 1, creative=None, form=FORM_REPORT_1, first_id=100)
    spend = Decimal(10 * (leads_total - 1))
    table = build_attribution(
        make(
            crm=_crm([*contacts, *more]),
            leads=[*with_creative, *without],
            spend=[_spend(CR_A, str(spend))],
        )
    )

    row = _row(table)
    assert row.creative_share == Decimal(1) / Decimal(leads_total)
    assert row.leads_without_creative == 1
    assert row.has_flag(FLAG_CREATIVE_FLOOR) is floored
    # Asset totals are unaffected by the creative floor.
    assert row.ql.render() == str(leads_total)
    known, unknown = _credit(row, CR_A), _credit(row, None)
    expected = f"≥ {leads_total - 1}" if floored else str(leads_total - 1)
    assert known.ql.render() == expected
    assert known.cost_per_ql.render() == ("≤ 10.00" if floored else "10.00")
    assert unknown.ql.render() == "1"
    if floored:
        assert row.flag_messages[FLAG_CREATIVE_FLOOR] == (
            "Per-creative breakdown is a floor: 10% of leads have no creative"
        )
    _assert_partition(row)


# ---------------------------------------------------------------------------
# Orphan-deal share (portal deals created from window start to run time)
# ---------------------------------------------------------------------------


def _deal_fetches(ok_empty: Sequence[str], ok: Sequence[str]) -> list[AssociationFetchInfo]:
    return [
        *(_fetch("deal", deal_id, "contact", "ok_empty") for deal_id in ok_empty),
        *(_fetch("deal", deal_id, "contact", "ok", "ok") for deal_id in ok),
    ]


def _deal_search(completeness: Completeness, reason: str | None) -> Freshness:
    return Freshness(
        dataset_id=HUBSPOT.dataset_id,
        dataset="hubspot.deal_search",
        last_attempt_at=NOW,
        last_success_at=NOW,
        covered_from=date(2026, 9, 7),
        covered_through=date(2026, 10, 5),
        completeness=completeness,
        reason=reason,
    )


NINE_LINKED = [f"d{index}" for index in range(1, 10)]
TEN_LINKED = [f"d{index}" for index in range(1, 11)]

ORPHAN_CASES: list[tuple[str, dict[str, Any], tuple[Any, ...]]] = [
    (
        "ten percent orphans",
        {"window_deal_ids": ["d0", *NINE_LINKED], "fetches": _deal_fetches(["d0"], NINE_LINKED)},
        (10, 1, 0, Decimal("0.1"), False, "10% (1 of 10 deals without a contact)", None, True),
    ),
    (
        "below the floor share",
        {"window_deal_ids": ["d0", *TEN_LINKED], "fetches": _deal_fetches(["d0"], TEN_LINKED)},
        (
            11,
            1,
            0,
            Decimal(1) / Decimal(11),
            False,
            "9.1% (1 of 11 deals without a contact)",
            None,
            False,
        ),
    ),
    (
        "repeated deal ids count once",
        {"window_deal_ids": ["d0", "d0", "d1"], "fetches": _deal_fetches(["d0"], ["d1"])},
        (2, 1, 0, Decimal("0.5"), False, "50% (1 of 2 deals without a contact)", None, True),
    ),
    (
        "no deals created",
        {},
        (0, 0, 0, None, False, "no deals created in range", None, False),
    ),
    (
        "unread deal target",
        {"window_deal_ids": ["d0"], "fetches": [_fetch("deal", "d0", "contact", "ok", "partial")]},
        (
            0,
            0,
            1,
            None,
            True,
            "unknown (1 deal not assessed)",
            "1 deal-to-contact fetch unknown",
            True,
        ),
    ),
    (
        "deal never fetched",
        {"window_deal_ids": ["d0", "d1"], "fetches": _deal_fetches(["d1"], [])},
        (
            1,
            1,
            1,
            Decimal(1),
            True,
            "unknown (1 deal not assessed)",
            "1 deal-to-contact fetch unknown",
            True,
        ),
    ),
    (
        "two deals not assessed",
        {
            "window_deal_ids": ["d0", "d1", "d2"],
            "fetches": [
                *_deal_fetches([], ["d2"]),
                _fetch("deal", "d0", "contact", "ok", "partial"),
            ],
        },
        (
            1,
            0,
            2,
            Decimal(0),
            True,
            "unknown (2 deals not assessed)",
            "2 deal-to-contact fetches unknown",
            True,
        ),
    ),
    (
        "one deal assessed",
        {"window_deal_ids": ["d1"], "fetches": _deal_fetches(["d1"], [])},
        (1, 1, 0, Decimal(1), False, "100% (1 of 1 deal without a contact)", None, True),
    ),
    (
        "deal search incomplete",
        {
            "window_deal_ids": ["d1"],
            "fetches": _deal_fetches([], ["d1"]),
            "deal_search_complete": False,
        },
        (
            1,
            0,
            0,
            Decimal(0),
            True,
            "unknown (deal search incomplete)",
            "deal search did not cover the range completely",
            True,
        ),
    ),
    (
        "incomplete search and unknown fetches",
        {"window_deal_ids": ["d0"], "deal_search_complete": False},
        (
            0,
            0,
            1,
            None,
            True,
            "unknown (deal search incomplete; at least 1 deal not assessed)",
            "deal search did not cover the range completely; 1 deal-to-contact fetch unknown",
            True,
        ),
    ),
]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [case[1:] for case in ORPHAN_CASES],
    ids=[case[0] for case in ORPHAN_CASES],
)
def test_the_orphan_deal_share_is_measured_over_the_assessed_deals(
    make: MakeContext, changes: dict[str, Any], expected: tuple[Any, ...]
) -> None:
    assessed, orphans, not_assessed, share, unknown, label, reason, floored = expected
    fields: dict[str, Any] = dict(changes)
    fetches = [*fields.pop("fetches", ())]
    crm = _crm([ContactInfo("c1", "lead")], fetches=fetches, **fields)
    table = build_attribution(make(crm=crm, leads=[_lead(1, _t(10), contact="c1")]))

    orphan = table.orphan_deals
    # The share stays exact for the floor comparison and serialises at six decimals.
    assert orphan.share == share
    assert orphan.to_json() == {
        "assessed": assessed,
        "orphans": orphans,
        "not_assessed": not_assessed,
        "share": None if share is None else format(bounded_decimal(share), "f"),
        "unknown": unknown,
        "label": label,
        "reason": reason,
    }
    assert orphan.is_floor(Decimal("0.10")) is floored
    row = _row(table)
    assert row.has_flag(FLAG_DEAL_FLOOR) is floored
    assert row.deals_after_touch.floor is floored
    assert _credit(row, CR_A).deals.floor is floored
    # Only the deal counts carry the orphan-deal floor.
    assert row.meetings_booked.floor is False
    if floored:
        assert row.flag_messages[FLAG_DEAL_FLOOR] == (
            f"Deal counts are a floor: orphan-deal share {label}"
        )
        assert row.deals_after_touch.render() == "≥ 0"


@pytest.mark.parametrize(
    ("completeness", "reason", "expected", "label"),
    [
        (
            "incomplete",
            "an hour above 10,000 results",
            "deal search incomplete: an hour above",
            "unknown (deal search incomplete)",
        ),
        ("unknown", None, "deal search unknown", "unknown (deal search completeness unknown)"),
    ],
)
def test_an_incomplete_deal_search_freshness_row_makes_the_share_unknown(
    make: MakeContext, completeness: Completeness, reason: str | None, expected: str, label: str
) -> None:
    crm = _crm(
        [ContactInfo("c1", "lead")],
        window_deal_ids=["d1"],
        fetches=_deal_fetches([], ["d1"]),
    )
    table = build_attribution(
        make(
            crm=crm,
            leads=[_lead(1, _t(10), contact="c1")],
            freshness=[_deal_search(completeness, reason)],
        )
    )

    orphan = table.orphan_deals
    assert (orphan.unknown, orphan.assessed, orphan.not_assessed) == (True, 1, 0)
    assert orphan.label == label
    assert orphan.reason is not None
    assert orphan.reason.startswith(expected)
    row = _row(table)
    assert row.has_flag(FLAG_DEAL_FLOOR)
    assert row.flag_messages[FLAG_DEAL_FLOOR] == (
        f"Deal counts are a floor: orphan-deal share {label}"
    )


def test_an_unknown_deal_search_with_unassessed_deals_states_a_lower_bound(
    make: MakeContext,
) -> None:
    crm = _crm(
        [ContactInfo("c1", "lead")],
        window_deal_ids=["d0", "d1"],
        fetches=_deal_fetches([], ["d1"]),
    )
    table = build_attribution(
        make(
            crm=crm,
            leads=[_lead(1, _t(10), contact="c1")],
            freshness=[_deal_search("unknown", None)],
        )
    )

    orphan = table.orphan_deals
    assert (orphan.unknown, orphan.assessed, orphan.not_assessed) == (True, 1, 1)
    assert orphan.label == (
        "unknown (deal search completeness unknown; at least 1 deal not assessed)"
    )
    assert orphan.reason == "deal search unknown; 1 deal-to-contact fetch unknown"
    assert _row(table).has_flag(FLAG_DEAL_FLOOR)


def test_a_complete_deal_search_freshness_row_keeps_the_share_known(make: MakeContext) -> None:
    crm = _crm(
        [ContactInfo("c1", "lead")],
        window_deal_ids=["d1"],
        fetches=_deal_fetches([], ["d1"]),
    )
    table = build_attribution(
        make(
            crm=crm,
            leads=[_lead(1, _t(10), contact="c1")],
            freshness=[_deal_search("complete", None)],
        )
    )
    assert table.orphan_deals.unknown is False
    assert table.orphan_deals.label == "0% (0 of 1 deal without a contact)"
    assert not _row(table).has_flag(FLAG_DEAL_FLOOR)


def test_the_orphan_share_is_a_floor_when_unknown_or_at_the_floor_share() -> None:
    known = OrphanDealShare(10, 1, 0, Decimal("0.1"), False, "10%")
    assert known.is_floor(Decimal("0.10")) is True
    assert known.is_floor(Decimal("0.11")) is False
    assert OrphanDealShare(0, 0, 3, None, True, "unknown (3 deals not assessed)").is_floor(
        Decimal("0.10")
    )
    assert (
        OrphanDealShare(0, 0, 0, None, False, "no deals created in range").is_floor(Decimal(0))
        is False
    )


# ---------------------------------------------------------------------------
# Stage bindings: unresolved means unavailable, never 0
# ---------------------------------------------------------------------------


def test_an_unresolved_ql_binding_is_unavailable_never_zero(make: MakeContext) -> None:
    contacts = [
        ContactInfo("c1", "customer", mql_at=_t(11), ql_at=_t(12)),
        ContactInfo("c2", "customer"),
    ]
    table = build_attribution(
        make(
            crm=_crm(contacts, bindings=_bindings(ql=False)),
            leads=[
                _lead(1, _t(10), contact="c1"),
                _lead(2, _t(10), contact="c2", creative=None),
            ],
            spend=[_spend(CR_A, "100.00")],
        )
    )

    assert (table.ql_available, table.mql_available) == (False, True)
    assert (table.mql_label, table.ql_label) == ("First meeting booked", "Qualified Lead")
    row = _row(table)
    assert row.ql == CountValue.missing(QL_REASON)
    assert (row.ql.render(), row.cost_per_ql.render()) == ("unavailable", "unavailable")
    assert row.cost_per_ql.note == "Qualified Lead count unavailable"
    assert (row.skipped_ql, row.already_ql_before) == (0, 0)
    # MQL is bound independently and still counts; c2 skipped it.
    assert (row.mql.render(), row.skipped_mql) == ("1", 1)
    assert row.flag_messages[FLAG_PIPELINE_UNAVAILABLE] == (
        f"Qualified Lead count unavailable: {QL_REASON}"
    )
    # A portal-wide binding is not per-contact evidence: it never turns counts into floors.
    assert (row.unknown_evidence_contacts, row.meetings_booked.render()) == (0, "0")
    for credit in row.creative_rows:
        assert credit.ql.render() == "unavailable"
        assert credit.cost_per_ql.render() == "unavailable"
    assert _credit(row, None).cost_per_meeting.render() == "—"
    _assert_partition(row)

    (cohort,) = table.cohorts
    assert (cohort.total_ql, cohort.median_cost_per_ql) == (None, None)
    binding, skipped = table.findings
    assert binding == Finding(
        FINDING_PIPELINE_UNAVAILABLE,
        WHITE_PAPERS,
        None,
        f"Qualified Lead count unavailable: {QL_REASON}",
        {
            "stage": "Qualified Lead",
            "label": "Qualified Lead",
            "property": None,
            "reason": QL_REASON,
        },
    )
    assert skipped.code == FINDING_SKIPPED_STAGE
    assert (
        skipped.message == "MQL (First meeting booked): 1 contact skipped this stage (not counted)"
    )


def test_an_unresolved_mql_binding_leaves_ql_counted(make: MakeContext) -> None:
    contact = ContactInfo("c1", "salesqualifiedlead", mql_at=_t(11), ql_at=_t(12))
    table = build_attribution(
        make(
            crm=_crm([contact], bindings=_bindings(mql=False)),
            leads=[_lead(1, _t(10), contact="c1")],
            spend=[_spend(CR_A, "30.00")],
        )
    )

    assert (table.ql_available, table.mql_available) == (True, False)
    row = _row(table)
    assert (row.mql.render(), row.mql.reason) == ("unavailable", MQL_REASON)
    assert (row.ql.render(), row.cost_per_ql.render()) == ("1", "30.00")
    assert FLAG_PIPELINE_UNAVAILABLE not in row.flags
    assert _credit(row, CR_A).mql.unavailable is True
    assert [finding.to_json() for finding in table.findings] == [
        {
            "code": FINDING_PIPELINE_UNAVAILABLE,
            "section": WHITE_PAPERS,
            "message": f"MQL (First meeting booked) count unavailable: {MQL_REASON}",
            "evidence": {
                "stage": "MQL",
                "label": "First meeting booked",
                "property": None,
                "reason": MQL_REASON,
            },
        }
    ]


def test_without_a_crm_dataset_every_crm_count_is_unavailable(make: MakeContext) -> None:
    lead = dataclasses.replace(_lead(1, _t(10)), match_status=None)
    table = build_attribution(make(crm=None, leads=[lead], spend=[_spend(CR_A, "50.00")]))

    assert (table.ql_available, table.mql_available) == (False, False)
    assert (table.mql_label, table.ql_label) == ("First meeting booked", "Qualified Lead")
    row = _row(table)
    counts = (
        row.mql,
        row.ql,
        row.meetings_booked,
        row.other_meetings,
        row.deals_after_touch,
    )
    assert all(count == CountValue.missing(NO_CRM_REASON) for count in counts)
    assert (row.cost_per_ql.render(), row.cost_per_meeting.render()) == (
        "unavailable",
        "unavailable",
    )
    assert row.cost_per_meeting.note == "meeting count unavailable"
    assert (row.matched_contacts, row.unknown_evidence_share) == (0, None)
    assert (row.contact_share, row.creative_share) == (Decimal(1), Decimal(0))
    assert row.flags == (FLAG_ATTRIBUTION_FLOOR, FLAG_DEAL_FLOOR, FLAG_PIPELINE_UNAVAILABLE)
    assert table.orphan_deals == OrphanDealShare(
        0, 0, 0, None, True, f"unknown ({NO_CRM_REASON})", NO_CRM_REASON
    )
    assert [finding.message for finding in table.findings] == [
        f"MQL (First meeting booked) count unavailable: {NO_CRM_REASON}",
        f"Qualified Lead count unavailable: {NO_CRM_REASON}",
    ]
    assert all(finding.evidence["property"] is None for finding in table.findings)
    _assert_partition(row)


def test_property_mode_labels_fall_back_to_the_bound_property(window: AnalysisWindow) -> None:
    config = _thresholds({"mql": {"mode": "property", "property": DATE_PROPERTY}})
    contact = ContactInfo("c1", "salesqualifiedlead", mql_at=_t(11))
    lead = _lead(1, _t(10), contact="c1")

    def table_for(crm: CrmData | None) -> AttributionTable:
        return build_attribution(_context(config, window, crm=crm, leads=[lead]))

    bound = CrmBindings(
        mql=ResolvedBinding(True, DATE_PROPERTY),
        ql=ResolvedBinding(True, "hs_v2_date_entered_1406943076", "1406943076", "Qualified Lead"),
        google=GoogleCrmBinding(False, reason="not configured"),
        verified_at=NOW,
    )
    resolved = table_for(_crm([contact], bindings=bound))
    # No label resolves in property mode: the resolved property heads the column.
    assert (resolved.mql_label, resolved.ql_label) == (DATE_PROPERTY, "Qualified Lead")
    assert _row(resolved).mql.render() == "1"

    # Without a resolved property, the configured one stands in.
    for crm in (None, _crm([contact], bindings=_bindings(mql=False))):
        table = table_for(crm)
        assert table.mql_label == DATE_PROPERTY
        assert table.findings[0].message.startswith(f"MQL ({DATE_PROPERTY}) count unavailable: ")


# ---------------------------------------------------------------------------
# Cohort median and the "Insufficient data" input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bench_ql", "median", "total"),
    [(0, Decimal(150), 5), (1, Decimal(200), 6)],
)
def test_the_cohort_median_covers_assets_with_ql(
    make: MakeContext, bench_ql: int, median: Decimal, total: int
) -> None:
    report_leads, report_contacts = _qualified(
        "r", 3, creative=CR_A, form=FORM_REPORT_1, first_id=1
    )
    playbook_leads, playbook_contacts = _qualified(
        "p", 2, creative=CR_PLAYBOOK, form=FORM_PLAYBOOK, first_id=10
    )
    bench_leads, bench_contacts = _qualified(
        "b", bench_ql, creative=CR_BENCH, form=FORM_BENCH, first_id=20
    )
    leads = [*report_leads, *playbook_leads, *bench_leads]
    if not bench_leads:
        leads.append(_lead(30, _t(9), creative=CR_BENCH, form=FORM_BENCH))
    table = build_attribution(
        make(
            crm=_crm([*report_contacts, *playbook_contacts, *bench_contacts]),
            leads=leads,
            spend=[
                _spend(CR_A, "300.00"),
                _spend(CR_PLAYBOOK, "400.00"),
                _spend(CR_BENCH, "500.00"),
            ],
        )
    )

    (cohort,) = table.cohorts
    assert [row.slug for row in cohort.rows] == [BENCH, REPORT, PLAYBOOK]
    assert [row.cost_per_ql.render() for row in cohort.rows][1:] == ["100.00", "200.00"]
    assert cohort.median_cost_per_ql == median
    assert cohort.total_ql == total
    assert cohort.spend_total == Decimal("1200.00")
    assert Decimal(cohort.to_json()["median_cost_per_ql"]) == median


# ---------------------------------------------------------------------------
# Rendering, invariants and memoisation
# ---------------------------------------------------------------------------


def test_row_json_keeps_mql_and_ql_separate_and_platform_leads_apart(make: MakeContext) -> None:
    contact = ContactInfo("c1", "salesqualifiedlead", mql_at=_t(11), ql_at=_t(12))
    table = build_attribution(
        make(
            crm=_crm([contact]),
            leads=[_lead(1, _t(10), contact="c1"), _lead(2, _t(10))],
            spend=[_spend(CR_A, "100.00")],
        )
    )
    row = _row(table)
    rendered = row.to_json()

    # No field combines MQL and QL, and platform leads are their own field.
    assert set(rendered) == ROW_KEYS
    assert rendered["mql"] == row.mql.to_json()
    assert rendered["ql"] == row.ql.to_json()
    assert rendered["linkedin_leads"] == 2
    assert (rendered["mql"]["display"], rendered["ql"]["display"]) == ("≥ 1", "≥ 1")
    assert rendered["spend"] == "100.00"
    assert rendered["contact_share"] == "0.5"
    assert rendered["unknown_evidence_share"] == "0"
    assert rendered["campaign_ids"] == [CAMPAIGN_USD]
    assert rendered["flags"] == [
        {"code": FLAG_ATTRIBUTION_FLOOR, "message": "Floor: 50% of leads unmatched in HubSpot"}
    ]
    assert rendered["cost_per_ql"]["display"] == "≤ 100.00"
    (credit,) = rendered["creative_rows"]
    assert set(credit) == CREDIT_KEYS
    assert credit["creative_urn"] == _urn(CR_A)
    assert credit["spend"] == "100.00"
    assert credit == row.creative_rows[0].to_json()
    assert table.rows() == (row,)


def test_an_asset_row_requires_a_message_for_every_flag(make: MakeContext) -> None:
    row = _row(build_attribution(make(crm=_crm(), spend=[_spend(CR_A, "10.00")])))
    with pytest.raises(ValueError, match="flags without a message"):
        dataclasses.replace(row, flags=(FLAG_DEAL_FLOOR,), flag_messages={})
    flagged = dataclasses.replace(
        row, flags=(FLAG_DEAL_FLOOR,), flag_messages={FLAG_DEAL_FLOOR: "Deal counts are a floor"}
    )
    assert flagged.to_flags() == (Flag(FLAG_DEAL_FLOOR, "Deal counts are a floor"),)


def test_the_partition_check_rejects_credits_that_do_not_sum_to_the_asset_total(
    make: MakeContext,
) -> None:
    contact = ContactInfo("c1", "salesqualifiedlead", ql_at=_t(12))
    row = _row(
        build_attribution(
            make(
                crm=_crm([contact]),
                leads=[_lead(1, _t(10), contact="c1")],
                spend=[_spend(CR_A, "10.00")],
            )
        )
    )
    attribution_module._check_partition(row)

    with pytest.raises(AssertionError, match="per-creative ql sums to 1, asset total 2"):
        attribution_module._check_partition(dataclasses.replace(row, ql=CountValue(2)))
    with pytest.raises(AssertionError, match="mql unavailable but credited"):
        attribution_module._check_partition(
            dataclasses.replace(row, mql=CountValue.missing(MQL_REASON))
        )
    (credit,) = row.creative_rows
    uncredited = dataclasses.replace(credit, deals=CountValue.missing(NO_CRM_REASON))
    with pytest.raises(AssertionError, match="per-creative deals sums to"):
        attribution_module._check_partition(dataclasses.replace(row, creative_rows=(uncredited,)))
    with pytest.raises(AssertionError, match="per-creative spend sums to"):
        attribution_module._check_partition(dataclasses.replace(row, spend=Decimal("11.00")))


def test_attribution_for_computes_the_table_once_per_run(make: MakeContext) -> None:
    ctx = make(crm=_crm(), spend=[_spend(CR_A, "10.00")])
    first = attribution_for(ctx)
    assert ctx.cache[CACHE_KEY] is first
    assert attribution_for(ctx) is first
    assert first == build_attribution(ctx)

    ctx.cache[CACHE_KEY] = "not a table"
    replaced = attribution_for(ctx)
    assert isinstance(replaced, AttributionTable)
    assert ctx.cache[CACHE_KEY] is replaced
