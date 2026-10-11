"""Unit tests for ``ads_engine.analysis.discrepancy``: rule C0, discrepancies resolved first.

They pin how C0 presents the unresolved discrepancies of :attr:`AnalysisContext.discrepancies`
(AAP 0.5.4.4, 0.5.4.2 P1): one pinned Campaigns > Discrepancies item per ``open`` or
``awaiting_platform`` discrepancy, oldest ``detected_at`` first with the discrepancy id as the
tie-break, the action "Resolve: platform shows {platform}, last decided {decided}", exactly the
resolutions ``ads_engine.decisions.RESOLUTIONS`` allows for the (kind, decision shape) pair in
``RESOLUTION_ORDER``, and values rendered at native precision so rounding never hides a
mismatch. Pure: no database and no network.
"""

import doctest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest

from ads_engine.analysis import discrepancy as discrepancy_module
from ads_engine.analysis.context import (
    C0,
    CAMPAIGNS,
    FLAG_NO_CRM_LINK,
    GROUP_DISCREPANCIES,
    AccountInfo,
    AnalysisContext,
    BudgetInfo,
    CampaignInfo,
    Candidate,
    CrmData,
    DatasetRef,
    DatasetStatus,
    DecisionShape,
    DecisionSubject,
    DiscrepancyInfo,
    DiscrepancyKind,
    Flag,
    Freshness,
    ProvenanceEntry,
    RuleResult,
    Source,
    SubjectType,
    json_value,
    objective_group,
)
from ads_engine.analysis.discrepancy import (
    NO_DECISION_TEXT,
    NOT_RETURNED_TEXT,
    RESOLUTION_ORDER,
    STATUS_LABELS,
    STATUS_NOT_REPORTED_TEXT,
    TOMBSTONE_TEXT,
    UNAVAILABLE_TEXT,
    evaluate,
    offered_resolutions,
    render_value,
)
from ads_engine.analysis.thresholds import CrmBindings, GoogleCrmBinding, Thresholds
from ads_engine.analysis.window import AnalysisWindow, SourceDayInfo
from ads_engine.decisions import RESOLUTIONS, Resolution, to_micros

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

NOW = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
DETECTED = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)
COVERED_THROUGH = date(2026, 10, 5)

GOOGLE = DatasetRef(11, "google", "live")
LINKEDIN = DatasetRef(12, "linkedin", "live")
HUBSPOT = DatasetRef(13, "hubspot", "live")

GOOGLE_ACCOUNT = 1
LINKEDIN_ACCOUNT = 2
UNLOADED_ACCOUNT = 99

OWN_CAMPAIGN = 7
MEMBER_CAMPAIGN = 8
LINKEDIN_CAMPAIGN = 9
ORPHAN_CAMPAIGN = 10
BLANK_ID_CAMPAIGN = 11

SHARED_BUDGET = 30
NON_SHARED_BUDGET = 31
OTHER_SHARED_BUDGET = 32

UNLINKED_REASON = "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured"
"""Why the Google-to-CRM link is unavailable in the unverified-link contexts."""

UNLINKED_FLAG = Flag(
    FLAG_NO_CRM_LINK, "No CRM link for Google campaigns: no active HubSpot dataset"
)
"""The ``no_crm_link`` flag of a Google item in a context without a Google CRM dataset."""

PLATFORM_OR_DECISION: tuple[Resolution, ...] = ("platform_correct", "decision_correct")
ADOPT_OR_INTEND: tuple[Resolution, ...] = ("adopt_platform", "record_intended")
REMOVAL: tuple[Resolution, ...] = ("decision_correct", "acknowledge_removal")


# ---------------------------------------------------------------------------
# Builders (self-contained: tests/conftest.py is not imported)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    return Thresholds.load(CONFIG_DIR)


@pytest.fixture(scope="module")
def window(thresholds: Thresholds) -> AnalysisWindow:
    return AnalysisWindow.default(thresholds, SourceDayInfo("America/New_York"), now=NOW)


def _google_money(
    amount: str | None, *, period: str = "DAILY", currency: str = "USD"
) -> dict[str, Any]:
    """A Google amount value as ``decisions.compare`` stores it, ``amount_micros`` included."""
    micros = None if amount is None else to_micros(Decimal(amount))
    return {
        "daily_budget": amount if period == "DAILY" else None,
        "total_budget": amount if period == "CUSTOM_PERIOD" else None,
        "currency": currency,
        "amount_micros": micros,
    }


def _linkedin_money(
    daily: str | None, total: str | None = None, *, currency: str = "EUR"
) -> dict[str, Any]:
    """A LinkedIn amount value as ``decisions.compare`` stores it (no ``amount_micros``)."""
    return {"daily_budget": daily, "total_budget": total, "currency": currency}


def _campaign(
    campaign_id: int,
    *,
    source: Source = "google",
    account_id: int = GOOGLE_ACCOUNT,
    external_id: str = "111",
    name: str = "Brand search",
    budget_id: int | None = None,
    currency: str = "USD",
) -> CampaignInfo:
    channel = "SEARCH" if source == "google" else "LEAD_GENERATION"
    return CampaignInfo(
        id=campaign_id,
        source=source,
        account_id=account_id,
        external_id=external_id,
        name=name,
        status="active",
        raw_status="ENABLED" if source == "google" else "ACTIVE",
        objective_or_channel=channel,
        objective_group=objective_group(source, channel),
        campaign_group_id=None,
        budget_id=budget_id,
        daily_budget=Decimal("50.00"),
        total_budget=None,
        budget_period="DAILY" if source == "google" else None,
        currency=currency,
    )


def _budget(
    budget_id: int, external_id: str, *, shared: bool, name: str = "", currency: str = "USD"
) -> BudgetInfo:
    return BudgetInfo(
        id=budget_id,
        external_id=external_id,
        name=name,
        amount=Decimal("100.00"),
        amount_micros=100_000_000,
        period="DAILY",
        explicitly_shared=shared,
        currency=currency,
    )


