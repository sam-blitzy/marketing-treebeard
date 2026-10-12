"""Acceptance tests for the White papers section: the attribution chain, floors, W1 and W2.

AAP 0.11.2 row ``acceptance/test_white_paper_attribution.py``, for AAP 0.5.4.7 (attribution
chain, causal check, credit and de-duplication, completeness floors, actions and ranking),
0.5.4.1 (the CRM pipeline link behind C1b and the C2 veto), P8 of 0.5.4.2 and FR-8 ("two lead
counts shown side by side, never blended").

Every guarantee runs against the scenario catalogue (``ads_engine.simulation.scenarios``): a
``fires`` scenario holds the guarantee and its ``does_not_fire`` twin moves one input across
its threshold. The catalogue guard checks every white-paper scenario against its own
``Expectation``; the tests after it pin each guarantee with the numbers and texts the AAP
names. Contexts come from the root ``make_context`` fixture, so no test needs a database or a
network.

Thresholds are read from ``ctx.thresholds``. The only literals are the AAP's own example
numbers (a 10% floor against 9.9%, W1's QL 3, W2's 28 days), each asserted against the loaded
configuration before it is relied on.

Derived contexts: where the catalogue has no twin for a sub-guarantee (for example a partial
target read inside a cohort that otherwise retires a white paper), a test derives one from a
catalogue context with :func:`dataclasses.replace`, moving exactly one input. A derived
context always starts with an empty ``cache``: ``AnalysisContext.cache`` memoises the
attribution table, the G1 set and the scale outcomes per run, and ``replace`` would otherwise
carry the original run's memo into the derived one.
"""

import dataclasses
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol, is_typeddict

import pydantic
import pytest
import yaml

from ads_engine.analysis import attribution, campaigns, precedence, white_papers
from ads_engine.analysis.attribution import (
    BOOKED_LABEL,
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
    VetoPopulation,
    contact_evidence_known,
)
from ads_engine.analysis.context import (
    C1B,
    C2,
    FINDING_PIPELINE_UNAVAILABLE,
    FINDING_SKIPPED_STAGE,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_CREATIVE_FLOOR,
    FLAG_DEAL_FLOOR,
    FLAG_MEETING_TIME_PROXY,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    W1,
    W2,
    WHITE_PAPERS,
    AnalysisContext,
    AssociationFetchInfo,
    Candidate,
    ContactInfo,
    CrmData,
    DecisionSubject,
    LeadInfo,
    MeetingInfo,
    RuleResult,
    TargetReadStatus,
    W,
    cohort_group,
    cohort_group_currency,
)
from ads_engine.analysis.metrics import spend_increase_blocked
from ads_engine.analysis.thresholds import (
    CONTENT_ASSETS_FILE,
    CREATIVE_URN_PREFIX,
    FORM_URN_PREFIX,
    ConfigError,
    ResolvedBinding,
    Thresholds,
)
from ads_engine.analysis.white_papers import (
    ACTION_DOUBLE_DOWN_PREFIX,
    ACTION_INSUFFICIENT_DATA,
    ACTION_NO_CHANGE,
    ACTION_RETIRE,
    METRIC_KEYS,
    W1_ALL_BLOCKED_REASON,
)
from ads_engine.api import schemas
from ads_engine.simulation.scenarios import MODULE_WHITE_PAPERS, SCENARIOS, Scenario, for_module

type MakeContext = Callable[..., AnalysisContext]
"""The root ``make_context`` fixture: ``make_context(scenario, *, thresholds=None, ...)``."""


class ThresholdsFactory(Protocol):
    """The root ``make_thresholds`` fixture: overridden copies of ``config/`` under ``tmp_path``."""

    def __call__(self, overrides: Mapping[str, Any] | None = None) -> Thresholds:
        """Write an overridden copy of ``config/`` and load it."""
        ...

    def write(self, overrides: Mapping[str, Any] | None = None) -> Path:
        """Write an overridden copy of ``config/`` and return its directory, without loading."""
        ...


# ---------------------------------------------------------------------------------------------
# Constants: AAP-fixed example numbers and catalogue identifiers
# ---------------------------------------------------------------------------------------------

WHITE_PAPER_SCENARIOS = for_module(MODULE_WHITE_PAPERS)
"""Every catalogue scenario this module runs, in name order."""

BUILDABLE_SCENARIOS = tuple(
    scenario for scenario in WHITE_PAPER_SCENARIOS if "raises" not in scenario.expect.values
)
"""The scenarios whose configuration loads (``wp.override_conflict`` fails config load)."""

AAP_TOPICS = (
    # Causal check and meeting proxy.
    "wp.causal_pre_touch",
    "wp.meeting_on_touch",
    "wp.meeting_starts_before_touch",
    "wp.meeting_canceled",
    "wp.meeting_proxy",
    # Stage binding and costs.
    "wp.mql_label",
    "wp.skipped_stage",
    "wp.ql_independent",
    "wp.costs",
    # Floors, incomplete associations, target reads and orphan deals.
    "wp.contact_floor",
    "wp.creative_floor",
    "wp.assoc_error",
    "wp.target_read_partial",
    "wp.meeting_read_failed",
    "wp.contact_refresh_failed",
    "wp.orphan_unread_target",
    "wp.orphan_share",
    "wp.deal_search_incomplete",
    # Credit, de-duplication, asset resolution, currencies and the cross-asset view.
    "wp.credit_latest_touch",
    "wp.meeting_two_contacts",
    "wp.repeat_touch_one_mql",
    "wp.override_precedence",
    "wp.override_conflict",
    "wp.currency_cohorts",
    "wp.cross_asset_overlap",
    # W1 and W2.
    "w1.cohort",
    "w1.unblocked_only",
    "w2.cohort",
)
"""The catalogue topics behind each AAP 0.11.2 white-paper guarantee."""

AAP_FLOOR_SHARE = Decimal("0.10")
"""AAP 0.5.4.7: a "meaningful share" of unresolved CRM links is 10% (9.9% is below it)."""

AAP_W1_MIN_QL = 3
"""AAP 0.5.4.7 W1: QL >= 3."""

AAP_W2_MIN_WINDOW_DAYS = 28
"""AAP 0.5.4.7 W2: a window of at least 28 days."""

SHIPPED_OVERRIDE_CREATIVE = f"{CREATIVE_URN_PREFIX}990001301"
"""The landing-page creative ``config/content_assets.yaml`` overrides to the playbook."""

SHIPPED_ASSET_FORMS: Mapping[str, tuple[str, ...]] = {
    "b2b-pipeline-report-2026": (f"{FORM_URN_PREFIX}990000101", f"{FORM_URN_PREFIX}990000102"),
    "paid-media-benchmarks-2026": (f"{FORM_URN_PREFIX}990000201",),
    "attribution-playbook": (f"{FORM_URN_PREFIX}990000301",),
}
"""The shipped white paper registry's canonical lead-form URNs, by asset slug."""

UNREAD_TARGET_ID = "9900399000000001"
"""A HubSpot id in the simulator's reserved range that no scenario creates: a target never read."""

