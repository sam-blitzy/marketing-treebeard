"""Unit tests for the CRM pipeline-veto population of ``ads_engine.analysis.attribution``.

They pin the matured population behind the C1b pause and the C2 pipeline veto (AAP 0.5.4.1
"CRM pipeline link", 0.5.4.4 C1b and C2): one population per LinkedIn campaign, read from
``ctx.linkedin_crm`` (the effective CRM dataset), and one per Google campaign, read from
``ctx.google_crm`` only when the Google-to-CRM link is verified, so a LinkedIn-derived subset
never stands in. Maturity is measured from the window end; unresolved leads stay in the
share's denominator; eligible contacts are de-duplicated at their earliest matured touch; QL,
MQL and meetings count only at or after that touch and never after the run. The pipeline is
unavailable when the Qualified Lead stage is unbound, an eligible contact's refresh,
association fetch or target read failed, or the unresolved share reaches the floor; an
unbound MQL never blocks the veto. ``attribution_for`` memoises the table under
``CACHE_KEY``. Pure: no database and no network.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest

from ads_engine.analysis import attribution as attribution_module
from ads_engine.analysis.attribution import (
    ATTRIBUTION_THRESHOLD_KEYS,
    CACHE_KEY,
    AttributionTable,
    VetoPopulation,
    attribution_for,
    build_attribution,
    contact_evidence_known,
)
from ads_engine.analysis.context import (
    FINDING_PIPELINE_UNAVAILABLE,
    FLAG_PIPELINE_UNAVAILABLE,
    WHITE_PAPERS,
    AccountInfo,
    AnalysisContext,
    AssociationFetchInfo,
    CampaignInfo,
    ContactInfo,
    ContactReadStatus,
    CrmData,
    DatasetRef,
    DealInfo,
    LeadInfo,
    MatchStatus,
    MeetingInfo,
    objective_group,
)
from ads_engine.analysis.thresholds import (
    CrmBindings,
    GoogleCrmBinding,
    ResolvedBinding,
    Thresholds,
)
from ads_engine.analysis.window import AnalysisWindow, SourceDayInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

# The default 28-day window at NOW ends on 2026-10-04 (yesterday in America/New_York), so its
# UTC instant range is [2026-09-07 00:00, 2026-10-05 00:00) and a lead is matured when it was
# submitted at or before 2026-10-05 00:00 minus crm.lead_maturity_days (14).
NOW = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 9, 7, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 5, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 21, tzinfo=UTC)
MATURED = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
YOUNG = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
ONE_SECOND = timedelta(seconds=1)
ONE_DAY = timedelta(days=1)

GOOGLE = DatasetRef(11, "google", "live")
LINKEDIN_SIM = DatasetRef(12, "linkedin", "simulated")
HUBSPOT = DatasetRef(13, "hubspot", "live")
COMPANION = DatasetRef(14, "hubspot", "simulated", "companion")

# A lead form outside config/content_assets.yaml, so the veto tests build no white-paper row;
# REGISTRY_FORM resolves to the "b2b-pipeline-report-2026" asset.
OTHER_FORM = "urn:li:leadGenForm:1"
REGISTRY_FORM = "urn:li:leadGenForm:990000101"

LI_A = 101
LI_B = 102
G_A = 201
G_B = 202
G_C = 203

QL_BOUND = ResolvedBinding(
    available=True,
    property="hs_v2_date_entered_1406943076",
    stage_id="1406943076",
    label="Qualified Lead",
)
MQL_BOUND = ResolvedBinding(available=True, property="date_first_meeting_was_booked")
QL_MISSING = ResolvedBinding(
    available=False,
    reason="stage labels 'Qualified Lead', 'Sales Qualified Lead' not in lifecyclestage options",
)
MQL_MISSING = ResolvedBinding(
    available=False, reason="stage label 'First meeting booked' not in lifecyclestage options"
)
GOOGLE_LINK = GoogleCrmBinding(
    available=True, campaign_property="google_campaign_id", touch_date_property="google_touch_at"
)
NO_GOOGLE_LINK = GoogleCrmBinding(
    available=False, reason="HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured"
)

NO_CRM_REASON = "no CRM dataset for LinkedIn-attributed evidence"
UNRESOLVED_STATUSES: tuple[MatchStatus | None, ...] = (
    "pending",
    "no_match",
    "no_email",
    "error",
    None,
)


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    return Thresholds.load(CONFIG_DIR)


@pytest.fixture(scope="module")
def window(thresholds: Thresholds) -> AnalysisWindow:
    return AnalysisWindow.default(thresholds, SourceDayInfo("America/New_York"), now=NOW)


# -- builders --------------------------------------------------------------------------------


def _bindings(
    *,
    mql: ResolvedBinding = MQL_BOUND,
    ql: ResolvedBinding = QL_BOUND,
    google: GoogleCrmBinding = NO_GOOGLE_LINK,
) -> CrmBindings:
    return CrmBindings(mql=mql, ql=ql, google=google, verified_at=NOW)


def _hs(n: int) -> str:
    return f"{9000 + n}"


def _contact(hs_id: str, **fields: Any) -> ContactInfo:
    return ContactInfo(hs_id, lifecycle_stage=fields.pop("lifecycle_stage", "lead"), **fields)


def _crm(
    dataset: DatasetRef,
    contacts: Iterable[ContactInfo],
    *,
    bindings: CrmBindings | None = None,
    fetches: Mapping[tuple[str, str, str], AssociationFetchInfo | None] | None = None,
    associations: Mapping[tuple[str, str, str], tuple[str, ...]] | None = None,
    meetings: Iterable[MeetingInfo] = (),
    deals: Iterable[DealInfo] = (),
    google_contacts: Sequence[ContactInfo] = (),
) -> CrmData:
    """Return a CRM dataset whose contacts have ``ok_empty`` (genuine zero) associations.

    ``fetches`` overrides single fetch rows; a ``None`` outcome removes the row (never fetched).
    """
    by_id = {contact.hs_id: contact for contact in contacts}
    rows: dict[tuple[str, str, str], AssociationFetchInfo] = {
        ("contact", hs_id, to_type): AssociationFetchInfo("contact", hs_id, to_type, "ok_empty")
        for hs_id in [*by_id, *(contact.hs_id for contact in google_contacts)]
        for to_type in ("meeting", "deal")
    }
    for key, outcome in (fetches or {}).items():
        if outcome is None:
            rows.pop(key, None)
        else:
            rows[key] = outcome
    return CrmData(
        dataset,
        bindings if bindings is not None else _bindings(),
        contacts=by_id,
        meetings={meeting.hs_id: meeting for meeting in meetings},
        deals={deal.hs_id: deal for deal in deals},
        associations=dict(associations or {}),
        fetches=rows,
        google_contacts=tuple(google_contacts),
    )


def _matched(
    lead_id: int,
    hs_id: str,
    *,
    campaign_id: int = LI_A,
    at: datetime = MATURED,
    read: ContactReadStatus = "ok",
    form: str = OTHER_FORM,
) -> LeadInfo:
    return LeadInfo(
        lead_id, f"resp-{lead_id}", campaign_id, None, None, form, 1, at, "matched", hs_id, read
    )


def _unresolved(
    lead_id: int,
    status: MatchStatus | None,
    *,
    campaign_id: int = LI_A,
    at: datetime = MATURED,
) -> LeadInfo:
    return LeadInfo(lead_id, f"resp-{lead_id}", campaign_id, None, None, OTHER_FORM, 1, at, status)


def _cohort(count: int, *, first: int = 1, campaign_id: int = LI_A) -> list[LeadInfo]:
    """Return ``count`` matured matched leads, one per contact ``_hs(first)`` onwards."""
    return [_matched(n, _hs(n), campaign_id=campaign_id) for n in range(first, first + count)]


def _contacts(leads: Iterable[LeadInfo]) -> list[ContactInfo]:
    """Return one contact without stage dates for each matched lead's contact."""
    ids = dict.fromkeys(lead.contact_hs_id for lead in leads if lead.contact_hs_id is not None)
    return [_contact(hs_id) for hs_id in ids]


