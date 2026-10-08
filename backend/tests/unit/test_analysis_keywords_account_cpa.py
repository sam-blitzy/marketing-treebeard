"""Unit tests for K1's account-wide withholding when the account cost/conversion is unavailable.

AAP 0.5.4.3 K1 compares a term's spend with the account cost per conversion, and AAP 0.11.1
keeps absent metrics unavailable: when a campaign day did not report conversions the account
total is unknown, and when no campaign day was recorded there is no total at all; the withheld
line says so instead of claiming 0 conversions, which only a reported zero earns. Contexts are
built in memory; no database and no network.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ads_engine.analysis import keywords
from ads_engine.analysis.context import (
    K1,
    AccountInfo,
    AnalysisContext,
    CampaignInfo,
    DatasetRef,
    DayFact,
    RateBaseline,
    SearchTermRow,
)
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.analysis.window import AnalysisWindow, SourceDayInfo

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
NY = "America/New_York"
START, END = date(2026, 9, 25), date(2026, 10, 1)
ACCOUNT = AccountInfo(1, "google", "1234567890", "Google Ads", "USD", NY)


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    return Thresholds.load(CONFIG_DIR)


def _campaign() -> CampaignInfo:
    return CampaignInfo(
        id=1,
        source="google",
        account_id=ACCOUNT.id,
        external_id="900001",
        name="Google 1",
        status="active",
        raw_status="ENABLED",
        objective_or_channel="SEARCH",
        objective_group="google",
        campaign_group_id=None,
        budget_id=None,
        daily_budget=Decimal("100.00"),
        total_budget=None,
        budget_period="DAILY",
        currency="USD",
    )


def _context(thresholds: Thresholds, conversions: tuple[str | None, ...]) -> AnalysisContext:
    """One Google campaign spending 100.00 a day for a week, with ``conversions`` per day."""
    days = tuple(
        DayFact(
            date=START + timedelta(days=offset),
            cost=Decimal("100"),
            impressions=1_000,
            clicks=50,
            conversions=None if value is None else Decimal(value),
        )
        for offset, value in enumerate(conversions)
    )
    term = SearchTermRow(
        ad_group_id=1,
        campaign_id=1,
        search_term="free trial",
        term_status="NONE",
        matched_criterion_id=None,
        matched_keyword_text=None,
        matched_match_type=None,
        cost=Decimal("80"),
        clicks=40,
        impressions=400,
        conversions=Decimal(0),
    )
    window = AnalysisWindow.custom(
        START, END, config=thresholds, sources=SourceDayInfo(NY, ("google",)), now=NOW
    )
    return AnalysisContext(
        window=window,
        thresholds=thresholds,
        now=NOW,
        datasets={"google": DatasetRef(1, "google", "simulated")},
        accounts={ACCOUNT.id: ACCOUNT},
        campaigns={1: _campaign()},
        campaign_days={1: days},
        search_terms=(term,),
        k1_baseline=RateBaseline(Decimal(100), 1_000, START - timedelta(days=83), END),
    )


def _account_lines(ctx: AnalysisContext) -> list[tuple[str, object, object, str]]:
    return [
        (line.reason, line.observed, line.required, line.unit)
        for line in keywords.evaluate(ctx).withheld
        if line.rule_id == K1 and line.entity.type == "account"
    ]


def test_an_unreported_day_withholds_k1_as_unavailable_not_zero(thresholds: Thresholds) -> None:
    # Six days of 0 conversions and one unreported day: the account total is unknown.
    ctx = _context(thresholds, ("0", "0", "0", None, "0", "0", "0"))
    assert _account_lines(ctx) == [
        (
            "account cost/conversion unavailable: conversions unavailable on 1 of 7 days in "
            "the 7-day window",
            6,
            7,
            "days with conversions reported",
        )
    ]


def test_zero_reported_conversions_withhold_k1_with_the_zero_count(thresholds: Thresholds) -> None:
    ctx = _context(thresholds, ("0",) * 7)
    assert _account_lines(ctx) == [
        (
            "account cost/conversion unavailable: 0 conversions in the 7-day window",
            Decimal(0),
            None,
            "conversions",
        )
    ]


def test_no_window_fact_rows_withhold_k1_as_no_data_not_zero(thresholds: Thresholds) -> None:
    # A usable baseline and an eligible term, but no campaign day in the window: no total.
    ctx = _context(thresholds, ())
    assert _account_lines(ctx) == [
        (
            "account cost/conversion unavailable: no conversion data recorded for the account "
            "in the 7-day window",
            None,
            None,
            "conversions",
        )
    ]


def test_an_account_without_campaign_totals_is_described_as_no_data(
    thresholds: Thresholds,
) -> None:
    # An account with no Google campaign has no totals; evaluate never reaches it from a term.
    ctx = _context(thresholds, ("0",) * 7)
    account = keywords._google_accounts(ctx)[ACCOUNT.id]
    line = keywords._k1_without_cpa(ctx, account, None)
    assert (line.rule_id, line.entity, line.reason, line.observed, line.required, line.unit) == (
        K1,
        account.ref,
        "account cost/conversion unavailable: no conversion data recorded for the account "
        "in the 7-day window",
        None,
        None,
        "conversions",
    )


def test_a_complete_account_total_withholds_nothing_account_wide(thresholds: Thresholds) -> None:
    # 700 spend over 7 conversions: the term's 80 is below the 100 floor, so nothing fires.
    ctx = _context(thresholds, ("1",) * 7)
    assert _account_lines(ctx) == []
    assert [line for line in keywords.evaluate(ctx).withheld if line.rule_id == K1] == []
