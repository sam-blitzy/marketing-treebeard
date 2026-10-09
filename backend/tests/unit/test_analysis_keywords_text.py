"""Unit tests of the Keywords section's rendered text: count nouns and values beside a minimum.

AAP 0.5.4 renders each reason from its item's evidence "so the conclusion can never drift from
the numbers". These tests hold the K1, K2 and K3 text to the counts it cites where the
catalogue's scenarios never reach: a count of exactly 1 takes the singular noun ("1
conversion", "1 click", "1 day"), and 0, fractional and larger counts take the plural. They
also hold the search-term coverage figures to the minimum they were compared with: a value is
shown on the same side of the configured minimum as the exact comparison put it. Contexts are
a catalogue scenario's, adjusted in memory with ``dataclasses.replace``, or built in memory; no
database and no network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

from ads_engine.analysis import keywords
from ads_engine.analysis.context import (
    FINDING_K1_NO_BASELINE,
    FINDING_SEARCH_TERM_COVERAGE,
    FLAG_SEARCH_TERM_COVERAGE,
    K1,
    K2,
    K3,
    AccountInfo,
    AnalysisContext,
    CampaignInfo,
    DatasetRef,
    DayFact,
    KeywordDay,
    KeywordInfo,
    RateBaseline,
    RuleResult,
    SearchTermRow,
)
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.analysis.window import AnalysisWindow, SourceDayInfo

type ContextFactory = Callable[..., AnalysisContext]
"""The root ``make_context`` fixture: ``make_context(scenario, *, thresholds=None, now=None)``."""

type ThresholdsFactory = Callable[..., Thresholds]
"""The root ``make_thresholds`` fixture: ``make_thresholds(overrides) -> Thresholds``."""

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
"""The run instant: the window's latest complete day is 2026-10-01 in New York and UTC."""

NY = "America/New_York"
END = date(2026, 10, 1)
ACCOUNT = AccountInfo(1, "google", "1234567890", "Google Ads", "USD", NY)


# ---------------------------------------------------------------------------------------------
# In-memory contexts
# ---------------------------------------------------------------------------------------------


def _campaign() -> CampaignInfo:
    """One active Google search campaign in USD."""
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


def _term(term: str, *, cost: str, clicks: int, conversions: str) -> SearchTermRow:
    """A ``NONE`` search term of ad group 1 with its window totals."""
    return SearchTermRow(
        ad_group_id=1,
        campaign_id=1,
        search_term=term,
        term_status="NONE",
        matched_criterion_id=None,
        matched_keyword_text=None,
        matched_match_type=None,
        cost=Decimal(cost),
        clicks=clicks,
        impressions=clicks * 10,
        conversions=Decimal(conversions),
    )


def _context(
    thresholds: Thresholds,
    *,
    days: int,
    conversions: str | None = "5",
    quality_score_days: frozenset[int] = frozenset(),
    baseline: RateBaseline | None = None,
) -> AnalysisContext:
    """A Google account over a ``days``-day window ending on :data:`END`.

    Each window day the campaign spends 100.00 over 50 clicks with ``conversions`` (``None``
    leaves them unreported), and its one keyword spends 10.00 over 20 clicks with 1 conversion,
    with a quality score of 7 on the day offsets in ``quality_score_days``. The search-term
    report holds a zero-conversion term K1 can block (80.00 over 40 clicks) and a converting
    term K2 can expand (1 conversion at 10.00). The K1 baseline is 100 conversions over 1,000
    clicks (n* = 30) unless ``baseline`` is given.
    """
    start = END - timedelta(days=days - 1)
    dates = [start + timedelta(days=offset) for offset in range(days)]
    campaign_days = tuple(
        DayFact(
            date=day,
            cost=Decimal("100"),
            impressions=1_000,
            clicks=50,
            conversions=None if conversions is None else Decimal(conversions),
        )
        for day in dates
    )
    keyword_days = tuple(
        KeywordDay(
            keyword_id=1,
            date=day,
            cost=Decimal("10"),
            clicks=20,
            impressions=200,
            conversions=Decimal(1),
            historical_quality_score=7 if offset in quality_score_days else None,
        )
        for offset, day in enumerate(dates)
    )
    window = AnalysisWindow.custom(
        start, END, config=thresholds, sources=SourceDayInfo(NY, ("google",)), now=NOW
    )
    return AnalysisContext(
        window=window,
        thresholds=thresholds,
        now=NOW,
        datasets={"google": DatasetRef(1, "google", "simulated")},
        accounts={ACCOUNT.id: ACCOUNT},
        campaigns={1: _campaign()},
        keywords={
            1: KeywordInfo(
                id=1,
                ad_group_id=1,
                campaign_id=1,
                criterion_id="1001",
                text="crm software",
                match_type="PHRASE",
                status="ENABLED",
            )
        },
        campaign_days={1: campaign_days},
        keyword_days={1: keyword_days},
        search_terms=(
            _term("free trial", cost="80", clicks=40, conversions="0"),
            _term("crm pricing", cost="10", clicks=5, conversions="1"),
        ),
        k1_baseline=(
            RateBaseline(Decimal(100), 1_000, END - timedelta(days=89), END)
            if baseline is None
            else baseline
        ),
    )