def _campaign(campaign_id: int, source: str, external_id: str) -> CampaignInfo:
    if source == "google":
        return CampaignInfo(
            campaign_id,
            "google",
            1,
            external_id,
            f"Google {campaign_id}",
            "active",
            "ENABLED",
            "SEARCH",
            objective_group("google", "SEARCH"),
            None,
            None,
            None,
            None,
            None,
            "USD",
        )
    return CampaignInfo(
        campaign_id,
        "linkedin",
        2,
        external_id,
        f"LinkedIn {campaign_id}",
        "active",
        "ACTIVE",
        "LEAD_GENERATION",
        objective_group("linkedin", "LEAD_GENERATION"),
        None,
        None,
        Decimal("100.00"),
        None,
        None,
        "USD",
    )


ACCOUNTS = {
    1: AccountInfo(1, "google", "1234567890", "Search account", "USD", "America/New_York"),
    2: AccountInfo(2, "linkedin", "508000001", "LinkedIn account", "USD"),
}
CAMPAIGNS = {
    LI_A: _campaign(LI_A, "linkedin", "990100101"),
    LI_B: _campaign(LI_B, "linkedin", "990100102"),
    G_A: _campaign(G_A, "google", "20001"),
    G_B: _campaign(G_B, "google", "20002"),
    G_C: _campaign(G_C, "google", "20003"),
}


def _context(
    thresholds: Thresholds,
    window: AnalysisWindow,
    *,
    linkedin_crm: CrmData | None,
    leads: Iterable[LeadInfo] = (),
    google_crm: CrmData | None = None,
    campaigns: Mapping[int, CampaignInfo] | None = None,
) -> AnalysisContext:
    return AnalysisContext(
        window=window,
        thresholds=thresholds,
        now=NOW,
        datasets={"google": GOOGLE, "linkedin": LINKEDIN_SIM, "hubspot": HUBSPOT},
        linkedin_crm=linkedin_crm,
        google_crm=google_crm,
        accounts=ACCOUNTS,
        campaigns=CAMPAIGNS if campaigns is None else campaigns,
        leads=tuple(leads),
    )


def _veto(ctx: AnalysisContext, campaign_id: int = LI_A) -> VetoPopulation:
    return build_attribution(ctx).veto[campaign_id]


# -- settings and fixture guard --------------------------------------------------------------