def _context(
    thresholds: Thresholds,
    window: AnalysisWindow,
    *discrepancies: DiscrepancyInfo,
    **changes: Any,
) -> AnalysisContext:
    fields: dict[str, Any] = {
        "window": window,
        "thresholds": thresholds,
        "now": NOW,
        "datasets": {"google": GOOGLE, "linkedin": LINKEDIN, "hubspot": HUBSPOT},
        "freshness": {
            (GOOGLE.dataset_id, "google.entities"): Freshness(
                dataset_id=GOOGLE.dataset_id,
                dataset="google.entities",
                last_attempt_at=NOW - timedelta(hours=1),
                last_success_at=NOW - timedelta(hours=1),
                covered_from=COVERED_THROUGH,
                covered_through=COVERED_THROUGH,
                completeness="complete",
            )
        },
        "accounts": {
            GOOGLE_ACCOUNT: AccountInfo(
                GOOGLE_ACCOUNT, "google", "1234567890", "Google", "USD", "America/New_York"
            ),
            LINKEDIN_ACCOUNT: AccountInfo(LINKEDIN_ACCOUNT, "linkedin", "5550001", "LI", "EUR"),
        },
        "campaigns": {
            OWN_CAMPAIGN: _campaign(OWN_CAMPAIGN),
            MEMBER_CAMPAIGN: _campaign(
                MEMBER_CAMPAIGN, external_id="222", name="Generic", budget_id=SHARED_BUDGET
            ),
            LINKEDIN_CAMPAIGN: _campaign(
                LINKEDIN_CAMPAIGN,
                source="linkedin",
                account_id=LINKEDIN_ACCOUNT,
                external_id="333",
                name="Lead gen EU",
                currency="EUR",
            ),
            ORPHAN_CAMPAIGN: _campaign(
                ORPHAN_CAMPAIGN,
                account_id=UNLOADED_ACCOUNT,
                external_id="444",
                name="Orphan",
                currency="GBP",
            ),
            BLANK_ID_CAMPAIGN: _campaign(BLANK_ID_CAMPAIGN, external_id="  ", name="Blank id"),
        },
        "budgets": {
            SHARED_BUDGET: _budget(SHARED_BUDGET, "123", shared=True),
            NON_SHARED_BUDGET: _budget(NON_SHARED_BUDGET, "456", shared=False),
            OTHER_SHARED_BUDGET: _budget(OTHER_SHARED_BUDGET, "789", shared=True, name="Pool B"),
        },
        "discrepancies": discrepancies,
    }
    fields.update(changes)
    return AnalysisContext(**fields)


def _discrepancy(discrepancy_id: int, **changes: Any) -> DiscrepancyInfo:
    fields: dict[str, Any] = {
        "id": discrepancy_id,
        "subject_type": "campaign",
        "subject_id": OWN_CAMPAIGN,
        "subject_name": "Brand search",
        "source": "google",
        "shape": "own_budget",
        "kind": "status",
        "decided_value": {"status": "active"},
        "platform_value": {"status": "paused"},
        "detected_at": DETECTED,
        "state": "open",
    }
    fields.update(changes)
    return DiscrepancyInfo(**fields)


def _only(ctx: AnalysisContext) -> Candidate:
    result = evaluate(ctx)
    assert len(result.candidates) == 1
    return result.candidates[0]


def _platform_text(candidate: Candidate) -> Any:
    return candidate.evidence.metric("platform").value


def _decided_text(candidate: Candidate) -> Any:
    return candidate.evidence.metric("decided").value


# ---------------------------------------------------------------------------
# Resolutions offered per (kind, shape)
# ---------------------------------------------------------------------------


def test_resolution_order_lists_every_resolution_once() -> None:
    assert RESOLUTION_ORDER == (
        "platform_correct",
        "decision_correct",
        "adopt_platform",
        "record_intended",
        "acknowledge_removal",
    )
    assert sorted(RESOLUTION_ORDER) == sorted(get_args(Resolution))


EXPECTED_RESOLUTIONS: dict[tuple[str, str], tuple[Resolution, ...]] = {
    ("status", "own_budget"): PLATFORM_OR_DECISION,
    ("status", "shared_member"): PLATFORM_OR_DECISION,
    ("budget", "own_budget"): PLATFORM_OR_DECISION,
    ("budget", "shared_member"): PLATFORM_OR_DECISION,
    ("budget", "budget"): PLATFORM_OR_DECISION,
    ("untracked", "own_budget"): ADOPT_OR_INTEND,
    ("untracked", "shared_member"): ADOPT_OR_INTEND,
    ("untracked", "budget"): ADOPT_OR_INTEND,
    ("missing", "own_budget"): REMOVAL,
    ("missing", "shared_member"): REMOVAL,
    ("missing", "budget"): REMOVAL,
}


def test_expected_table_covers_exactly_the_resolution_matrix() -> None:
    assert set(EXPECTED_RESOLUTIONS) == set(RESOLUTIONS)


@pytest.mark.parametrize(("kind", "shape"), sorted(RESOLUTIONS))
def test_offered_resolutions_match_the_matrix_in_resolution_order(
    kind: DiscrepancyKind, shape: DecisionShape
) -> None:
    offered = offered_resolutions(kind, shape)
    assert offered == EXPECTED_RESOLUTIONS[(kind, shape)]
    assert frozenset(offered) == RESOLUTIONS[(kind, shape)]
    assert len(offered) == len(RESOLUTIONS[(kind, shape)])
    assert list(offered) == sorted(offered, key=RESOLUTION_ORDER.index)