EVIDENCE_COLUMNS = (
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
"""AAP 0.5.5 White papers columns after "asset" and before "action", in display order."""

_COMBINED_TEXT = re.compile(
    r"MQLs?\s*(?:\+|&|/|and|plus|or)\s*(?:QLs?|Qualified)|combined|blended|total leads",
    re.IGNORECASE,
)
"""Wording that would present MQL and QL as one count."""

_TOTALLING_TOKENS = frozenset({"total", "sum", "combined", "blended", "all", "crm"})
"""Name tokens that, beside ``lead``/``leads``, would make a field a blended lead count."""


# ---------------------------------------------------------------------------------------------
# One evaluated scenario
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Run:
    """A scenario's context with its attribution table and the White papers rule result."""

    scenario: Scenario
    ctx: AnalysisContext
    table: AttributionTable
    result: RuleResult

    @property
    def crm(self) -> CrmData:
        """The effective CRM dataset LinkedIn-attributed evidence reads."""
        crm = self.ctx.linkedin_crm
        assert crm is not None, f"{self.scenario.name}: no effective CRM dataset"
        return crm

    def rows(self, slug: object = None) -> tuple[AssetRow, ...]:
        """Every asset row, or the rows of one asset slug."""
        return tuple(row for row in self.table.rows() if slug is None or row.slug == slug)

    def row(self, slug: str | None = None) -> AssetRow:
        """The single asset row (of ``slug`` when given)."""
        rows = self.rows(slug)
        assert len(rows) == 1, f"{self.scenario.name}: expected one row, found {rows}"
        return rows[0]

    def subject_rows(self, values: Mapping[str, object]) -> tuple[AssetRow, ...]:
        """The rows an expectation is about: its ``asset`` when named, else every row."""
        return self.rows(values.get("asset"))

    def item(self, row: AssetRow) -> Candidate:
        """The one White papers item of ``row`` (every asset row yields exactly one)."""
        items = [c for c in self.result.candidates if c.entity_ref.external_id == row.external_id]
        assert len(items) == 1, f"{self.scenario.name}: {row.external_id} has items {items}"
        return items[0]

    def items(self, rule_id: str | None = None, slug: object = None) -> tuple[Candidate, ...]:
        """The White papers items of ``rule_id`` (any rule when ``None``), of ``slug`` if given."""
        keys = {row.external_id for row in self.rows(slug)}
        return tuple(
            item
            for item in self.result.candidates
            if (rule_id is None or item.rule_id == rule_id) and item.entity_ref.external_id in keys
        )

    def withheld(self, rule_id: str) -> tuple[str, ...]:
        """The reasons of the withheld lines of ``rule_id``."""
        return tuple(line.reason for line in self.result.withheld if line.rule_id == rule_id)

    def creative_key(self, creative_id: int | None) -> str | None:
        """The catalogue spec key of a creative id (``None`` for the unknown creative)."""
        linkedin = self.scenario.spec.linkedin
        assert linkedin is not None
        keys = {
            self.scenario.ids.id("linkedin", "creative", spec.key): spec.key
            for spec in linkedin.creatives
        }
        return None if creative_id is None else keys[creative_id]


def evaluate(scenario: Scenario, ctx: AnalysisContext) -> _Run:
    """Build the attribution table and evaluate W1/W2 on ``ctx``."""
    return _Run(scenario, ctx, attribution.build_attribution(ctx), white_papers.evaluate(ctx))


def run_scenario(make_context: MakeContext, name: str) -> _Run:
    """Evaluate the catalogue scenario ``name`` through the ``make_context`` fixture."""
    scenario = SCENARIOS[name]
    return evaluate(scenario, make_context(scenario))


def rerun(run: _Run, ctx: AnalysisContext) -> _Run:
    """Evaluate a context derived from ``run``'s scenario."""
    return evaluate(run.scenario, ctx)


# ---------------------------------------------------------------------------------------------
# Derived contexts: one input moved, a fresh per-run cache
# ---------------------------------------------------------------------------------------------


def derive(ctx: AnalysisContext, **changes: Any) -> AnalysisContext:
    """Return ``ctx`` with ``changes`` applied and an empty per-run cache."""
    return dataclasses.replace(ctx, cache={}, **changes)


def with_crm(ctx: AnalysisContext, **changes: Any) -> AnalysisContext:
    """Return ``ctx`` whose effective CRM dataset has ``changes`` applied."""
    crm = ctx.linkedin_crm
    assert crm is not None
    return derive(ctx, linkedin_crm=dataclasses.replace(crm, **changes))


def with_unread_target(
    ctx: AnalysisContext, hs_id: str, to_type: str, target_read_status: TargetReadStatus
) -> AnalysisContext:
    """Make one contact's contact-to-``to_type`` fetch ``ok`` with one target left unread.

    The association to :data:`UNREAD_TARGET_ID` is fetched successfully, but the target's batch
    read did not return it (``target_read_status`` ``partial`` or ``error``). AAP 0.3.3: an
    associated but unread meeting or deal is never treated as absent.
    """
    crm = ctx.linkedin_crm
    assert crm is not None
    assert UNREAD_TARGET_ID not in crm.meetings
    assert UNREAD_TARGET_ID not in crm.deals
    key = ("contact", hs_id, to_type)
    fetch = AssociationFetchInfo(
        "contact", hs_id, to_type, "ok", target_read_status, (UNREAD_TARGET_ID,)
    )
    return with_crm(
        ctx,
        associations={**crm.associations, key: (*crm.targets(*key), UNREAD_TARGET_ID)},
        fetches={**crm.fetches, key: fetch},
    )


def with_contacts(
    ctx: AnalysisContext, hs_ids: Iterable[str], change: Callable[[ContactInfo], ContactInfo]
) -> AnalysisContext:
    """Return ``ctx`` with ``change`` applied to the contacts ``hs_ids`` of its CRM dataset."""
    crm = ctx.linkedin_crm
    assert crm is not None
    selected = set(hs_ids)
    assert selected
    assert selected <= set(crm.contacts)
    contacts = {
        hs_id: change(contact) if hs_id in selected else contact
        for hs_id, contact in crm.contacts.items()
    }
    return with_crm(ctx, contacts=contacts)


def with_leads(ctx: AnalysisContext, change: Callable[[LeadInfo], LeadInfo]) -> AnalysisContext:
    """Return ``ctx`` with ``change`` applied to every lead."""
    return derive(ctx, leads=tuple(change(lead) for lead in ctx.leads))


def without_ql(contact: ContactInfo) -> ContactInfo:
    """The contact with no Qualified Lead date, at an entry stage (so it is not "skipped")."""
    return dataclasses.replace(contact, ql_at=None, lifecycle_stage="lead")


def without_mql(contact: ContactInfo) -> ContactInfo:
    """The contact with no MQL date, keeping every other field."""
    return dataclasses.replace(contact, mql_at=None)


# ---------------------------------------------------------------------------------------------
# Oracles: the inputs a row reads, recomputed from the context alone
# ---------------------------------------------------------------------------------------------


def lead_row_key(ctx: AnalysisContext, lead: LeadInfo) -> tuple[str, str] | None:
    """The (asset slug, currency) a lead resolves to: creative override, else its own form."""
    creative = None if lead.creative_id is None else ctx.creatives.get(lead.creative_id)
    urn = creative.urn if creative is not None else lead.creative_urn
    asset = ctx.resolve_asset(creative_urn=urn, form_urn=lead.lead_form_urn)
    if asset is None:
        return None
    return asset.slug, ctx.account_currency(lead.campaign_id).strip().upper()


def row_leads(run: _Run, row: AssetRow) -> tuple[LeadInfo, ...]:
    """The window leads that resolve to ``row``'s asset and currency."""
    key = (row.slug, row.currency)
    return tuple(lead for lead in run.ctx.leads if lead_row_key(run.ctx, lead) == key)


def row_contacts(run: _Run, row: AssetRow) -> tuple[str, ...]:
    """The distinct contacts matched to ``row``'s leads, sorted."""
    return tuple(
        sorted(
            {
                lead.contact_hs_id
                for lead in row_leads(run, row)
                if lead.matched and lead.contact_hs_id is not None
            }
        )
    )


def anchors(run: _Run, row: AssetRow) -> dict[str, datetime]:
    """Each matched contact's earliest touch with the row's asset: its causal-check anchor."""
    found: dict[str, datetime] = {}
    for lead in row_leads(run, row):
        if lead.matched and lead.contact_hs_id is not None:
            prior = found.get(lead.contact_hs_id)
            if prior is None or lead.submitted_at < prior:
                found[lead.contact_hs_id] = lead.submitted_at
    return found


def associated(run: _Run, contacts: Iterable[str], to_type: str) -> dict[str, tuple[str, ...]]:
    """Each stored associated ``to_type`` id with the given contacts it links to."""
    linked: dict[str, list[str]] = {}
    for hs_id in sorted(set(contacts)):
        for target in run.crm.targets("contact", hs_id, to_type):
            linked.setdefault(target, []).append(hs_id)
    return {target: tuple(members) for target, members in sorted(linked.items())}


def row_meetings(run: _Run, row: AssetRow) -> dict[str, MeetingInfo]:
    """The meetings associated with ``row``'s contacts, by HubSpot id."""
    meetings = associated(run, row_contacts(run, row), "meeting")
    return {mid: run.crm.meetings[mid] for mid in meetings if mid in run.crm.meetings}


def contact_fetches(
    run: _Run, row: AssetRow, to_types: tuple[str, ...] = ("meeting", "deal")
) -> list[AssociationFetchInfo]:
    """The contact-to-meeting and contact-to-deal fetches of ``row``'s contacts."""
    found: list[AssociationFetchInfo] = []
    for hs_id in row_contacts(run, row):
        for to_type in to_types:
            fetch = run.crm.fetch("contact", hs_id, to_type)
            if fetch is not None:
                found.append(fetch)
    return found


def matched_lead(run: _Run, hs_id: str) -> LeadInfo:
    """A matched lead of the contact ``hs_id``."""
    return next(lead for lead in run.ctx.leads if lead.matched and lead.contact_hs_id == hs_id)


def money(amount: Decimal) -> str:
    """Render an amount at two decimals, as the catalogue states money."""
    return format(amount.quantize(Decimal("0.01")), "f")


def reason_money(amount: Decimal) -> str:
    """Render a USD amount as a W reason words it: grouped, rounded half up to the cent.

    That is the web client's ``formatMoney`` text for the same stored value in the table cell
    (with a plain space for its no-break one): ``2766`` is ``USD 2,766.00`` and a tie such as
    ``24.005`` rounds away from zero, ``USD 24.01``.
    """
    return f"USD {amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def known(count: CountValue) -> int:
    """The value of a count that is known exactly: neither a floor nor unavailable."""
    assert not count.unavailable, count
    assert not count.floor, count
    assert count.value is not None
    return count.value


def has_events(*counts: CountValue) -> bool:
    """Whether any of the counts is known and above zero."""
    return any((count.value or 0) > 0 for count in counts)


_CREDIT_COLUMNS: tuple[
    tuple[str, Callable[[CreditRow], object], Callable[[AssetRow], object]], ...
] = (
    ("spend", lambda credit: credit.spend, lambda row: row.spend),
    ("leads", lambda credit: credit.leads, lambda row: row.linkedin_leads),
    ("mql", lambda credit: credit.mql.value, lambda row: row.mql.value),
    ("ql", lambda credit: credit.ql.value, lambda row: row.ql.value),
    (
        "meetings_booked",
        lambda credit: credit.meetings_booked.value,
        lambda row: row.meetings_booked.value,
    ),
    (
        "other_meetings",
        lambda credit: credit.other_meetings.value,
        lambda row: row.other_meetings.value,
    ),
    ("deals", lambda credit: credit.deals.value, lambda row: row.deals_after_touch.value),
)
"""Each per-creative column with the asset-row total it must sum to."""


def partitions(row: AssetRow) -> bool:
    """Whether the per-creative rows sum exactly to the asset row in every column."""
    for _name, part, total in _CREDIT_COLUMNS:
        values = [part(credit) for credit in row.creative_rows]
        expected = total(row)
        if expected is None:
            if any(value is not None for value in values):
                return False
            continue
        if any(not isinstance(value, int | Decimal) for value in values):
            return False
        if sum((value for value in values if isinstance(value, int | Decimal)), Decimal(0)) != (
            expected
        ):
            return False
    return True


# ---------------------------------------------------------------------------------------------
# Never blended: field names and rendered text
# ---------------------------------------------------------------------------------------------


def combines_mql_and_ql(name: str) -> bool:
    """Whether a field name would combine the MQL and QL counts into one value.

    True for a name carrying both ``mql`` and ``ql`` tokens (``mql_ql``, ``mql_plus_ql``) and
    for a totalling lead count such as ``leads_total`` or ``crm_leads``. A platform count
    (``linkedin_leads``) and a QL-only quantity (``total_ql``) are not combinations.
    """
    tokens = set(re.split(r"[^a-z0-9]+", name.lower())) - {""}
    if {"mql", "ql"} <= tokens:
        return True
    return bool(tokens & {"lead", "leads"}) and bool(tokens & _TOTALLING_TOKENS)


def field_names(owner: type) -> tuple[str, ...]:
    """The field names of a dataclass, a pydantic model or a TypedDict."""
    if dataclasses.is_dataclass(owner):
        return tuple(item.name for item in dataclasses.fields(owner))
    if issubclass(owner, pydantic.BaseModel):
        return tuple(owner.model_fields)
    if is_typeddict(owner):
        # TypedDict classes carry their key sets at runtime; static types do not know them.
        typed: Any = owner
        return tuple(sorted(typed.__required_keys__ | typed.__optional_keys__))
    return ()


def api_models() -> tuple[type, ...]:
    """Every pydantic model and TypedDict ``ads_engine.api.schemas`` defines."""
    return tuple(
        value
        for value in vars(schemas).values()
        if isinstance(value, type)
        and value.__module__ == schemas.__name__
        and (issubclass(value, pydantic.BaseModel) or is_typeddict(value))
    )


ATTRIBUTION_TYPES: tuple[type, ...] = (
    AssetRow,
    CreditRow,
    Cohort,
    VetoPopulation,
    AttributionTable,
)
"""The attribution value types whose fields carry CRM counts."""


def blended_fields() -> tuple[str, ...]:
    """``Owner.field`` for every attribution or API field that combines MQL and QL."""
    return tuple(
        f"{owner.__name__}.{name}"
        for owner in (*ATTRIBUTION_TYPES, *api_models())
        for name in field_names(owner)
        if combines_mql_and_ql(name)
    )


def presents_combined_count(text: str, row: AssetRow) -> bool:
    """Whether ``text`` presents MQL and QL as one count, in words or as their sum."""
    if _COMBINED_TEXT.search(text):
        return True
    mql, ql = row.mql.value, row.ql.value
    if not mql or not ql:
        return False
    pattern = rf"\b{mql + ql}\s+(?:MQLs?|QLs?|Qualified|leads?|contacts?)\b"
    return re.search(pattern, text) is not None


# ---------------------------------------------------------------------------------------------
# Catalogue expectations: one extractor per ``Expectation.values`` key
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Each:
    """One observed value per subject row; the expected value must hold for every one."""

    values: tuple[object, ...]


type _Extract = Callable[[_Run, Mapping[str, object]], object]


def matches(observed: object, expected: object) -> bool:
    """Compare an observed value with a catalogue value, in the catalogue's representation.

    Counts compare as rendered text ("≥ 900", "unavailable") when the catalogue states text and
    as an exact known value (no floor, not unavailable) when it states an integer. Costs compare
    as rendered text ("100.00", "≤ 5.56", "—"). Decimals compare numerically with a decimal
    string ("0.10" equals 0.1).
    """
    if isinstance(observed, _Each):
        return bool(observed.values) and all(matches(value, expected) for value in observed.values)
    if isinstance(observed, CountValue):
        if isinstance(expected, str):
            return observed.render() == expected
        return not observed.floor and not observed.unavailable and observed.value == expected
    if isinstance(observed, CostValue):
        return isinstance(expected, str) and observed.render() == expected
    if isinstance(observed, Decimal) and isinstance(expected, str):
        return observed == Decimal(expected)
    if isinstance(observed, tuple) and isinstance(expected, tuple):
        return len(observed) == len(expected) and all(
            matches(value, wanted) for value, wanted in zip(observed, expected, strict=True)
        )
    return observed == expected


def unique(values: Iterable[object]) -> object:
    """The one distinct value, or every distinct value as a tuple (which then fails a match)."""
    distinct = tuple(dict.fromkeys(values))
    return distinct[0] if len(distinct) == 1 else distinct


def per_row(read: Callable[[_Run, AssetRow], object]) -> _Extract:
    """An extractor reading one value from each subject row."""

    def extract(run: _Run, values: Mapping[str, object]) -> object:
        return _Each(tuple(read(run, row) for row in run.subject_rows(values)))

    return extract


def cohort_of(run: _Run, row: AssetRow) -> Cohort:
    """The currency cohort holding ``row``."""
    cohort = run.table.cohort(row.currency)
    assert cohort is not None
    return cohort


def canceled_excluded(run: _Run, row: AssetRow) -> int:
    """Associated meetings whose outcome ``crm.meeting_outcome_exclusions`` excludes."""
    exclusions = {
        value.strip().upper()
        for value in run.ctx.thresholds.strings("crm.meeting_outcome_exclusions")
    }
    return sum(
        1
        for meeting in row_meetings(run, row).values()
        if (meeting.outcome or "").strip().upper() in exclusions
    )


def meetings_on_touch_counted(run: _Run, row: AssetRow) -> bool:
    """Whether meetings created exactly on a touch instant exist and every meeting counted."""
    touches = {lead.submitted_at for lead in row_leads(run, row) if lead.matched}
    meetings = row_meetings(run, row)
    on_touch = [meeting for meeting in meetings.values() if meeting.created_at in touches]
    return bool(on_touch) and row.meetings_booked.value == len(meetings)


def shared_meetings_counted_once(run: _Run, row: AssetRow) -> bool:
    """Whether a meeting links two of the row's contacts and every meeting counts once."""
    meetings = associated(run, row_contacts(run, row), "meeting")
    shared = [mid for mid, contacts in meetings.items() if len(contacts) >= 2]
    return bool(shared) and row.meetings_booked.value == len(meetings)


def credited_creative(run: _Run, row: AssetRow) -> object:
    """The spec key of the creative every CRM event of the row is credited to."""
    return unique(
        run.creative_key(credit.creative_id)
        for credit in row.creative_rows
        if has_events(
            credit.mql, credit.ql, credit.meetings_booked, credit.other_meetings, credit.deals
        )
    )


def last_touch_creative(run: _Run, row: AssetRow) -> str | None:
    """The spec key of the creative of the row's latest lead."""
    latest = max(row_leads(run, row), key=lambda lead: (lead.submitted_at, lead.id))
    return run.creative_key(latest.creative_id)


def override_row(run: _Run, row: AssetRow) -> CreditRow:
    """The per-creative row of the creative the row's asset lists as an override."""
    asset = next(item for item in run.ctx.thresholds.content_assets if item.slug == row.slug)
    rows = [credit for credit in row.creative_rows if credit.creative_urn in asset.creative_urns]
    assert len(rows) == 1, rows
    return rows[0]


def cross_currency_amounts(run: _Run) -> bool:
    """Whether any total, median or amount of the result spans or leaves its currency."""
    for cohort in run.table.cohorts:
        if any(row.currency != cohort.currency for row in cohort.rows):
            return True
        if cohort.spend_total != sum((row.spend for row in cohort.rows), Decimal(0)):
            return True
    for item in run.result.candidates:
        currency = cohort_group_currency(item.group)
        own = None if currency is None else run.table.cohort(currency)
        view = item.evidence.extra["cohort"]
        if own is None or view["currency"] != currency:
            return True
        if view["spend_total"] != own.spend_total:
            return True
        if any(metric.currency not in (None, currency) for metric in item.evidence.metrics):
            return True
    return False


def assets_credited(run: _Run) -> int:
    """How many asset rows credit a contact who touched every asset (any-touch credit)."""
    rows = run.table.rows()
    contacts = [set(row_contacts(run, row)) for row in rows]
    shared = set.intersection(*contacts) if contacts else set()
    return sum(
        1
        for row, members in zip(rows, contacts, strict=True)
        if shared & members
        and has_events(
            row.mql, row.ql, row.meetings_booked, row.other_meetings, row.deals_after_touch
        )
    )


def orphan_share(run: _Run) -> str:
    """The orphan-deal share at four decimals, or ``unknown``."""
    share = run.table.orphan_deals
    if share.unknown or share.share is None:
        return "unknown"
    return format(share.share.quantize(Decimal("0.0001")), "f")


def extra_names(run: _Run, row: AssetRow, key: str) -> tuple[str, ...]:
    """The campaign names an item lists under ``evidence.extra[key]``."""
    return tuple(entry["name"] for entry in run.item(row).evidence.extra.get(key, ()))


EXTRACTORS: Mapping[str, _Extract] = {
    # Row selection and platform counts.
    "asset": per_row(lambda run, row: row.slug),
    "spend": per_row(lambda run, row: money(row.spend)),
    "leads": per_row(lambda run, row: row.linkedin_leads),
    "contacts": per_row(lambda run, row: row.matched_contacts),
    # CRM counts, each its own field.
    "mql": per_row(lambda run, row: row.mql),
    "ql": per_row(lambda run, row: row.ql),
    "meetings_booked": per_row(lambda run, row: row.meetings_booked),
    "other_meetings": per_row(lambda run, row: row.other_meetings),
    "deals": per_row(lambda run, row: row.deals_after_touch),
    "mql_label": per_row(lambda run, row: row.mql_label),
    # Costs and the reason a cost is not shown.
    "cost_per_ql": per_row(lambda run, row: row.cost_per_ql),
    "cost_per_meeting": per_row(lambda run, row: row.cost_per_meeting),
    "reason": per_row(lambda run, row: row.cost_per_ql.note),
    "ql_cost_note": per_row(lambda run, row: row.cost_per_ql.note),
    "meeting_cost_note": per_row(lambda run, row: row.cost_per_meeting.note),
    # Causal check and stage binding.
    "already_before_touch": per_row(
        lambda run, row: unique((row.already_mql_before, row.already_ql_before))
    ),
    "pre_touch_deals_excluded": per_row(lambda run, row: row.deals_before_touch),
    "excluded_started_before_touch": per_row(lambda run, row: row.meetings_before_touch),
    "canceled_excluded": per_row(canceled_excluded),
    "meetings_on_touch_counted": per_row(meetings_on_touch_counted),
    "skipped": per_row(
        lambda run, row: unique(value for value in (row.skipped_mql, row.skipped_ql) if value)
    ),
    "blended_fields": lambda run, values: blended_fields(),
    # Floors and evidence completeness.
    "unmatched": per_row(lambda run, row: row.unmatched_leads),
    "contact_share": per_row(lambda run, row: row.contact_share),
    "unknown_evidence_share": per_row(lambda run, row: row.unknown_evidence_share),
    "creative_floor": per_row(
        lambda run, row: (
            row.has_flag(FLAG_CREATIVE_FLOOR)
            and all(credit.ql.floor for credit in row.creative_rows if not credit.is_unknown)
        )
    ),
    "association_status": per_row(
        lambda run, row: unique(fetch.status for fetch in contact_fetches(run, row, ("deal",)))
    ),
    "target_read_status": per_row(
        lambda run, row: unique(
            fetch.target_read_status for fetch in contact_fetches(run, row) if not fetch.known
        )
    ),
    "missing_target_ids": per_row(
        lambda run, row: tuple(
            sorted(
                target
                for fetch in contact_fetches(run, row)
                if not fetch.known
                for target in fetch.missing_target_ids
            )
        )
    ),
    "contact_read_status": per_row(
        lambda run, row: unique(
            lead.contact_read_status
            for lead in row_leads(run, row)
            if lead.matched and lead.contact_read_status != "ok"
        )
    ),
    # Orphan deals (portal-wide).
    "window_deals": lambda run, values: len(run.crm.window_deal_ids),
    "assessed_deals": lambda run, values: run.table.orphan_deals.assessed,
    "orphan_deals": lambda run, values: run.table.orphan_deals.orphans,
    "unassessed": lambda run, values: tuple(
        sorted(
            deal_id
            for deal_id in run.crm.window_deal_ids
            if not run.crm.association_known("deal", deal_id, "contact")
        )
    ),
    "orphan_share": lambda run, values: orphan_share(run),
    # Credit, de-duplication and asset resolution.
    "credited_creative": per_row(credited_creative),
    "last_touch_creative": per_row(last_touch_creative),
    "rows_sum_to_total": per_row(lambda run, row: partitions(row)),
    "shared_meetings_counted_once": per_row(shared_meetings_counted_once),
    "override_creative": per_row(lambda run, row: override_row(run, row).creative_urn),
    "override_spend": per_row(lambda run, row: money(override_row(run, row).spend)),
    "override_leads": per_row(lambda run, row: override_row(run, row).leads),
    "override_ql": per_row(lambda run, row: override_row(run, row).ql),
    # Currency cohorts and the cross-asset view.
    "cohorts": lambda run, values: tuple(cohort.currency for cohort in run.table.cohorts),
    "rows_for_asset": lambda run, values: max(Counter(r.slug for r in run.table.rows()).values()),
    "cross_currency_total": lambda run, values: cross_currency_amounts(run),
    "note": lambda run, values: unique(
        [cohort.overlap_note for cohort in run.table.cohorts]
        + [item.evidence.extra["cohort"]["overlap_note"] for item in run.result.candidates]
    ),
    "assets_credited": lambda run, values: assets_credited(run),
    # W1 and W2.
    "median": per_row(lambda run, row: money(cohort_of(run, row).median_cost_per_ql or Decimal(0))),
    "campaigns": per_row(lambda run, row: extra_names(run, row, "double_down_campaigns")),
    "blocked": per_row(lambda run, row: extra_names(run, row, "blocked_campaigns")),
    "spend_to_median": per_row(
        lambda run, row: run.item(row).evidence.extra.get("spend_to_median")
    ),
}
"""How each catalogue ``Expectation.values`` key is observed on a scenario's output."""


# ---------------------------------------------------------------------------------------------
# Phase 1: the catalogue guard
# ---------------------------------------------------------------------------------------------


def test_the_catalogue_has_white_paper_scenarios_for_every_guarantee() -> None:
    """The catalogue gives this module a scenario per AAP 0.11.2 cell, with W1 and W2 firing."""
    names = {scenario.name for scenario in WHITE_PAPER_SCENARIOS}
    assert len(names) == len(WHITE_PAPER_SCENARIOS)
    topics = {name.rsplit(".", 1)[0] for name in names}
    assert set(AAP_TOPICS) <= topics, sorted(set(AAP_TOPICS) - topics)
    fired = {scenario.expect.rule_id for scenario in WHITE_PAPER_SCENARIOS if scenario.expect.fires}
    assert {W1, W2} <= fired
    for scenario in WHITE_PAPER_SCENARIOS:
        assert scenario.expect.rule_id in {W1, W2}
        if scenario.pair is not None:
            twin = SCENARIOS[scenario.pair]
            assert twin.pair == scenario.name
            assert twin.module == MODULE_WHITE_PAPERS


def test_every_expectation_key_has_an_extractor() -> None:
    """No catalogue value goes unchecked: every key this module's scenarios use is observed."""
    keys = {key for scenario in WHITE_PAPER_SCENARIOS for key in scenario.expect.values}
    assert keys - set(EXTRACTORS) <= {"raises"}


@pytest.mark.parametrize("scenario", WHITE_PAPER_SCENARIOS, ids=lambda scenario: scenario.name)
def test_catalogue_scenario_matches_its_expectation(
    make_context: MakeContext, scenario: Scenario
) -> None:
    """Every white-paper scenario's attribution table and W1/W2 items match its Expectation."""
    expect = scenario.expect
    if "raises" in expect.values:
        # A registry that fails config load builds no context at all.
        assert expect.values["raises"] == ConfigError.__name__
        assert not expect.fires
        with pytest.raises(ConfigError):
            make_context(scenario)
        return
    run = evaluate(scenario, make_context(scenario))
    asset = expect.values.get("asset")
    emitted = run.items(expect.rule_id, asset)
    assert bool(emitted) is expect.fires, (
        f"{scenario.name}: {expect.rule_id} emitted {[(i.rule_id, i.action) for i in emitted]}"
    )
    subject = emitted or run.items(slug=asset)
    for code in expect.flags:
        assert any(item.has_flag(code) for item in subject), f"{scenario.name}: no flag {code}"
    if expect.finding is not None:
        assert expect.finding in {finding.code for finding in run.result.findings}
    if expect.withheld_reason is not None:
        assert any(
            expect.withheld_reason.lower() in reason.lower()
            for reason in run.withheld(str(expect.rule_id))
        ), f"{scenario.name}: withheld {run.result.withheld}"
    for key, expected in expect.values.items():
        observed = EXTRACTORS[key](run, expect.values)
        assert matches(observed, expected), (
            f"{scenario.name}: {key} observed {observed!r}, expected {expected!r}"
        )


@pytest.mark.parametrize("scenario", BUILDABLE_SCENARIOS, ids=lambda scenario: scenario.name)
def test_every_asset_row_yields_one_item_in_its_cohort_group(
    make_context: MakeContext, scenario: Scenario
) -> None:
    """Each asset row is one item (W1, W2 or W) in ``cohort:{CUR}``, with its row's flags."""
    run = evaluate(scenario, make_context(scenario))
    assert len(run.result.candidates) == len(run.table.rows())
    for row in run.table.rows():
        item = run.item(row)
        assert item.rule_id in {W1, W2, W}
        assert item.section == WHITE_PAPERS
        assert item.group == cohort_group(row.currency)
        assert item.decision_subject is None
        assert item.flag_codes == row.flags
        assert item.entity_ref.name == row.title
        assert partitions(row)
    assert all(finding.section == WHITE_PAPERS for finding in run.result.findings)


# ---------------------------------------------------------------------------------------------
# Phase 2: causal check and the meeting-time proxy
# ---------------------------------------------------------------------------------------------


def test_events_before_the_touch_are_excluded_and_reported(make_context: MakeContext) -> None:
    """Causal check: MQL, QL, meetings and deals dated before the anchor are excluded, reported.

    The anchor is the contact's earliest touch with the asset in the window. In the ``fires``
    world every contact was already MQL and QL before its touch; in the twin those stages come
    one to three days after it.
    """
    before = run_scenario(make_context, "wp.causal_pre_touch.fires")
    after = run_scenario(make_context, "wp.causal_pre_touch.does_not_fire")
    row, twin = before.row(), after.row()

    touches = anchors(before, row)
    assert len(touches) == 10
    for hs_id, anchor in touches.items():
        contact = before.crm.contacts[hs_id]
        assert contact.mql_at is not None
        assert contact.mql_at < anchor
        assert contact.ql_at is not None
        assert contact.ql_at < anchor
    assert (known(row.mql), known(row.ql), known(row.meetings_booked)) == (0, 0, 0)
    assert (row.already_mql_before, row.already_ql_before) == (10, 10)
    assert row.meetings_before_touch == 13
    assert dict(before.item(row).evidence.extra["already_before"]) == {
        "mql": 10,
        "ql": 10,
        "meetings": 13,
        "deals": 10,
    }

    assert (known(twin.mql), known(twin.ql), known(twin.meetings_booked)) == (10, 10, 13)
    assert (twin.already_mql_before, twin.already_ql_before, twin.meetings_before_touch) == (
        0,
        0,
        0,
    )


def test_a_deal_counts_only_when_created_at_or_after_the_touch(make_context: MakeContext) -> None:
    """Causal check on deals: ``createdate`` before the anchor is excluded, at or after counted.

    Every contact in the world has one deal created before its touch and one created two to four
    days after it, so the same row shows both outcomes of the check.
    """
    run = run_scenario(make_context, "wp.causal_pre_touch.does_not_fire")
    row = run.row()
    touches = anchors(run, row)
    deals = associated(run, touches, "deal")
    created_before = after_touch = 0
    for deal_id, contacts in deals.items():
        created = run.crm.deals[deal_id].created_at
        assert created is not None
        if all(created < touches[hs_id] for hs_id in contacts):
            created_before += 1
        else:
            after_touch += 1
    assert (created_before, after_touch) == (10, 10)
    assert row.deals_before_touch == created_before
    assert known(row.deals_after_touch) == after_touch


def test_a_meeting_created_on_the_touch_instant_is_counted(make_context: MakeContext) -> None:
    """Causal check: a meeting whose record is created exactly at the touch is "after touch"."""
    run = run_scenario(make_context, "wp.meeting_on_touch.case")
    row = run.row()
    touches = anchors(run, row)
    meetings = associated(run, touches, "meeting")
    assert len(meetings) == 13
    on_touch = [
        mid
        for mid, contacts in meetings.items()
        if run.crm.meetings[mid].created_at in {touches[hs_id] for hs_id in contacts}
    ]
    assert on_touch
    assert known(row.meetings_booked) == len(meetings)
    assert row.meetings_before_touch == 0
    assert row.cost_per_meeting.value == row.spend / len(meetings)


def test_a_meeting_starting_before_the_touch_is_excluded(make_context: MakeContext) -> None:
    """Causal check: created after the touch but starting before it fails the check."""
    run = run_scenario(make_context, "wp.meeting_starts_before_touch.case")
    row = run.row()
    touches = anchors(run, row)
    meetings = associated(run, touches, "meeting")
    assert len(meetings) == 13
    for mid, contacts in meetings.items():
        meeting = run.crm.meetings[mid]
        assert meeting.created_at is not None
        assert meeting.start_at is not None
        assert all(meeting.created_at >= touches[hs_id] for hs_id in contacts)
        assert all(meeting.start_at < touches[hs_id] for hs_id in contacts)
    assert known(row.meetings_booked) == 0
    assert known(row.other_meetings) == 0
    assert row.meetings_before_touch == 13
    assert row.cost_per_meeting.render() == "—"


def test_canceled_meetings_are_excluded_by_the_configured_outcomes(
    make_context: MakeContext, make_thresholds: ThresholdsFactory
) -> None:
    """A ``CANCELED`` meeting is never counted; with no outcome exclusion configured it is."""
    scenario = SCENARIOS["wp.meeting_canceled.case"]
    run = evaluate(scenario, make_context(scenario))
    row = run.row()
    exclusions = run.ctx.thresholds.strings("crm.meeting_outcome_exclusions")
    assert "CANCELED" in exclusions
    meetings = row_meetings(run, row)
    assert len(meetings) == 13
    assert {meeting.outcome for meeting in meetings.values()} == {"CANCELED"}
    assert (known(row.meetings_booked), known(row.other_meetings)) == (0, 0)
    assert row.meetings_before_touch == 0
    assert row.cost_per_meeting.note == NO_MEETINGS_NOTE

    # One input across: the same world under a configuration that excludes no outcome.
    lenient = make_thresholds({"crm.meeting_outcome_exclusions": []})
    counted = evaluate(scenario, make_context(scenario, thresholds=lenient)).row()
    assert known(counted.meetings_booked) == 13


def test_a_non_booking_source_meeting_is_ordering_unverified(
    make_context: MakeContext, make_thresholds: ThresholdsFactory
) -> None:
    """Meeting proxy: a meeting from a source outside ``crm.booking_sources`` is "Other meetings".

    It passes the time checks, so it is counted in "Other meetings (ordering unverified)", the
    row carries ``meeting_time_proxy`` and cost per meeting excludes it. A ``MEETINGS_PUBLIC``
    meeting is "booked (record created)" and is the cost's denominator.
    """
    scenario = SCENARIOS["wp.meeting_proxy.case"]
    run = evaluate(scenario, make_context(scenario))
    row = run.row()
    sources = run.ctx.thresholds.strings("crm.booking_sources")
    meetings = row_meetings(run, row)
    assert len(meetings) == 13
    assert all(meeting.meeting_source not in sources for meeting in meetings.values())
    assert known(row.other_meetings) == 13
    assert known(row.meetings_booked) == 0
    assert row.has_flag(FLAG_MEETING_TIME_PROXY)
    assert OTHER_MEETINGS_LABEL.lower() in row.flag_messages[FLAG_MEETING_TIME_PROXY]
    assert (row.cost_per_meeting.render(), row.cost_per_meeting.note) == ("—", NO_MEETINGS_NOTE)
    evidence = run.item(row).evidence
    assert evidence.metric("other_meetings").label == OTHER_MEETINGS_LABEL
    assert evidence.metric("other_meetings").value == 13
    assert evidence.metric("meetings_booked").label == f"Meetings {BOOKED_LABEL}"
    assert evidence.metric("cost_per_meeting").value == "—"

    booking = run_scenario(make_context, "wp.meeting_on_touch.case")
    booked = booking.row()
    assert {meeting.meeting_source for meeting in row_meetings(booking, booked).values()} <= set(
        sources
    )
    assert known(booked.meetings_booked) == 13
    assert not booked.has_flag(FLAG_MEETING_TIME_PROXY)
    assert booked.cost_per_meeting.render() == money(booked.spend / 13)

    # One input across: the same source configured as a booking source is booked.
    configured = make_thresholds(
        {"crm.booking_sources": [*sources, *{m.meeting_source for m in meetings.values()}]}
    )
    rebooked = evaluate(scenario, make_context(scenario, thresholds=configured)).row()
    assert (known(rebooked.meetings_booked), known(rebooked.other_meetings)) == (13, 0)
    assert not rebooked.has_flag(FLAG_MEETING_TIME_PROXY)


# ---------------------------------------------------------------------------------------------
# Phase 3: stage binding, and MQL and QL never blended
# ---------------------------------------------------------------------------------------------


def test_the_blend_detector_recognises_combined_names_and_text(make_context: MakeContext) -> None:
    """The never-blended guards below are not vacuous: they flag combinations they must flag."""
    for name in ("mql_ql", "mql_plus_ql", "leads_total", "total_leads", "crm_leads"):
        assert combines_mql_and_ql(name), name
    for name in ("mql", "ql", "mql_label", "total_ql", "linkedin_leads", "matched_leads"):
        assert not combines_mql_and_ql(name), name
    row = run_scenario(make_context, "wp.causal_pre_touch.does_not_fire").row()
    assert (known(row.mql), known(row.ql)) == (10, 10)
    assert presents_combined_count("20 leads reached the CRM", row)
    assert presents_combined_count("MQL + QL: 20", row)
    assert not presents_combined_count("10 Qualified Leads; 10 MQL (First meeting booked)", row)


def test_mql_and_ql_are_separate_fields_everywhere() -> None:
    """Never blended: attribution types and API schemas expose MQL and QL separately.

    No field of ``AssetRow``, ``CreditRow``, ``Cohort``, ``VetoPopulation``,
    ``AttributionTable`` or any ``ads_engine.api.schemas`` model or TypedDict combines them,
    and the types that carry MQL carry QL as its own field.
    """
    assert blended_fields() == ()
    for owner in (AssetRow, CreditRow, VetoPopulation):
        names = set(field_names(owner))
        assert {"mql", "ql"} <= names, owner
    models = api_models()
    assert schemas.HubSpotMappingView in models
    assert schemas.CrmBindingsView in models
    for model in models:
        names = set(field_names(model))
        if "mql" in names:
            assert "ql" in names, model


def test_serialised_evidence_keeps_mql_and_ql_separate_by_construction(
    make_context: MakeContext,
) -> None:
    """Never blended: MQL and QL are separate metrics, and neither moves any other number.

    Clearing every contact's MQL date changes the MQL metric only: QL, costs, the rank key, the
    action, the reason and the cohort median are unchanged, so no metric, total or sort key is
    built from both. Clearing QL leaves the MQL metric unchanged.
    """
    base = run_scenario(make_context, "wp.causal_pre_touch.does_not_fire")
    row = base.row()
    item = base.item(row)
    rendered = item.evidence.to_json()
    metrics = {metric["key"]: metric for metric in rendered["metrics"]}
    assert tuple(metrics) == EVIDENCE_COLUMNS
    assert (metrics["mql"]["value"], metrics["ql"]["value"]) == (10, 10)
    assert metrics["mql"]["label"] == "MQL (First meeting booked)"
    assert metrics["ql"]["label"] == "Qualified Lead"
    for credit in rendered["extra"]["rows"]:
        assert credit["mql"]["value"] == credit["ql"]["value"] == 10

    contacts = row_contacts(base, row)
    no_mql = rerun(base, with_contacts(base.ctx, contacts, without_mql))
    changed_row = no_mql.row()
    changed = no_mql.item(changed_row)
    changed_metrics = {m["key"]: m for m in changed.evidence.to_json()["metrics"]}
    assert changed_metrics["mql"]["value"] == 0
    assert {key: m for key, m in changed_metrics.items() if key != "mql"} == {
        key: m for key, m in metrics.items() if key != "mql"
    }
    assert (changed.rank_key, changed.action, changed.reason) == (
        item.rank_key,
        item.action,
        item.reason,
    )
    assert (
        cohort_of(no_mql, changed_row).median_cost_per_ql == cohort_of(base, row).median_cost_per_ql
    )
    assert cohort_of(no_mql, changed_row).total_ql == cohort_of(base, row).total_ql

    no_ql = rerun(base, with_contacts(base.ctx, contacts, without_ql))
    assert no_ql.item(no_ql.row()).evidence.metric("mql").value == 10
    assert no_ql.item(no_ql.row()).evidence.metric("ql").value == 0


@pytest.mark.parametrize("scenario", BUILDABLE_SCENARIOS, ids=lambda scenario: scenario.name)
def test_reason_and_action_never_present_a_combined_count(
    make_context: MakeContext, scenario: Scenario
) -> None:
    """Render assertion: no white-paper headline or reason presents MQL and QL as one count."""
    run = evaluate(scenario, make_context(scenario))
    for row in run.table.rows():
        item = run.item(row)
        assert not presents_combined_count(item.action, row), item.action
        assert not presents_combined_count(item.reason, row), item.reason


def test_mql_resolves_from_its_stage_label_or_renders_unavailable(
    make_context: MakeContext,
) -> None:
    """Stage binding: MQL is bound to "First meeting booked"; unresolved, it is unavailable.

    An unresolved label is never rendered as 0: the count, its per-creative rows and its
    evidence metric are unavailable, a ``pipeline_unavailable`` finding names the missing
    stage, and the Qualified Lead count, bound on its own, stays known.
    """
    resolved = run_scenario(make_context, "wp.mql_label.fires")
    configured = resolved.ctx.thresholds.mapping.mql
    label = "First meeting booked"
    assert configured.stage_labels == (label,)
    binding = resolved.crm.bindings.mql
    assert binding.available
    assert binding.label == label
    assert binding.property is not None
    assert binding.property.startswith("hs_v2_date_entered_")
    row = resolved.row()
    assert resolved.table.mql_label == row.mql_label == label
    assert known(row.mql) == 10
    assert resolved.item(row).evidence.metric("mql").label == f"MQL ({label})"
    # One input across: a bound label that only repeats the stage name is not parenthesised.
    repeated = dataclasses.replace(
        resolved.crm.bindings, mql=dataclasses.replace(binding, label="MQL")
    )
    bare = rerun(resolved, with_crm(resolved.ctx, bindings=repeated))
    assert bare.item(bare.row()).evidence.metric("mql").label == "MQL"

    missing = run_scenario(make_context, "wp.mql_label.does_not_fire")
    assert not missing.crm.bindings.mql.available
    unresolved = missing.row()
    assert unresolved.mql.unavailable
    assert unresolved.mql.value is None
    assert unresolved.mql.render() == "unavailable"
    assert all(credit.mql.unavailable for credit in unresolved.creative_rows)
    metric = missing.item(unresolved).evidence.metric("mql")
    assert metric.value is None
    assert metric.to_json()["unavailable"] is True
    assert known(unresolved.ql) == 0
    # An unavailable stage property is unknown evidence for every contact; it is portal-wide,
    # so the count is "unavailable" instead of turning the other counts into floors.
    hs_id = row_contacts(missing, unresolved)[0]
    known_now, reason = contact_evidence_known(missing.crm, hs_id, matched_lead(missing, hs_id))
    assert not known_now
    assert reason is not None
    assert reason.startswith("MQL stage unavailable")
    assert unresolved.unknown_evidence_contacts == 0
    assert not unresolved.ql.floor
    assert not missing.table.mql_available
    assert missing.table.ql_available
    findings = [f for f in missing.result.findings if f.code == FINDING_PIPELINE_UNAVAILABLE]
    assert len(findings) == 1
    assert label in findings[0].message
    assert findings[0].evidence["stage"] == "MQL"


def test_a_skipped_stage_is_reported_and_not_counted(make_context: MakeContext) -> None:
    """Skipped stages: an empty bound date is not counted, even with a later current stage."""
    run = run_scenario(make_context, "wp.skipped_stage.case")
    row = run.row()
    for hs_id in row_contacts(run, row):
        contact = run.crm.contacts[hs_id]
        assert contact.mql_at is None
        assert contact.lifecycle_stage not in attribution.ENTRY_LIFECYCLE_STAGES
        assert contact.ql_at is not None
    assert known(row.mql) == 0
    assert row.skipped_mql == 10
    assert known(row.ql) == 10
    assert dict(run.item(row).evidence.extra["skipped"]) == {"mql": 10, "ql": 0}
    findings = [f for f in run.result.findings if f.code == FINDING_SKIPPED_STAGE]
    assert len(findings) == 1
    assert findings[0].message == (
        "MQL (First meeting booked): 10 contacts skipped this stage (not counted)"
    )
    assert findings[0].evidence["stage"] == "MQL"
    assert findings[0].entity == row.entity


def test_ql_is_observed_from_its_own_property(make_context: MakeContext) -> None:
    """QL independence: a QL date without an MQL date counts toward QL only, and vice versa."""
    ql_only = run_scenario(make_context, "wp.ql_independent.case")
    row = ql_only.row()
    for hs_id in row_contacts(ql_only, row):
        contact = ql_only.crm.contacts[hs_id]
        assert contact.mql_at is None
        assert contact.ql_at is not None
    assert (known(row.mql), known(row.ql)) == (0, 10)

    mql_only = run_scenario(make_context, "wp.mql_label.fires")
    mirror = mql_only.row()
    for hs_id in row_contacts(mql_only, mirror):
        contact = mql_only.crm.contacts[hs_id]
        assert contact.mql_at is not None
        assert contact.ql_at is None
    assert (known(mirror.mql), known(mirror.ql)) == (10, 0)


# ---------------------------------------------------------------------------------------------
# Phase 4: costs and floors
# ---------------------------------------------------------------------------------------------


def test_costs_divide_spend_by_ql_and_by_booked_meetings(make_context: MakeContext) -> None:
    """Cost per QL = spend ÷ QL and cost per meeting = spend ÷ meetings booked, exactly.

    With QL 0 the cost per QL is "—" with "no QL yet"; with no booked meeting the cost per
    meeting is "—" with "no meetings yet": never a finite value and never a "≤ X" ceiling.
    """
    run = run_scenario(make_context, "wp.costs.fires")
    row = run.row()
    assert row.spend == Decimal("1000.00")
    assert row.cost_per_ql.value == row.spend / known(row.ql) == Decimal(100)
    assert row.cost_per_meeting.value == row.spend / known(row.meetings_booked)
    assert row.cost_per_meeting.value == Decimal(1000) / Decimal(13)
    assert not row.cost_per_ql.ceiling
    assert not row.cost_per_meeting.ceiling
    assert (row.cost_per_ql.render(), row.cost_per_meeting.render()) == ("100.00", "76.92")
    evidence = run.item(row).evidence
    assert evidence.metric("cost_per_ql").value == Decimal(100)
    assert evidence.metric("cost_per_ql").currency == row.currency == "USD"
    # One value per cited number (AAP 0.5.4.8): the metric, extra.costs and the creative row
    # cite 1000/13 at the stored six decimals, never as a 28-digit quotient.
    rendered = evidence.to_json()
    metrics = {metric["key"]: metric["value"] for metric in rendered["metrics"]}
    (credit,) = rendered["extra"]["rows"]
    for key in ("cost_per_ql", "cost_per_meeting"):
        assert metrics[key] == rendered["extra"]["costs"][key]["value"] == credit[key]["value"]
    assert (metrics["cost_per_ql"], metrics["cost_per_meeting"]) == ("100.00", "76.923077")

    no_ql = run_scenario(make_context, "wp.costs.does_not_fire").row()
    assert known(no_ql.ql) == 0
    assert no_ql.cost_per_ql == CostValue(None, note=NO_QL_NOTE)
    assert no_ql.cost_per_ql.render() == "—"
    assert no_ql.cost_per_meeting.render() == "76.92"
    no_ql_run = run_scenario(make_context, "wp.costs.does_not_fire")
    assert no_ql_run.item(no_ql).evidence.metric("cost_per_ql").value == "—"
    assert no_ql_run.item(no_ql).evidence.extra["costs"]["cost_per_ql"]["note"] == NO_QL_NOTE

    no_meetings = run_scenario(make_context, "wp.causal_pre_touch.fires").row()
    assert known(no_meetings.meetings_booked) == 0
    assert no_meetings.cost_per_meeting == CostValue(None, note=NO_MEETINGS_NOTE)
    assert no_meetings.cost_per_meeting.render() == "—"


def test_the_contact_floor_holds_at_ten_percent_and_not_at_nine_point_nine(
    make_context: MakeContext,
) -> None:
    """Contact floor: 100 of 1,000 leads unmatched renders "≥ N" and "≤ X"; 99 of 1,000 does not."""
    floored = run_scenario(make_context, "wp.contact_floor.fires")
    floor_share = floored.ctx.thresholds.ratio("attribution.floor_share")
    assert floor_share == AAP_FLOOR_SHARE
    row = floored.row()
    unmatched = [lead for lead in row_leads(floored, row) if not lead.matched]
    assert (row.linkedin_leads, len(unmatched), row.unmatched_leads) == (1000, 100, 100)
    assert row.contact_share == Decimal(100) / Decimal(1000) == floor_share
    assert row.has_flag(FLAG_ATTRIBUTION_FLOOR)
    assert row.flag_messages[FLAG_ATTRIBUTION_FLOOR] == "Floor: 10% of leads unmatched in HubSpot"
    assert row.mql.render() == "≥ 900"
    assert row.ql.render() == "≥ 900"
    assert row.meetings_booked.render() == "≥ 1099"
    assert row.deals_after_touch.render() == "≥ 900"
    assert row.cost_per_ql.ceiling
    assert row.cost_per_ql.render() == "≤ 5.56"
    assert row.cost_per_meeting.ceiling
    assert row.cost_per_meeting.render() == "≤ 4.55"
    evidence = floored.item(row).evidence
    assert evidence.metric("ql").floor
    assert evidence.metric("cost_per_ql").floor
    assert FLAG_ATTRIBUTION_FLOOR in floored.item(row).flag_codes
    # The QL sample is the floored count too, so it renders "≥ 900 / 3", never "900 / 3".
    assert evidence.sample is not None
    assert (evidence.sample.observed, evidence.sample.floor) == (900, True)
    assert evidence.to_json()["sample"]["floor"] is True

    clear_run = run_scenario(make_context, "wp.contact_floor.does_not_fire")
    clear_sample = clear_run.item(clear_run.row()).evidence.sample
    assert clear_sample is not None
    assert (clear_sample.observed, clear_sample.floor) == (901, False)
    assert "floor" not in clear_sample.to_json()

    clear = run_scenario(make_context, "wp.contact_floor.does_not_fire").row()
    assert clear.unmatched_leads == 99
    assert clear.contact_share == Decimal("0.099") < floor_share
    assert not clear.has_flag(FLAG_ATTRIBUTION_FLOOR)
    assert (known(clear.mql), known(clear.ql), known(clear.meetings_booked)) == (901, 901, 1100)
    assert known(clear.deals_after_touch) == 901
    assert not clear.cost_per_ql.ceiling
    assert clear.cost_per_ql.render() == "5.55"


def test_a_floored_zero_denominator_cost_is_a_dash_never_a_ceiling(
    make_context: MakeContext,
) -> None:
    """Under the contact floor, QL "≥ 0" and meetings "≥ 0" give "—" costs, never "≤ X"."""
    run = run_scenario(make_context, "wp.contact_floor_no_outcomes.case")
    row = run.row()
    assert row.has_flag(FLAG_ATTRIBUTION_FLOOR)
    assert (row.ql.render(), row.meetings_booked.render()) == ("≥ 0", "≥ 0")
    assert row.cost_per_ql == CostValue(None, note=NO_QL_NOTE)
    assert row.cost_per_meeting == CostValue(None, note=NO_MEETINGS_NOTE)
    assert (row.cost_per_ql.render(), row.cost_per_meeting.render()) == ("—", "—")
    # The cohort's QL total sums a floored count, so its shortfall keeps the floor as well.
    cohort_sample = dict(run.item(row).evidence.extra["cohort_sample"])
    assert (cohort_sample["observed"], cohort_sample["floor"]) == (0, True)


def test_the_creative_floor_labels_only_the_per_creative_breakdown(
    make_context: MakeContext,
) -> None:
    """Creative floor: leads without a creative at >= 10% floor the breakdown, not the totals.

    The twin gives those leads their creative (one input): the breakdown is no longer a floor
    and the asset totals are identical.
    """
    run = run_scenario(make_context, "wp.creative_floor.case")
    row = run.row()
    no_creative = [lead for lead in row_leads(run, row) if lead.creative_id is None]
    assert row.leads_without_creative == len(no_creative) == 6
    assert row.creative_share == Decimal(6) / Decimal(40)
    assert row.creative_share >= run.ctx.thresholds.ratio("attribution.floor_share")
    assert row.flags == (FLAG_CREATIVE_FLOOR,)
    known_rows = [credit for credit in row.creative_rows if not credit.is_unknown]
    unknown_rows = [credit for credit in row.creative_rows if credit.is_unknown]
    assert all(credit.ql.floor and credit.mql.floor for credit in known_rows)
    assert len(unknown_rows) == 1
    unknown = unknown_rows[0]
    assert unknown.label == UNKNOWN_CREATIVE_LABEL
    assert (unknown.spend, unknown.leads) == (Decimal(0), 6)
    assert row.creative_rows[-1] is unknown
    for count in (row.mql, row.ql, row.meetings_booked, row.deals_after_touch):
        assert not count.floor
    assert partitions(row)

    creative = run.ctx.creatives[next(iter(run.ctx.creatives))]
    attributed = rerun(
        run,
        with_leads(
            run.ctx,
            lambda lead: (
                dataclasses.replace(lead, creative_id=creative.id, creative_urn=creative.urn)
                if lead.creative_id is None
                else lead
            ),
        ),
    ).row()
    assert attributed.creative_share == 0
    assert not attributed.has_flag(FLAG_CREATIVE_FLOOR)
    assert all(not credit.is_unknown and not credit.ql.floor for credit in attributed.creative_rows)
    assert (attributed.spend, attributed.linkedin_leads) == (row.spend, row.linkedin_leads)
    assert (attributed.mql, attributed.ql) == (row.mql, row.ql)


# ---------------------------------------------------------------------------------------------
# Phase 5: incomplete associations and target reads
# ---------------------------------------------------------------------------------------------


def test_an_error_association_fetch_makes_the_evidence_unknown(make_context: MakeContext) -> None:
    """An ``error`` contact-to-deal fetch: unknown evidence, "≥ N" counts and W2 withheld.

    Its twin reads the same associations as ``ok_empty``: a genuine zero, so the zero-QL white
    paper retires (W2) and the pipeline can be judged.
    """
    failed = run_scenario(make_context, "wp.assoc_error.fires")
    for row in failed.table.rows():
        fetches = contact_fetches(failed, row, ("deal",))
        assert {fetch.status for fetch in fetches} == {"error"}
        assert row.unknown_evidence_share == 1
        assert row.unknown_evidence_contacts == row.matched_contacts == 5
        assert row.has_flag(FLAG_UNKNOWN_CRM_EVIDENCE)
        assert row.deals_after_touch.floor
        assert row.deals_after_touch.render() == "≥ 0"
        assert row.ql.floor
        hs_id = row_contacts(failed, row)[0]
        assert contact_evidence_known(failed.crm, hs_id, matched_lead(failed, hs_id)) == (
            False,
            "contact-to-deal association fetch error",
        )
    assert not failed.items(W2)
    assert failed.item(failed.row("guide-d")).action == ACTION_NO_CHANGE
    assert "unknown CRM evidence on 5 contacts" in failed.withheld(W2)
    veto = next(iter(failed.table.veto.values()))
    assert not veto.available
    assert not veto.veto_fires
    assert veto.unknown_evidence_contacts == veto.eligible_contacts == 20
    # This campaign's QL is 15, so C1b has no zero to rest on and emits nothing either way; its
    # withheld line under unknown evidence is pinned by the target-read tests below.
    assert not [item for item in campaigns.evaluate(failed.ctx).candidates if item.rule_id == C1B]

    genuine = run_scenario(make_context, "wp.assoc_error.does_not_fire")
    retired = genuine.row("guide-d")
    assert {fetch.status for fetch in contact_fetches(genuine, retired, ("deal",))} == {"ok_empty"}
    assert known(retired.deals_after_touch) == 0
    assert retired.unknown_evidence_share == 0
    assert not any(row.has_flag(FLAG_UNKNOWN_CRM_EVIDENCE) for row in genuine.table.rows())
    assert [item.action for item in genuine.items(W2)] == [ACTION_RETIRE]
    assert next(iter(genuine.table.veto.values())).available


def unknown_fetch(run: _Run, row: AssetRow) -> AssociationFetchInfo:
    """The one contact fetch of ``row`` that is not known evidence."""
    unknown = [fetch for fetch in contact_fetches(run, row) if not fetch.known]
    assert len(unknown) == 1, unknown
    return unknown[0]


def c1b_withheld(ctx: AnalysisContext) -> tuple[str, ...]:
    """The reasons of the withheld C1b lines ``campaigns.evaluate`` reports for ``ctx``."""
    result = campaigns.evaluate(ctx)
    assert not [item for item in result.candidates if item.rule_id == C1B]
    return tuple(line.reason for line in result.withheld if line.rule_id == C1B)


def test_a_partial_deal_target_read_makes_the_contacts_evidence_unknown(
    make_context: MakeContext,
) -> None:
    """Target reads: an ``ok`` contact-to-deal fetch whose deal read omitted one deal is unknown.

    The contact's evidence is unknown (``contact_evidence_known`` is False), the deal it could
    not read is never counted as absent, C1b is withheld and the pipeline is unavailable.
    """
    run = run_scenario(make_context, "wp.target_read_partial.case")
    row = run.row()
    fetch = unknown_fetch(run, row)
    assert (fetch.to_type, fetch.status, fetch.target_read_status) == ("deal", "ok", "partial")
    hs_id = fetch.object_id
    assert len(fetch.missing_target_ids) == 1
    assert fetch.missing_target_ids[0] in run.crm.targets("contact", hs_id, "deal")
    assert contact_evidence_known(run.crm, hs_id, matched_lead(run, hs_id)) == (
        False,
        "deal target reads partial",
    )
    for other in row_contacts(run, row):
        if other != hs_id:
            assert contact_evidence_known(run.crm, other, matched_lead(run, other)) == (True, None)
    assert row.unknown_evidence_contacts == 1
    assert row.has_flag(FLAG_UNKNOWN_CRM_EVIDENCE)
    # Only the nine fully read contacts' deals are proven; the tenth's are a floor, not absent.
    assert row.deals_after_touch.floor
    assert row.deals_after_touch.value == 9
    assert row.deals_after_touch.render() == "≥ 9"
    assert c1b_withheld(run.ctx) == ("unknown CRM evidence for 1 of 10 eligible contacts",)
    veto = next(iter(run.table.veto.values()))
    assert not veto.available
    assert veto.unknown_evidence_contacts == 1


def test_a_failed_meeting_target_read_makes_the_evidence_unknown(make_context: MakeContext) -> None:
    """Target reads: a failed meeting read leaves meetings unknown, never a genuine zero."""
    run = run_scenario(make_context, "wp.meeting_read_failed.case")
    row = run.row()
    fetches = contact_fetches(run, row, ("meeting",))
    assert {(f.status, f.target_read_status) for f in fetches} == {("ok", "error")}
    assert all(f.missing_target_ids for f in fetches)
    for hs_id in row_contacts(run, row):
        assert contact_evidence_known(run.crm, hs_id, matched_lead(run, hs_id)) == (
            False,
            "meeting target reads error",
        )
    assert row.unknown_evidence_share == 1
    assert row.meetings_booked.floor
    assert row.meetings_booked.render() == "≥ 0"
    assert c1b_withheld(run.ctx) == ("unknown CRM evidence for 10 of 10 eligible contacts",)


UNREAD_TARGETS = pytest.mark.parametrize(
    ("to_type", "target_read_status"),
    [("deal", "partial"), ("meeting", "error")],
    ids=["deal-read-partial", "meeting-read-failed"],
)


@UNREAD_TARGETS
def test_an_unread_target_withholds_w2(
    make_context: MakeContext, to_type: str, target_read_status: TargetReadStatus
) -> None:
    """W2 needs an unknown CRM evidence share of 0: one contact's unread target withholds it."""
    base = run_scenario(make_context, "w2.cohort.fires")
    retired = base.row("guide-d")
    assert [item.entity_ref.external_id for item in base.items(W2)] == [retired.external_id]
    hs_id = row_contacts(base, retired)[0]
    derived = rerun(base, with_unread_target(base.ctx, hs_id, to_type, target_read_status))
    row = derived.row("guide-d")
    known_now, reason = contact_evidence_known(derived.crm, hs_id, matched_lead(derived, hs_id))
    assert not known_now
    assert reason == f"{to_type} target reads {target_read_status}"
    assert row.unknown_evidence_contacts == 1
    assert not derived.items(W2)
    assert derived.item(row).action == ACTION_NO_CHANGE
    assert derived.withheld(W2) == ("unknown CRM evidence on 1 contact",)


@UNREAD_TARGETS
def test_an_unread_target_makes_the_c2_veto_unavailable(
    make_context: MakeContext, to_type: str, target_read_status: TargetReadStatus
) -> None:
    """The C2 pipeline veto needs every eligible contact's evidence known.

    The catalogue's Hold world (21 eligible contacts, QL 0) holds; with one eligible contact's
    target read failing, the veto is unavailable, so the outcome is "Scale" flagged
    ``pipeline_unavailable`` (with its B1), not "Hold", and C1b is withheld.
    """
    base_scenario = SCENARIOS["c2.outcome_hold.case"]
    base_ctx = make_context(base_scenario)
    [hold] = campaigns.scale_outcomes(base_ctx)
    assert isinstance(hold, campaigns.ScaleOutcome)
    assert hold.kind == "hold"
    assert hold.veto is not None
    assert hold.veto.veto_fires
    hs_id = sorted({lead.contact_hs_id for lead in base_ctx.leads if lead.contact_hs_id})[0]

    ctx = with_unread_target(base_ctx, hs_id, to_type, target_read_status)
    [entry] = campaigns.scale_outcomes(ctx)
    assert isinstance(entry, campaigns.ScaleOutcome)
    assert entry.kind == "scale"
    assert FLAG_PIPELINE_UNAVAILABLE in {flag.code for flag in entry.flags}
    assert entry.veto is not None
    assert not entry.veto.available
    assert not entry.veto.veto_fires
    assert entry.veto.unknown_evidence_contacts == 1
    c2_items = [item for item in campaigns.evaluate(ctx).candidates if item.rule_id == C2]
    assert [item.action for item in c2_items] == [campaigns.SCALE_ACTION]
    assert c2_items[0].has_flag(FLAG_PIPELINE_UNAVAILABLE)
    assert c1b_withheld(ctx) == (
        f"unknown CRM evidence for 1 of {entry.veto.eligible_contacts} eligible contacts",
    )


def test_a_failed_contact_refresh_makes_stage_counts_unknown(make_context: MakeContext) -> None:
    """A failed contact refresh keeps the stored stage dates but marks them unknown, never zero.

    Its twin is the same world refreshed successfully: QL is an exact 10 there, a floor here.
    """
    run = run_scenario(make_context, "wp.contact_refresh_failed.case")
    row = run.row()
    failed = [lead for lead in row_leads(run, row) if lead.contact_read_status == "error"]
    assert len(failed) == 1
    lead = failed[0]
    assert lead.contact_hs_id is not None
    known_now, reason = contact_evidence_known(run.crm, lead.contact_hs_id, lead)
    assert not known_now
    assert reason is not None
    assert reason.startswith("contact refresh failed")
    assert row.ql.floor
    assert row.ql.value == 10
    assert row.ql.render() == "≥ 10"
    assert row.cost_per_ql.ceiling
    assert row.has_flag(FLAG_UNKNOWN_CRM_EVIDENCE)
    # Its current stage is not known, so it is not reported as having skipped MQL.
    assert row.skipped_mql == row.matched_contacts - 1
    veto = next(iter(run.table.veto.values()))
    assert not veto.available

    refreshed = run_scenario(make_context, "wp.ql_independent.case").row()
    assert known(refreshed.ql) == 10
    assert not refreshed.has_flag(FLAG_UNKNOWN_CRM_EVIDENCE)


def test_an_orphan_fetch_with_an_unread_target_is_not_assessed(make_context: MakeContext) -> None:
    """A deal-to-contact fetch whose target read failed is excluded from the known scope."""
    run = run_scenario(make_context, "wp.orphan_unread_target.case")
    window_deals = run.crm.window_deal_ids
    unknown = [
        deal_id
        for deal_id in window_deals
        if not run.crm.association_known("deal", deal_id, "contact")
    ]
    assert len(window_deals) == 10
    assert len(unknown) == 1
    fetch = run.crm.fetch("deal", unknown[0], "contact")
    assert fetch is not None
    assert (fetch.status, fetch.target_read_status) == ("ok", "error")
    share = run.table.orphan_deals
    assert (share.assessed, share.orphans, share.not_assessed) == (9, 0, 1)
    assert share.unknown
    assert share.label == "unknown (1 deal not assessed)"
    assert share.reason == "1 deal-to-contact fetch unknown"
    row = run.row()
    assert row.has_flag(FLAG_DEAL_FLOOR)
    assert row.deals_after_touch.render() == "≥ 10"


def refresh_not_recorded(ctx: AnalysisContext, hs_id: str) -> AnalysisContext:
    """The contact's leads record no refresh outcome at all."""
    return with_leads(
        ctx,
        lambda lead: (
            dataclasses.replace(lead, contact_read_status=None)
            if lead.contact_hs_id == hs_id
            else lead
        ),
    )


def contact_record_not_read(ctx: AnalysisContext, hs_id: str) -> AnalysisContext:
    """The matched contact's record is missing from the CRM dataset."""
    crm = ctx.linkedin_crm
    assert crm is not None
    return with_crm(ctx, contacts={k: v for k, v in crm.contacts.items() if k != hs_id})


def meetings_not_fetched(ctx: AnalysisContext, hs_id: str) -> AnalysisContext:
    """The contact-to-meeting association fetch never ran for the contact."""
    crm = ctx.linkedin_crm
    assert crm is not None
    key = ("contact", hs_id, "meeting")
    return with_crm(ctx, fetches={k: v for k, v in crm.fetches.items() if k != key})


def meeting_record_not_read(ctx: AnalysisContext, hs_id: str) -> AnalysisContext:
    """The fetch and its target reads report ``ok``, yet the associated meeting is not stored."""
    crm = ctx.linkedin_crm
    assert crm is not None
    key = ("contact", hs_id, "meeting")
    fetch = AssociationFetchInfo("contact", hs_id, "meeting", "ok", "ok")
    return with_crm(
        ctx,
        associations={**crm.associations, key: (UNREAD_TARGET_ID,)},
        fetches={**crm.fetches, key: fetch},
    )


@pytest.mark.parametrize(
    ("unknown_by", "reason"),
    [
        (refresh_not_recorded, "contact refresh not recorded; stage dates not current"),
        (contact_record_not_read, "contact record not read"),
        (meetings_not_fetched, "contact-to-meeting associations not fetched"),
        (meeting_record_not_read, "1 associated meeting record(s) not read"),
    ],
    ids=["refresh-not-recorded", "contact-not-read", "not-fetched", "record-not-read"],
)
def test_every_unknown_evidence_cause_is_a_floor_and_withholds_w2(
    make_context: MakeContext,
    unknown_by: Callable[[AnalysisContext, str], AnalysisContext],
    reason: str,
) -> None:
    """Unknown CRM evidence covers every unread step, and none of them is ever read as zero.

    A refresh never recorded, a contact record never read, a fetch that never ran and an
    associated record missing despite an ``ok`` read each make one contact's evidence unknown:
    the row's counts become floors, W2 is withheld and the pipeline is unavailable.
    """
    base = run_scenario(make_context, "w2.cohort.fires")
    hs_id = row_contacts(base, base.row("guide-d"))[0]
    lead = matched_lead(base, hs_id)
    run = rerun(base, unknown_by(base.ctx, hs_id))
    lead = next(item for item in run.ctx.leads if item.id == lead.id)
    assert contact_evidence_known(run.crm, hs_id, lead) == (False, reason)
    row = run.row("guide-d")
    assert row.unknown_evidence_contacts == 1
    assert row.has_flag(FLAG_UNKNOWN_CRM_EVIDENCE)
    assert row.ql.floor
    assert not run.items(W2)
    assert run.withheld(W2) == ("unknown CRM evidence on 1 contact",)
    assert not next(iter(run.table.veto.values())).available


def test_without_a_crm_dataset_every_crm_count_is_unavailable(make_context: MakeContext) -> None:
    """With no effective CRM dataset, MQL, QL, meetings and deals are unavailable, never 0."""
    base = run_scenario(make_context, "wp.creative_floor.case")
    run = rerun(base, derive(base.ctx, linkedin_crm=None))
    row = run.row()
    for count in (
        row.mql,
        row.ql,
        row.meetings_booked,
        row.other_meetings,
        row.deals_after_touch,
    ):
        assert count.unavailable
        assert count.render() == "unavailable"
    assert row.cost_per_ql.render() == row.cost_per_meeting.render() == "unavailable"
    unknown = next(credit for credit in row.creative_rows if credit.is_unknown)
    assert unknown.cost_per_ql.unavailable
    assert (row.spend, row.linkedin_leads) == (base.row().spend, base.row().linkedin_leads)
    assert run.table.orphan_deals.unknown
    findings = [f for f in run.result.findings if f.code == FINDING_PIPELINE_UNAVAILABLE]
    assert [f.evidence["stage"] for f in findings] == ["MQL", "Qualified Lead"]
    assert run.item(row).action == ACTION_INSUFFICIENT_DATA
    veto = next(iter(run.table.veto.values()))
    assert not veto.available
    assert veto.ql is None
    assert veto.meetings is None


def test_events_after_the_run_are_not_yet_counted(make_context: MakeContext) -> None:
    """Downstream events count as of run time: a QL, meeting or deal after the run is not one yet.

    Moving every QL entry, meeting and post-touch deal one hour past the run leaves MQL as it
    was and the moved events uncounted, neither credited nor reported as before the touch.
    """
    base = run_scenario(make_context, "wp.causal_pre_touch.does_not_fire")
    row = base.row()
    later = base.ctx.now + timedelta(hours=1)
    touches = anchors(base, row)
    crm = base.crm
    contacts = {
        hs_id: dataclasses.replace(contact, ql_at=later) if hs_id in touches else contact
        for hs_id, contact in crm.contacts.items()
    }
    meetings = {
        mid: dataclasses.replace(meeting, created_at=later, start_at=later + timedelta(days=1))
        for mid, meeting in crm.meetings.items()
    }
    after_touch = {
        deal_id
        for deal_id, members in associated(base, touches, "deal").items()
        if any((crm.deals[deal_id].created_at or later) >= touches[hs] for hs in members)
    }
    deals = {
        deal_id: dataclasses.replace(deal, created_at=later) if deal_id in after_touch else deal
        for deal_id, deal in crm.deals.items()
    }
    run = rerun(base, with_crm(base.ctx, contacts=contacts, meetings=meetings, deals=deals))
    moved = run.row()
    assert known(moved.mql) == known(row.mql) == 10
    assert (known(moved.ql), known(moved.meetings_booked), known(moved.deals_after_touch)) == (
        0,
        0,
        0,
    )
    assert (moved.already_ql_before, moved.meetings_before_touch) == (0, 0)
    assert moved.deals_before_touch == row.deals_before_touch == 10
    veto = next(iter(run.table.veto.values()))
    assert (veto.ql, veto.meetings) == (0, 0)


# ---------------------------------------------------------------------------------------------
# Phase 6: orphan deals
# ---------------------------------------------------------------------------------------------


def test_the_orphan_deal_share_is_measured_over_the_assessed_deals(
    make_context: MakeContext,
) -> None:
    """Orphan share = ``ok_empty`` deal-to-contact fetches ÷ known fetches; >= 10% floors deals."""
    run = run_scenario(make_context, "wp.orphan_share.case")
    fetches = [run.crm.fetch("deal", deal_id, "contact") for deal_id in run.crm.window_deal_ids]
    assessed = [fetch for fetch in fetches if fetch is not None and fetch.known]
    orphans = [fetch for fetch in assessed if fetch.is_empty]
    assert (len(assessed), len(orphans)) == (13, 3)
    share = run.table.orphan_deals
    assert share.share == Decimal(3) / Decimal(13)
    assert not share.unknown
    assert share.label == "23.1% (3 of 13 deals without a contact)"
    assert share.is_floor(run.ctx.thresholds.ratio("attribution.floor_share"))
    row = run.row()
    assert row.has_flag(FLAG_DEAL_FLOOR)
    assert row.flag_messages[FLAG_DEAL_FLOOR] == (
        "Deal counts are a floor: orphan-deal share 23.1% (3 of 13 deals without a contact)"
    )
    assert row.deals_after_touch.render() == "≥ 10"
    assert dict(run.item(row).evidence.extra["orphan_deals"])["orphans"] == 3
    # The evidence cites 3/13 at the stored six decimals; the floor compared the exact share.
    assert dict(run.item(row).evidence.extra["orphan_deals"])["share"] == "0.230769"

    below = run_scenario(make_context, "wp.target_read_partial.case")
    assert below.table.orphan_deals.share == 0
    assert not below.row().has_flag(FLAG_DEAL_FLOOR)


def test_an_incomplete_deal_search_makes_the_orphan_share_unknown(
    make_context: MakeContext,
) -> None:
    """An ``incomplete`` deal-search partition: the share is unknown and deals carry a floor.

    Deals past the search ceiling were never returned, so the label states the incomplete
    search rather than a count of 0 deals not assessed. The twin marks the same search
    complete (one input): the share is known and deals are exact.
    """
    run = run_scenario(make_context, "wp.deal_search_incomplete.case")
    dataset_id = run.crm.dataset.dataset_id
    freshness = run.ctx.freshness_for("hubspot.deal_search", dataset_id)
    assert freshness is not None
    assert freshness.completeness == "incomplete"
    share = run.table.orphan_deals
    assert share.unknown
    assert share.share is not None
    assert re.fullmatch(
        # N >= 1 agrees with its noun: "1 deal", "2 deals", "10 deals".
        r"unknown \(deal search incomplete(; at least (1 deal|([2-9]|1\d)\d* deals) not "
        r"assessed)?\)",
        share.label,
    )
    assert "0 deals not assessed" not in share.label
    row = run.row()
    assert row.has_flag(FLAG_DEAL_FLOOR)
    assert row.flag_messages[FLAG_DEAL_FLOOR] == (
        f"Deal counts are a floor: orphan-deal share {share.label}"
    )
    assert row.deals_after_touch.render() == "≥ 10"

    complete = dataclasses.replace(freshness, completeness="complete", reason=None)
    searched = derive(
        run.ctx,
        linkedin_crm=dataclasses.replace(run.crm, deal_search_complete=True),
        freshness={**run.ctx.freshness, (dataset_id, "hubspot.deal_search"): complete},
    )
    resolved = rerun(run, searched)
    assert not resolved.table.orphan_deals.unknown
    assert resolved.table.orphan_deals.share == 0
    assert not resolved.row().has_flag(FLAG_DEAL_FLOOR)
    assert known(resolved.row().deals_after_touch) == 10

    # The freshness row alone is enough: a search that reports covering its range but whose
    # dataset_freshness row is incomplete still leaves the share unknown.
    flagged_only = derive(
        resolved.ctx,
        freshness={**resolved.ctx.freshness, (dataset_id, "hubspot.deal_search"): freshness},
    )
    share = rerun(run, flagged_only).table.orphan_deals
    assert share.unknown
    assert share.reason == f"deal search incomplete: {freshness.reason}"
    assert share.label == "unknown (deal search incomplete)"


# ---------------------------------------------------------------------------------------------
# Phase 7: credit, de-duplication and asset resolution
# ---------------------------------------------------------------------------------------------


def credit_for(run: _Run, row: AssetRow, key: str) -> CreditRow:
    """The per-creative row of the catalogue creative ``key``."""
    rows = [credit for credit in row.creative_rows if run.creative_key(credit.creative_id) == key]
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.parametrize(
    ("name", "credited", "other"),
    [
        ("wp.credit_latest_touch.case", "cr-a2", "cr-a1"),
        ("wp.credit_between_touches.case", "cr-a1", "cr-a2"),
    ],
    ids=["events-after-both-touches", "events-between-the-touches"],
)
def test_events_are_credited_to_the_latest_touch_at_or_before_them(
    make_context: MakeContext, name: str, credited: str, other: str
) -> None:
    """Credit: each MQL, QL, meeting and deal goes once to the latest touch at or before it.

    One contact touches the asset through cr-a1 on day -28 and cr-a2 on day -25. Events five
    days after the first touch follow both touches and go to cr-a2; events one day after it
    fall between them and go to cr-a1, not to the contact's last touch. The per-creative rows
    sum exactly to the asset total.
    """
    run = run_scenario(make_context, name)
    row = run.row()
    [hs_id] = row_contacts(run, row)
    leads = sorted(row_leads(run, row), key=lambda lead: lead.submitted_at)
    assert [run.creative_key(lead.creative_id) for lead in leads] == ["cr-a1", "cr-a2"]
    contact = run.crm.contacts[hs_id]
    assert contact.mql_at is not None
    latest = max(
        (lead for lead in leads if lead.submitted_at <= contact.mql_at),
        key=lambda lead: lead.submitted_at,
    )
    assert run.creative_key(latest.creative_id) == credited

    winner, loser = credit_for(run, row, credited), credit_for(run, row, other)
    assert (known(winner.mql), known(winner.ql), known(winner.deals)) == (1, 1, 1)
    assert known(winner.meetings_booked) == 2
    assert not has_events(loser.mql, loser.ql, loser.meetings_booked, loser.deals)
    assert (winner.leads, loser.leads) == (1, 1)
    assert (known(row.mql), known(row.ql), known(row.deals_after_touch)) == (1, 1, 1)
    assert known(row.meetings_booked) == 2
    assert partitions(row)
    extra_rows = run.item(row).evidence.extra["rows"]
    assert sum(credit["ql"]["value"] for credit in extra_rows) == known(row.ql)
    assert sum(credit["meetings_booked"]["value"] for credit in extra_rows) == 2


def test_a_touch_without_a_creative_is_credited_to_the_unknown_creative_row(
    make_context: MakeContext,
) -> None:
    """Credit: an event of a contact whose touch has no creative lands in "unknown creative".

    Every contact of the creative-floor world reaches QL a day after its touch: the leads
    LinkedIn gave no creative credit their QL to the unknown row, and the rows still sum to
    the asset total.
    """
    run = run_scenario(make_context, "wp.creative_floor.case")
    row = run.row()
    touches = anchors(run, row)
    contacts = {
        hs_id: dataclasses.replace(contact, ql_at=touches[hs_id] + timedelta(days=1))
        if hs_id in touches
        else contact
        for hs_id, contact in run.crm.contacts.items()
    }
    qualified = rerun(run, with_crm(run.ctx, contacts=contacts))
    row = qualified.row()
    without_creative = {
        lead.contact_hs_id for lead in row_leads(qualified, row) if lead.creative_id is None
    }
    unknown = next(credit for credit in row.creative_rows if credit.is_unknown)
    assert unknown.label == UNKNOWN_CREATIVE_LABEL
    assert unknown.ql.value == len(without_creative) == 6
    assert known(row.ql) == len(touches) == 40
    assert sum(credit.ql.value or 0 for credit in row.creative_rows) == known(row.ql)
    assert partitions(row)
    assert unknown.cost_per_ql.value is None


def test_a_meeting_associated_with_two_contacts_counts_once(make_context: MakeContext) -> None:
    """De-duplication: a meeting of two contacts of the asset is one booked meeting."""
    run = run_scenario(make_context, "wp.meeting_two_contacts.case")
    row = run.row()
    meetings = associated(run, row_contacts(run, row), "meeting")
    shared = [mid for mid, contacts in meetings.items() if len(contacts) >= 2]
    links = sum(len(contacts) for contacts in meetings.values())
    assert shared
    assert links > len(meetings) == 13
    assert known(row.meetings_booked) == len(meetings)
    assert partitions(row)


def event_instants(run: _Run, meetings: Iterable[str], deals: Iterable[str]) -> list[datetime]:
    """Credit instants: each meeting's earlier of creation and start, and each deal's creation.

    Every meeting and deal of the derived two-contact world records these instants, so none is
    left out of the oracle.
    """
    instants: list[datetime] = []
    for meeting_id in meetings:
        meeting = run.crm.meetings[meeting_id]
        assert meeting.created_at is not None
        assert meeting.start_at is not None
        instants.append(min(meeting.created_at, meeting.start_at))
    for deal_id in deals:
        created = run.crm.deals[deal_id].created_at
        assert created is not None
        instants.append(created)
    return instants


def latest_touch_across(
    run: _Run, row: AssetRow, contacts: Iterable[str], at: datetime | None = None
) -> str | None:
    """The spec key of the creative of the latest touch across ``contacts`` at or before ``at``.

    Without ``at`` it is the contacts' latest touch overall: the one a selector ignoring the
    event time would credit.
    """
    members = set(contacts)
    touches = [
        lead
        for lead in row_leads(run, row)
        if lead.matched
        and lead.contact_hs_id in members
        and (at is None or lead.submitted_at <= at)
    ]
    assert touches, (members, at)
    latest = max(touches, key=lambda lead: (lead.submitted_at, lead.id))
    return run.creative_key(latest.creative_id)


def placed[K, V](stored: Mapping[K, V], added: Mapping[K, V], *, first: bool) -> dict[K, V]:
    """``stored`` with the new keys of ``added`` inserted before it (``first``) or after it."""
    assert not added.keys() & stored.keys()
    return {**added, **stored} if first else {**stored, **added}


def share_with_second_contact(
    run: _Run, *, winner_above: bool, reverse: bool
) -> tuple[AnalysisContext, str, str]:
    """Split the one contact of ``wp.credit_latest_touch`` in two that share every event.

    The catalogue contact A touches the asset through cr-a1 on day -28 and cr-a2 on day -25,
    and its two booked meetings, its deal and its MQL and QL dates follow both touches. The
    derived context moves the cr-a2 lead to a new contact B (A's record and stage dates under a
    new id, matched and read like A), associates B with the same meetings and deal through
    known fetches (and the deal with B), and gives A a later cr-a1 lead a day after the last
    event, still inside the window. B's id sorts just above A's when ``winner_above`` and just
    below it otherwise; ``reverse`` stores B's contact, fetches and associations before A's,
    B's targets and the deal's contacts in reverse order, and the leads reversed.

    Returns:
        The derived context, A's HubSpot id and B's.
    """
    crm, row = run.crm, run.row()
    [hs_a] = row_contacts(run, row)
    early, middle = sorted(row_leads(run, row), key=lambda lead: lead.submitted_at)
    assert [run.creative_key(lead.creative_id) for lead in (early, middle)] == ["cr-a1", "cr-a2"]
    hs_b = str(int(hs_a) + (1 if winner_above else -1))
    assert (hs_b > hs_a) == winner_above
    assert hs_b not in crm.contacts
    assert all(lead.contact_hs_id != hs_b for lead in run.ctx.leads)

    meetings = crm.targets("contact", hs_a, "meeting")
    deals = crm.targets("contact", hs_a, "deal")
    assert meetings
    assert deals
    later_at = max(event_instants(run, meetings, deals)) + timedelta(days=1)
    assert later_at <= run.ctx.now
    assert later_at.date() <= run.ctx.window.end
    later = dataclasses.replace(
        early,
        id=max(lead.id for lead in run.ctx.leads) + 1,
        external_id=f"{early.external_id}-later",
        submitted_at=later_at,
    )
    assert all(lead.external_id != later.external_id for lead in run.ctx.leads)
    leads = (
        *(
            dataclasses.replace(lead, contact_hs_id=hs_b) if lead.id == middle.id else lead
            for lead in run.ctx.leads
        ),
        later,
    )

    added: dict[tuple[str, str, str], tuple[str, ...]] = {}
    added_fetches: dict[tuple[str, str, str], AssociationFetchInfo] = {}
    for to_type in ("meeting", "deal"):
        fetch = crm.fetch("contact", hs_a, to_type)
        assert fetch is not None
        assert fetch.known
        assert not fetch.is_empty
        targets = crm.targets("contact", hs_a, to_type)
        added[("contact", hs_b, to_type)] = targets[::-1] if reverse else targets
        added_fetches[("contact", hs_b, to_type)] = dataclasses.replace(fetch, object_id=hs_b)
    relinked: dict[tuple[str, str, str], tuple[str, ...]] = {}
    for deal_id in deals:
        key = ("deal", deal_id, "contact")
        assert crm.association_known(*key)
        linked = crm.targets(*key)
        relinked[key] = (hs_b, *linked) if reverse else (*linked, hs_b)

    ctx = with_crm(
        derive(run.ctx, leads=leads[::-1] if reverse else leads),
        contacts=placed(
            crm.contacts, {hs_b: dataclasses.replace(crm.contacts[hs_a], hs_id=hs_b)}, first=reverse
        ),
        associations=placed({**crm.associations, **relinked}, added, first=reverse),
        fetches=placed(crm.fetches, added_fetches, first=reverse),
    )
    return ctx, hs_a, hs_b


@pytest.mark.parametrize("reverse", [False, True], ids=["stored-order", "reversed-order"])
@pytest.mark.parametrize("winner_above", [False, True], ids=["winner-id-below", "winner-id-above"])
def test_a_shared_meeting_and_deal_go_to_the_latest_touch_across_their_contacts(
    make_context: MakeContext, winner_above: bool, reverse: bool
) -> None:
    """Credit: an event of two contacts counts once, to the latest qualifying touch across them.

    Contact A touches the asset through cr-a1 before the shared events and again a day after
    them; contact B touches it through cr-a2 between A's first touch and the events. Each
    booked meeting and the deal is one object of both contacts, credited once to B's cr-a2
    touch: not to the touch of the contact that sorts first, and not to A's last touch, which
    follows the events. Each contact's own MQL and QL go to its own latest touch at or before
    its date. The credit is identical whichever contact sorts first and in whatever order the
    contacts, leads and associations are stored, and the per-creative rows sum to the asset
    total.
    """
    base = run_scenario(make_context, "wp.credit_latest_touch.case")
    ctx, hs_a, hs_b = share_with_second_contact(base, winner_above=winner_above, reverse=reverse)
    run = rerun(base, ctx)
    row = run.row()
    contacts = tuple(sorted((hs_a, hs_b)))
    assert row_contacts(run, row) == contacts

    # The oracle: every shared event's latest qualifying touch is B's cr-a2 touch, while the
    # latest touch overall, and with A sorting first the first contact's touch, is cr-a1.
    meetings = associated(run, contacts, "meeting")
    deals = associated(run, contacts, "deal")
    assert (len(meetings), len(deals)) == (2, 1)
    assert {*meetings.values(), *deals.values()} == {contacts}
    instants = event_instants(run, meetings, deals)
    assert {latest_touch_across(run, row, contacts, at) for at in instants} == {"cr-a2"}
    assert latest_touch_across(run, row, contacts) == "cr-a1"
    assert {latest_touch_across(run, row, contacts[:1], at) for at in instants} == {
        "cr-a1" if winner_above else "cr-a2"
    }

    assert not any(credit.is_unknown for credit in row.creative_rows)
    assert len(row.creative_rows) == 2
    winner, loser = credit_for(run, row, "cr-a2"), credit_for(run, row, "cr-a1")
    assert (known(winner.meetings_booked), known(winner.deals)) == (2, 1)
    assert (known(loser.meetings_booked), known(loser.deals)) == (0, 0)
    assert (known(winner.other_meetings), known(loser.other_meetings)) == (0, 0)
    assert (known(row.meetings_booked), known(row.deals_after_touch)) == (2, 1)
    assert (known(winner.mql), known(winner.ql), known(loser.mql), known(loser.ql)) == (1, 1, 1, 1)
    assert (known(row.mql), known(row.ql)) == (2, 2)
    assert (winner.leads, loser.leads, row.linkedin_leads, row.matched_contacts) == (1, 2, 3, 2)
    assert row.unknown_evidence_share == 0
    assert partitions(row)

    extra_rows = run.item(row).evidence.extra["rows"]
    for key, total in (
        ("mql", row.mql),
        ("ql", row.ql),
        ("meetings_booked", row.meetings_booked),
        ("deals", row.deals_after_touch),
    ):
        assert sum(credit[key]["value"] for credit in extra_rows) == known(total), key
    assert sum(credit["leads"] for credit in extra_rows) == row.linkedin_leads
    assert {
        run.creative_key(credit["creative_id"]): (
            credit["meetings_booked"]["value"],
            credit["deals"]["value"],
        )
        for credit in extra_rows
    } == {"cr-a2": (2, 1), "cr-a1": (0, 0)}

    stored = share_with_second_contact(base, winner_above=False, reverse=False)[0]
    assert row.creative_rows == rerun(base, stored).row().creative_rows


def test_repeat_touches_give_one_mql_and_one_ql_per_asset(make_context: MakeContext) -> None:
    """De-duplication: a contact with three touches contributes one MQL and one QL."""
    run = run_scenario(make_context, "wp.repeat_touch_one_mql.case")
    row = run.row()
    assert (row.linkedin_leads, row.matched_contacts) == (3, 1)
    assert known(row.mql) == 1
    # The one contact without a QL date is reported once, in the singular, and the QL label
    # that equals its stage name is not repeated.
    assert [f.message for f in run.result.findings if f.code == FINDING_SKIPPED_STAGE] == [
        "Qualified Lead: 1 contact skipped this stage (not counted)"
    ]
    [hs_id] = row_contacts(run, row)
    anchor = anchors(run, row)[hs_id]
    qualified = rerun(
        run,
        with_contacts(
            run.ctx,
            [hs_id],
            lambda contact: dataclasses.replace(contact, ql_at=anchor + timedelta(days=1)),
        ),
    ).row()
    assert (known(qualified.mql), known(qualified.ql)) == (1, 1)
    assert qualified.linkedin_leads == 3


def test_the_creative_override_applies_to_both_spend_and_leads(
    make_context: MakeContext, thresholds: Thresholds
) -> None:
    """Asset resolution: the creative override first, else the canonical form, for spend and leads.

    In the catalogue world the override creative 990001301 runs on a form no asset lists: its
    spend and its leads both resolve to the override's asset. In the shipped registry the same
    creative resolves to ``attribution-playbook`` even beside another asset's form.
    """
    run = run_scenario(make_context, "wp.override_precedence.case")
    row = run.row("guide-c")
    override = override_row(run, row)
    assert override.creative_urn == SHIPPED_OVERRIDE_CREATIVE
    creative = next(c for c in run.ctx.creatives.values() if c.urn == override.creative_urn)
    assert creative.lead_form_urn is not None
    assert run.ctx.thresholds.asset_for_form(creative.lead_form_urn) is None
    override_leads = [lead for lead in row_leads(run, row) if lead.creative_id == creative.id]
    assert {lead.lead_form_urn for lead in override_leads} == {creative.lead_form_urn}
    assert (override.spend, override.leads, known(override.ql)) == (Decimal(500), 3, 3)
    assert (row.spend, row.linkedin_leads, known(row.ql)) == (Decimal(750), 8, 8)
    assert creative.campaign_id in row.campaign_ids

    # A lead joins its creative on the associatedEntity URN: with only the URN, it resolves
    # (through the override) and is credited exactly as before.
    by_urn = rerun(
        run, with_leads(run.ctx, lambda lead: dataclasses.replace(lead, creative_id=None))
    )
    assert by_urn.row("guide-c").creative_rows == row.creative_rows

    assert {asset.slug: asset.lead_form_urns for asset in thresholds.content_assets} == dict(
        SHIPPED_ASSET_FORMS
    )
    playbook = thresholds.resolve_asset(creative_urn=SHIPPED_OVERRIDE_CREATIVE, form_urn=None)
    assert playbook is not None
    assert playbook.slug == "attribution-playbook"
    beside_report = thresholds.resolve_asset(
        creative_urn=SHIPPED_OVERRIDE_CREATIVE,
        form_urn=SHIPPED_ASSET_FORMS["b2b-pipeline-report-2026"][0],
    )
    assert beside_report is playbook
    for slug, forms in SHIPPED_ASSET_FORMS.items():
        for form in forms:
            resolved = thresholds.resolve_asset(
                creative_urn=f"{CREATIVE_URN_PREFIX}990001000", form_urn=form
            )
            assert resolved is not None
            assert resolved.slug == slug


def edit_registry(directory: Path, slug: str, field: str, urn: str) -> None:
    """Append ``urn`` to one asset's ``field`` list in a copied ``content_assets.yaml``."""
    path = directory / CONTENT_ASSETS_FILE
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = next(asset for asset in document["assets"] if asset["slug"] == slug)
    entry[field] = [*(entry.get(field) or []), urn]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("slug", "field", "urn"),
    [
        ("paid-media-benchmarks-2026", "lead_forms", f"{FORM_URN_PREFIX}990000101"),
        ("b2b-pipeline-report-2026", "creative_overrides", SHIPPED_OVERRIDE_CREATIVE),
    ],
    ids=["form-under-two-assets", "creative-under-two-assets"],
)
def test_a_form_or_creative_listed_under_two_assets_fails_config_load(
    make_thresholds: ThresholdsFactory, slug: str, field: str, urn: str
) -> None:
    """Config load fails when a form or a creative override is listed under two assets."""
    unedited = Thresholds.load(make_thresholds.write())
    assert unedited.content_assets

    directory = make_thresholds.write()
    edit_registry(directory, slug, field, urn)
    with pytest.raises(ConfigError, match="is listed under two assets") as raised:
        Thresholds.load(directory)
    assert raised.value.file == CONTENT_ASSETS_FILE
    assert raised.value.key is not None
    assert field in raised.value.key