def test_settings_read_every_attribution_threshold_from_config(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    assert window.utc_bounds() == (WINDOW_START, WINDOW_END)
    assert window.utc_bounds()[1] - timedelta(days=14) == CUTOFF
    settings = attribution_module._Settings.of(_context(thresholds, window, linkedin_crm=None))
    assert settings.floor_share == Decimal("0.10")
    assert settings.maturity == timedelta(days=14)
    assert thresholds.int("crm.lead_maturity_days") == 14
    assert settings.veto_min == thresholds.int("crm.veto_min_leads") == 20
    assert settings.booking_sources == frozenset({"MEETINGS_PUBLIC"})
    assert settings.exclusions == frozenset({"CANCELED"})
    assert settings.median_min_ql == 1
    for key in ATTRIBUTION_THRESHOLD_KEYS:
        assert thresholds.value(key) is not None
    assert CACHE_KEY == "attribution"


# -- the LinkedIn matured population (AAP 0.5.4.1) -------------------------------------------


def test_veto_fires_on_twenty_matured_contacts_while_young_leads_reach_ql(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    matured = _cohort(20)
    young = [_matched(n, _hs(n), at=YOUNG) for n in (21, 22, 23)]
    young_unresolved = _unresolved(24, "no_match", at=YOUNG)
    contacts = [
        *_contacts(matured),
        _contact(_hs(21), ql_at=YOUNG + ONE_DAY, mql_at=YOUNG + ONE_DAY),
        _contact(_hs(22), ql_at=YOUNG + 2 * ONE_DAY),
        _contact(_hs(23)),
    ]
    ctx = _context(
        thresholds,
        window,
        linkedin_crm=_crm(COMPANION, contacts),
        leads=[*matured, *young, young_unresolved],
    )
    population = _veto(ctx)
    assert population.source == "linkedin"
    assert population.campaign_id == LI_A
    assert population.leads == 24
    assert population.matured_leads == 20
    assert population.matched_leads == 20
    assert population.unresolved_leads == 0
    assert population.eligible_contacts == 20
    assert population.unresolved_share == Decimal(0)
    assert population.unknown_evidence_contacts == 0
    assert not population.unknown_evidence
    assert (population.mql, population.ql, population.meetings) == (0, 0, 0)
    assert population.available
    assert population.reason is None
    assert not population.attribution_floor
    assert population.veto_fires


@pytest.mark.parametrize(
    ("last_touch", "matured", "fires"),
    [
        (CUTOFF, 20, True),
        (CUTOFF + ONE_SECOND, 19, False),
    ],
    ids=["touch-on-the-cutoff-is-matured", "one-second-younger-leaves-19-contacts"],
)
def test_maturity_is_measured_back_from_the_window_end(
    thresholds: Thresholds,
    window: AnalysisWindow,
    last_touch: datetime,
    matured: int,
    fires: bool,
) -> None:
    leads = [*_cohort(19), _matched(20, _hs(20), at=last_touch)]
    ctx = _context(thresholds, window, linkedin_crm=_crm(COMPANION, _contacts(leads)), leads=leads)
    population = _veto(ctx)
    assert population.leads == 20
    assert population.matured_leads == population.eligible_contacts == matured
    assert population.available
    assert population.ql == 0
    assert population.veto_fires is fires


def test_repeat_leads_count_once_as_contacts_and_once_each_in_the_share(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    leads = [
        *_cohort(22),
        _matched(23, _hs(1), at=MATURED + ONE_DAY),
        _unresolved(24, "pending"),
        _unresolved(25, "no_email"),
    ]
    ctx = _context(thresholds, window, linkedin_crm=_crm(COMPANION, _contacts(leads)), leads=leads)
    population = _veto(ctx)
    assert population.matured_leads == 25
    assert population.matched_leads == 23
    assert population.unresolved_leads == 2
    assert population.eligible_contacts == 22
    assert population.unresolved_share == Decimal("0.08")
    assert population.available
    assert population.veto_fires
    rendered = population.to_json()
    assert {
        key: rendered[key]
        for key in ("matured_leads", "matched_leads", "unresolved_leads", "eligible_contacts")
    } == {"matured_leads": 25, "matched_leads": 23, "unresolved_leads": 2, "eligible_contacts": 22}
    assert rendered["unresolved_share"] == "0.08"


@pytest.mark.parametrize(
    ("matched", "unresolved", "share", "reason"),
    [
        (22, 3, Decimal("0.12"), "12% of matured leads unresolved in HubSpot (floor 10%)"),
        (27, 3, Decimal("0.10"), "10% of matured leads unresolved in HubSpot (floor 10%)"),
        (23, 2, Decimal("0.08"), None),
    ],
    ids=["12-percent-unavailable", "exactly-the-floor-unavailable", "8-percent-evaluated"],
)
def test_unresolved_share_reaching_the_floor_makes_the_pipeline_unavailable(
    thresholds: Thresholds,
    window: AnalysisWindow,
    matched: int,
    unresolved: int,
    share: Decimal,
    reason: str | None,
) -> None:
    leads = [
        *_cohort(matched),
        *(
            _unresolved(matched + n, UNRESOLVED_STATUSES[n % len(UNRESOLVED_STATUSES)])
            for n in range(1, unresolved + 1)
        ),
    ]
    ctx = _context(thresholds, window, linkedin_crm=_crm(COMPANION, _contacts(leads)), leads=leads)
    population = _veto(ctx)
    assert population.matured_leads == matched + unresolved
    assert population.unresolved_leads == unresolved
    assert population.unresolved_share == share
    # Every matched lead's evidence is fully known: the floor alone decides availability.
    assert population.unknown_evidence_contacts == 0
    assert population.ql == 0
    assert population.reason == reason
    assert population.available is (reason is None)
    assert population.attribution_floor is (reason is not None)
    assert population.veto_fires is (reason is None)


@pytest.mark.parametrize(
    "status",
    [*UNRESOLVED_STATUSES, "matched"],
    ids=["pending", "no_match", "no_email", "error", "no-match-row", "matched-without-contact"],
)
def test_every_unresolved_lead_stays_in_the_matured_population(
    thresholds: Thresholds, window: AnalysisWindow, status: MatchStatus | None
) -> None:
    lead = LeadInfo(99, "resp-99", LI_A, None, None, OTHER_FORM, 1, MATURED, status, None)
    leads = [*_cohort(20), lead]
    ctx = _context(thresholds, window, linkedin_crm=_crm(COMPANION, _contacts(leads)), leads=leads)
    population = _veto(ctx)
    assert population.matured_leads == 21
    assert population.matched_leads == 20
    assert population.unresolved_leads == 1
    assert population.eligible_contacts == 20
    assert population.unresolved_share == Decimal(1) / Decimal(21)
    assert population.veto_fires


# -- unknown CRM evidence (AAP 0.5.4.1, 0.5.4.7, 0.3.3) --------------------------------------


@dataclass(frozen=True, slots=True)
class _Breakage:
    """One failed read on the first eligible contact, and the reason it makes evidence unknown."""

    reason: str
    read: ContactReadStatus = "ok"
    contact_read: bool = True
    fetch: tuple[str, AssociationFetchInfo | None] | None = None
    targets: tuple[str, tuple[str, ...]] | None = None


BROKEN = _hs(1)
BREAKAGES = [
    pytest.param(
        _Breakage("contact refresh failed; stage dates not current", read="error"),
        id="contact-refresh-failed",
    ),
    pytest.param(
        _Breakage("contact record not read", contact_read=False), id="contact-record-not-read"
    ),
    pytest.param(
        _Breakage("contact-to-meeting associations not fetched", fetch=("meeting", None)),
        id="meeting-fetch-missing",
    ),
    pytest.param(
        _Breakage(
            "contact-to-meeting association fetch error",
            fetch=("meeting", AssociationFetchInfo("contact", BROKEN, "meeting", "error")),
        ),
        id="meeting-fetch-error",
    ),
    pytest.param(
        _Breakage(
            "contact-to-deal association fetch partial",
            fetch=("deal", AssociationFetchInfo("contact", BROKEN, "deal", "partial", "ok")),
        ),
        id="deal-fetch-partial",
    ),
    pytest.param(
        _Breakage(
            "deal target reads partial",
            fetch=(
                "deal",
                AssociationFetchInfo("contact", BROKEN, "deal", "ok", "partial", ("d-2",)),
            ),
            targets=("deal", ("d-1", "d-2")),
        ),
        id="deal-target-reads-partial",
    ),
    pytest.param(
        _Breakage(
            "meeting target reads error",
            fetch=("meeting", AssociationFetchInfo("contact", BROKEN, "meeting", "ok", "error")),
            targets=("meeting", ("m-1",)),
        ),
        id="meeting-target-reads-error",
    ),
    pytest.param(
        _Breakage(
            "meeting target reads not recorded",
            fetch=("meeting", AssociationFetchInfo("contact", BROKEN, "meeting", "ok")),
            targets=("meeting", ("m-1",)),
        ),
        id="meeting-target-reads-not-recorded",
    ),
    pytest.param(
        _Breakage(
            "1 associated meeting record(s) not read",
            fetch=("meeting", AssociationFetchInfo("contact", BROKEN, "meeting", "ok", "ok")),
            targets=("meeting", ("m-1",)),
        ),
        id="associated-meeting-not-read",
    ),
]


@pytest.mark.parametrize("breakage", BREAKAGES)
def test_unknown_contact_evidence_makes_the_pipeline_unavailable(
    thresholds: Thresholds, window: AnalysisWindow, breakage: _Breakage
) -> None:
    leads = [_matched(1, BROKEN, read=breakage.read), *_cohort(19, first=2)]
    contacts = [c for c in _contacts(leads) if breakage.contact_read or c.hs_id != BROKEN]
    fetches: dict[tuple[str, str, str], AssociationFetchInfo | None] = {}
    if breakage.fetch is not None:
        to_type, outcome = breakage.fetch
        fetches[("contact", BROKEN, to_type)] = outcome
    associations: dict[tuple[str, str, str], tuple[str, ...]] = {}
    if breakage.targets is not None:
        to_type, targets = breakage.targets
        associations[("contact", BROKEN, to_type)] = targets
    crm = _crm(COMPANION, contacts, fetches=fetches, associations=associations)
    ctx = _context(thresholds, window, linkedin_crm=crm, leads=leads)

    refresh_failed = attribution_module._refresh_failures(ctx.leads)
    problem = attribution_module._read_evidence_problem(
        crm, BROKEN, crm.contacts.get(BROKEN), refresh_failed=BROKEN in refresh_failed
    )
    assert problem == breakage.reason

    population = _veto(ctx)
    assert population.eligible_contacts == 20
    assert population.unknown_evidence_contacts == 1
    assert population.unknown_evidence
    assert not population.available
    assert population.reason == "CRM evidence unknown for 1 of 20 eligible contacts"
    assert not population.veto_fires


def test_known_evidence_includes_genuine_zeros_and_fully_read_associations(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    leads = _cohort(20)
    fetches: dict[tuple[str, str, str], AssociationFetchInfo | None] = {
        ("contact", _hs(1), "deal"): AssociationFetchInfo("contact", _hs(1), "deal", "ok", "ok"),
    }
    crm = _crm(
        COMPANION,
        _contacts(leads),
        fetches=fetches,
        associations={("contact", _hs(1), "deal"): ("d-1",)},
        deals=[DealInfo("d-1", created_at=MATURED + ONE_DAY)],
    )
    ctx = _context(thresholds, window, linkedin_crm=crm, leads=leads)
    assert (
        attribution_module._read_evidence_problem(
            crm, _hs(1), crm.contacts[_hs(1)], refresh_failed=False
        )
        is None
    )
    population = _veto(ctx)
    assert population.unknown_evidence_contacts == 0
    assert population.available
    assert population.veto_fires


def test_a_failed_refresh_on_any_matched_lead_marks_the_contact_unknown(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    # The contact's matured lead read it, but the refresh behind its younger lead failed:
    # the contact's stage dates are not current, so its evidence is unknown.
    leads = [*_cohort(20), _matched(21, _hs(1), at=YOUNG, read="error")]
    ctx = _context(thresholds, window, linkedin_crm=_crm(COMPANION, _contacts(leads)), leads=leads)
    assert attribution_module._refresh_failures(ctx.leads) == frozenset({_hs(1)})
    population = _veto(ctx)
    assert population.unknown_evidence_contacts == 1
    assert not population.available
    assert not population.veto_fires


def test_read_failures_outside_the_eligible_contacts_never_enter_the_veto(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    young_contact, other_campaign_contact = _hs(50), _hs(51)
    leads = [
        *_cohort(20),
        _unresolved(30, "error"),
        _matched(31, young_contact, at=YOUNG),
        _matched(32, other_campaign_contact, campaign_id=LI_B),
    ]
    crm = _crm(
        COMPANION,
        _contacts(leads),
        fetches={
            ("contact", young_contact, "meeting"): AssociationFetchInfo(
                "contact", young_contact, "meeting", "error"
            ),
            ("contact", other_campaign_contact, "deal"): AssociationFetchInfo(
                "contact", other_campaign_contact, "deal", "partial"
            ),
        },
    )
    table = build_attribution(_context(thresholds, window, linkedin_crm=crm, leads=leads))
    first, second = table.veto[LI_A], table.veto[LI_B]
    assert first.leads == 22
    assert first.matured_leads == 21
    assert first.eligible_contacts == 20
    assert first.unknown_evidence_contacts == 0
    assert first.available
    assert first.veto_fires
    assert second.eligible_contacts == 1
    assert second.unknown_evidence_contacts == 1
    assert second.reason == "CRM evidence unknown for 1 of 1 eligible contacts"


# -- stage bindings (AAP 0.5.4.1 "Pipeline unavailable", 0.5.4.7 "Stage binding") ------------


def test_unbound_ql_stage_makes_the_pipeline_unavailable_and_never_zero(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    leads = [_matched(1, _hs(1), form=REGISTRY_FORM), *_cohort(19, first=2)]
    contacts = [_contact(_hs(1), mql_at=MATURED + ONE_DAY), *_contacts(leads[1:])]
    crm = _crm(COMPANION, contacts, bindings=_bindings(ql=QL_MISSING))
    table = build_attribution(_context(thresholds, window, linkedin_crm=crm, leads=leads))

    population = table.veto[LI_A]
    assert population.eligible_contacts == 20
    assert population.ql is None
    assert population.to_json()["ql"] is None
    assert population.mql == 1
    assert population.meetings == 0
    assert not population.available
    assert population.reason == f"Qualified Lead stage unavailable: {QL_MISSING.reason}"
    assert not population.veto_fires

    assert not table.ql_available
    assert table.mql_available
    findings = [found for found in table.findings if found.code == FINDING_PIPELINE_UNAVAILABLE]
    assert len(findings) == 1
    assert findings[0].section == WHITE_PAPERS
    assert findings[0].evidence["stage"] == "Qualified Lead"
    assert findings[0].evidence["reason"] == QL_MISSING.reason
    assert QL_MISSING.reason is not None
    assert QL_MISSING.reason in findings[0].message
    (row,) = table.rows()
    assert row.has_flag(FLAG_PIPELINE_UNAVAILABLE)
    assert row.ql.render() == "unavailable"


def test_unbound_mql_stage_does_not_block_the_veto(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    leads = _cohort(20)
    crm = _crm(COMPANION, _contacts(leads), bindings=_bindings(mql=MQL_MISSING))
    table = build_attribution(_context(thresholds, window, linkedin_crm=crm, leads=leads))
    population = table.veto[LI_A]
    assert population.mql is None
    assert population.ql == 0
    assert population.unknown_evidence_contacts == 0
    assert population.available
    assert population.reason is None
    assert population.veto_fires
    assert table.ql_available
    assert not table.mql_available
    # The white-paper evidence check counts an unbound stage as unknown evidence; the veto
    # judges Qualified Leads and meetings only, so only the per-contact reads enter it.
    assert contact_evidence_known(crm, _hs(1), leads[0]) == (
        False,
        f"MQL stage unavailable: {MQL_MISSING.reason}",
    )


def test_contact_evidence_known_reports_each_per_contact_failure() -> None:
    lead = _matched(1, _hs(1))
    known = _crm(COMPANION, [_contact(_hs(1))])
    assert contact_evidence_known(known, _hs(1), lead) == (True, None)
    assert contact_evidence_known(known, _hs(1), _matched(1, _hs(1), read="error")) == (
        False,
        "contact refresh failed; stage dates not current",
    )
    unbound = _crm(COMPANION, [_contact(_hs(1))], bindings=_bindings(ql=QL_MISSING))
    assert contact_evidence_known(unbound, _hs(1), lead) == (
        False,
        f"Qualified Lead stage unavailable: {QL_MISSING.reason}",
    )
    missing = _crm(COMPANION, [_contact(_hs(1))], fetches={("contact", _hs(1), "deal"): None})
    assert contact_evidence_known(missing, _hs(1), lead) == (
        False,
        "contact-to-deal associations not fetched",
    )


@pytest.mark.parametrize(
    ("ql", "unknown", "unresolved", "reason"),
    [
        (QL_MISSING, True, 3, f"Qualified Lead stage unavailable: {QL_MISSING.reason}"),
        (QL_BOUND, True, 3, "CRM evidence unknown for 1 of 20 eligible contacts"),
        (QL_BOUND, False, 3, "13% of matured leads unresolved in HubSpot (floor 10%)"),
    ],
    ids=["ql-stage-first", "unknown-evidence-before-floor", "floor-last"],
)
def test_the_unavailable_reason_names_the_first_failing_condition(
    thresholds: Thresholds,
    window: AnalysisWindow,
    ql: ResolvedBinding,
    unknown: bool,
    unresolved: int,
    reason: str,
) -> None:
    leads = [
        _matched(1, _hs(1), read="error" if unknown else "ok"),
        *_cohort(19, first=2),
        *(_unresolved(40 + n, "no_match") for n in range(unresolved)),
    ]
    crm = _crm(COMPANION, _contacts(leads), bindings=_bindings(ql=ql))
    population = _veto(_context(thresholds, window, linkedin_crm=crm, leads=leads))
    assert population.reason == reason
    assert not population.available
    assert population.attribution_floor
    assert population.unknown_evidence_contacts == (1 if unknown else 0)
    assert not population.veto_fires


# -- causal ordering and run time (AAP 0.5.4.1 "Veto counts", 0.5.4.7 "Causal ordering") -----


@pytest.mark.parametrize(
    ("ql_at", "ql", "fires"),
    [
        (NOW + ONE_SECOND, 0, True),
        (NOW, 1, False),
    ],
    ids=["entry-after-the-run-not-counted", "entry-at-the-run-counted"],
)
def test_crm_events_after_the_run_are_not_counted(
    thresholds: Thresholds,
    window: AnalysisWindow,
    ql_at: datetime,
    ql: int,
    fires: bool,
) -> None:
    leads = _cohort(20)
    contacts = [
        _contact(_hs(1), ql_at=ql_at),
        _contact(_hs(2), mql_at=NOW + timedelta(hours=1)),
        *_contacts(leads[2:]),
    ]
    meeting_fetch = AssociationFetchInfo("contact", _hs(3), "meeting", "ok", "ok")
    crm = _crm(
        COMPANION,
        contacts,
        fetches={("contact", _hs(3), "meeting"): meeting_fetch},
        associations={("contact", _hs(3), "meeting"): ("m-future",)},
        meetings=[
            MeetingInfo(
                "m-future",
                created_at=NOW + ONE_SECOND,
                start_at=NOW + ONE_DAY,
                meeting_source="MEETINGS_PUBLIC",
            )
        ],
    )
    population = _veto(_context(thresholds, window, linkedin_crm=crm, leads=leads))
    assert population.ql == ql
    assert population.mql == 0
    assert population.meetings == 0
    assert population.available
    assert population.veto_fires is fires


def test_stage_entries_count_only_at_or_after_the_earliest_matured_touch(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    later = MATURED + 3 * ONE_DAY
    leads = [*_cohort(20), _matched(21, _hs(3), at=later)]
    contacts = [
        _contact(_hs(1), ql_at=MATURED - ONE_SECOND, mql_at=MATURED - ONE_DAY),
        _contact(_hs(2), ql_at=MATURED, mql_at=MATURED + ONE_DAY),
        # Two matured leads: the anchor is the earlier one, so an entry between them counts.
        _contact(_hs(3), ql_at=MATURED + ONE_DAY),
        *_contacts(leads[3:20]),
    ]
    population = _veto(
        _context(thresholds, window, linkedin_crm=_crm(COMPANION, contacts), leads=leads)
    )
    assert population.matched_leads == 21
    assert population.eligible_contacts == 20
    assert population.ql == 2
    assert population.mql == 1
    assert population.available
    assert not population.veto_fires


def test_veto_meetings_count_every_plausible_meeting_after_the_touch(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    early = MATURED - 5 * ONE_DAY
    leads = [_matched(5, _hs(5), at=early), *_cohort(4), *_cohort(15, first=6)]
    leads.append(_matched(30, _hs(30), at=YOUNG))
    after, before = MATURED + ONE_DAY, MATURED - ONE_DAY
    meetings = [
        MeetingInfo("m-booked", after, after + ONE_DAY, None, None, "MEETINGS_PUBLIC"),
        MeetingInfo("m-other-source", after, after, None, "COMPLETED", "CRM_UI"),
        MeetingInfo("m-shared", after, after, None, None, "MEETINGS_PUBLIC"),
        MeetingInfo("m-canceled", after, after, None, " canceled ", "MEETINGS_PUBLIC"),
        MeetingInfo("m-created-before", before, after, None, None, "MEETINGS_PUBLIC"),
        MeetingInfo("m-start-before", after, MATURED - ONE_SECOND, None, None, "MEETINGS_PUBLIC"),
        MeetingInfo("m-timestamp-before", after, None, before, None, "MEETINGS_PUBLIC"),
        MeetingInfo("m-undated"),
        MeetingInfo("m-created-after-run", NOW + ONE_SECOND, NOW + ONE_DAY),
        MeetingInfo("m-at-touch", MATURED, MATURED, None, None, "MEETINGS_PUBLIC"),
        MeetingInfo("m-starts-after-run", after, NOW + 7 * ONE_DAY, None, None, "MEETINGS_PUBLIC"),
        # Before contact 4's touch but after contact 5's earlier touch: counted once, via 5.
        MeetingInfo("m-mixed-touches", before, before, None, None, "MEETINGS_PUBLIC"),
        MeetingInfo("m-young-contact", YOUNG + ONE_DAY, YOUNG + ONE_DAY),
    ]
    associations: dict[tuple[str, str, str], tuple[str, ...]] = {
        ("contact", _hs(1), "meeting"): ("m-booked", "m-other-source", "m-canceled"),
        ("contact", _hs(2), "meeting"): ("m-shared", "m-created-before", "m-start-before"),
        ("contact", _hs(3), "meeting"): ("m-shared", "m-timestamp-before", "m-undated"),
        ("contact", _hs(4), "meeting"): ("m-created-after-run", "m-at-touch", "m-mixed-touches"),
        ("contact", _hs(5), "meeting"): ("m-starts-after-run", "m-mixed-touches"),
        ("contact", _hs(30), "meeting"): ("m-young-contact",),
    }
    fetches: dict[tuple[str, str, str], AssociationFetchInfo | None] = {
        key: AssociationFetchInfo("contact", key[1], "meeting", "ok", "ok") for key in associations
    }
    crm = _crm(
        COMPANION, _contacts(leads), fetches=fetches, associations=associations, meetings=meetings
    )
    population = _veto(_context(thresholds, window, linkedin_crm=crm, leads=leads))
    assert population.eligible_contacts == 20
    assert population.unknown_evidence_contacts == 0
    # m-booked, m-other-source, m-shared (once), m-undated, m-at-touch, m-starts-after-run and
    # m-mixed-touches.
    assert population.meetings == 7
    assert population.ql == 0
    assert population.available
    assert population.veto_fires


# -- no CRM dataset and the Google-to-CRM link (AAP 0.5.4.1, 0.3.3) --------------------------


def test_without_a_linkedin_crm_dataset_every_population_is_unavailable(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    leads = _cohort(20)
    table = build_attribution(_context(thresholds, window, linkedin_crm=None, leads=leads))
    assert sorted(table.veto) == [LI_A, LI_B]
    population, empty = table.veto[LI_A], table.veto[LI_B]
    assert population.eligible_contacts == 20
    assert population.matched_leads == 20
    assert (population.mql, population.ql, population.meetings) == (None, None, None)
    assert population.unknown_evidence_contacts == 0
    assert not population.available
    assert population.reason == NO_CRM_REASON
    assert not population.veto_fires
    assert empty.matured_leads == 0
    assert empty.unresolved_share is None
    assert not empty.available
    assert not table.ql_available
    assert not table.mql_available


@pytest.mark.parametrize(
    "google_crm",
    [
        None,
        _crm(
            HUBSPOT,
            [],
            google_contacts=[
                _contact(f"g-{n}", google_campaign_ref="20001", google_touch_at=MATURED)
                for n in range(25)
            ],
        ),
    ],
    ids=["no-primary-crm-dataset", "google-link-not-verified"],
)
def test_no_google_population_without_the_verified_google_link(
    thresholds: Thresholds, window: AnalysisWindow, google_crm: CrmData | None
) -> None:
    # The LinkedIn companion carries a verified Google binding and LinkedIn contacts with a
    # Google campaign touch: that LinkedIn-derived subset must never stand in.
    leads = _cohort(25)
    linkedin_contacts = [
        _contact(_hs(n), google_campaign_ref="20001", google_touch_at=MATURED) for n in range(1, 26)
    ]
    companion = _crm(
        COMPANION,
        linkedin_contacts,
        bindings=_bindings(google=GOOGLE_LINK),
        google_contacts=linkedin_contacts,
    )
    table = build_attribution(
        _context(thresholds, window, linkedin_crm=companion, leads=leads, google_crm=google_crm)
    )
    assert sorted(table.veto) == [LI_A, LI_B]
    assert all(population.source == "linkedin" for population in table.veto.values())
    assert table.veto[LI_A].veto_fires


def test_google_population_is_the_matured_touch_date_contacts_of_the_primary_dataset(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    discovered = [
        _contact(f"g-{n}", google_campaign_ref=" 20001 ", google_touch_at=MATURED)
        for n in range(1, 21)
    ]
    excluded = [
        _contact("g-young", google_campaign_ref="20001", google_touch_at=CUTOFF + ONE_SECOND),
        _contact("g-early", google_campaign_ref="20001", google_touch_at=WINDOW_START - ONE_SECOND),
        _contact("g-at-end", google_campaign_ref="20001", google_touch_at=WINDOW_END),
        _contact("g-no-ref", google_campaign_ref=None, google_touch_at=MATURED),
        _contact("g-blank-ref", google_campaign_ref="  ", google_touch_at=MATURED),
        _contact("g-no-touch", google_campaign_ref="20001", google_touch_at=None),
    ]
    repeat = [
        _contact(
            "g-dup", google_campaign_ref="20002", google_touch_at=MATURED, ql_at=MATURED - ONE_DAY
        ),
        _contact(
            "g-dup",
            google_campaign_ref="20002",
            google_touch_at=MATURED - 2 * ONE_DAY,
            ql_at=MATURED - ONE_DAY,
        ),
        _contact(
            "g-ql-before",
            google_campaign_ref="20002",
            google_touch_at=MATURED,
            ql_at=MATURED - ONE_SECOND,
        ),
    ]
    google_crm = _crm(
        HUBSPOT,
        # The primary dataset's refreshed copy of g-dup has no stage date; the discovered
        # record (with its QL entry) is the one the Google population reads.
        [_contact("g-dup")],
        bindings=_bindings(google=GOOGLE_LINK),
        google_contacts=[*discovered, *excluded, *repeat],
    )
    # The companion holds LinkedIn contacts with the same ids and QL entries after the touch;
    # none of them may reach the Google population.
    companion = _crm(
        COMPANION,
        [_contact(f"g-{n}", ql_at=MATURED + ONE_DAY) for n in range(1, 21)],
        bindings=_bindings(google=GOOGLE_LINK),
    )
    table = build_attribution(
        _context(thresholds, window, linkedin_crm=companion, google_crm=google_crm)
    )
    assert sorted(table.veto) == [LI_A, LI_B, G_A, G_B, G_C]

    first = table.veto[G_A]
    assert first.source == "google"
    assert first.leads == first.matured_leads == first.matched_leads == 20
    assert first.eligible_contacts == 20
    assert first.unresolved_leads == 0
    assert first.unresolved_share == Decimal(0)
    assert first.unknown_evidence_contacts == 0
    assert (first.mql, first.ql, first.meetings) == (0, 0, 0)
    assert first.available
    assert first.veto_fires

    second = table.veto[G_B]
    assert second.eligible_contacts == 2
    assert second.ql == 1
    assert not second.veto_fires

    empty = table.veto[G_C]
    assert empty.matured_leads == empty.eligible_contacts == 0
    assert empty.unresolved_share is None
    assert empty.available
    assert not empty.veto_fires


def test_google_evidence_and_bindings_come_from_the_primary_dataset(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    discovered = [
        _contact(f"g-{n}", google_campaign_ref="20001", google_touch_at=MATURED)
        for n in range(1, 21)
    ]
    broken = AssociationFetchInfo("contact", "g-1", "deal", "error")
    unknown = _crm(
        HUBSPOT,
        [],
        bindings=_bindings(google=GOOGLE_LINK),
        fetches={("contact", "g-1", "deal"): broken},
        google_contacts=discovered,
    )
    unbound = _crm(
        HUBSPOT,
        [],
        bindings=_bindings(ql=QL_MISSING, google=GOOGLE_LINK),
        google_contacts=discovered,
    )
    leads = _cohort(20)
    companion = _crm(COMPANION, _contacts(leads))

    with_unknown = build_attribution(
        _context(thresholds, window, linkedin_crm=companion, leads=leads, google_crm=unknown)
    )
    assert with_unknown.veto[G_A].unknown_evidence_contacts == 1
    assert with_unknown.veto[G_A].reason == "CRM evidence unknown for 1 of 20 eligible contacts"
    assert with_unknown.veto[LI_A].veto_fires

    with_unbound = build_attribution(
        _context(thresholds, window, linkedin_crm=companion, leads=leads, google_crm=unbound)
    )
    assert with_unbound.veto[G_A].ql is None
    assert with_unbound.veto[G_A].reason == (
        f"Qualified Lead stage unavailable: {QL_MISSING.reason}"
    )
    assert not with_unbound.veto[G_A].available
    # The LinkedIn population keeps its own (companion) binding.
    assert with_unbound.veto[LI_A].ql == 0
    assert with_unbound.veto[LI_A].veto_fires
    assert with_unbound.ql_available


# -- the table, the population type and the helpers ------------------------------------------


def test_the_veto_mapping_holds_every_campaign_sorted_and_read_only(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    campaigns = {key: CAMPAIGNS[key] for key in (G_B, LI_B, G_A, LI_A)}
    leads = [*_cohort(20), _matched(90, _hs(90), campaign_id=999)]
    google_crm = _crm(HUBSPOT, [], bindings=_bindings(google=GOOGLE_LINK))
    ctx = _context(
        thresholds,
        window,
        linkedin_crm=_crm(COMPANION, _contacts(leads)),
        leads=leads,
        google_crm=google_crm,
        campaigns=campaigns,
    )
    assert [c.id for c in attribution_module._campaigns(ctx, "linkedin")] == [LI_A, LI_B]
    assert [c.id for c in attribution_module._campaigns(ctx, "google")] == [G_A, G_B]
    table = build_attribution(ctx)
    assert list(table.veto) == [LI_A, LI_B, G_A, G_B]
    assert isinstance(table.veto, MappingProxyType)
    with pytest.raises(TypeError):
        cast(Any, table.veto)[999] = table.veto[LI_A]
    assert table.veto[LI_A].leads == 20
    assert table.veto[LI_B].leads == 0
    assert table.veto[LI_B].unresolved_share is None
    assert table.veto[LI_B].available
    assert not table.veto[LI_B].veto_fires
    assert table.cohorts == ()
    assert table.rows() == ()
    assert table.cohort("usd") is None
    assert table.findings == ()


def test_an_empty_context_has_no_veto_population(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    table = build_attribution(AnalysisContext(window, thresholds, NOW))
    assert dict(table.veto) == {}
    assert table.cohorts == ()
    assert table.findings == ()


def _population(**changes: Any) -> VetoPopulation:
    fields: dict[str, Any] = {
        "source": "linkedin",
        "campaign_id": LI_A,
        "matured_leads": 25,
        "matched_leads": 23,
        "unresolved_leads": 2,
        "eligible_contacts": 22,
        "unresolved_share": Decimal("0.08"),
        "unknown_evidence_contacts": 0,
        "mql": 4,
        "ql": 0,
        "meetings": 1,
        "available": True,
        "reason": None,
        "veto_fires": True,
        "leads": 30,
    }
    fields.update(changes)
    return VetoPopulation(**fields)


def test_veto_population_invariants_and_json() -> None:
    with pytest.raises(ValueError, match="unavailable without a reason"):
        _population(available=False, veto_fires=False)
    with pytest.raises(ValueError, match="veto fired while unavailable"):
        _population(available=False, reason="CRM evidence unknown for 1 of 22 eligible contacts")
    population = _population()
    assert not population.unknown_evidence
    assert _population(unknown_evidence_contacts=1).unknown_evidence
    assert population.to_json() == {
        "source": "linkedin",
        "campaign_id": LI_A,
        "leads": 30,
        "matured_leads": 25,
        "matched_leads": 23,
        "unresolved_leads": 2,
        "eligible_contacts": 22,
        "unresolved_share": "0.08",
        "unknown_evidence_contacts": 0,
        "mql": 4,
        "ql": 0,
        "meetings": 1,
        "available": True,
        "reason": None,
        "veto_fires": True,
        "attribution_floor": False,
    }
    assert _population(unresolved_share=None).to_json()["unresolved_share"] is None


@pytest.mark.parametrize(
    ("values", "median"),
    [
        ([], None),
        ([Decimal("12.50")], Decimal("12.50")),
        ([Decimal(3), Decimal(1), Decimal(2)], Decimal(2)),
        ([Decimal(4), Decimal(1), Decimal(3), Decimal(2)], Decimal("2.5")),
    ],
    ids=["empty", "single", "odd-count", "even-count-mean-of-middle-two"],
)
def test_median(values: list[Decimal], median: Decimal | None) -> None:
    assert attribution_module._median(values) == median


def test_attribution_for_computes_the_table_once_per_run(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    leads = _cohort(20)
    ctx = _context(thresholds, window, linkedin_crm=_crm(COMPANION, _contacts(leads)), leads=leads)
    assert CACHE_KEY not in ctx.cache
    table = attribution_for(ctx)
    assert ctx.cache[CACHE_KEY] is table
    assert attribution_for(ctx) is table
    assert table == build_attribution(ctx)
    assert table.veto[LI_A].veto_fires

    stale = _context(thresholds, window, linkedin_crm=None)
    stale.cache[CACHE_KEY] = "not a table"
    rebuilt = attribution_for(stale)
    assert isinstance(rebuilt, AttributionTable)
    assert stale.cache[CACHE_KEY] is rebuilt
    assert rebuilt.veto[LI_A].reason == NO_CRM_REASON