@pytest.mark.parametrize(
    ("kind", "shape"),
    [("status", "budget"), ("bogus", "own_budget"), ("budget", "campaign_group")],
)
def test_offered_resolutions_rejects_a_pair_without_resolutions(kind: str, shape: str) -> None:
    with pytest.raises(ValueError, match="no resolution is defined"):
        offered_resolutions(kind, shape)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# render_value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "shape", "value", "currency", "expected"),
    [
        # status
        pytest.param("status", "own_budget", {"status": "paused"}, "USD", "Paused", id="status"),
        pytest.param("status", "own_budget", {"status": "active"}, "USD", "Active", id="active"),
        pytest.param(
            "status", "shared_member", {"status": " ended "}, None, "Ended", id="status-stripped"
        ),
        pytest.param(
            "status", "own_budget", {"status": "archived"}, None, "archived", id="status-as-sent"
        ),
        pytest.param(
            "status", "own_budget", {"status": None}, None, "status not reported", id="status-none"
        ),
        pytest.param(
            "status", "own_budget", {"status": "  "}, None, "status not reported", id="status-blank"
        ),
        pytest.param("status", "own_budget", {}, None, "status not reported", id="status-absent"),
        # own_budget and budget amounts, LinkedIn decimal text
        pytest.param(
            "budget",
            "own_budget",
            _linkedin_money("50.000000", currency="USD"),
            None,
            "daily budget USD 50.00",
            id="daily-usd-2dp",
        ),
        pytest.param(
            "budget",
            "own_budget",
            _linkedin_money("50.00", "1000.00"),
            None,
            "daily budget EUR 50.00 · total budget EUR 1,000.00",
            id="daily-and-total",
        ),
        pytest.param(
            "budget",
            "own_budget",
            _linkedin_money(None, "1500.5", currency="usd"),
            None,
            "total budget USD 1,500.50",
            id="total-only-code-uppercased",
        ),
        pytest.param(
            "budget",
            "own_budget",
            _linkedin_money(None),
            "EUR",
            "no budget set",
            id="linkedin-no-budget",
        ),
        pytest.param(
            "budget",
            "own_budget",
            _linkedin_money("40.01"),
            None,
            "daily budget EUR 40.01",
            id="linkedin-minor-unit",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": " 50.00 ", "currency": "USD"},
            None,
            "daily budget USD 50.00",
            id="text-stripped",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "100", "currency": "USD"},
            None,
            "daily budget USD 100.00",
            id="whole-number-text",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": 50, "currency": "USD"},
            None,
            "daily budget USD 50.00",
            id="int-amount",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": Decimal("12.5")},
            "GBP",
            "daily budget GBP 12.50",
            id="decimal-amount-currency-fallback",
        ),
        # native precision: the currency's minor unit, more when significant, at most a micro
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "5000", "currency": "JPY"},
            None,
            "daily budget JPY 5,000",
            id="jpy-no-minor-unit",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "5000.5", "currency": "JPY"},
            None,
            "daily budget JPY 5,000.5",
            id="jpy-significant-digit-kept",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "1.5", "currency": "KWD"},
            None,
            "daily budget KWD 1.500",
            id="kwd-three-places",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "1.1234567", "currency": "USD"},
            None,
            "daily budget USD 1.123457",
            id="capped-at-one-micro",
        ),
        # Google micros, converted exactly with from_micros
        pytest.param(
            "budget",
            "own_budget",
            _google_money("50.000001"),
            None,
            "daily budget USD 50.000001",
            id="google-one-micro-shown",
        ),
        pytest.param(
            "budget",
            "budget",
            _google_money("100.000000"),
            None,
            "daily budget USD 100.00",
            id="google-daily",
        ),
        pytest.param(
            "budget",
            "own_budget",
            _google_money("250.000000", period="CUSTOM_PERIOD"),
            None,
            "total budget USD 250.00",
            id="google-custom-period",
        ),
        pytest.param(
            "budget",
            "budget",
            {"currency": "USD", "amount_micros": 1_000_000},
            None,
            "budget USD 1.00",
            id="google-micros-no-period-field",
        ),
        pytest.param(
            "budget",
            "budget",
            {
                "daily_budget": "1",
                "total_budget": "1",
                "currency": "USD",
                "amount_micros": 1_000_000,
            },
            None,
            "budget USD 1.00",
            id="google-micros-both-period-fields",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "999.00", "currency": "USD", "amount_micros": 12_345_678},
            None,
            "daily budget USD 12.345678",
            id="google-micros-win-over-text",
        ),
        pytest.param(
            "budget",
            "budget",
            {"amount_micros": 2_500_000},
            "JPY",
            "budget JPY 2.5",
            id="google-micros-fallback-currency",
        ),
        # unknown amounts are unavailable, never zero
        pytest.param(
            "budget",
            "budget",
            _google_money(None),
            None,
            "budget unavailable",
            id="google-micros-omitted",
        ),
        pytest.param(
            "budget",
            "budget",
            {"amount_micros": True, "currency": "USD"},
            None,
            "budget unavailable",
            id="google-micros-bool",
        ),
        pytest.param(
            "budget",
            "budget",
            {"currency": "USD"},
            "USD",
            "budget unavailable",
            id="no-amount-keys",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "fifty", "currency": "USD"},
            None,
            "budget unavailable",
            id="malformed-text",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "NaN", "currency": "USD"},
            None,
            "budget unavailable",
            id="nan-text",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"total_budget": "Infinity", "currency": "USD"},
            None,
            "budget unavailable",
            id="infinite-text",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": 50.5, "currency": "USD"},
            None,
            "budget unavailable",
            id="float-amount",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": True, "currency": "USD"},
            None,
            "budget unavailable",
            id="bool-amount",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "fifty", "total_budget": "20"},
            "USD",
            "total budget USD 20.00",
            id="malformed-daily-valid-total",
        ),
        # currency resolution
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "7.5", "currency": "  "},
            "eur",
            "daily budget EUR 7.50",
            id="blank-code-uses-fallback",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "7.5"},
            None,
            "daily budget 7.50",
            id="no-currency-default-2dp",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"daily_budget": "7.5"},
            "  ",
            "daily budget 7.50",
            id="blank-fallback-ignored",
        ),
        # budget assignments
        pytest.param(
            "budget",
            "shared_member",
            {"budget_id": 7, "budget_external_id": "123"},
            None,
            "shared budget 123",
            id="shared-member-assignment",
        ),
        pytest.param(
            "budget",
            "shared_member",
            {"budget_id": 7, "budget_external_id": " 456 ", "explicitly_shared": False},
            None,
            "non-shared budget 456",
            id="assignment-not-shared",
        ),
        pytest.param(
            "budget",
            "shared_member",
            {"budget_id": 7, "budget_external_id": None},
            None,
            "budget (internal id 7)",
            id="assignment-internal-id",
        ),
        pytest.param(
            "budget",
            "shared_member",
            {"budget_id": 7, "budget_external_id": "  "},
            None,
            "budget (internal id 7)",
            id="assignment-blank-external-id",
        ),
        pytest.param(
            "budget",
            "own_budget",
            {"budget_id": None, "budget_external_id": None},
            "USD",
            "own budget (not shared)",
            id="own-budget-assignment",
        ),
        pytest.param(
            "budget",
            "shared_member",
            {"status": "active"},
            None,
            "budget assignment unavailable",
            id="assignment-absent",
        ),
        # untracked and missing: the complete state of the shape
        pytest.param(
            "untracked",
            "own_budget",
            {"status": "active", **_google_money("50.000000")},
            None,
            "Active · daily budget USD 50.00",
            id="untracked-own-budget",
        ),
        pytest.param(
            "untracked",
            "shared_member",
            {"status": "paused", "budget_id": 7, "budget_external_id": "123"},
            None,
            "Paused · shared budget 123",
            id="untracked-shared-member",
        ),
        pytest.param(
            "untracked",
            "budget",
            _google_money("20.000000"),
            None,
            "daily budget USD 20.00",
            id="untracked-budget-amount-only",
        ),
        pytest.param(
            "missing",
            "own_budget",
            {"status": "active", **_linkedin_money(None, "900.00")},
            None,
            "Active · total budget EUR 900.00",
            id="missing-own-budget",
        ),
        pytest.param(
            "untracked",
            "own_budget",
            {"status": "active", **_linkedin_money("50.00", "1000.00")},
            None,
            "Active · daily budget EUR 50.00 · total budget EUR 1,000.00",
            id="untracked-own-budget-daily-and-total",
        ),
        pytest.param(
            "missing",
            "shared_member",
            {"status": None, "budget_id": None},
            None,
            "status not reported · own budget (not shared)",
            id="missing-shared-member-unknown",
        ),
        pytest.param(
            "missing",
            "own_budget",
            {"status": "ended", "tombstone": False},
            "USD",
            "Ended · budget unavailable",
            id="tombstone-false-is-a-state",
        ),
        # tombstones and unknown values
        pytest.param(
            "missing",
            "own_budget",
            {"status": "ended", "tombstone": True},
            "USD",
            "Removal acknowledged · Ended",
            id="tombstone",
        ),
        pytest.param(
            "status",
            "shared_member",
            {"status": "paused", "tombstone": True},
            None,
            "Removal acknowledged · Paused",
            id="tombstone-own-status",
        ),
        pytest.param(
            "status",
            "own_budget",
            {"status": " ", "tombstone": True},
            None,
            "Removal acknowledged · Ended",
            id="tombstone-blank-status-is-ended",
        ),
        pytest.param(
            "missing",
            "shared_member",
            {"tombstone": True},
            None,
            "Removal acknowledged · Ended",
            id="tombstone-no-status-is-ended",
        ),
        pytest.param(
            "budget",
            "budget",
            {**_google_money("10.000000"), "tombstone": True},
            None,
            TOMBSTONE_TEXT,
            id="budget-tombstone",
        ),
        pytest.param("status", "own_budget", None, "USD", UNAVAILABLE_TEXT, id="none-status"),
        pytest.param("budget", "budget", None, "USD", UNAVAILABLE_TEXT, id="none-budget"),
        pytest.param("untracked", "own_budget", None, None, UNAVAILABLE_TEXT, id="none-untracked"),
        pytest.param("missing", "budget", None, None, UNAVAILABLE_TEXT, id="none-missing"),
    ],
)
def test_render_value(
    kind: DiscrepancyKind,
    shape: DecisionShape,
    value: dict[str, Any] | None,
    currency: str | None,
    expected: str,
) -> None:
    assert render_value(kind, shape, value, currency) == expected