def test_an_override_contradicting_its_creatives_form_fails(
    make_thresholds: ThresholdsFactory,
) -> None:
    """An override whose creative's own form maps to another asset fails ``check_overrides``.

    The loaded registry overrides creative 990001301 to ``attribution-playbook``. A stored
    creative with that URN whose call-to-action form belongs to the pipeline report
    contradicts it; one with no lead-gen form, a form of the same asset or an unlisted form
    does not.
    """
    loaded = Thresholds.load(make_thresholds.write())
    report_form = SHIPPED_ASSET_FORMS["b2b-pipeline-report-2026"][0]
    with pytest.raises(ConfigError, match="contradicts its creative's own form mapping") as raised:
        loaded.check_overrides({SHIPPED_OVERRIDE_CREATIVE: report_form})
    assert raised.value.file == CONTENT_ASSETS_FILE
    for form in (
        None,
        SHIPPED_ASSET_FORMS["attribution-playbook"][0],
        f"{FORM_URN_PREFIX}990000601",
    ):
        loaded.check_overrides({SHIPPED_OVERRIDE_CREATIVE: form})

    with pytest.raises(ConfigError, match="listed under two assets"):
        SCENARIOS["wp.override_conflict.case"].thresholds()


# ---------------------------------------------------------------------------------------------
# Phase 8: currency cohorts, the cross-asset view, W1, W2, ranking and columns
# ---------------------------------------------------------------------------------------------