def _reasons(result: RuleResult, rule_id: str) -> list[str]:
    """The reasons of ``rule_id``'s items, in the order the rule emitted them."""
    return [item.reason for item in result.candidates if item.rule_id == rule_id]


def _withheld(result: RuleResult, rule_id: str) -> list[str]:
    """The reasons of ``rule_id``'s withheld lines."""
    return [line.reason for line in result.withheld if line.rule_id == rule_id]


# ---------------------------------------------------------------------------------------------
# Count nouns
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, "1 click"),
        (Decimal(1), "1 click"),
        (0, "0 clicks"),
        (Decimal(0), "0 clicks"),
        (Decimal("0.5"), "0.5 clicks"),
        (Decimal("1.5"), "1.5 clicks"),
        (2, "2 clicks"),
        (5_661, "5,661 clicks"),
        (Decimal(3000), "3,000 clicks"),
    ],
)
def test_counted_takes_the_singular_only_for_a_count_of_exactly_one(
    value: Decimal | int, expected: str
) -> None:
    """Only exactly 1 is singular; 0, fractions and larger counts keep the plural noun."""
    assert keywords._counted(value, "click", "clicks") == expected


@pytest.mark.parametrize(
    ("conversions", "clicks", "minimum", "detail", "needs", "line"),
    [
        (
            Decimal(1),
            1,
            10,
            "has 1 conversion over 1 click, below the 10 required",
            "at least 10 baseline conversions and",
            "baseline conversions 1 of 10 required",
        ),
        (
            Decimal(0),
            1,
            1,
            "has 0 conversions over 1 click, below the 1 required",
            "at least 1 baseline conversion and",
            "baseline conversions 0 of 1 required",
        ),
        (
            Decimal("0.5"),
            1,
            1,
            "has 0.5 conversions over 1 click, below the 1 required",
            "at least 1 baseline conversion and",
            "baseline conversions 0.5 of 1 required",
        ),
        (
            Decimal(1),
            0,
            1,
            "has 1 conversion but no clicks",
            "at least 1 baseline conversion and",
            "baseline rate unavailable: the trailing 90-day baseline has 1 conversion but no "
            "clicks",
        ),
    ],
)
def test_k1_no_baseline_finding_agrees_each_noun_with_its_count(
    make_context: ContextFactory,
    make_thresholds: ThresholdsFactory,
    conversions: Decimal,
    clicks: int,
    minimum: int,
    detail: str,
    needs: str,
    line: str,
) -> None:
    """K1 unusable baseline: "1 conversion", "1 click" and "1 baseline conversion" agree."""
    configured = make_thresholds({"k1.min_baseline_conversions": minimum})
    ctx = make_context("k1.no_baseline.does_not_fire", thresholds=configured)
    assert ctx.k1_baseline is not None
    adjusted = replace(
        ctx, k1_baseline=replace(ctx.k1_baseline, conversions=conversions, clicks=clicks), cache={}
    )
    result = keywords.evaluate(adjusted)
    (finding,) = (item for item in result.findings if item.code == FINDING_K1_NO_BASELINE)
    assert f"the trailing 90-day baseline {detail}." in finding.message
    assert needs in finding.message
    assert _withheld(result, K1) == [line]