def test_render_value_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="discrepancy kind must be one of"):
        render_value("removed", "own_budget", {"status": "ended"}, "USD")  # type: ignore[arg-type]


def test_render_value_keeps_a_one_micro_difference_visible() -> None:
    decided = render_value("budget", "budget", _google_money("50.000000"), None)
    platform = render_value("budget", "budget", _google_money("50.000001"), None)
    assert decided == "daily budget USD 50.00"
    assert platform == "daily budget USD 50.000001"
    assert decided != platform


# The web client renders these amounts on /decisions with formatExactMoney: the same literal
# strings are asserted by web/src/lib/format.test.ts and DiscrepancyResolver.test.tsx, with a
# no-break space (U+00A0) where these have a plain space. Change both sides together. The
# places come from ads_engine.decisions.minor_unit_exponent (ISO 4217), never CLDR: IQD has 3
# and AFN and ALL 2, where CLDR gives 0.
MONEY_PARITY: tuple[tuple[str, str, str], ...] = (
    ("400.000000", "USD", "USD 400.00"),
    ("50.00", "EUR", "EUR 50.00"),
    ("5000", "JPY", "JPY 5,000"),
    ("5000.5", "JPY", "JPY 5,000.5"),
    ("1.5", "KWD", "KWD 1.500"),
    ("50.00", "IQD", "IQD 50.000"),
    ("1234.5", "IQD", "IQD 1,234.500"),
    ("50.000001", "IQD", "IQD 50.000001"),
    ("50.00", "AFN", "AFN 50.00"),
    ("50", "ALL", "ALL 50.00"),
    ("5", "XYZ", "XYZ 5.00"),
)
"""``(stored amount, currency, rendered amount)``: common currencies, those whose ISO 4217 minor
unit differs from CLDR's, and a well-formed unknown code, which has two places."""


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    MONEY_PARITY,
    ids=[f"{currency}-{amount}" for amount, currency, _ in MONEY_PARITY],
)
def test_render_value_words_amounts_as_the_web_resolver(
    amount: str, currency: str, expected: str
) -> None:
    linkedin = _linkedin_money(amount, currency=currency)
    assert render_value("budget", "own_budget", linkedin, None) == f"daily budget {expected}"
    # The subject's currency words a side that names none, at the same places.
    bare = {"daily_budget": amount, "total_budget": None}
    assert render_value("budget", "own_budget", bare, f" {currency.lower()} ") == (
        f"daily budget {expected}"
    )
    # A Google side is read from its micros, converted exactly, at the same places.
    google = _google_money(amount, currency=currency)
    assert render_value("budget", "budget", google, None) == f"daily budget {expected}"


def test_text_constants() -> None:
    assert UNAVAILABLE_TEXT == "unavailable"
    assert NOT_RETURNED_TEXT == "not returned by the platform"
    assert NO_DECISION_TEXT == "no decision recorded"
    assert TOMBSTONE_TEXT == "Removal acknowledged"
    assert STATUS_NOT_REPORTED_TEXT == "status not reported"
    assert dict(STATUS_LABELS) == {"active": "Active", "paused": "Paused", "ended": "Ended"}


# ---------------------------------------------------------------------------
# Docstring examples
# ---------------------------------------------------------------------------


def test_module_docstring_examples() -> None:
    assert (
        render_value("budget", "own_budget", {"daily_budget": "50.000000", "currency": "USD"}, None)
        == "daily budget USD 50.00"
    )
    assert (
        render_value("budget", "shared_member", {"budget_id": 7, "budget_external_id": "123"}, None)
        == "shared budget 123"
    )
    assert offered_resolutions("missing", "own_budget") == (
        "decision_correct",
        "acknowledge_removal",
    )
    assert offered_resolutions("untracked", "budget") == ("adopt_platform", "record_intended")
    assert render_value("status", "own_budget", {"status": "paused"}, "USD") == "Paused"
    value = {"daily_budget": "50.000001", "currency": "USD", "amount_micros": 50000001}
    assert render_value("budget", "budget", value, "USD") == "daily budget USD 50.000001"
    value = {"status": "active", "daily_budget": "400.000000", "currency": "USD"}
    assert render_value("untracked", "own_budget", value, "USD") == (
        "Active · daily budget USD 400.00"
    )
    assert (
        render_value("missing", "own_budget", {"status": "ended", "tombstone": True}, "USD")
        == "Removal acknowledged · Ended"
    )