def money_values(item: Candidate) -> list[Decimal]:
    """Every amount an item's evidence carries: metrics, baselines, cohort view and rows."""
    rendered = item.evidence.to_json()
    found: list[object] = [m["value"] for m in rendered["metrics"] if m["unit"] == "currency"]
    found += [baseline["value"] for baseline in rendered["baselines"]]
    view = rendered["extra"]["cohort"]
    found += [view[key] for key in ("spend_total", "median_cost_per_ql", "double_down_ceiling")]
    found.append(view["retire_spend_floor"])
    for credit in rendered["extra"]["rows"]:
        found += [
            credit["spend"],
            credit["cost_per_ql"]["value"],
            credit["cost_per_meeting"]["value"],
        ]
    return [Decimal(str(value)) for value in found if value not in (None, "—")]


def test_two_currencies_give_two_cohorts_with_nothing_summed_across(
    make_context: MakeContext,
) -> None:
    """Currency cohorts: one table per currency; no cross-currency total or median anywhere.

    The asset has a USD creative (spend 1,000, cost per QL 100) and a EUR one (spend 800, cost
    per QL 80). Neither 1,800 (a cross-currency spend total) nor 90 (a cross-currency median)
    appears in any item, and every amount carries its own cohort's currency.
    """
    run = run_scenario(make_context, "wp.currency_cohorts.case")
    assert tuple(cohort.currency for cohort in run.table.cohorts) == ("EUR", "USD")
    assert {row.currency for row in run.rows("guide-a")} == {"EUR", "USD"}
    eur, usd = run.table.cohorts
    assert (eur.spend_total, usd.spend_total) == (Decimal(800), Decimal(1000))
    assert (eur.median_cost_per_ql, usd.median_cost_per_ql) == (Decimal(80), Decimal(100))
    for cohort in run.table.cohorts:
        assert all(row.currency == cohort.currency for row in cohort.rows)
    assert {item.group for item in run.result.candidates} == {"cohort:EUR", "cohort:USD"}
    assert not cross_currency_amounts(run)
    for item in run.result.candidates:
        currency = cohort_group_currency(item.group)
        assert item.evidence.extra["cohort"]["currency"] == currency
        assert all(m.currency in (None, currency) for m in item.evidence.metrics)
        amounts = money_values(item)
        assert Decimal(1800) not in amounts
        assert Decimal(90) not in amounts
    assert [known(row.ql) for row in run.rows("guide-a")] == [10, 10]