def test_a_one_day_window_says_one_day_and_one_conversion(
    make_thresholds: ThresholdsFactory,
) -> None:
    """K1 and K2 reasons and the K3 window line say "1 day"; K2 says "1 conversion"."""
    thresholds = make_thresholds({"k2.min_conversions": 1})
    result = keywords.evaluate(_context(thresholds, days=1))
    assert _reasons(result, K1) == [
        "Spent USD 80.00 over 1 day with 0 conversions across 40 clicks, at or above the "
        "minimum of 30 clicks for the account's conversion rate of 0.1 (conversions ÷ clicks), "
        "and at least 1x the account cost/conversion of USD 20.00."
    ]
    (k2,) = _reasons(result, K2)
    assert k2.startswith(
        "1 conversion at USD 10.00 per conversion over 1 day, at or below 1x the campaign's "
        "USD 20.00 per conversion."
    )
    assert _withheld(result, K3) == ["window 1 day of 14 required"]


def test_a_one_day_window_with_unreported_conversions_says_one_of_one_day(
    make_thresholds: ThresholdsFactory,
) -> None:
    """K1 account line: the unreported days agree with the window's 1 day."""
    result = keywords.evaluate(_context(make_thresholds({}), days=1, conversions=None))
    assert (
        "account cost/conversion unavailable: conversions unavailable on 1 of 1 day in the "
        "1-day window"
    ) in _withheld(result, K1)


def test_a_one_day_baseline_span_says_one_day(make_thresholds: ThresholdsFactory) -> None:
    """K1: a missing 1-day baseline says "the trailing 1 day"; its coverage "1 of 1 day"."""
    thresholds = make_thresholds({"k1.baseline_days": 1})
    ctx = replace(_context(thresholds, days=7), k1_baseline=None, cache={})
    result = keywords.evaluate(ctx)
    (finding,) = (item for item in result.findings if item.code == FINDING_K1_NO_BASELINE)
    assert finding.message.startswith(
        "Block candidates withheld account-wide: no Google campaign facts in the trailing 1 day. "
    )
    assert _withheld(result, K1) == [
        "baseline unavailable: no Google campaign facts in the trailing 1 day"
    ]

    coverage = keywords._BaselineCoverage(
        start=END,
        end=END,
        days=1,
        recorded=True,
        covered_from=END,
        covered_through=END,
        completeness="incomplete",
        reason=None,
    )
    flag = coverage.flag()
    assert flag is not None
    assert "of the 1-day baseline 2026-10-01 to 2026-10-01 (1 of 1 day covered)" in flag.message


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        (
            1,
            (
                "Quality score, first 1 day (mean of daily scores)",
                "Quality score, last 1 day (mean of daily scores)",
            ),
        ),
        (
            7,
            (
                "Quality score, first 7 days (mean of daily scores)",
                "Quality score, last 7 days (mean of daily scores)",
            ),
        ),
    ],
)
def test_k3_quality_score_labels_agree_with_the_span(span: int, expected: tuple[str, str]) -> None:
    """K3: the daily quality-score labels name "1 day" for a one-day span."""
    assert keywords._qs_labels("daily", span) == expected


def test_k3_quality_score_shortfall_agrees_with_one_required_day(
    make_thresholds: ThresholdsFactory,
) -> None:
    """K3: a one-day span needing one scored day reads "0 of 1 required day in the last 1 day"."""
    overrides: dict[str, Any] = {
        "k3.min_window_days": 7,
        "k3.qs_span_days": 1,
        "k3.min_qs_days_per_span": 1,
    }
    ctx = _context(make_thresholds(overrides), days=7, quality_score_days=frozenset({0}))
    result = keywords.evaluate(ctx)
    assert _reasons(result, K3) == []
    assert _withheld(result, K3) == ["quality score on 0 of 1 required day in the last 1 day"]