def test_module_doctests() -> None:
    result = doctest.testmod(discrepancy_module, report=False)
    assert result.failed == 0
    assert result.attempted >= 7


# ---------------------------------------------------------------------------
# evaluate: the pinned C0 items
# ---------------------------------------------------------------------------


def test_empty_context_yields_an_empty_result(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    result = evaluate(_context(thresholds, window))
    assert result == RuleResult()
    assert result.candidates == ()
    assert result.findings == ()
    assert result.withheld == ()
    assert result.suppressed == {}


def test_resolved_discrepancies_never_reach_the_context() -> None:
    # The context holds only unresolved rows, so evaluate never sees a resolved one.
    with pytest.raises(ValueError, match=r"DiscrepancyInfo\.state"):
        _discrepancy(1, state="resolved")


def test_one_pinned_item_per_unresolved_discrepancy_oldest_first(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    eastern = timezone(timedelta(hours=-4))
    discrepancies = (
        _discrepancy(5, detected_at=DETECTED + timedelta(hours=2)),
        _discrepancy(
            4,
            subject_type="budget",
            subject_id=SHARED_BUDGET,
            subject_name="",
            shape="budget",
            kind="budget",
            decided_value=_google_money("100.000000"),
            platform_value=_google_money("120.000000"),
            state="awaiting_platform",
        ),
        _discrepancy(3, subject_id=MEMBER_CAMPAIGN, shape="shared_member"),
        # 05:30 at UTC-4 is 09:30 UTC: the same instant as DETECTED, so the id breaks the tie.
        _discrepancy(
            2,
            subject_id=LINKEDIN_CAMPAIGN,
            source="linkedin",
            kind="untracked",
            decided_value=None,
            platform_value={"status": "active", **_linkedin_money("40.00")},
            detected_at=datetime(2026, 10, 5, 5, 30, tzinfo=eastern),
        ),
        _discrepancy(
            1,
            kind="missing",
            decided_value={"status": "active", **_google_money("50.000000")},
            platform_value=None,
            detected_at=DETECTED - timedelta(days=1),
        ),
    )
    ctx = _context(thresholds, window, *discrepancies)

    result = evaluate(ctx)

    assert len(result.candidates) == len(discrepancies)
    order = [candidate.evidence.extra["discrepancy_id"] for candidate in result.candidates]
    assert order == [1, 2, 3, 4, 5]
    assert result.findings == ()
    assert result.withheld == ()
    assert result.suppressed == {}
    by_id = {item.id: item for item in discrepancies}
    for candidate in result.candidates:
        item = by_id[candidate.evidence.extra["discrepancy_id"]]
        subject = DecisionSubject(item.subject_type, item.subject_id)
        assert candidate.rule_id == C0
        assert candidate.section == CAMPAIGNS
        assert candidate.group == GROUP_DISCREPANCIES
        assert candidate.pinned is True
        assert candidate.is_discrepancy is True
        assert candidate.held is False
        assert candidate.held_reason is None
        assert candidate.decision_subject is None
        assert candidate.depends_on == (subject,)
        assert candidate.increases_spend_on == ()
        # No Google CRM dataset: Google items carry no_crm_link, LinkedIn items never do.
        assert candidate.flags == ((UNLINKED_FLAG,) if item.source == "google" else ())
        assert candidate.rank_key == (item.detected_at, item.id)
        assert candidate.required_datasets == (f"{item.source}.entities",)
        assert candidate.entity_ref.source == item.source
        assert candidate.entity_ref.type == item.subject_type
        assert candidate.entity_ref.id == item.subject_id
        assert candidate.evidence.extra["resolutions"] == tuple(
            offered_resolutions(item.kind, item.shape)
        )
        assert candidate.evidence.extra["kind"] == item.kind
        assert candidate.evidence.extra["shape"] == item.shape
        assert candidate.evidence.extra["state"] == item.state
        assert candidate.action == (
            f"Resolve: platform shows {_platform_text(candidate)}, "
            f"last decided {_decided_text(candidate)}"
        )
    assert result.candidates[1].evidence.extra["detected_at"] == "2026-10-05T09:30:00+00:00"
    assert sorted(result.candidates, key=lambda candidate: candidate.sort_key) == list(
        result.candidates
    )


def test_evaluate_is_deterministic_for_any_input_order(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    items = [_discrepancy(n, detected_at=DETECTED + timedelta(minutes=n % 3)) for n in range(1, 7)]
    forward = evaluate(_context(thresholds, window, *items))
    backward = evaluate(_context(thresholds, window, *reversed(items)))
    assert forward == backward
    keys = [candidate.rank_key for candidate in forward.candidates]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# The Google-to-CRM link (AAP 0.3.3, 0.5.4.1)
# ---------------------------------------------------------------------------


def _google_crm(*, linked: bool) -> CrmData:
    """HubSpot's primary dataset with the Google-to-CRM link verified, or not configured."""
    google = (
        GoogleCrmBinding(
            available=True,
            campaign_property="google_campaign_id",
            touch_date_property="google_touch_at",
        )
        if linked
        else GoogleCrmBinding(available=False, reason=UNLINKED_REASON)
    )
    bindings = CrmBindings.unavailable("CRM stage bindings not verified", at=NOW)
    return CrmData(HUBSPOT, replace(bindings, google=google))


def _sourced_discrepancies() -> tuple[DiscrepancyInfo, ...]:
    """A Google campaign, a Google shared budget and a LinkedIn campaign, one discrepancy each."""
    return (
        _discrepancy(1),
        _discrepancy(
            2,
            subject_type="budget",
            subject_id=SHARED_BUDGET,
            subject_name="Pool A",
            shape="budget",
            kind="budget",
            decided_value=_google_money("100.000000"),
            platform_value=_google_money("120.000000"),
        ),
        _discrepancy(
            3, subject_id=LINKEDIN_CAMPAIGN, subject_name="Lead gen EU", source="linkedin"
        ),
    )


@pytest.mark.parametrize(
    ("google_crm", "expected"),
    [
        pytest.param(None, UNLINKED_FLAG, id="no-google-crm-dataset"),
        pytest.param(
            _google_crm(linked=False),
            Flag(FLAG_NO_CRM_LINK, f"No CRM link for Google campaigns: {UNLINKED_REASON}"),
            id="link-not-configured",
        ),
    ],
)
def test_google_c0_items_carry_no_crm_link_without_the_verified_link(
    thresholds: Thresholds, window: AnalysisWindow, google_crm: CrmData | None, expected: Flag
) -> None:
    """AAP 0.3.3: Google items, C0 included, carry no_crm_link; LinkedIn items never do."""
    ctx = _context(thresholds, window, *_sourced_discrepancies(), google_crm=google_crm)
    result = evaluate(ctx)
    flags = {item.evidence.extra["discrepancy_id"]: item.flags for item in result.candidates}
    assert flags == {1: (expected,), 2: (expected,), 3: ()}
    # C0 counts no section finding; the Campaigns rules own the no_crm_link finding.
    assert result.findings == ()


def test_c0_items_carry_no_crm_link_on_no_source_once_the_link_is_verified(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    """AAP 0.5.4.1: with both Google CRM properties verified no item carries no_crm_link."""
    ctx = _context(
        thresholds, window, *_sourced_discrepancies(), google_crm=_google_crm(linked=True)
    )
    result = evaluate(ctx)
    assert [item.entity_ref.source for item in result.candidates] == [
        "google",
        "google",
        "linkedin",
    ]
    assert all(item.flags == () for item in result.candidates)


@pytest.mark.parametrize(
    ("changes", "action", "reason", "unit", "currencies", "resolutions"),
    [
        pytest.param(
            {},
            "Resolve: platform shows Paused, last decided Active",
            "Platform status differs from the last decided status.",
            "status",
            (None, None),
            PLATFORM_OR_DECISION,
            id="status-own-budget",
        ),
        pytest.param(
            {"subject_id": MEMBER_CAMPAIGN, "shape": "shared_member", "state": "awaiting_platform"},
            "Resolve: platform shows Paused, last decided Active",
            "Platform status differs from the last decided status."
            " Awaiting a matching platform snapshot.",
            "status",
            (None, None),
            PLATFORM_OR_DECISION,
            id="status-shared-member-awaiting",
        ),
        pytest.param(
            {
                "kind": "budget",
                "decided_value": _google_money("50.000000"),
                "platform_value": _google_money("50.000001"),
            },
            "Resolve: platform shows daily budget USD 50.000001, "
            "last decided daily budget USD 50.00",
            "Platform budget differs from the last decided budget.",
            "budget",
            ("USD", "USD"),
            PLATFORM_OR_DECISION,
            id="budget-google-one-micro",
        ),
        pytest.param(
            {
                "subject_id": LINKEDIN_CAMPAIGN,
                "source": "linkedin",
                "kind": "budget",
                "decided_value": _linkedin_money("40.00"),
                "platform_value": _linkedin_money("40.01"),
            },
            "Resolve: platform shows daily budget EUR 40.01, last decided daily budget EUR 40.00",
            "Platform budget differs from the last decided budget.",
            "budget",
            ("EUR", "EUR"),
            PLATFORM_OR_DECISION,
            id="budget-linkedin-one-minor-unit",
        ),
        pytest.param(
            {
                "subject_id": MEMBER_CAMPAIGN,
                "shape": "shared_member",
                "kind": "budget",
                "decided_value": {"budget_id": SHARED_BUDGET, "budget_external_id": "123"},
                "platform_value": {"budget_id": OTHER_SHARED_BUDGET, "budget_external_id": None},
            },
            "Resolve: platform shows shared budget 789, last decided shared budget 123",
            "Campaign is assigned to a different shared budget than decided.",
            "budget_assignment",
            (None, None),
            PLATFORM_OR_DECISION,
            id="budget-member-moved",
        ),
        pytest.param(
            {
                "kind": "budget",
                "decided_value": {"budget_id": None, "budget_external_id": None},
                "platform_value": {"budget_id": SHARED_BUDGET, "budget_external_id": "123"},
            },
            "Resolve: platform shows shared budget 123, last decided own budget (not shared)",
            "Campaign's budget assignment differs from the last decided assignment.",
            "budget_assignment",
            (None, None),
            PLATFORM_OR_DECISION,
            id="budget-own-campaign-joined-shared",
        ),
        pytest.param(
            {
                "kind": "budget",
                "decided_value": {"budget_id": None, "budget_external_id": None},
                "platform_value": {"budget_id": NON_SHARED_BUDGET},
            },
            "Resolve: platform shows non-shared budget 456, last decided own budget (not shared)",
            "Campaign's budget assignment differs from the last decided assignment.",
            "budget_assignment",
            (None, None),
            PLATFORM_OR_DECISION,
            id="budget-own-campaign-other-own-budget",
        ),
        pytest.param(
            {
                "subject_type": "budget",
                "subject_id": SHARED_BUDGET,
                "shape": "budget",
                "kind": "budget",
                "decided_value": _google_money("100.000000"),
                "platform_value": _google_money("120.000000"),
            },
            "Resolve: platform shows daily budget USD 120.00, last decided daily budget USD 100.00",
            "Platform budget differs from the last decided budget.",
            "budget",
            ("USD", "USD"),
            PLATFORM_OR_DECISION,
            id="budget-shared-budget-subject",
        ),
        pytest.param(
            {
                "kind": "untracked",
                "decided_value": None,
                "platform_value": {"status": "active", **_google_money("25.000000")},
            },
            "Resolve: platform shows Active · daily budget USD 25.00, "
            f"last decided {NO_DECISION_TEXT}",
            "Entity first seen after this dataset's baseline, with no decision.",
            "state",
            ("USD", None),
            ADOPT_OR_INTEND,
            id="untracked-own-budget-campaign",
        ),
        pytest.param(
            {
                "subject_id": MEMBER_CAMPAIGN,
                "shape": "shared_member",
                "kind": "untracked",
                "decided_value": None,
                "platform_value": {"status": "active", "budget_id": SHARED_BUDGET},
            },
            f"Resolve: platform shows Active · shared budget 123, last decided {NO_DECISION_TEXT}",
            "Entity first seen after this dataset's baseline, with no decision.",
            "state",
            (None, None),
            ADOPT_OR_INTEND,
            id="untracked-shared-member",
        ),
        pytest.param(
            {
                "subject_type": "budget",
                "subject_id": SHARED_BUDGET,
                "shape": "budget",
                "kind": "untracked",
                "decided_value": _google_money("80.000000"),
                "platform_value": _google_money("60.000000"),
                "state": "awaiting_platform",
            },
            "Resolve: platform shows daily budget USD 60.00, last decided daily budget USD 80.00",
            "Entity first seen after this dataset's baseline; its intended state is recorded."
            " Awaiting a matching platform snapshot.",
            "state",
            ("USD", "USD"),
            ADOPT_OR_INTEND,
            id="untracked-budget-record-intended",
        ),
        pytest.param(
            {
                "kind": "missing",
                "decided_value": {"status": "active", **_google_money("50.000000")},
                "platform_value": None,
            },
            f"Resolve: platform shows {NOT_RETURNED_TEXT}, last decided Active · "
            "daily budget USD 50.00",
            "A tracked entity was not returned by a complete enumeration.",
            "state",
            (None, "USD"),
            REMOVAL,
            id="missing-campaign",
        ),
        pytest.param(
            {
                "subject_type": "budget",
                "subject_id": SHARED_BUDGET,
                "shape": "budget",
                "kind": "missing",
                "decided_value": _google_money("100.000000"),
                "platform_value": None,
                "state": "awaiting_platform",
            },
            f"Resolve: platform shows {NOT_RETURNED_TEXT}, last decided daily budget USD 100.00",
            "A tracked entity was not returned by a complete enumeration."
            " Awaiting a matching platform snapshot.",
            "state",
            (None, "USD"),
            REMOVAL,
            id="missing-budget-decision-correct",
        ),
        pytest.param(
            {
                "subject_id": MEMBER_CAMPAIGN,
                "shape": "shared_member",
                "kind": "missing",
                "decided_value": {
                    "status": "active",
                    "budget_id": SHARED_BUDGET,
                    "budget_external_id": "123",
                },
                "platform_value": None,
            },
            f"Resolve: platform shows {NOT_RETURNED_TEXT}, last decided Active · shared budget 123",
            "A tracked entity was not returned by a complete enumeration.",
            "state",
            (None, None),
            REMOVAL,
            id="missing-shared-member",
        ),
        pytest.param(
            {"decided_value": {"status": "ended", "tombstone": True}},
            f"Resolve: platform shows Paused, last decided {TOMBSTONE_TEXT} · Ended",
            "An entity whose removal was acknowledged was returned by a complete enumeration.",
            "status",
            (None, None),
            PLATFORM_OR_DECISION,
            id="tombstone-campaign-reappeared",
        ),
        pytest.param(
            {
                "subject_type": "budget",
                "subject_id": SHARED_BUDGET,
                "shape": "budget",
                "kind": "budget",
                "decided_value": {**_google_money("100.000000"), "tombstone": True},
                "platform_value": _google_money("100.000000"),
            },
            f"Resolve: platform shows daily budget USD 100.00, last decided {TOMBSTONE_TEXT}",
            "An entity whose removal was acknowledged was returned by a complete enumeration.",
            "budget",
            ("USD", None),
            PLATFORM_OR_DECISION,
            id="tombstone-budget-reappeared",
        ),
    ],
)
def test_each_kind_and_subject_renders_its_item(
    thresholds: Thresholds,
    window: AnalysisWindow,
    changes: dict[str, Any],
    action: str,
    reason: str,
    unit: str,
    currencies: tuple[str | None, str | None],
    resolutions: tuple[Resolution, ...],
) -> None:
    item = _discrepancy(42, **changes)
    candidate = _only(_context(thresholds, window, item))

    assert candidate.action == action
    assert candidate.reason == reason
    platform, decided = candidate.evidence.metrics
    assert (platform.key, platform.label) == ("platform", "Platform")
    assert (decided.key, decided.label) == ("decided", "Last decided")
    assert (platform.unit, decided.unit) == (unit, unit)
    assert (platform.currency, decided.currency) == currencies
    assert (
        candidate.action
        == f"Resolve: platform shows {platform.value}, last decided {decided.value}"
    )
    assert tuple(candidate.evidence.extra["resolutions"]) == resolutions
    assert candidate.evidence.extra["resolutions"] == tuple(
        offered_resolutions(item.kind, item.shape)
    )
    assert candidate.evidence.extra["decided"] == (
        None if item.decided_value is None else dict(item.decided_value)
    )
    assert candidate.evidence.extra["platform"] == (
        None if item.platform_value is None else dict(item.platform_value)
    )
    assert candidate.depends_on == (DecisionSubject(item.subject_type, item.subject_id),)
    assert candidate.decision_subject is None
    assert candidate.pinned is True


def test_evidence_records_the_entity_dataset_and_frozen_values(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    platform_value = {"budget_id": OTHER_SHARED_BUDGET, "budget_external_id": None}
    item = _discrepancy(
        6,
        subject_id=MEMBER_CAMPAIGN,
        shape="shared_member",
        kind="budget",
        decided_value={"budget_id": SHARED_BUDGET, "budget_external_id": "123"},
        platform_value=platform_value,
        detected_at=datetime(2026, 10, 5, 23, 15, tzinfo=timezone(timedelta(hours=2))),
    )
    candidate = _only(_context(thresholds, window, item))
    evidence = candidate.evidence

    assert json_value(evidence.window) == window.to_json()
    assert evidence.thresholds == {}
    # The coverage date carries the Google day boundary the window records (AAP 0.5.5).
    assert evidence.datasets == (
        DatasetStatus("google.entities", "complete", COVERED_THROUGH, window.boundary("google")),
    )
    assert window.boundary("google") == "America/New_York"
    assert evidence.provenance == (ProvenanceEntry("google", 11, "live", "primary"),)
    assert evidence.baselines == ()
    assert evidence.sample is None
    assert evidence.test is None
    assert evidence.extra == {
        "discrepancy_id": 6,
        "kind": "budget",
        "shape": "shared_member",
        "state": "open",
        "detected_at": "2026-10-05T21:15:00+00:00",
        "decided": {"budget_id": SHARED_BUDGET, "budget_external_id": "123"},
        # The stored value is kept as stored; only the display text is enriched.
        "platform": {"budget_id": OTHER_SHARED_BUDGET, "budget_external_id": None},
        "resolutions": ("platform_correct", "decision_correct"),
    }
    # No headline yet: precedence appends ``held_recommendations`` once every hold is final.
    assert [metric.key for metric in evidence.metrics] == ["platform", "decided"]
    assert _platform_text(candidate) == "shared budget 789"
    assert platform_value == {"budget_id": OTHER_SHARED_BUDGET, "budget_external_id": None}


def test_linkedin_item_reads_the_linkedin_entities_dataset(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    item = _discrepancy(7, subject_id=LINKEDIN_CAMPAIGN, source="linkedin")
    candidate = _only(_context(thresholds, window, item))

    assert candidate.required_datasets == ("linkedin.entities",)
    assert candidate.evidence.datasets == (DatasetStatus("linkedin.entities", "unknown"),)
    assert candidate.evidence.provenance == (ProvenanceEntry("linkedin", 12, "live", "primary"),)
    assert candidate.entity_ref.key == "linkedin:campaign:333"
    assert candidate.entity_ref.name == "Brand search"


# ---------------------------------------------------------------------------
# evaluate: subjects, names, ids and currencies from the context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject_id", "value", "expected_text", "expected_currency"),
    [
        pytest.param(
            LINKEDIN_CAMPAIGN,
            {"daily_budget": "40.00"},
            "daily budget EUR 40.00",
            "EUR",
            id="account-currency",
        ),
        pytest.param(
            ORPHAN_CAMPAIGN,
            {"daily_budget": "40.00"},
            "daily budget GBP 40.00",
            "GBP",
            id="campaign-currency-without-account",
        ),
        pytest.param(
            404,
            {"daily_budget": "40.00"},
            "daily budget 40.00",
            None,
            id="unknown-campaign-no-currency",
        ),
        pytest.param(
            OWN_CAMPAIGN,
            {"daily_budget": "40.00", "currency": "CHF"},
            "daily budget CHF 40.00",
            "CHF",
            id="value-currency-wins",
        ),
    ],
)
def test_campaign_currency_fallback(
    thresholds: Thresholds,
    window: AnalysisWindow,
    subject_id: int,
    value: dict[str, Any],
    expected_text: str,
    expected_currency: str | None,
) -> None:
    item = _discrepancy(
        8,
        subject_id=subject_id,
        kind="budget",
        decided_value=value,
        platform_value={**value, "daily_budget": "45.00"},
    )
    candidate = _only(_context(thresholds, window, item))

    assert _decided_text(candidate) == expected_text
    assert candidate.evidence.metric("decided").currency == expected_currency
    assert candidate.evidence.metric("platform").currency == expected_currency


def test_budget_subject_currency_and_default_name(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    jpy = _budget(50, "900", shared=True, currency="JPY")
    item = _discrepancy(
        9,
        subject_type="budget",
        subject_id=50,
        subject_name="  ",
        shape="budget",
        kind="budget",
        decided_value={"daily_budget": "5000", "amount_micros": 5_000_000_000},
        platform_value={"daily_budget": "6000", "amount_micros": 6_000_000_000},
    )
    ctx = _context(thresholds, window, item, budgets={50: jpy})
    candidate = _only(ctx)

    assert candidate.action == (
        "Resolve: platform shows daily budget JPY 6,000, last decided daily budget JPY 5,000"
    )
    assert candidate.entity_ref.type == "budget"
    assert candidate.entity_ref.external_id == "900"
    assert candidate.entity_ref.name == "Shared budget 900"
    assert candidate.depends_on == (DecisionSubject("budget", 50),)


@pytest.mark.parametrize(
    ("subject_type", "subject_id", "shape", "kind", "external_id", "name"),
    [
        pytest.param(
            "campaign", 404, "own_budget", "status", "internal-404", "Campaign #404", id="campaign"
        ),
        pytest.param("budget", 505, "budget", "budget", "internal-505", "Budget #505", id="budget"),
        pytest.param(
            "campaign",
            BLANK_ID_CAMPAIGN,
            "own_budget",
            "status",
            f"internal-{BLANK_ID_CAMPAIGN}",
            "Blank id",
            id="blank-external-id",
        ),
    ],
)
def test_subject_missing_from_the_context_is_still_shown(
    thresholds: Thresholds,
    window: AnalysisWindow,
    subject_type: SubjectType,
    subject_id: int,
    shape: DecisionShape,
    kind: DiscrepancyKind,
    external_id: str,
    name: str,
) -> None:
    values = (
        ({"status": "active"}, {"status": "paused"})
        if kind == "status"
        else (_google_money("10.000000"), _google_money("11.000000"))
    )
    item = _discrepancy(
        10,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_name="",
        shape=shape,
        kind=kind,
        decided_value=values[0],
        platform_value=values[1],
    )
    candidate = _only(_context(thresholds, window, item))

    assert candidate.entity_ref.external_id == external_id
    assert candidate.entity_ref.name == name
    assert candidate.depends_on == (DecisionSubject(subject_type, subject_id),)


@pytest.mark.parametrize(
    ("platform_value", "expected"),
    [
        pytest.param(
            {"budget_id": SHARED_BUDGET, "budget_external_id": "999"},
            "shared budget 999",
            id="stored-external-id-kept",
        ),
        pytest.param(
            {"budget_id": SHARED_BUDGET, "budget_external_id": " "},
            "shared budget 123",
            id="blank-external-id-filled",
        ),
        pytest.param(
            {"budget_id": SHARED_BUDGET, "explicitly_shared": False},
            "non-shared budget 123",
            id="stored-sharing-kept",
        ),
        pytest.param({"budget_id": 77}, "budget (internal id 77)", id="budget-not-in-context"),
    ],
)
def test_assignment_display_enrichment(
    thresholds: Thresholds,
    window: AnalysisWindow,
    platform_value: dict[str, Any],
    expected: str,
) -> None:
    item = _discrepancy(
        11,
        subject_id=MEMBER_CAMPAIGN,
        shape="shared_member",
        kind="budget",
        decided_value={"budget_id": OTHER_SHARED_BUDGET},
        platform_value=platform_value,
    )
    candidate = _only(_context(thresholds, window, item))

    assert _platform_text(candidate) == expected
    assert _decided_text(candidate) == "shared budget 789"
    assert candidate.evidence.extra["platform"] == platform_value


# ---------------------------------------------------------------------------
# evaluate: contract violations detection never produces
# ---------------------------------------------------------------------------


def test_source_without_decision_subjects_is_rejected(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    item = _discrepancy(12, source="hubspot")
    with pytest.raises(ValueError, match="has no decision subjects"):
        evaluate(_context(thresholds, window, item))


def test_pair_without_resolutions_is_rejected(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    item = _discrepancy(
        13,
        subject_type="budget",
        subject_id=SHARED_BUDGET,
        shape="budget",
        kind="status",
    )
    with pytest.raises(ValueError, match="no resolution is defined"):
        evaluate(_context(thresholds, window, item))