def test_the_cross_asset_view_totals_spend_only(make_context: MakeContext) -> None:
    """Cross-asset view: spend totals only, with "counts overlap across assets" (any-touch).

    One contact touched both assets and is credited to both; the cohort exposes no MQL, QL,
    meeting or deal total. With 2 QL across the cohort, below the 3 a stable median needs,
    every row shows "Insufficient data".
    """
    run = run_scenario(make_context, "wp.cross_asset_overlap.case")
    first, second = run.rows("guide-a")[0], run.rows("guide-b")[0]
    assert row_contacts(run, first) == row_contacts(run, second)
    assert (known(first.ql), known(second.ql)) == (1, 1)
    [cohort] = run.table.cohorts
    assert cohort.spend_total == first.spend + second.spend == Decimal(1000)
    assert cohort.overlap_note == OVERLAP_NOTE == "counts overlap across assets"
    assert set(cohort.to_json()) == {
        "currency",
        "spend_total",
        "median_cost_per_ql",
        "overlap_note",
        "rows",
    }
    minimum = run.ctx.thresholds.int("w.cohort_min_total_ql")
    assert cohort.total_ql is not None
    assert cohort.total_ql < minimum
    for row in (first, second):
        item = run.item(row)
        view = item.evidence.extra["cohort"]
        assert view["overlap_note"] == OVERLAP_NOTE
        assert view["spend_total"] == Decimal(1000)
        assert not set(view) & {"mql", "ql", "total_ql", "meetings", "deals", "leads"}
        assert (item.rule_id, item.action) == (W, ACTION_INSUFFICIENT_DATA)
        assert dict(item.evidence.extra["cohort_sample"])["observed"] == 2
        assert item.evidence.sample is None
    assert run.withheld(W) == (
        f"W1 and W2 not evaluated for the USD cohort: 2 Qualified Leads of {minimum} required "
        "for a stable median cost per Qualified Lead",
    )

    sufficient = run_scenario(make_context, "w1.cohort.fires")
    assert not any(item.action == ACTION_INSUFFICIENT_DATA for item in sufficient.result.candidates)