# ---------------------------------------------------------------------------------------------
# Values shown beside the minimum they were compared with
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "minimum", "places", "expected"),
    [
        # Below: one place would reach the minimum, so places are added until it is below.
        (Fraction(884_654, 10_000), Decimal("88.48"), 1, Decimal("88.47")),
        (Fraction(79_996, 1_000), Decimal("80"), 1, Decimal("79.996")),
        (Fraction(884_654, 1_000_000), Decimal("0.8847"), 4, Decimal("0.88465")),
        # Equal: not below, so one place rounding down below the minimum is not enough.
        (Fraction(8_854, 100), Decimal("88.54"), 1, Decimal("88.54")),
        (Fraction(8_858, 100), Decimal("88.58"), 1, Decimal("88.6")),
        # Above: rounding down to the minimum itself is not below it.
        (Fraction(885_401, 10_000), Decimal("88.54"), 1, Decimal("88.54")),
        (Fraction(802, 10), Decimal("80"), 1, Decimal("80.2")),
    ],
)
def test_beside_minimum_shows_a_value_on_the_side_the_exact_comparison_found(
    value: Fraction, minimum: Decimal, places: int, expected: Decimal
) -> None:
    """Shown < minimum exactly when value < minimum, with the fewest places that achieve it."""
    shown = keywords._beside_minimum(value, minimum, places)
    assert shown == expected
    assert (Fraction(shown) < Fraction(minimum)) is (value < Fraction(minimum))
    assert str(shown) == str(expected), "trailing zeros are stripped, never added"


@pytest.mark.parametrize(
    "share",
    [
        Fraction(5_661, 7_161),
        Fraction(4, 5),
        Fraction(802, 1_000),
        Fraction(0),
        Fraction(1),
        Fraction(654_975, 739_459),
    ],
)
def test_beside_the_default_minimum_values_keep_their_usual_places(share: Fraction) -> None:
    """At the default 0.80 minimum, shares away from it show four places, percentages one."""
    minimum = Decimal("0.80")
    assert keywords._beside_minimum(share, minimum, 4) == keywords._ratio(share)
    percent = keywords._beside_minimum(share * 100, minimum * 100, 1)
    assert percent == keywords._plain(keywords._round(share * 100, 1))


def test_coverage_figures_agree_across_finding_flag_and_extra_beside_a_finer_minimum(
    make_thresholds: ThresholdsFactory,
) -> None:
    """Coverage 90.00 of 101.73 under a 0.8847 minimum shows 88.469% and 0.88469, below it.

    The exact share is 0.884694…: one place would show 88.5% and four places 0.8847, each at
    or above the minimum the text calls it below.
    """
    thresholds = make_thresholds({"keywords.min_search_term_coverage": Decimal("0.8847")})
    ctx = _context(thresholds, days=1)
    day = ctx.keyword_days[1][0]
    ctx = replace(ctx, keyword_days={1: (replace(day, cost=Decimal("101.73")),)}, cache={})
    result = keywords.evaluate(ctx)
    (finding,) = (item for item in result.findings if item.code == FINDING_SEARCH_TERM_COVERAGE)
    assert finding.evidence["below_minimum"] is True
    assert finding.evidence["coverage_percent"] == Decimal("88.469")
    assert finding.evidence["coverage_share"] == Decimal("0.88469")
    assert finding.evidence["min_share"] == Decimal("0.8847")
    assert finding.message.startswith(
        "Search terms cover 88.469% of keyword spend (USD 90.00 of USD 101.73) over the 1-day "
        "window."
    )
    assert " Coverage is below the 88.47% minimum, " in finding.message
    items = [item for item in result.candidates if item.rule_id in (K1, K2)]
    assert items
    for item in items:
        assert [flag.message for flag in item.flags if flag.code == FLAG_SEARCH_TERM_COVERAGE] == [
            "Low search-term coverage: 88.469% of keyword spend"
        ]
        assert item.evidence.extra["search_term_coverage"]["percent"] == Decimal("88.469")