def test_w1_doubles_down_at_or_below_the_ceiling(make_context: MakeContext) -> None:
    """W1: QL >= 3 and cost per QL <= 0.8 x the cohort median doubles down; above it does not."""
    run = run_scenario(make_context, "w1.cohort.fires")
    thresholds = run.ctx.thresholds
    assert thresholds.int("w.min_ql") == AAP_W1_MIN_QL
    ratio = thresholds.ratio("w.double_down_ratio")
    row = run.row("guide-a")
    median = cohort_of(run, row).median_cost_per_ql
    assert median == Decimal(30)
    assert row.cost_per_ql.value is not None
    assert row.cost_per_ql.value <= ratio * median
    [item] = run.items(W1)
    assert item.entity_ref.external_id == row.external_id
    assert item.action == ACTION_DOUBLE_DOWN_PREFIX + "Pipeline Report - Lead gen"
    assert item.spend_targets == {("campaign", campaign_id) for campaign_id in row.campaign_ids}
    assert item.evidence.sample is not None
    assert (item.evidence.sample.observed, item.evidence.sample.required) == (5, AAP_W1_MIN_QL)
    assert [b.value for b in item.evidence.baselines] == [Decimal(30)]

    twin = run_scenario(make_context, "w1.cohort.does_not_fire")
    above = twin.row("guide-a")
    assert above.cost_per_ql.value == Decimal(25) > ratio * Decimal(30)
    assert not twin.items(W1)
    assert twin.item(above).action == ACTION_NO_CHANGE
    assert "USD 25.00" in twin.item(above).reason
    assert "USD 24.00" in twin.item(above).reason


@pytest.mark.parametrize(
    ("spend", "cost_per_ql", "fires"),
    [(Decimal("120.00"), Decimal("24.00"), True), (Decimal("120.05"), Decimal("24.01"), False)],
    ids=["at-24.00", "above-24.01"],
)
def test_w1_ceiling_is_inclusive_at_exactly_the_ceiling(
    make_context: MakeContext, spend: Decimal, cost_per_ql: Decimal, fires: bool
) -> None:
    """W1 (AAP 0.5.4.7) needs cost per QL <= 0.8 x the cohort median: equality doubles down.

    Derived from the W1 cohort with only guide-a's window spend moved: 120.00 over its 5 QL
    is exactly the 24.00 ceiling (0.8 x the median of 30) and doubles down; 120.05 is 24.01,
    one cent above it, and is "No change warranted". guide-a stays below guide-b's 30, so the
    median over A, B and C stays 30 on both sides.
    """
    base = run_scenario(make_context, "w1.cohort.fires")
    run = rerun(base, with_creative_costs(base, {"cr-a": spend}))
    thresholds = run.ctx.thresholds
    assert thresholds.int("w.min_ql") == AAP_W1_MIN_QL
    row = run.row("guide-a")
    median = cohort_of(run, row).median_cost_per_ql
    assert median is not None
    assert median == cohort_of(base, base.row("guide-a")).median_cost_per_ql == Decimal(30)
    ceiling = thresholds.ratio("w.double_down_ratio") * median
    assert row.spend == spend
    assert known(row.ql) == 5
    assert row.cost_per_ql.value == cost_per_ql
    assert not row.cost_per_ql.ceiling
    assert cost_per_ql - ceiling == (Decimal(0) if fires else Decimal("0.01"))
    item = run.item(row)
    assert item.evidence.sample is not None
    assert (item.evidence.sample.observed, item.evidence.sample.required) == (5, AAP_W1_MIN_QL)
    assert [baseline.value for baseline in item.evidence.baselines] == [median]
    assert dict(item.evidence.extra["cohort"])["double_down_ceiling"] == ceiling
    assert not run.withheld(W1)
    judged = f"Cost per Qualified Lead {reason_money(cost_per_ql)}"
    against = f"the double-down ceiling of {reason_money(ceiling)}"
    if fires:
        assert (item.rule_id, item.action) == (
            W1,
            ACTION_DOUBLE_DOWN_PREFIX + "Pipeline Report - Lead gen",
        )
        assert run.items(W1) == (item,)
        assert item.spend_targets == {("campaign", cid) for cid in row.campaign_ids}
        assert f"{judged} is at or below {against}" in item.reason
    else:
        assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
        assert not run.items(W1)
        assert not item.increases_spend_on
        assert f"{judged} is above {against}" in item.reason


@pytest.mark.parametrize(("ql", "fires"), [(3, True), (2, False)], ids=["ql-3", "ql-2"])
def test_w1_needs_three_qualified_leads(make_context: MakeContext, ql: int, fires: bool) -> None:
    """W1 needs QL >= 3: at QL 2 the cost pattern still holds, yet W1 is withheld.

    Derived from the W1 cohort with the white paper's spend set to 40, so its cost per QL
    (13.33 at QL 3, 20 at QL 2) stays below the 24 ceiling and only QL crosses the minimum.
    """
    base = run_scenario(make_context, "w1.cohort.fires")
    row = base.row("guide-a")
    creative_id = base.scenario.ids.id("linkedin", "creative", "cr-a")
    totals = base.ctx.creative_totals[creative_id]
    cheaper = derive(
        base.ctx,
        creative_totals={
            **base.ctx.creative_totals,
            creative_id: dataclasses.replace(totals, cost=Decimal("40.00")),
        },
    )
    run = rerun(base, with_contacts(cheaper, row_contacts(base, row)[ql:], without_ql))
    derived = run.row("guide-a")
    assert known(derived.ql) == ql
    ceiling = base.ctx.thresholds.ratio("w.double_down_ratio") * Decimal(30)
    assert derived.cost_per_ql.value is not None
    assert derived.cost_per_ql.value <= ceiling
    assert cohort_of(run, derived).median_cost_per_ql == Decimal(30)
    item = run.item(derived)
    if fires:
        assert item.rule_id == W1
        assert not run.withheld(W1)
    else:
        assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
        assert run.withheld(W1) == (f"2 Qualified Leads of the {AAP_W1_MIN_QL} W1 needs",)
        assert item.evidence.sample is None
        assert dict(item.evidence.extra["w1_sample"])["observed"] == 2


def test_w1_names_only_the_campaigns_g1_leaves_unblocked(make_context: MakeContext) -> None:
    """W1 lists only unblocked campaigns: the B2-blocked campaign is left out, before and after G1.

    The white paper's spend comes from a healthy campaign and from a high-utilization,
    poor-efficiency one that G1 blocks. W1 names the healthy one only, so precedence keeps it;
    a W1 that named the blocked campaign would be removed whole.
    """
    run = run_scenario(make_context, "w1.unblocked_only.case")
    ids = run.scenario.ids
    healthy = ids.id("linkedin", "campaign", "li-wp")
    hot = ids.id("linkedin", "campaign", "li-hot")
    row = run.row("guide-a")
    assert row.campaign_ids == (healthy, hot)
    blocked = spend_increase_blocked(run.ctx)
    assert ("campaign", hot) in blocked
    assert ("campaign", healthy) not in blocked

    [item] = run.items(W1)
    assert item.action == ACTION_DOUBLE_DOWN_PREFIX + ids.name("linkedin", "campaign", "li-wp")
    assert item.spend_targets == {("campaign", healthy)}
    assert extra_names(run, row, "blocked_campaigns") == ("Hot - Lead gen",)
    assert "G1 blocks spend increases on them: Hot - Lead gen" in item.reason

    output = precedence.apply_precedence(run.result, run.ctx)
    kept = [ranked.candidate for ranked in output.items[WHITE_PAPERS]]
    assert [c.action for c in kept if c.rule_id == W1] == [item.action]
    assert output.suppressed[WHITE_PAPERS].get("g1_blocked", 0) == 0
    for ranked in output.all_items():
        assert not ranked.candidate.spend_targets & blocked

    naming_hot = dataclasses.replace(
        item, increases_spend_on=(*item.increases_spend_on, DecisionSubject("campaign", hot))
    )
    survivors, removed = precedence.apply_g1((naming_hot,), blocked)
    assert (survivors, removed) == ((), (naming_hot,))


def test_w1_is_withheld_when_every_contributing_campaign_is_blocked(
    make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1 with no unblocked campaign left is withheld, and its row is "No change warranted".

    The W rules read the G1 set through ``white_papers.spend_increase_blocked``; here it also
    blocks the healthy campaign, so both campaigns feeding the white paper are blocked.
    """
    scenario = SCENARIOS["w1.unblocked_only.case"]
    healthy = scenario.ids.id("linkedin", "campaign", "li-wp")

    def everything_blocked(ctx: AnalysisContext) -> frozenset[tuple[str, int]]:
        return spend_increase_blocked(ctx) | {("campaign", healthy)}

    monkeypatch.setattr(white_papers, "spend_increase_blocked", everything_blocked)
    run = evaluate(scenario, make_context(scenario))
    row = run.row("guide-a")
    assert not run.items(W1)
    item = run.item(row)
    assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
    assert not item.increases_spend_on
    assert run.withheld(W1) == (W1_ALL_BLOCKED_REASON,)
    assert extra_names(run, row, "double_down_campaigns") == ()
    assert set(extra_names(run, row, "blocked_campaigns")) == {
        "Pipeline Report - Lead gen",
        "Hot - Lead gen",
    }


def test_w2_retires_a_zero_ql_paper_at_the_spend_multiple(make_context: MakeContext) -> None:
    """W2: QL 0 with spend 10 x the cohort median retires; 2 x (below the 3 x multiple) does not."""
    run = run_scenario(make_context, "w2.cohort.fires")
    thresholds = run.ctx.thresholds
    multiple = thresholds.ratio("w.retire_spend_multiple")
    assert thresholds.int("w.retire_min_window_days") == AAP_W2_MIN_WINDOW_DAYS
    assert run.ctx.window.days == AAP_W2_MIN_WINDOW_DAYS
    row = run.row("guide-d")
    median = cohort_of(run, row).median_cost_per_ql
    assert median is not None
    assert known(row.ql) == 0
    assert row.spend >= multiple * median
    [item] = run.items(W2)
    assert item.entity_ref.external_id == row.external_id
    assert item.action == ACTION_RETIRE
    assert item.evidence.extra["spend_to_median"] == Decimal(10)
    assert item.evidence.sample is not None
    assert (item.evidence.sample.observed, item.evidence.sample.unit) == (28, "days")
    assert not item.increases_spend_on
    assert item.decision_subject is None
    assert not run.withheld(W2)

    twin = run_scenario(make_context, "w2.cohort.does_not_fire")
    kept = twin.row("guide-d")
    assert kept.spend < multiple * Decimal(30)
    assert not twin.items(W2)
    assert twin.item(kept).action == ACTION_NO_CHANGE
    assert twin.item(kept).evidence.extra["spend_to_median"] == Decimal(2)
    assert not twin.withheld(W2)


@pytest.mark.parametrize(
    ("spend", "spend_to_median", "fires"),
    [(Decimal("90.00"), Decimal(3), True), (Decimal("87.00"), Decimal("2.9"), False)],
    ids=["at-3x", "below-2.9x"],
)
def test_w2_retires_at_exactly_the_spend_multiple(
    make_context: MakeContext, spend: Decimal, spend_to_median: Decimal, fires: bool
) -> None:
    """W2 (AAP 0.5.4.7) needs QL 0 and spend / median cost per QL >= 3: exactly 3 x retires.

    Derived from the W2 cohort with only guide-d's window spend moved: 90.00 is exactly 3 x
    the cohort median of 30 and retires; 87.00 is 2.9 x, below the multiple, and is "No change
    warranted" without any W2 line withheld. guide-d has no QL, so the median over A, B and C
    stays 30 on both sides.
    """
    base = run_scenario(make_context, "w2.cohort.fires")
    run = rerun(base, with_creative_costs(base, {"cr-d": spend}))
    thresholds = run.ctx.thresholds
    assert thresholds.int("w.retire_min_window_days") == AAP_W2_MIN_WINDOW_DAYS
    assert run.ctx.window.days == AAP_W2_MIN_WINDOW_DAYS
    row = run.row("guide-d")
    median = cohort_of(run, row).median_cost_per_ql
    assert median is not None
    assert median == cohort_of(base, base.row("guide-d")).median_cost_per_ql == Decimal(30)
    multiple = thresholds.ratio("w.retire_spend_multiple")
    retire_at = multiple * median
    assert known(row.ql) == 0
    assert row.spend == spend
    assert spend / median == spend_to_median
    assert spend_to_median == (multiple if fires else multiple - Decimal("0.1"))
    assert (spend == retire_at) is fires
    item = run.item(row)
    assert item.evidence.extra["spend_to_median"] == spend_to_median
    assert dict(item.evidence.extra["cohort"])["retire_spend_floor"] == retire_at
    assert not run.withheld(W2)
    assert not item.increases_spend_on
    compared = (
        f"{spend_to_median} x the USD cohort median cost per Qualified Lead of "
        f"{reason_money(median)}"
    )
    multiple_text = format(multiple.normalize(), "f")
    retire_text = reason_money(retire_at)
    if fires:
        assert (item.rule_id, item.action) == (W2, ACTION_RETIRE)
        assert run.items(W2) == (item,)
        assert item.decision_subject is None
        assert item.evidence.sample is not None
        assert (item.evidence.sample.observed, item.evidence.sample.unit) == (28, "days")
        assert item.evidence.sample.required == AAP_W2_MIN_WINDOW_DAYS
        assert f"{compared} (W2 retires at {multiple_text} x, {retire_text})" in item.reason
    else:
        assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
        assert not run.items(W2)
        assert item.evidence.sample is None
        below = f"below the {multiple_text} x ({retire_text}) at which W2 retires"
        assert f"{compared}, {below}" in item.reason
        assert "withheld" not in item.reason


def test_w1_reason_words_a_tie_cost_half_up_as_the_table_cell_does(
    make_context: MakeContext,
) -> None:
    """A cost per QL on a rounding tie reads as its table cell does, half up at the cent.

    Derived from the W1 cohort with only guide-a's window spend moved to 120.025: over its 5 QL
    that is 24.005, half a cent above the 24.00 ceiling, so the row is "No change warranted".
    The reason rounds the tie away from zero, "USD 24.01", as ``formatMoney`` renders the stored
    24.005 in the cell; half to even would read "USD 24.00 is above ... USD 24.00".
    """
    base = run_scenario(make_context, "w1.cohort.fires")
    run = rerun(base, with_creative_costs(base, {"cr-a": Decimal("120.025")}))
    row = run.row("guide-a")
    assert known(row.ql) == 5
    assert row.cost_per_ql.value == Decimal("24.005")
    assert cohort_of(run, row).median_cost_per_ql == Decimal(30)
    item = run.item(row)
    assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
    assert item.evidence.metric("cost_per_ql").value == Decimal("24.005")
    assert (
        "Cost per Qualified Lead USD 24.01 is above the double-down ceiling of USD 24.00 "
        "(0.8 x the USD cohort median of USD 30.00)"
    ) in item.reason


def test_w2_reason_groups_thousands_as_the_table_cell_does(make_context: MakeContext) -> None:
    """A spend of 1,000 or more reads grouped in the W2 reason, as its table cell shows it.

    Derived from the W2 cohort with only guide-d's window spend moved to 2,766: 92.2 x the
    cohort median of 30, so W2 retires, and the reason cites "USD 2,766.00", never
    "USD 2766.00".
    """
    base = run_scenario(make_context, "w2.cohort.fires")
    run = rerun(base, with_creative_costs(base, {"cr-d": Decimal(2766)}))
    row = run.row("guide-d")
    assert row.spend == Decimal(2766)
    assert known(row.ql) == 0
    item = run.item(row)
    assert (item.rule_id, item.action) == (W2, ACTION_RETIRE)
    assert item.reason == (
        "No Qualified Lead after USD 2,766.00 of spend over 28 days: 92.2 x the USD cohort "
        "median cost per Qualified Lead of USD 30.00 (W2 retires at 3 x, USD 90.00)."
    )


def test_w2_needs_a_window_of_at_least_28_days(make_context: MakeContext) -> None:
    """W2 on a 27-day window is withheld: the same cohort, its window one day shorter."""
    base = SCENARIOS["w2.cohort.fires"]
    shorter = dataclasses.replace(
        base, context=dataclasses.replace(base.context, window=(-28, -2), kind="custom")
    )
    run = evaluate(shorter, make_context(shorter))
    assert run.ctx.window.days == AAP_W2_MIN_WINDOW_DAYS - 1
    row = run.row("guide-d")
    median = cohort_of(run, row).median_cost_per_ql
    assert median is not None
    assert row.spend >= run.ctx.thresholds.ratio("w.retire_spend_multiple") * median
    assert not run.items(W2)
    assert run.item(row).action == ACTION_NO_CHANGE
    assert run.withheld(W2) == ("window of 27 days is below the 28 days W2 needs",)


def unmatch_leads(ctx: AnalysisContext, lead_ids: Iterable[int]) -> AnalysisContext:
    """The context with the leads ``lead_ids`` unresolved in HubSpot (``no_match``)."""
    selected = set(lead_ids)
    return with_leads(
        ctx,
        lambda lead: (
            dataclasses.replace(
                lead, match_status="no_match", contact_hs_id=None, contact_read_status=None
            )
            if lead.id in selected
            else lead
        ),
    )


def test_w2_needs_no_contact_floor(make_context: MakeContext) -> None:
    """W2 under a contact floor is withheld: one of the paper's five leads left unmatched."""
    base = run_scenario(make_context, "w2.cohort.fires")
    target = row_leads(base, base.row("guide-d"))[0]
    run = rerun(base, unmatch_leads(base.ctx, [target.id]))
    row = run.row("guide-d")
    assert row.contact_share == Decimal(1) / Decimal(5)
    assert row.has_flag(FLAG_ATTRIBUTION_FLOOR)
    assert not run.items(W2)
    assert run.withheld(W2) == ("Floor: 20% of leads unmatched in HubSpot (W2 needs below 10%)",)


@pytest.mark.parametrize(("unresolved", "available"), [(1, True), (2, False)], ids=["5%", "10%"])
def test_the_pipeline_veto_is_unavailable_at_the_attribution_floor(
    make_context: MakeContext, unresolved: int, available: bool
) -> None:
    """P8 on the pipeline: 2 of 20 matured leads unresolved (10%) make it unavailable; 1 does not.

    The unresolved leads stay in the matured population, so its denominator never shrinks.
    """
    base = run_scenario(make_context, "w2.cohort.fires")
    leads = sorted(base.ctx.leads, key=lambda lead: lead.id)
    run = rerun(base, unmatch_leads(base.ctx, [lead.id for lead in leads[:unresolved]]))
    veto = next(iter(run.table.veto.values()))
    assert (veto.matured_leads, veto.unresolved_leads) == (20, unresolved)
    assert veto.unresolved_share == Decimal(unresolved) / Decimal(20)
    assert veto.available is available
    assert veto.attribution_floor is not available
    if not available:
        assert veto.unresolved_share == AAP_FLOOR_SHARE
        assert veto.reason == "10% of matured leads unresolved in HubSpot (floor 10%)"


def with_creative_costs(run: _Run, costs: Mapping[str, Decimal]) -> AnalysisContext:
    """The run's context with the window spend of the catalogue creatives ``costs`` replaced."""
    totals = dict(run.ctx.creative_totals)
    for key, cost in costs.items():
        creative_id = run.scenario.ids.id("linkedin", "creative", key)
        totals[creative_id] = dataclasses.replace(totals[creative_id], cost=cost)
    return derive(run.ctx, creative_totals=totals)


def test_a_white_paper_without_spend_is_never_retired(make_context: MakeContext) -> None:
    """W2 needs spend: a zero-QL white paper with no window spend is "No change warranted"."""
    base = run_scenario(make_context, "w2.cohort.fires")
    run = rerun(base, with_creative_costs(base, {"cr-d": Decimal(0)}))
    row = run.row("guide-d")
    assert row.spend == 0
    assert known(row.ql) == 0
    item = run.item(row)
    assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
    assert item.reason == "No spend and no Qualified Lead in the window."
    assert not run.items(W2)
    assert not run.withheld(W2)


def test_a_zero_cohort_median_withholds_w2(make_context: MakeContext) -> None:
    """W2 compares spend with a positive median only: at a median of 0 it is withheld."""
    base = run_scenario(make_context, "w2.cohort.fires")
    free = {key: Decimal(0) for key in ("cr-a", "cr-b", "cr-c")}
    run = rerun(base, with_creative_costs(base, free))
    row = run.row("guide-d")
    assert cohort_of(run, row).median_cost_per_ql == 0
    assert not run.items(W2)
    item = run.item(row)
    assert item.action == ACTION_NO_CHANGE
    assert item.evidence.extra["spend_to_median"] is None
    assert item.evidence.extra["cohort"]["retire_spend_floor"] is None
    assert run.withheld(W2) == (
        "spend-to-median comparison unavailable: the USD cohort median cost per Qualified Lead "
        "is USD 0.00",
    )


def test_a_cohort_with_no_asset_at_the_median_minimum_is_insufficient(
    make_context: MakeContext,
) -> None:
    """With no asset reaching ``w.median_min_ql`` QL there is no median: every row is insufficient.

    The W1 cohort's assets have 5 QL each; requiring 6 for the median (one input) leaves the
    cohort without one, although its 15 QL together pass the 3-QL minimum.
    """
    base = SCENARIOS["w1.cohort.fires"]
    stricter = dataclasses.replace(
        base,
        context=dataclasses.replace(base.context, threshold_overrides={"w.median_min_ql": 6}),
    )
    run = evaluate(stricter, make_context(stricter))
    [cohort] = run.table.cohorts
    assert cohort.median_cost_per_ql is None
    assert cohort.total_ql == 15
    assert cohort.total_ql >= run.ctx.thresholds.int("w.cohort_min_total_ql")
    assert {item.action for item in run.result.candidates} == {ACTION_INSUFFICIENT_DATA}
    assert run.withheld(W) == (
        "W1 and W2 not evaluated for the USD cohort: no asset with at least 6 Qualified Leads",
    )


def test_an_unresolved_ql_stage_leaves_every_row_insufficient(make_context: MakeContext) -> None:
    """With the QL stage unavailable, no W1 or W2 is judged and QL is never shown as 0."""
    base = run_scenario(make_context, "w2.cohort.fires")
    reason = "stage label 'Qualified Lead' not found in lifecyclestage options"
    bindings = dataclasses.replace(
        base.crm.bindings, ql=ResolvedBinding(available=False, reason=reason)
    )
    run = rerun(base, with_crm(base.ctx, bindings=bindings))
    assert not run.table.ql_available
    assert not run.items(W1)
    assert not run.items(W2)
    for row in run.table.rows():
        assert row.ql.unavailable
        assert row.ql.render() == "unavailable"
        assert row.cost_per_ql.render() == "unavailable"
        assert row.has_flag(FLAG_PIPELINE_UNAVAILABLE)
        item = run.item(row)
        assert (item.rule_id, item.action) == (W, ACTION_INSUFFICIENT_DATA)
        assert item.reason.startswith("The Qualified Lead count is unavailable")
        assert item.evidence.metric("ql").value is None
    [cohort] = run.table.cohorts
    assert cohort.total_ql is None
    assert any("Qualified Lead count unavailable" in reason for reason in run.withheld(W))
    findings = [f for f in run.result.findings if f.code == FINDING_PIPELINE_UNAVAILABLE]
    assert [f.evidence["stage"] for f in findings] == ["Qualified Lead"]
    veto = next(iter(run.table.veto.values()))
    assert not veto.available
    assert veto.ql is None


def test_no_change_warranted_carries_its_numbers(make_context: MakeContext) -> None:
    """Otherwise rule ``W`` "No change warranted", with the numbers it was judged on."""
    run = run_scenario(make_context, "w1.cohort.fires")
    row = run.row("guide-b")
    item = run.item(row)
    assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
    assert item.evidence.metric("ql").value == 5
    assert item.evidence.metric("cost_per_ql").value == Decimal(30)
    assert [baseline.value for baseline in item.evidence.baselines] == [Decimal(30)]
    assert dict(item.evidence.extra["cohort"])["double_down_ceiling"] == Decimal(24)
    assert "USD 30.00" in item.reason
    assert "USD 24.00" in item.reason


def ordered_slugs(run: _Run) -> list[str]:
    """The asset slugs of the White papers items, in the order ``evaluate`` returns them."""
    return [item.entity_ref.external_id.split("|")[0] for item in run.result.candidates]


def test_rows_rank_by_cost_per_ql_then_zero_ql_rows_by_spend(make_context: MakeContext) -> None:
    """Ranking: QL >= 1 rows by cost per QL ascending, then zero-QL rows by spend descending.

    The derived twin takes guide-c's QL away (one input): it joins guide-d among the zero-QL
    rows, which then rank by spend, guide-d's 300 before guide-c's 250.
    """
    run = run_scenario(make_context, "w1.cohort.fires")
    assert ordered_slugs(run) == ["guide-a", "guide-b", "guide-c", "guide-d"]
    assert [item.rank_key for item in run.result.candidates] == [
        (0, Decimal(10)),
        (0, Decimal(30)),
        (0, Decimal(50)),
        (1, -Decimal(300)),
    ]
    output = precedence.apply_precedence(run.result, run.ctx)
    ranked = output.items[WHITE_PAPERS]
    assert [r.candidate.entity_ref.external_id.split("|")[0] for r in ranked] == ordered_slugs(run)
    assert [r.rank for r in ranked] == [1, 2, 3, 4]

    contacts = row_contacts(run, run.row("guide-c"))
    derived = rerun(run, with_contacts(run.ctx, contacts, without_ql))
    assert known(derived.row("guide-c").ql) == 0
    assert ordered_slugs(derived) == ["guide-a", "guide-b", "guide-d", "guide-c"]
    zero_ql = [item for item in derived.result.candidates if item.rank_key[0] == 1]
    assert [item.rank_key[1] for item in zero_ql] == [-Decimal(300), -Decimal(250)]


def test_evidence_columns_follow_the_white_paper_table(make_context: MakeContext) -> None:
    """Column order (AAP 0.5.5): asset, spend, LinkedIn leads, MQL, QL, meetings booked, other
    meetings, deals after touch, cost per QL, cost per meeting, then the action."""
    run = run_scenario(make_context, "w1.cohort.fires")
    row = run.row("guide-a")
    item = run.item(row)
    assert METRIC_KEYS == EVIDENCE_COLUMNS
    assert tuple(metric.key for metric in item.evidence.metrics) == EVIDENCE_COLUMNS
    assert {metric.key: metric.label for metric in item.evidence.metrics} == {
        "spend": "Spend",
        "linkedin_leads": "LinkedIn leads (platform)",
        "mql": "MQL (First meeting booked)",
        "ql": "Qualified Lead",
        "meetings_booked": f"Meetings {BOOKED_LABEL}",
        "other_meetings": OTHER_MEETINGS_LABEL,
        "deals_after_touch": "Deals after touch",
        "cost_per_ql": "Cost per Qualified Lead",
        "cost_per_meeting": "Cost per meeting booked",
    }
    assert item.entity_ref.name == row.title
    assert item.action.startswith(ACTION_DOUBLE_DOWN_PREFIX)
    currencies = {m.key: m.currency for m in item.evidence.metrics if m.unit == "currency"}
    assert currencies == {"spend": "USD", "cost_per_ql": "USD", "cost_per_meeting": "USD"}
