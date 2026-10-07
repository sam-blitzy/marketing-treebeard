"""Rules K1, K2 and K3: the Keywords to adjust section (Google).

AAP 0.5.4.3, 0.2.4 and FR-4, after the user's directives:

* **K1 Block / negative candidate** (group ``block``): "Search terms that have spent money with
  no conversions → flagged as block/negative-keyword candidates, with spend-wasted shown".
* **K2 Expansion candidate** (group ``expand``): "Terms that are converting well → flagged as
  expansion candidates, with suggested adjacent/related terms to try".
* **K3 Review** (group ``review``): "Keywords with declining quality score or rising
  cost-per-click without a matching rise in conversion → flagged for review".

Every item shows, in this order, the term (or keyword text), spend, conversions and
cost/conversion ("—" when conversions are 0), with its campaign and ad group in
``evidence.extra``, and a reason sentence rendered from a per-rule template that reads only the
item's own evidence, so the text can never drift from the numbers (AAP 0.5.4).

The section also carries one ``search_term_coverage`` finding per Google account: search-term
spend divided by keyword spend over the window. Google withholds low-volume search terms for
privacy, so search-term totals fall below keyword totals; below
``keywords.min_search_term_coverage`` every K1 and K2 item carries the
``search_term_coverage`` flag.

Adjacent terms (K2) come from the account's own search-term report, because Google's Keyword
Planner is not reachable through the n8n webhook (AAP 0.2.4, 0.7.2), and are labelled so.

Arithmetic (decision 6): every threshold comparison is exact, on :class:`fractions.Fraction`
values built from the stored ``Decimal`` amounts, so decimal rounding can neither create nor
hide an item. Evidence shows ``Decimal`` display values: money at the currency's minor unit,
ratios to four places, rates to four significant digits and quality-score means to two places.
Conversions are decimals throughout: data-driven attribution makes them fractional.

Purity: no database, network, settings or logging access. The module imports only the rule
contract (``context``), pure helpers of ``metrics``, the text primitives of ``text`` and the
``Thresholds`` type; it never imports ``precedence`` or ``runner``. Every threshold is read
from ``ctx.thresholds`` and copied into ``evidence.thresholds``.

Example:
    >>> from decimal import Decimal
    >>> k1_min_clicks(Decimal("0.01"), floor=10, rule_of_three=3)
    300
    >>> k1_min_clicks(Decimal("1.4"), floor=10, rule_of_three=3)
    10
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal
from fractions import Fraction
from typing import Any, Final, Literal

from ads_engine.analysis.context import (
    ENTITY_ACCOUNT,
    ENTITY_KEYWORD,
    ENTITY_SEARCH_TERM,
    FINDING_K1_NO_BASELINE,
    FINDING_SEARCH_TERM_COVERAGE,
    FLAG_SEARCH_TERM_COVERAGE,
    GROUP_BLOCK,
    GROUP_EXPAND,
    GROUP_REVIEW,
    K1,
    K2,
    K3,
    KEYWORDS,
    AnalysisContext,
    Baseline,
    CampaignInfo,
    Candidate,
    DecisionSubject,
    EntityRef,
    Evidence,
    Finding,
    Flag,
    KeywordDay,
    KeywordInfo,
    Metric,
    RateBaseline,
    RuleResult,
    Sample,
    SearchTermRow,
    Withheld,
    search_term_external_id,
)
from ads_engine.analysis.metrics import Totals, window_totals
from ads_engine.analysis.text import adjacent_terms, tokenize
from ads_engine.analysis.thresholds import Thresholds

__all__ = [
    "ADJACENT_TERMS_LABEL",
    "COVERAGE_THRESHOLD_KEY",
    "K1_ACTION",
    "K1_DATASETS",
    "K1_STATUSES",
    "K1_THRESHOLD_KEYS",
    "K2_ACTION_TEMPLATE",
    "K2_DATASETS",
    "K2_THRESHOLD_KEYS",
    "K3_ACTION",
    "K3_DATASETS",
    "K3_THRESHOLD_KEYS",
    "MIN_CLICKS_LABEL",
    "evaluate",
    "k1_min_clicks",
]

# ---------------------------------------------------------------------------
# Rule constants: actions, labels, datasets and threshold keys
# ---------------------------------------------------------------------------

K1_ACTION: Final = "Add as exact-match negative"
"""K1's action: always a block candidate; K1 never recommends pausing a keyword."""

K2_ACTION_TEMPLATE: Final = "Add as phrase-match keyword in {ad_group}"
"""K2's action, filled with the term's ad group name."""

K3_ACTION: Final = "Review keyword: bid, match type or landing page"
"""K3's action."""

MIN_CLICKS_LABEL: Final = "Minimum clicks for the account's conversion rate"
"""Label of K1's click gate n*: a heuristic on the rule-of-three scale; no statistical claim."""

ADJACENT_TERMS_LABEL: Final = "from your search-term report"
"""Label of K2's adjacent terms: Keyword Planner is unreachable through the n8n webhook."""

K1_STATUSES: Final[frozenset[str]] = frozenset({"NONE", "ADDED"})
"""Search-term statuses K1 evaluates; ``EXCLUDED`` and ``ADDED_EXCLUDED`` are already blocked."""

_K2_STATUSES: Final[frozenset[str]] = frozenset({"NONE"})
"""K2 evaluates terms that are neither keywords nor excluded."""

_EXCLUDED_STATUSES: Final[frozenset[str]] = frozenset({"EXCLUDED", "ADDED_EXCLUDED"})
"""Statuses that never qualify as an adjacent term."""

K1_DATASETS: Final[tuple[str, ...]] = ("google.search_terms", "google.metrics_daily")
"""The datasets K1 reads; its items' ``stale`` and ``incomplete`` flags derive from them."""

K2_DATASETS: Final[tuple[str, ...]] = ("google.search_terms", "google.metrics_daily")
"""The datasets K2 reads."""

K3_DATASETS: Final[tuple[str, ...]] = ("google.metrics_daily", "google.entities")
"""The datasets K3 reads: keyword daily facts and the Q-G7 keyword snapshots."""

COVERAGE_THRESHOLD_KEY: Final = "keywords.min_search_term_coverage"
"""The share of keyword spend below which K1 and K2 items carry ``search_term_coverage``."""

K1_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "k1.baseline_days",
    "k1.min_clicks_floor",
    "k1.rule_of_three",
    "k1.min_baseline_conversions",
    "k1.min_spend_cpa_multiple",
    COVERAGE_THRESHOLD_KEY,
)
"""Every threshold K1 applies, copied into each K1 item's ``evidence.thresholds``."""

K2_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "k2.min_conversions",
    "k2.max_cpa_ratio",
    "k2.adjacent_jaccard",
    "k2.adjacent_max_terms",
    "k2.adjacent_min_clicks",
    COVERAGE_THRESHOLD_KEY,
)
"""Every threshold K2 applies."""

K3_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "k3.min_window_days",
    "window.sustained_min_days",
    "k3.min_clicks_per_half",
    "k3.qs_span_days",
    "k3.min_qs_days_per_span",
    "k3.qs_drop",
    "k3.rise_ratio",
    "k3.min_first_half_conversions",
)
"""Every threshold K3 applies."""

_GOOGLE: Final = "google"

# ---------------------------------------------------------------------------
# Exact arithmetic and display values
# ---------------------------------------------------------------------------

_WIDE: Final = Context(prec=60, rounding=ROUND_HALF_EVEN)
"""Wide enough that converting and quantizing any stored amount or ratio is exact or correctly
rounded, independent of the caller's decimal context."""

_RATIO_PLACES: Final = 4
"""Decimal places of a displayed ratio (CPC ratio, cost/conversion ratio, coverage share)."""

_MEAN_PLACES: Final = 2
"""Decimal places of a displayed quality-score mean."""

_RATE_DIGITS: Final = 4
"""Significant digits of the displayed K1 baseline rate (conversions per click)."""

_PERCENT_PLACES: Final = 1
"""Decimal places of a displayed percentage."""

# ISO 4217 minor units, mirrored from ``ads_engine.decisions`` (outside this module's
# dependency set) so evidence and reasons show money at the precision the account bills in.
_ZERO_DECIMAL_CURRENCIES: Final[frozenset[str]] = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "UYI",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)
"""ISO 4217 currencies with no minor unit."""

_THREE_DECIMAL_CURRENCIES: Final[frozenset[str]] = frozenset(
    {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
)
"""ISO 4217 currencies whose minor unit is a thousandth."""


def _minor_unit(currency: str) -> int:
    """Return the ISO 4217 minor-unit exponent of ``currency`` (2 for most currencies)."""
    code = currency.strip().upper()
    if code in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in _THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def _exact(value: Decimal | int) -> Fraction:
    """Return a stored amount, count or configured value as an exact fraction."""
    return Fraction(value)


def _to_decimal(value: Fraction | Decimal | int) -> Decimal:
    """Return ``value`` as a ``Decimal``, correctly rounded at 60 significant digits."""
    if isinstance(value, Fraction):
        return _WIDE.divide(Decimal(value.numerator), Decimal(value.denominator))
    return Decimal(value)


def _plain(value: Decimal) -> Decimal:
    """Strip trailing zeros without exponent notation: ``3.000000`` → ``3``, ``300`` → ``300``."""
    normalized = value.normalize(_WIDE)
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        return normalized.quantize(Decimal(1), context=_WIDE)
    return normalized


def _round(value: Fraction | Decimal | int, places: int) -> Decimal:
    """Round ``value`` half-even to ``places`` decimal places."""
    return _to_decimal(value).quantize(Decimal(1).scaleb(-places), context=_WIDE)


def _money(value: Fraction | Decimal | int, currency: str) -> Decimal:
    """Return a money value at the currency's minor unit: ``Decimal("61")`` → ``61.00`` (USD)."""
    return _round(value, _minor_unit(currency))


def _ratio(value: Fraction) -> Decimal:
    """Return a displayed ratio: four places, trailing zeros stripped (``6/5`` → ``1.2``)."""
    return _plain(_round(value, _RATIO_PLACES))


def _mean(value: Fraction) -> Decimal:
    """Return a displayed quality-score mean: two places, trailing zeros stripped."""
    return _plain(_round(value, _MEAN_PLACES))


def _percent(share: Fraction) -> Decimal:
    """Return a share as a percentage with one decimal: ``5661/7161`` → ``79.1``."""
    return _plain(_round(share * 100, _PERCENT_PLACES))


def _significant(value: Fraction, digits: int = _RATE_DIGITS) -> Decimal:
    """Return ``value`` to ``digits`` significant digits: ``280/5661`` → ``0.04946``."""
    exact = _to_decimal(value)
    if exact == 0:
        return Decimal(0)
    exponent = exact.adjusted() - (digits - 1)
    return _plain(exact.quantize(Decimal(1).scaleb(exponent), context=_WIDE))


def _count(value: Decimal) -> Decimal:
    """Return a conversion count for display: exact, trailing zeros stripped (``0.3`` stays)."""
    return _plain(value)


def _money_text(value: Decimal, currency: str | None) -> str:
    """Render money as ``"USD 1,234.50"``; the value is already at the minor unit."""
    if currency is None:
        return format(value, ",f")
    places = _minor_unit(currency)
    return f"{currency} {value:,.{places}f}"


def _number_text(value: Decimal | int) -> str:
    """Render a count or ratio with thousands separators and no exponent: ``3000`` → ``3,000``."""
    if isinstance(value, int):
        return f"{value:,}"
    return format(value, ",f")


def _render(metric: Metric) -> str:
    """Render a metric value for a reason sentence: ``—`` for ``None``, money with currency."""
    value = metric.value
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if metric.unit == "currency":
        return _money_text(Decimal(value), metric.currency)
    return _number_text(value)


def _baseline(evidence: Evidence, key: str) -> Baseline:
    """Return the evidence baseline with ``key``.

    Raises:
        KeyError: When no baseline has that key.
    """
    for item in evidence.baselines:
        if item.key == key:
            return item
    raise KeyError(key)


def _number(value: object) -> Decimal:
    """Return an evidence value a reason cites as a ``Decimal``.

    Raises:
        TypeError: When the value is not a ``Decimal`` or ``int`` (a reason never cites an
            unavailable figure as a number).
    """
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise TypeError(f"a cited evidence number must be a Decimal or int, found {value!r}")
    return Decimal(value)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Config:
    """Every keyword threshold, read once per run from ``ctx.thresholds``.

    Numeric thresholds are exact fractions for comparisons; the configured values themselves
    reach evidence through ``ctx.make_evidence(threshold_keys=…)``.
    """

    min_coverage: Fraction
    k1_baseline_days: int
    k1_floor: int
    k1_rule_of_three: Decimal
    k1_min_baseline_conversions: Decimal
    k1_spend_multiple: Decimal
    k2_min_conversions: Fraction
    k2_max_cpa_ratio: Fraction
    k2_jaccard: Decimal
    k2_max_terms: int
    k2_min_clicks: int
    k3_min_window_days: int
    k3_min_clicks_per_half: int
    k3_span_days: int
    k3_min_qs_days: int
    k3_qs_drop: Fraction
    k3_rise_ratio: Fraction
    k3_min_first_half_conversions: Fraction

    @classmethod
    def load(cls, thresholds: Thresholds) -> _Config:
        """Read the keyword thresholds.

        Raises:
            ConfigError: When a key is missing or has the wrong kind; ``Thresholds.load``
                already validates every key, so this only fails on a hand-built configuration.
        """
        return cls(
            min_coverage=_exact(thresholds.ratio(COVERAGE_THRESHOLD_KEY)),
            k1_baseline_days=thresholds.int("k1.baseline_days"),
            k1_floor=thresholds.int("k1.min_clicks_floor"),
            k1_rule_of_three=thresholds.decimal("k1.rule_of_three"),
            k1_min_baseline_conversions=thresholds.decimal("k1.min_baseline_conversions"),
            k1_spend_multiple=thresholds.ratio("k1.min_spend_cpa_multiple"),
            k2_min_conversions=_exact(thresholds.decimal("k2.min_conversions")),
            k2_max_cpa_ratio=_exact(thresholds.ratio("k2.max_cpa_ratio")),
            k2_jaccard=thresholds.ratio("k2.adjacent_jaccard"),
            k2_max_terms=thresholds.int("k2.adjacent_max_terms"),
            k2_min_clicks=thresholds.int("k2.adjacent_min_clicks"),
            k3_min_window_days=thresholds.int("k3.min_window_days"),
            k3_min_clicks_per_half=thresholds.int("k3.min_clicks_per_half"),
            k3_span_days=thresholds.int("k3.qs_span_days"),
            k3_min_qs_days=thresholds.int("k3.min_qs_days_per_span"),
            k3_qs_drop=_exact(thresholds.decimal("k3.qs_drop")),
            k3_rise_ratio=_exact(thresholds.ratio("k3.rise_ratio")),
            k3_min_first_half_conversions=_exact(
                thresholds.decimal("k3.min_first_half_conversions")
            ),
        )


# ---------------------------------------------------------------------------
# K1's click gate
# ---------------------------------------------------------------------------


def _min_clicks_exact(rate: Fraction, floor: int, rule_of_three: Fraction) -> int:
    """Return ``max(floor, ceil(rule_of_three / rate))`` on exact fractions."""
    return max(floor, math.ceil(rule_of_three / rate))


def k1_min_clicks(r: Decimal | Fraction, *, floor: int, rule_of_three: Decimal | int) -> int:
    """Return K1's click gate n* = max(floor, ceil(rule_of_three ÷ r)), with no upper cap.

    AAP 0.5.4.3 and 0.2.4 ("Spent with no conversions"). ``r`` is the account's conversions ÷
    clicks over ``k1.baseline_days``: a rate, which can reach or exceed 1 when data-driven
    attribution splits credit fractionally or ``MANY_PER_CLICK`` actions count several
    conversions per click. The gate is a heuristic on the rule-of-three scale, labelled
    :data:`MIN_CLICKS_LABEL`; it makes no statistical claim.

    The quotient is computed exactly (``Decimal`` → ``Fraction``), so an exact integer quotient
    is never pushed up by decimal rounding, and any ``r`` at or above ``rule_of_three ÷ floor``
    (0.3 with the defaults, including ``r`` = 1 and 1.4) gives the floor without error.

    Args:
        r: The baseline rate, conversions per click; must be positive.
        floor: ``k1.min_clicks_floor``, the gate's minimum (10).
        rule_of_three: ``k1.rule_of_three``, the scale (3).

    Returns:
        The minimum clicks a zero-conversion term needs before K1 flags it.

    Raises:
        TypeError: When ``r`` or ``rule_of_three`` is not a ``Decimal``, ``Fraction`` or
            ``int``, or ``floor`` is not an ``int`` (``bool`` and ``float`` are rejected).
        ValueError: When ``r`` or ``rule_of_three`` is not a positive finite number, or
            ``floor`` is negative.

    Example:
        >>> [k1_min_clicks(Decimal(r), floor=10, rule_of_three=3) for r in ("0.01", "0.001")]
        [300, 3000]
        >>> [k1_min_clicks(Decimal(r), floor=10, rule_of_three=3) for r in ("0.047", "1", "1.4")]
        [64, 10, 10]
    """
    rate = _positive_fraction(r, "r")
    scale = _positive_fraction(rule_of_three, "rule_of_three")
    if isinstance(floor, bool) or not isinstance(floor, int):
        raise TypeError(f"floor must be an int, found {floor!r}")
    if floor < 0:
        raise ValueError(f"floor must be >= 0, found {floor}")
    return _min_clicks_exact(rate, floor, scale)


def _positive_fraction(value: object, what: str) -> Fraction:
    """Return a positive finite ``Decimal``, ``Fraction`` or ``int`` as an exact fraction.

    Raises:
        TypeError: For any other type (``bool`` and ``float`` included).
        ValueError: When the value is not finite or not positive.
    """
    if isinstance(value, bool) or not isinstance(value, Decimal | Fraction | int):
        raise TypeError(f"{what} must be a Decimal, Fraction or int, found {value!r}")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"{what} must be finite, found {value}")
    exact = Fraction(value)
    if exact <= 0:
        raise ValueError(f"{what} must be positive, found {value}")
    return exact


# ---------------------------------------------------------------------------
# Google accounts and search-term coverage
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Account:
    """One Google account: its reference, currency and campaigns in the context."""

    id: int
    ref: EntityRef
    currency: str
    campaign_ids: tuple[int, ...]


def _google_active(ctx: AnalysisContext) -> bool:
    """Whether Google has an active dataset or campaigns in this run."""
    return ctx.dataset(_GOOGLE) is not None or any(
        campaign.source == _GOOGLE for campaign in ctx.campaigns.values()
    )


def _google_accounts(ctx: AnalysisContext) -> dict[int, _Account]:
    """Return every Google account of the context, keyed by id, ascending.

    An account is listed when ``ctx.accounts`` holds it or a Google campaign references it, so
    a campaign whose account row is not loaded still has a currency (the campaign's own).
    """
    campaigns: dict[int, list[CampaignInfo]] = {}
    for campaign in ctx.campaigns.values():
        if campaign.source == _GOOGLE:
            campaigns.setdefault(campaign.account_id, []).append(campaign)
    ids = {info.id for info in ctx.accounts.values() if info.source == _GOOGLE} | set(campaigns)
    accounts: dict[int, _Account] = {}
    for account_id in sorted(ids):
        members = sorted(campaigns.get(account_id, []), key=lambda item: item.id)
        info = ctx.accounts.get(account_id)
        if info is not None:
            ref = EntityRef(_GOOGLE, ENTITY_ACCOUNT, info.id, info.external_id, info.name)
            currency = info.currency
        elif members:
            ref = EntityRef(
                _GOOGLE,
                ENTITY_ACCOUNT,
                account_id,
                str(account_id),
                f"Google account #{account_id}",
            )
            currency = members[0].currency
        else:
            continue
        accounts[account_id] = _Account(
            account_id, ref, currency, tuple(member.id for member in members)
        )
    return accounts


def _google_campaign(ctx: AnalysisContext, campaign_id: int) -> CampaignInfo | None:
    """Return a Google campaign of the context, or ``None``.

    ``metrics.load_context`` loads every campaign of the active dataset, so a search term or
    keyword always finds its campaign; a row that does not cannot be placed in an account and
    currency, and is left out rather than compared against another account's figures.
    """
    campaign = ctx.campaigns.get(campaign_id)
    if campaign is None or campaign.source != _GOOGLE:
        return None
    return campaign


@dataclass(frozen=True, slots=True)
class _Coverage:
    """Search-term coverage of one Google account over the window.

    Attributes:
        account: The account.
        search_term_spend: Σ window search-term spend (exact).
        keyword_spend: Σ window keyword spend (exact).
        share: ``search_term_spend / keyword_spend``; ``None`` without keyword spend.
        below_minimum: Whether ``share`` is below ``keywords.min_search_term_coverage``.
    """

    account: _Account
    search_term_spend: Decimal
    keyword_spend: Decimal
    share: Fraction | None
    below_minimum: bool

    def flag(self) -> Flag | None:
        """Return the ``search_term_coverage`` flag for this account's K1/K2 items, if low."""
        if not self.below_minimum or self.share is None:
            return None
        percent = format(_percent(self.share), "f")
        return Flag(
            FLAG_SEARCH_TERM_COVERAGE, f"Low search-term coverage: {percent}% of keyword spend"
        )

    def extra(self) -> dict[str, Any]:
        """Return the coverage summary an item's ``evidence.extra`` carries."""
        currency = self.account.currency
        return {
            "search_term_spend": _money(self.search_term_spend, currency),
            "keyword_spend": _money(self.keyword_spend, currency),
            "percent": None if self.share is None else _percent(self.share),
            "below_minimum": self.below_minimum,
        }


def _coverage(
    ctx: AnalysisContext, cfg: _Config, accounts: Mapping[int, _Account]
) -> dict[int, _Coverage]:
    """Compute search-term coverage per Google account (AAP 0.5.4.3, 0.2.4).

    Search-term spend is Σ ``ctx.search_terms`` cost (already window totals) and keyword spend
    Σ ``ctx.keyword_days`` cost on window dates, each over the account's campaigns. Coverage is
    a ratio within one account, so no amount crosses a currency.
    """
    term_spend = dict.fromkeys(accounts, Decimal(0))
    keyword_spend = dict.fromkeys(accounts, Decimal(0))
    for row in ctx.search_terms:
        campaign = _google_campaign(ctx, row.campaign_id)
        if campaign is not None and campaign.account_id in term_spend:
            term_spend[campaign.account_id] += row.cost
    window = ctx.window
    for keyword_id, days in ctx.keyword_days.items():
        keyword = ctx.keywords.get(keyword_id)
        campaign = None if keyword is None else _google_campaign(ctx, keyword.campaign_id)
        if campaign is None or campaign.account_id not in keyword_spend:
            continue
        keyword_spend[campaign.account_id] += sum(
            (day.cost for day in days if window.start <= day.date <= window.end), Decimal(0)
        )
    coverage: dict[int, _Coverage] = {}
    for account_id, account in accounts.items():
        terms, keywords = term_spend[account_id], keyword_spend[account_id]
        share = _exact(terms) / _exact(keywords) if keywords > 0 else None
        coverage[account_id] = _Coverage(
            account=account,
            search_term_spend=terms,
            keyword_spend=keywords,
            share=share,
            below_minimum=share is not None and share < cfg.min_coverage,
        )
    return coverage


def _coverage_finding(ctx: AnalysisContext, cfg: _Config, item: _Coverage) -> Finding:
    """Return the section finding ``search_term_coverage`` for one account."""
    currency = item.account.currency
    terms = _money(item.search_term_spend, currency)
    keywords = _money(item.keyword_spend, currency)
    days = ctx.window.days
    privacy = (
        "Google withholds low-volume search terms for privacy, so search-term totals fall "
        "below keyword totals."
    )
    minimum = _percent(cfg.min_coverage)
    if item.share is None:
        message = (
            f"Search-term coverage unavailable: no keyword spend over the {days}-day window. "
            f"{privacy}"
        )
    else:
        percent = _percent(item.share)
        message = (
            f"Search terms cover {format(percent, 'f')}% of keyword spend "
            f"({_money_text(terms, currency)} of {_money_text(keywords, currency)}) over the "
            f"{days}-day window. {privacy}"
        )
        if item.below_minimum:
            message += (
                f" Coverage is below the {format(minimum, 'f')}% minimum, so block and "
                "expansion candidates carry the low-coverage flag."
            )
    return Finding(
        code=FINDING_SEARCH_TERM_COVERAGE,
        section=KEYWORDS,
        entity=item.account.ref,
        message=message,
        evidence={
            "currency": currency,
            "search_term_spend": terms,
            "keyword_spend": keywords,
            "coverage_share": None if item.share is None else _ratio(item.share),
            "coverage_percent": None if item.share is None else _percent(item.share),
            "min_share": _plain(_to_decimal(cfg.min_coverage)),
            "below_minimum": item.below_minimum,
            "window": ctx.window.to_json(),
            "thresholds": ctx.thresholds.subset(COVERAGE_THRESHOLD_KEY),
        },
    )


# ---------------------------------------------------------------------------
# Shared item parts: entities, names and the common metrics
# ---------------------------------------------------------------------------


def _ad_group_name(ctx: AnalysisContext, ad_group_id: int) -> str:
    """Return an ad group's display name, or ``Ad group #{id}`` when it is not loaded."""
    group = ctx.ad_groups.get(ad_group_id)
    return f"Ad group #{ad_group_id}" if group is None else group.name


def _search_term_ref(ctx: AnalysisContext, row: SearchTermRow) -> EntityRef:
    """Return a search term's entity: keyed by ad group, matched criterion and term.

    The key is built from platform ids (:func:`search_term_external_id`), so feedback on the
    term matches it across runs and dataset transitions.
    """
    group = ctx.ad_groups.get(row.ad_group_id)
    group_external = str(row.ad_group_id) if group is None else group.external_id
    return EntityRef(
        source=_GOOGLE,
        type=ENTITY_SEARCH_TERM,
        id=None,
        external_id=search_term_external_id(
            group_external, row.matched_criterion_id, row.search_term
        ),
        name=row.search_term,
    )


def _keyword_ref(keyword: KeywordInfo) -> EntityRef:
    """Return a keyword's entity, keyed by its Google criterion id."""
    return EntityRef(
        source=_GOOGLE,
        type=ENTITY_KEYWORD,
        id=keyword.id,
        external_id=keyword.criterion_id,
        name=keyword.text,
    )


def _matched_keyword(row: SearchTermRow) -> str | None:
    """Return the matched keyword as ``"text (MATCH_TYPE)"``, or ``None`` without one."""
    text = (row.matched_keyword_text or "").strip()
    if not text:
        return None
    match_type = (row.matched_match_type or "").strip()
    return f"{text} ({match_type})" if match_type else text


def _common_metrics(
    *,
    term_label: str,
    term: str,
    spend: Decimal,
    conversions: Decimal | None,
    cost_per_conversion: Decimal | None,
    currency: str,
) -> list[Metric]:
    """Return the four fields every keyword item shows first, in the user's order.

    Term (or keyword text), spend, conversions and cost/conversion (AAP 0.5.4.3, "Each item
    shows: term, spend, conversions, cost/conversion"). A ``None`` cost/conversion (0
    conversions) renders "—"; ``None`` conversions mean the platform did not report them.
    """
    return [
        Metric("term", term_label, term, "text"),
        Metric("spend", "Spend", spend, "currency", currency=currency),
        Metric("conversions", "Conversions", conversions, "conversions"),
        Metric(
            "cost_per_conversion",
            "Cost/conversion",
            cost_per_conversion,
            "currency",
            currency=currency,
        ),
    ]


def _campaign_extra(
    ctx: AnalysisContext, campaign: CampaignInfo, ad_group_id: int
) -> dict[str, Any]:
    """Return the campaign and ad group names an item's ``evidence.extra`` carries."""
    return {
        "campaign": campaign.name,
        "campaign_external_id": campaign.external_id,
        "ad_group": _ad_group_name(ctx, ad_group_id),
    }


def _flags(flag: Flag | None) -> tuple[Flag, ...]:
    """Return ``(flag,)``, or ``()`` without one."""
    return () if flag is None else (flag,)


# ---------------------------------------------------------------------------
# K1 Block / negative candidate
# ---------------------------------------------------------------------------


def _k1_missing(cfg: _Config) -> tuple[str, str]:
    """Return ``(withheld reason, finding detail)`` for a run without any baseline facts."""
    detail = f"no Google campaign facts in the trailing {cfg.k1_baseline_days} days"
    return f"baseline unavailable: {detail}", detail


def _k1_unusable(baseline: RateBaseline, cfg: _Config) -> tuple[str, str] | None:
    """Return ``(withheld reason, finding detail)`` when K1's baseline is unusable, else ``None``.

    Unusable: fewer than ``k1.min_baseline_conversions`` conversions (which covers a rate of
    0) or no clicks (rate unavailable).
    """
    span = cfg.k1_baseline_days
    required = _number_text(_plain(cfg.k1_min_baseline_conversions))
    conversions = _number_text(_count(baseline.conversions))
    if baseline.conversions < cfg.k1_min_baseline_conversions:
        return (
            f"baseline conversions {_count(baseline.conversions):f} of "
            f"{_plain(cfg.k1_min_baseline_conversions):f} required",
            f"the trailing {span}-day baseline has {conversions} conversions over "
            f"{baseline.clicks:,} clicks, below the {required} required",
        )
    # ``k1.min_baseline_conversions`` is validated positive, so a rate of 0 (no conversions)
    # always fails the check above; only a baseline without clicks is left to reject.
    if baseline.clicks <= 0:
        detail = f"the trailing {span}-day baseline has {conversions} conversions but no clicks"
        return f"baseline rate unavailable: {detail}", detail
    return None


def _k1_no_baseline(ctx: AnalysisContext, cfg: _Config, reasons: tuple[str, str]) -> RuleResult:
    """Withhold K1 account-wide: a ``k1_no_baseline`` finding and one withheld line, no items."""
    reason, detail = reasons
    baseline = ctx.k1_baseline
    rate = None
    if baseline is not None and baseline.clicks > 0:
        rate = _significant(_exact(baseline.conversions) / baseline.clicks)
    finding = Finding(
        code=FINDING_K1_NO_BASELINE,
        section=KEYWORDS,
        entity=None,
        message=(
            f"Block candidates withheld account-wide: {detail}. K1 sets its minimum clicks "
            "from the account's conversion rate, which needs at least "
            f"{_number_text(_plain(cfg.k1_min_baseline_conversions))} baseline conversions "
            "and a rate above 0."
        ),
        evidence={
            "baseline_conversions": None if baseline is None else _count(baseline.conversions),
            "baseline_clicks": None if baseline is None else baseline.clicks,
            "baseline_rate": rate,
            "baseline_start": None if baseline is None else baseline.start,
            "baseline_end": None if baseline is None else baseline.end,
            "required": _plain(cfg.k1_min_baseline_conversions),
            "window": ctx.window.to_json(),
            "thresholds": ctx.thresholds.subset(*K1_THRESHOLD_KEYS),
        },
    )
    withheld = Withheld(
        rule_id=K1,
        section=KEYWORDS,
        entity=None,
        reason=reason,
        observed=None if baseline is None else _count(baseline.conversions),
        required=_plain(cfg.k1_min_baseline_conversions),
        unit="conversions",
    )
    return RuleResult(findings=(finding,), withheld=(withheld,))


def _k1_reason(evidence: Evidence) -> str:
    """Render K1's reason from its evidence only (AAP 0.5.4: the text cannot drift)."""
    days = evidence.window["days"]
    spend = _render(evidence.metric("wasted_spend"))
    clicks = _render(evidence.metric("clicks"))
    n_star = _render(evidence.metric("min_clicks"))
    rate = _number(_baseline(evidence, "baseline_rate").value)
    cpa = _number(_baseline(evidence, "account_cost_per_conversion").value)
    currency = evidence.metric("spend").currency
    multiple = _number(evidence.extra["min_spend_cpa_multiple"])
    reason = (
        f"Spent {spend} over {days} days with 0 conversions across {clicks} clicks, at or "
        f"above the minimum of {n_star} clicks for the account's conversion rate of "
        f"{_number_text(rate)} (conversions ÷ clicks), and at least {_number_text(multiple)}x "
        f"the account cost/conversion of {_money_text(cpa, currency)}."
    )
    matched = evidence.extra.get("matched_keyword_text")
    if evidence.extra.get("term_status") == "ADDED" and matched:
        match_type = evidence.extra.get("matched_match_type") or "unknown match type"
        criterion = evidence.extra.get("matched_criterion_id") or "unknown"
        reason += (
            f" The term is already a keyword: {matched} ({match_type}, criterion {criterion}). "
            "An exact-match negative also blocks that keyword's exact-match traffic."
        )
    elif "matched_keyword" in {metric.key for metric in evidence.metrics}:
        reason += f" Matched keyword: {_render(evidence.metric('matched_keyword'))}."
    return reason


def _k1_candidate(
    ctx: AnalysisContext,
    cfg: _Config,
    row: SearchTermRow,
    campaign: CampaignInfo,
    account_totals: Totals,
    baseline: RateBaseline,
    n_star: int,
    coverage: _Coverage,
) -> Candidate:
    """Build one K1 item: a zero-conversion term that cleared the click and spend gates."""
    currency = coverage.account.currency
    spend = _money(row.cost, currency)
    outcomes = account_totals.outcomes if account_totals.outcomes is not None else Decimal(0)
    account_cpa = _exact(account_totals.spend) / _exact(outcomes)
    rate = _exact(baseline.conversions) / baseline.clicks
    metrics = _common_metrics(
        term_label="Search term",
        term=row.search_term,
        spend=spend,
        conversions=_count(row.conversions),
        cost_per_conversion=None,
        currency=currency,
    )
    metrics += [
        Metric("wasted_spend", "Wasted spend", spend, "currency", currency=currency),
        Metric("clicks", "Clicks", row.clicks, "clicks"),
        Metric("min_clicks", MIN_CLICKS_LABEL, n_star, "clicks"),
    ]
    matched = _matched_keyword(row)
    if matched is not None:
        metrics.append(Metric("matched_keyword", "Matched keyword", matched, "text"))
    baselines = (
        Baseline(
            "account_cost_per_conversion",
            f"Account cost/conversion ({currency})",
            _money(account_cpa, currency),
        ),
        Baseline(
            "baseline_rate",
            f"Account conversions per click, {baseline.start.isoformat()} to "
            f"{baseline.end.isoformat()}",
            _significant(rate),
        ),
        Baseline("baseline_conversions", "Baseline conversions", _count(baseline.conversions)),
        Baseline("baseline_clicks", "Baseline clicks", baseline.clicks),
    )
    extra: dict[str, Any] = _campaign_extra(ctx, campaign, row.ad_group_id)
    extra |= {
        "term_status": row.term_status,
        "matched_criterion_id": row.matched_criterion_id,
        "matched_keyword_text": row.matched_keyword_text,
        "matched_match_type": row.matched_match_type,
        "negative_match_type": "EXACT",
        "min_spend_cpa_multiple": _plain(cfg.k1_spend_multiple),
        "min_clicks_label": MIN_CLICKS_LABEL,
        "baseline_window": {
            "start": baseline.start,
            "end": baseline.end,
            "days": cfg.k1_baseline_days,
        },
        "search_term_coverage": coverage.extra(),
    }
    evidence = ctx.make_evidence(
        required_datasets=K1_DATASETS,
        crm_scope=None,
        metrics=metrics,
        baselines=baselines,
        sample=Sample(observed=row.clicks, required=n_star, unit="clicks"),
        threshold_keys=K1_THRESHOLD_KEYS,
        extra=extra,
    )
    return Candidate(
        rule_id=K1,
        section=KEYWORDS,
        group=GROUP_BLOCK,
        entity_ref=_search_term_ref(ctx, row),
        action=K1_ACTION,
        reason=_k1_reason(evidence),
        evidence=evidence,
        rank_key=(-row.cost,),
        flags=_flags(coverage.flag()),
        required_datasets=K1_DATASETS,
        increases_spend_on=(),
        # Keyword items offer "Mark done", never "Record decision" (AAP 0.5.5).
        decision_subject=None,
        depends_on=(DecisionSubject("campaign", campaign.id),),
    )


def _account_totals(
    ctx: AnalysisContext, account: _Account, cache: dict[int, Totals | None]
) -> Totals | None:
    """Return the account's window totals over its Google campaigns (memoised per run)."""
    if account.id not in cache:
        cache[account.id] = (
            window_totals(ctx, account.campaign_ids) if account.campaign_ids else None
        )
    return cache[account.id]


def _evaluate_k1(
    ctx: AnalysisContext,
    cfg: _Config,
    accounts: Mapping[int, _Account],
    coverage: Mapping[int, _Coverage],
) -> RuleResult:
    """Rule K1 Block / negative candidate: spend with exactly zero conversions.

    AAP 0.5.4.3 K1, 0.2.4, FR-4 (a). A search term with status ``NONE`` or ``ADDED`` fires
    when it has exactly 0 conversions in the window (any fractional conversion, such as 0.3,
    counts as converting), clicks ≥ n* and spend ≥ ``k1.min_spend_cpa_multiple`` x the
    account cost/conversion. ``EXCLUDED`` and ``ADDED_EXCLUDED`` terms are already blocked
    and skipped. The action is always "Add as exact-match negative"; for an ``ADDED`` term the
    item names the matched keyword criterion and notes that an exact negative also blocks that
    keyword's exact-match traffic. K1 never recommends pausing a keyword; keyword-level
    problems belong to K3.

    Threshold keys: ``k1.baseline_days``, ``k1.min_clicks_floor``, ``k1.rule_of_three``,
    ``k1.min_baseline_conversions``, ``k1.min_spend_cpa_multiple`` and
    ``keywords.min_search_term_coverage``.

    Rationale: r = conversions ÷ clicks over the trailing ``k1.baseline_days`` (90) ending at
    the window end; n* = max(``k1.min_clicks_floor``, ceil(``k1.rule_of_three`` ÷ r)) with no
    upper cap (300 clicks at r = 1%, 3,000 at r = 0.1%). r is a rate that can reach or exceed
    1, so n* is a heuristic labelled "minimum clicks for the account's conversion rate" and
    makes no statistical claim (:func:`k1_min_clicks`). With fewer than
    ``k1.min_baseline_conversions`` baseline conversions, or no usable rate, K1 is withheld
    account-wide with the ``k1_no_baseline`` finding. The spend floor requires at least one
    typical conversion's cost to have been spent. A term at the spend floor but below n* is
    listed as withheld with its clicks observed and required.

    Returns:
        Items in group ``block`` ranked by spend descending (``rank_key = (-spend,)``), the
        withheld lines, and the ``k1_no_baseline`` finding when the baseline is unusable.
    """
    baseline = ctx.k1_baseline
    if baseline is None:
        return _k1_no_baseline(ctx, cfg, _k1_missing(cfg))
    unusable = _k1_unusable(baseline, cfg)
    if unusable is not None:
        return _k1_no_baseline(ctx, cfg, unusable)
    rate = _exact(baseline.conversions) / baseline.clicks
    n_star = _min_clicks_exact(rate, cfg.k1_floor, _exact(cfg.k1_rule_of_three))
    multiple = _exact(cfg.k1_spend_multiple)
    totals_cache: dict[int, Totals | None] = {}
    without_cpa: set[int] = set()
    candidates: list[Candidate] = []
    withheld: list[Withheld] = []
    for row in ctx.search_terms:
        # Exactly zero: a fractional conversion under data-driven attribution is converting.
        if row.term_status not in K1_STATUSES or row.conversions != 0 or row.cost <= 0:
            continue
        campaign = _google_campaign(ctx, row.campaign_id)
        account = None if campaign is None else accounts.get(campaign.account_id)
        if campaign is None or account is None:
            continue
        totals = _account_totals(ctx, account, totals_cache)
        if totals is None or totals.outcomes is None or totals.outcomes <= 0:
            if account.id not in without_cpa:
                without_cpa.add(account.id)
                withheld.append(
                    Withheld(
                        rule_id=K1,
                        section=KEYWORDS,
                        entity=account.ref,
                        reason=(
                            "account cost/conversion unavailable: 0 conversions in the "
                            f"{ctx.window.days}-day window"
                        ),
                        observed=None if totals is None else totals.outcomes,
                        required=None,
                        unit="conversions",
                    )
                )
            continue
        # spend ≥ multiple x (account spend ÷ account conversions), cross-multiplied exactly.
        if _exact(row.cost) * _exact(totals.outcomes) < multiple * _exact(totals.spend):
            continue
        if row.clicks < n_star:
            withheld.append(
                Withheld(
                    rule_id=K1,
                    section=KEYWORDS,
                    entity=_search_term_ref(ctx, row),
                    reason=f"clicks {row.clicks} of {n_star} required",
                    observed=row.clicks,
                    required=n_star,
                    unit="clicks",
                )
            )
            continue
        candidates.append(
            _k1_candidate(ctx, cfg, row, campaign, totals, baseline, n_star, coverage[account.id])
        )
    return RuleResult(candidates=tuple(candidates), withheld=tuple(withheld))


# ---------------------------------------------------------------------------
# K2 Expansion candidate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TermTotals:
    """One distinct search term's window totals within an account (K2 adjacent terms)."""

    term: str
    spend: Decimal
    clicks: int
    conversions: Decimal


@dataclass(frozen=True, slots=True)
class _AdjacentPool:
    """An account's eligible adjacent terms, already in K2's order, with a token index.

    Attributes:
        ranked: Terms that are not keywords, never excluded and have at least
            ``k2.adjacent_min_clicks`` clicks, ordered by conversions descending, then clicks
            descending, then term.
        by_term: ``ranked`` keyed by term.
        postings: For each token, the positions in ``ranked`` of the terms containing it,
            ascending. ``k2.adjacent_jaccard`` is above 0, so a term sharing no token with the
            converting term can never be adjacent; the index narrows each lookup to the terms
            that can, without changing the result.
    """

    ranked: tuple[_TermTotals, ...]
    by_term: Mapping[str, _TermTotals]
    postings: Mapping[str, tuple[int, ...]]


def _adjacent_pool(ctx: AnalysisContext, cfg: _Config, account: _Account) -> _AdjacentPool:
    """Build an account's adjacent-term pool from its own search-term report (AAP 0.2.4).

    Rows of one term across ad groups and matched criteria are summed. A term is ineligible
    when any of its rows is ``EXCLUDED`` or ``ADDED_EXCLUDED`` (the advertiser already blocked
    it), when its tokens equal a keyword's tokens (already a keyword, exact normalised match),
    or when it has fewer than ``k2.adjacent_min_clicks`` clicks.
    """
    members = frozenset(account.campaign_ids)
    keyword_tokens = {
        tokenize(keyword.text)
        for keyword in ctx.keywords.values()
        # A keyword whose campaign is not loaded still counts: excluding a term too often is
        # harmless, suggesting an existing keyword is not.
        if keyword.campaign_id in members or keyword.campaign_id not in ctx.campaigns
    }
    sums: dict[str, list[Any]] = {}
    excluded: set[str] = set()
    for row in ctx.search_terms:
        if row.campaign_id not in members:
            continue
        if row.term_status in _EXCLUDED_STATUSES:
            excluded.add(row.search_term)
        entry = sums.setdefault(row.search_term, [Decimal(0), 0, Decimal(0)])
        entry[0] += row.cost
        entry[1] += row.clicks
        entry[2] += row.conversions
    eligible: list[tuple[_TermTotals, frozenset[str]]] = []
    for term, (spend, clicks, conversions) in sums.items():
        tokens = tokenize(term)
        if term in excluded or clicks < cfg.k2_min_clicks or tokens in keyword_tokens:
            continue
        eligible.append((_TermTotals(term, spend, clicks, conversions), tokens))
    eligible.sort(key=lambda pair: (-pair[0].conversions, -pair[0].clicks, pair[0].term))
    postings: dict[str, list[int]] = {}
    for position, (_, tokens) in enumerate(eligible):
        for token in tokens:
            postings.setdefault(token, []).append(position)
    ranked = tuple(item for item, _ in eligible)
    return _AdjacentPool(
        ranked=ranked,
        by_term={item.term: item for item in ranked},
        postings={token: tuple(positions) for token, positions in postings.items()},
    )


def _adjacent(pool: _AdjacentPool, cfg: _Config, term: str) -> list[_TermTotals]:
    """Return up to ``k2.adjacent_max_terms`` adjacent terms for a converting ``term``.

    The candidates are the pool's terms sharing a token with ``term``, in the pool's order;
    :func:`text.adjacent_terms` keeps that order, applies the exact Jaccard threshold and drops
    the term itself, so the first matches are the best ranked.
    """
    positions = sorted(
        {position for token in tokenize(term) for position in pool.postings.get(token, ())}
    )
    names = [pool.ranked[position].term for position in positions]
    matches = adjacent_terms(term, names, threshold=cfg.k2_jaccard)
    return [pool.by_term[name] for name in matches[: cfg.k2_max_terms]]


def _k2_reason(evidence: Evidence) -> str:
    """Render K2's reason from its evidence only."""
    days = evidence.window["days"]
    conversions = _render(evidence.metric("conversions"))
    cpa = _render(evidence.metric("cost_per_conversion"))
    currency = evidence.metric("spend").currency
    campaign_cpa = _number(_baseline(evidence, "campaign_cost_per_conversion").value)
    ratio = _number(evidence.extra["max_cpa_ratio"])
    label = evidence.extra["adjacent_terms_label"]
    reason = (
        f"{conversions} conversions at {cpa} per conversion over {days} days, at or below "
        f"{_number_text(ratio)}x the campaign's {_money_text(campaign_cpa, currency)} per "
        "conversion."
    )
    terms = [str(item["term"]) for item in evidence.extra["adjacent_terms"]]
    if terms:
        reason += f" Adjacent terms to try, {label}: {', '.join(terms)}."
    else:
        reason += f" No adjacent terms {label} qualified."
    return reason


def _k2_candidate(
    ctx: AnalysisContext,
    cfg: _Config,
    row: SearchTermRow,
    campaign: CampaignInfo,
    campaign_totals: Totals,
    adjacent: Sequence[_TermTotals],
    coverage: _Coverage,
) -> Candidate:
    """Build one K2 item: a converting ``NONE`` term at or better than campaign efficiency."""
    currency = coverage.account.currency
    term_cpa = _exact(row.cost) / _exact(row.conversions)
    outcomes = campaign_totals.outcomes if campaign_totals.outcomes is not None else Decimal(0)
    campaign_cpa = _exact(campaign_totals.spend) / _exact(outcomes)
    ad_group = _ad_group_name(ctx, row.ad_group_id)
    metrics = _common_metrics(
        term_label="Search term",
        term=row.search_term,
        spend=_money(row.cost, currency),
        conversions=_count(row.conversions),
        cost_per_conversion=_money(term_cpa, currency),
        currency=currency,
    )
    metrics.append(Metric("clicks", "Clicks", row.clicks, "clicks"))
    matched = _matched_keyword(row)
    if matched is not None:
        metrics.append(Metric("matched_keyword", "Matched keyword", matched, "text"))
    baselines = (
        Baseline(
            "campaign_cost_per_conversion",
            f"Campaign cost/conversion ({currency})",
            _money(campaign_cpa, currency),
        ),
        Baseline(
            "max_cost_per_conversion",
            f"Highest qualifying cost/conversion ({currency})",
            _money(cfg.k2_max_cpa_ratio * campaign_cpa, currency),
        ),
    )
    extra: dict[str, Any] = _campaign_extra(ctx, campaign, row.ad_group_id)
    extra |= {
        "term_status": row.term_status,
        "matched_criterion_id": row.matched_criterion_id,
        "matched_keyword_text": row.matched_keyword_text,
        "matched_match_type": row.matched_match_type,
        "suggested_match_type": "PHRASE",
        "max_cpa_ratio": _plain(_to_decimal(cfg.k2_max_cpa_ratio)),
        "adjacent_terms": [
            {
                "term": item.term,
                "spend": _money(item.spend, currency),
                "currency": currency,
                "clicks": item.clicks,
                "conversions": _count(item.conversions),
            }
            for item in adjacent
        ],
        "adjacent_terms_label": ADJACENT_TERMS_LABEL,
        "search_term_coverage": coverage.extra(),
    }
    evidence = ctx.make_evidence(
        required_datasets=K2_DATASETS,
        crm_scope=None,
        metrics=metrics,
        baselines=baselines,
        sample=Sample(
            observed=_count(row.conversions),
            required=_plain(_to_decimal(cfg.k2_min_conversions)),
            unit="conversions",
        ),
        threshold_keys=K2_THRESHOLD_KEYS,
        extra=extra,
    )
    return Candidate(
        rule_id=K2,
        section=KEYWORDS,
        group=GROUP_EXPAND,
        entity_ref=_search_term_ref(ctx, row),
        action=K2_ACTION_TEMPLATE.format(ad_group=ad_group),
        reason=_k2_reason(evidence),
        evidence=evidence,
        rank_key=(-row.conversions, _to_decimal(term_cpa)),
        flags=_flags(coverage.flag()),
        required_datasets=K2_DATASETS,
        increases_spend_on=(),
        decision_subject=None,
        depends_on=(DecisionSubject("campaign", campaign.id),),
    )


def _evaluate_k2(
    ctx: AnalysisContext,
    cfg: _Config,
    accounts: Mapping[int, _Account],
    coverage: Mapping[int, _Coverage],
) -> RuleResult:
    """Rule K2 Expansion candidate: a converting term at or better than campaign efficiency.

    AAP 0.5.4.3 K2, FR-4 (b). A search term with status ``NONE`` fires when it has
    conversions ≥ ``k2.min_conversions`` and cost/conversion ≤ ``k2.max_cpa_ratio`` x its
    campaign's window cost/conversion. The action is "Add as phrase-match keyword in {ad
    group}". ``evidence.extra.adjacent_terms`` lists up to ``k2.adjacent_max_terms`` other
    window search terms of the same account with token Jaccard ≥ ``k2.adjacent_jaccard``
    (lowercased, stopwords removed), not already keywords, never excluded and with at least
    ``k2.adjacent_min_clicks`` clicks, ordered by conversions then clicks, labelled "from your
    search-term report".

    Threshold keys: ``k2.min_conversions``, ``k2.max_cpa_ratio``, ``k2.adjacent_jaccard``,
    ``k2.adjacent_max_terms``, ``k2.adjacent_min_clicks`` and
    ``keywords.min_search_term_coverage``.

    Rationale: three conversions avoid one-off conversions, and a ratio of 1.0 keeps only terms
    at or better than campaign efficiency. Adjacent terms come from the account's own report,
    because Keyword Planner is not reachable through the n8n webhook.

    Returns:
        Items in group ``expand`` ranked by conversions descending, then cost/conversion
        ascending (``rank_key = (-conversions, cost_per_conversion)``).
    """
    pools: dict[int, _AdjacentPool] = {}
    totals_cache: dict[int, Totals] = {}
    candidates: list[Candidate] = []
    for row in ctx.search_terms:
        if row.term_status not in _K2_STATUSES or row.conversions <= 0:
            continue
        if _exact(row.conversions) < cfg.k2_min_conversions:
            continue
        campaign = _google_campaign(ctx, row.campaign_id)
        account = None if campaign is None else accounts.get(campaign.account_id)
        if campaign is None or account is None:
            continue
        if campaign.id not in totals_cache:
            totals_cache[campaign.id] = window_totals(ctx, (campaign.id,))
        totals = totals_cache[campaign.id]
        if totals.outcomes is None or totals.outcomes <= 0:
            continue
        # term cost ÷ term conversions ≤ ratio x campaign spend ÷ campaign conversions.
        if _exact(row.cost) * _exact(totals.outcomes) > (
            cfg.k2_max_cpa_ratio * _exact(totals.spend) * _exact(row.conversions)
        ):
            continue
        if account.id not in pools:
            pools[account.id] = _adjacent_pool(ctx, cfg, account)
        adjacent = _adjacent(pools[account.id], cfg, row.search_term)
        candidates.append(
            _k2_candidate(ctx, cfg, row, campaign, totals, adjacent, coverage[account.id])
        )
    return RuleResult(candidates=tuple(candidates))


# ---------------------------------------------------------------------------
# K3 Review: declining quality score, or CPC rising without a matching conversion rise
# ---------------------------------------------------------------------------

type _QsBasis = Literal["daily", "snapshots"]


@dataclass(frozen=True, slots=True)
class _Half:
    """One half of the window for one keyword: its dates and summed facts.

    ``conversions`` is ``None`` when no day of the half reported conversions.
    """

    start: date
    end: date
    cost: Decimal
    clicks: int
    conversions: Decimal | None


def _half(days: Sequence[KeywordDay], start: date, end: date) -> _Half:
    """Sum a keyword's days from ``start`` to ``end`` inclusive."""
    cost = Decimal(0)
    clicks = 0
    conversions = Decimal(0)
    reported = False
    for day in days:
        if start <= day.date <= end:
            cost += day.cost
            clicks += day.clicks
            if day.conversions is not None:
                conversions += day.conversions
                reported = True
    return _Half(start, end, cost, clicks, conversions if reported else None)


@dataclass(frozen=True, slots=True)
class _QualityScore:
    """K3 branch (a): the quality-score comparison of one keyword.

    Attributes:
        basis: ``daily`` (means of ``historical_quality_score`` over the first and last
            ``k3.qs_span_days``), ``snapshots`` (Q-G7 snapshots at window start and end), or
            ``None`` when neither is available.
        first: The earlier score (a mean for ``daily``, a snapshot score otherwise).
        last: The later score.
        days_first: Days with a daily score in the first span.
        days_last: Days with a daily score in the last span.
        first_at: The earlier snapshot's instant (``snapshots`` only).
        last_at: The later snapshot's instant (``snapshots`` only).
        fires: Whether ``last ≤ first - k3.qs_drop``.
        shortfall: ``(span, days)`` of the sparser span when daily scores exist but fall short
            of ``k3.min_qs_days_per_span``; the branch then does not fire and no snapshot
            fallback applies.
    """

    basis: _QsBasis | None
    first: Fraction | None
    last: Fraction | None
    days_first: int
    days_last: int
    first_at: datetime | None
    last_at: datetime | None
    fires: bool
    shortfall: tuple[str, int] | None


def _google_zone(ctx: AnalysisContext, campaign: CampaignInfo) -> str:
    """Return the Google native-day zone: the window's Google boundary, else the account's."""
    try:
        return ctx.window.boundary(_GOOGLE)
    except KeyError:
        account = ctx.accounts.get(campaign.account_id)
        if account is not None and account.timezone:
            return account.timezone
        return "UTC"


def _daily_quality_score(
    ctx: AnalysisContext, cfg: _Config, scored: Sequence[KeywordDay]
) -> _QualityScore:
    """Compare mean daily quality scores of the first and last ``k3.qs_span_days``."""
    window = ctx.window
    span = cfg.k3_span_days
    if span > window.days:
        return _QualityScore(None, None, None, 0, 0, None, None, False, None)
    first_start, first_end = window.first_n(span)
    last_start, last_end = window.last_n(span)
    first = [day.historical_quality_score for day in scored if first_start <= day.date <= first_end]
    last = [day.historical_quality_score for day in scored if last_start <= day.date <= last_end]
    first_scores = [score for score in first if score is not None]
    last_scores = [score for score in last if score is not None]
    minimum = cfg.k3_min_qs_days
    if len(first_scores) < minimum or len(last_scores) < minimum:
        sparse = (
            ("first", len(first_scores))
            if len(first_scores) <= len(last_scores)
            else ("last", len(last_scores))
        )
        return _QualityScore(
            None, None, None, len(first_scores), len(last_scores), None, None, False, sparse
        )
    mean_first = Fraction(sum(first_scores), len(first_scores))
    mean_last = Fraction(sum(last_scores), len(last_scores))
    return _QualityScore(
        basis="daily",
        first=mean_first,
        last=mean_last,
        days_first=len(first_scores),
        days_last=len(last_scores),
        first_at=None,
        last_at=None,
        fires=mean_last <= mean_first - cfg.k3_qs_drop,
        shortfall=None,
    )


def _snapshot_quality_score(
    ctx: AnalysisContext, cfg: _Config, keyword: KeywordInfo, campaign: CampaignInfo
) -> _QualityScore:
    """Compare the keyword's Q-G7 quality-score snapshots at window start and end.

    The first observation at or after the window's first instant and the last at or before
    its last, in Google's native day, each carrying a score; ``metrics.load_context`` reads
    exactly these two per keyword.
    """
    window = ctx.window
    zone = _google_zone(ctx, campaign)
    lower, _ = window.day_bounds(window.start, zone)
    _, upper = window.day_bounds(window.end, zone)
    observed = sorted(
        (
            item
            for item in ctx.observations.get((ENTITY_KEYWORD, keyword.id), ())
            if lower <= item.captured_at <= upper and item.quality_score is not None
        ),
        key=lambda item: item.captured_at,
    )
    if len(observed) < 2 or observed[0].captured_at >= observed[-1].captured_at:
        return _QualityScore(None, None, None, 0, 0, None, None, False, None)
    start, end = observed[0], observed[-1]
    if start.quality_score is None or end.quality_score is None:  # filtered above
        return _QualityScore(None, None, None, 0, 0, None, None, False, None)
    first, last = Fraction(start.quality_score), Fraction(end.quality_score)
    return _QualityScore(
        basis="snapshots",
        first=first,
        last=last,
        days_first=0,
        days_last=0,
        first_at=start.captured_at,
        last_at=end.captured_at,
        fires=last <= first - cfg.k3_qs_drop,
        shortfall=None,
    )


def _quality_score(
    ctx: AnalysisContext,
    cfg: _Config,
    keyword: KeywordInfo,
    campaign: CampaignInfo,
    days: Sequence[KeywordDay],
) -> _QualityScore:
    """K3 branch (a): daily scores when the keyword has any, else the snapshot fallback."""
    scored = [day for day in days if day.historical_quality_score is not None]
    if scored:
        return _daily_quality_score(ctx, cfg, scored)
    return _snapshot_quality_score(ctx, cfg, keyword, campaign)


@dataclass(frozen=True, slots=True)
class _CpcRise:
    """K3 branch (b): CPC and cost/conversion by half for one keyword.

    Attributes:
        cpc_first: First-half spend ÷ clicks.
        cpc_second: Second-half spend ÷ clicks.
        cpc_ratio: ``cpc_second ÷ cpc_first``; ``None`` when the first-half CPC is 0.
        cpa_first: First-half cost/conversion; ``None`` at 0 or unreported conversions.
        cpa_second: Second-half cost/conversion; ``None`` at 0 or unreported conversions.
        cpa_ratio: ``cpa_second ÷ cpa_first`` when both exist.
        conversions_fell: Second-half conversions are 0 while the first half had at least
            ``k3.min_first_half_conversions``.
        fires: ``cpc_ratio ≥ k3.rise_ratio`` and either ``cpa_ratio ≥ k3.rise_ratio`` or
            ``conversions_fell``: CPC rose without a matching rise in conversion.
    """

    cpc_first: Fraction
    cpc_second: Fraction
    cpc_ratio: Fraction | None
    cpa_first: Fraction | None
    cpa_second: Fraction | None
    cpa_ratio: Fraction | None
    conversions_fell: bool
    fires: bool


def _cost_per(cost: Decimal, count: Decimal | int | None) -> Fraction | None:
    """Return ``cost ÷ count`` exactly, or ``None`` at a zero or unreported count."""
    if count is None or count <= 0:
        return None
    return _exact(cost) / _exact(count)


def _cpc_rise(cfg: _Config, first: _Half, second: _Half) -> _CpcRise:
    """Evaluate K3 branch (b) on two halves that both cleared the click gate."""
    cpc_first = _exact(first.cost) / first.clicks
    cpc_second = _exact(second.cost) / second.clicks
    cpc_ratio = cpc_second / cpc_first if cpc_first > 0 else None
    cpa_first = _cost_per(first.cost, first.conversions)
    cpa_second = _cost_per(second.cost, second.conversions)
    cpa_ratio = cpa_second / cpa_first if cpa_first is not None and cpa_second is not None else None
    conversions_fell = (
        second.conversions is not None
        and second.conversions == 0
        and first.conversions is not None
        and _exact(first.conversions) >= cfg.k3_min_first_half_conversions
    )
    rise = cfg.k3_rise_ratio
    fires = (
        cpc_ratio is not None
        and cpc_ratio >= rise
        and ((cpa_ratio is not None and cpa_ratio >= rise) or conversions_fell)
    )
    return _CpcRise(
        cpc_first,
        cpc_second,
        cpc_ratio,
        cpa_first,
        cpa_second,
        cpa_ratio,
        conversions_fell,
        fires,
    )


def _qs_value(value: Fraction | None, basis: _QsBasis | None) -> Decimal | int | None:
    """Return a quality score for display: a snapshot's integer, or a mean to two places."""
    if value is None:
        return None
    if basis == "snapshots" and value.denominator == 1:
        return value.numerator
    return _mean(value)


def _qs_labels(basis: _QsBasis | None, span: int) -> tuple[str, str]:
    """Return the labels of the two quality-score metrics for a basis."""
    if basis == "snapshots":
        return (
            "Quality score at window start (keyword snapshot)",
            "Quality score at window end (keyword snapshot)",
        )
    return (
        f"Quality score, first {span} days (mean of daily scores)",
        f"Quality score, last {span} days (mean of daily scores)",
    )


def _optional_money(value: Fraction | None, currency: str) -> Decimal | None:
    """Return a money value at the minor unit, or ``None``."""
    return None if value is None else _money(value, currency)


def _optional_ratio(value: Fraction | None) -> Decimal | None:
    """Return a displayed ratio, or ``None``."""
    return None if value is None else _ratio(value)


def _optional_count(value: Decimal | None) -> Decimal | None:
    """Return a displayed conversion count, or ``None`` when unreported."""
    return None if value is None else _count(value)


def _span_json(start: date, end: date) -> dict[str, date]:
    """Return an inclusive native-date span for ``evidence.extra``."""
    return {"start": start, "end": end}


def _k3_reason(evidence: Evidence) -> str:
    """Render K3's reason from its evidence only."""
    extra = evidence.extra
    triggers = extra["triggers"]
    sentences: list[str] = []
    if "quality_score" in triggers:
        first = _render(evidence.metric("qs_first"))
        last = _render(evidence.metric("qs_last"))
        if extra["qs_basis"] == "snapshots":
            sentences.append(
                f"Quality score fell from {first} to {last} between the keyword snapshots at "
                "window start and end."
            )
        else:
            spans = extra["qs_spans"]
            sentences.append(
                f"Quality score fell from a mean of {first} ({spans['first']['start']} to "
                f"{spans['first']['end']}) to {last} ({spans['last']['start']} to "
                f"{spans['last']['end']})."
            )
    if "cpc" in triggers:
        halves = extra["halves"]
        sentence = (
            f"CPC rose from {_render(evidence.metric('cpc_first_half'))} in the first half "
            f"({halves['first']['start']} to {halves['first']['end']}) to "
            f"{_render(evidence.metric('cpc_second_half'))} in the second half "
            f"({halves['second']['start']} to {halves['second']['end']}), "
            f"{_render(evidence.metric('cpc_ratio'))}x, "
        )
        if extra["conversions_fell"]:
            sentence += (
                f"while conversions fell from {_render(evidence.metric('conversions_first_half'))}"
                " to 0."
            )
        else:
            sentence += (
                "while cost/conversion rose from "
                f"{_render(evidence.metric('cost_per_conversion_first_half'))} to "
                f"{_render(evidence.metric('cost_per_conversion_second_half'))}, "
                f"{_render(evidence.metric('cpa_ratio'))}x."
            )
        sentences.append(sentence)
    return " ".join(sentences)


def _k3_candidate(
    ctx: AnalysisContext,
    cfg: _Config,
    keyword: KeywordInfo,
    campaign: CampaignInfo,
    days: Sequence[KeywordDay],
    halves: tuple[_Half, _Half],
    quality: _QualityScore,
    cpc: _CpcRise,
) -> Candidate:
    """Build one K3 item from the branches that fired."""
    currency = ctx.account_currency(campaign.id)
    whole = _half(days, ctx.window.start, ctx.window.end)
    first, second = halves
    qs_first_label, qs_last_label = _qs_labels(quality.basis, cfg.k3_span_days)
    metrics = _common_metrics(
        term_label="Keyword",
        term=keyword.text,
        spend=_money(whole.cost, currency),
        conversions=_optional_count(whole.conversions),
        cost_per_conversion=_optional_money(_cost_per(whole.cost, whole.conversions), currency),
        currency=currency,
    )
    metrics += [
        Metric("clicks", "Clicks", whole.clicks, "clicks"),
        Metric("qs_first", qs_first_label, _qs_value(quality.first, quality.basis), "score"),
        Metric("qs_last", qs_last_label, _qs_value(quality.last, quality.basis), "score"),
        Metric(
            "cpc_first_half",
            "CPC, first half",
            _money(cpc.cpc_first, currency),
            "currency",
            currency=currency,
        ),
        Metric(
            "cpc_second_half",
            "CPC, second half",
            _money(cpc.cpc_second, currency),
            "currency",
            currency=currency,
        ),
        Metric("cpc_ratio", "CPC, second ÷ first half", _optional_ratio(cpc.cpc_ratio), "ratio"),
        Metric(
            "cost_per_conversion_first_half",
            "Cost/conversion, first half",
            _optional_money(cpc.cpa_first, currency),
            "currency",
            currency=currency,
        ),
        Metric(
            "cost_per_conversion_second_half",
            "Cost/conversion, second half",
            _optional_money(cpc.cpa_second, currency),
            "currency",
            currency=currency,
        ),
        Metric(
            "cpa_ratio",
            "Cost/conversion, second ÷ first half",
            _optional_ratio(cpc.cpa_ratio),
            "ratio",
        ),
        Metric(
            "conversions_first_half",
            "Conversions, first half",
            _optional_count(first.conversions),
            "conversions",
        ),
        Metric(
            "conversions_second_half",
            "Conversions, second half",
            _optional_count(second.conversions),
            "conversions",
        ),
        Metric("clicks_first_half", "Clicks, first half", first.clicks, "clicks"),
        Metric("clicks_second_half", "Clicks, second half", second.clicks, "clicks"),
    ]
    triggers = [
        name for name, fired in (("quality_score", quality.fires), ("cpc", cpc.fires)) if fired
    ]
    first_span = ctx.window.first_n(min(cfg.k3_span_days, ctx.window.days))
    last_span = ctx.window.last_n(min(cfg.k3_span_days, ctx.window.days))
    extra: dict[str, Any] = _campaign_extra(ctx, campaign, keyword.ad_group_id)
    extra |= {
        "criterion_id": keyword.criterion_id,
        "match_type": keyword.match_type,
        "triggers": triggers,
        "qs_basis": quality.basis,
        "qs_days_first": quality.days_first,
        "qs_days_last": quality.days_last,
        "qs_spans": {"first": _span_json(*first_span), "last": _span_json(*last_span)},
        "qs_snapshots": (
            {"start_at": quality.first_at, "end_at": quality.last_at}
            if quality.basis == "snapshots"
            else None
        ),
        "halves": {
            "first": _span_json(first.start, first.end),
            "second": _span_json(second.start, second.end),
        },
        "conversions_fell": cpc.conversions_fell,
    }
    evidence = ctx.make_evidence(
        required_datasets=K3_DATASETS,
        crm_scope=None,
        metrics=metrics,
        sample=Sample(
            observed=min(first.clicks, second.clicks),
            required=cfg.k3_min_clicks_per_half,
            unit="clicks per half",
        ),
        threshold_keys=K3_THRESHOLD_KEYS,
        extra=extra,
    )
    return Candidate(
        rule_id=K3,
        section=KEYWORDS,
        group=GROUP_REVIEW,
        entity_ref=_keyword_ref(keyword),
        action=K3_ACTION,
        reason=_k3_reason(evidence),
        evidence=evidence,
        rank_key=(-whole.cost,),
        flags=(),
        required_datasets=K3_DATASETS,
        increases_spend_on=(),
        decision_subject=None,
        depends_on=(DecisionSubject("campaign", campaign.id),),
    )


def _k3_required_days(ctx: AnalysisContext, cfg: _Config) -> int:
    """Return K3's minimum window: ``k3.min_window_days``, the sustained minimum, and 2 halves."""
    return max(cfg.k3_min_window_days, ctx.window.sustained_min_days, 2)


def _evaluate_k3(ctx: AnalysisContext, cfg: _Config) -> RuleResult:
    """Rule K3 Review: declining quality score, or CPC rising without a matching conversion rise.

    AAP 0.5.4.3 K3, FR-4 (c). The window must reach ``k3.min_window_days`` and
    ``window.sustained_min_days``; otherwise K3 is withheld with the observed and required
    days. A keyword with at least ``k3.min_clicks_per_half`` clicks in each half of the window
    fires on either branch:

    * (a) quality score: the mean daily ``historical_quality_score`` (Q-G5) of the last
      ``k3.qs_span_days`` is at most the first span's mean - ``k3.qs_drop``, with scores on at
      least ``k3.min_qs_days_per_span`` days in each span. When the keyword has no daily
      scores, its Q-G7 snapshots at window start and end are compared instead. Daily scores on
      too few days withhold the branch (no snapshot fallback).
    * (b) CPC: second-half CPC ÷ first-half CPC ≥ ``k3.rise_ratio`` and either cost/conversion
      second ÷ first half ≥ ``k3.rise_ratio``, or second-half conversions are 0 with at least
      ``k3.min_first_half_conversions`` in the first half.

    Threshold keys: ``k3.min_window_days``, ``window.sustained_min_days``,
    ``k3.min_clicks_per_half``, ``k3.qs_span_days``, ``k3.min_qs_days_per_span``,
    ``k3.qs_drop``, ``k3.rise_ratio`` and ``k3.min_first_half_conversions``.

    Rationale: 30 clicks per half give a stable CPC; a one-point drop on the integer 1-10
    quality-score scale is material; 15% is above normal auction noise, and requiring
    cost/conversion to rise as well means CPC rose without a matching rise in conversion.

    Returns:
        Items in group ``review`` ranked by window spend descending (``rank_key =
        (-spend,)``), and the withheld lines.
    """
    window = ctx.window
    required = _k3_required_days(ctx, cfg)
    if window.days < required:
        if not any(ctx.keyword_days.values()):
            return RuleResult()
        return RuleResult(
            withheld=(
                Withheld(
                    rule_id=K3,
                    section=KEYWORDS,
                    entity=None,
                    reason=f"window {window.days} days of {required} required",
                    observed=window.days,
                    required=required,
                    unit="days",
                ),
            )
        )
    (first_start, first_end), (second_start, second_end) = window.halves()
    candidates: list[Candidate] = []
    withheld: list[Withheld] = []
    for keyword_id in sorted(ctx.keyword_days):
        keyword = ctx.keywords.get(keyword_id)
        campaign = None if keyword is None else _google_campaign(ctx, keyword.campaign_id)
        if keyword is None or campaign is None:
            continue
        days = [
            day for day in ctx.keyword_days[keyword_id] if window.start <= day.date <= window.end
        ]
        first = _half(days, first_start, first_end)
        second = _half(days, second_start, second_end)
        if min(first.clicks, second.clicks) < cfg.k3_min_clicks_per_half:
            continue
        quality = _quality_score(ctx, cfg, keyword, campaign, days)
        cpc = _cpc_rise(cfg, first, second)
        if quality.fires or cpc.fires:
            candidates.append(
                _k3_candidate(ctx, cfg, keyword, campaign, days, (first, second), quality, cpc)
            )
        elif quality.shortfall is not None:
            span_name, observed = quality.shortfall
            withheld.append(
                Withheld(
                    rule_id=K3,
                    section=KEYWORDS,
                    entity=_keyword_ref(keyword),
                    reason=(
                        f"quality score on {observed} of {cfg.k3_min_qs_days} required days in "
                        f"the {span_name} {cfg.k3_span_days} days"
                    ),
                    observed=observed,
                    required=cfg.k3_min_qs_days,
                    unit="days",
                )
            )
    return RuleResult(candidates=tuple(candidates), withheld=tuple(withheld))


# ---------------------------------------------------------------------------
# The section
# ---------------------------------------------------------------------------


def evaluate(ctx: AnalysisContext) -> RuleResult:
    """Evaluate the Keywords to adjust section: K1, K2, K3 and search-term coverage.

    AAP 0.5.4.3 and FR-4. Pure: reads only ``ctx`` and performs no I/O. When Google has no
    active dataset and no campaigns in the run, the section has nothing to evaluate and the
    result is empty. Otherwise the result holds one ``search_term_coverage`` finding per
    Google account, then the K1, K2 and K3 results in that order (:func:`_evaluate_k1`,
    :func:`_evaluate_k2`, :func:`_evaluate_k3`). Precedence (holds, P2 suppression,
    stale/incomplete/short-window flags, feedback and ranking) is applied by the runner.

    Args:
        ctx: The analysis context.

    Returns:
        The section's candidates, findings and withheld lines; no suppression counts.

    Raises:
        ConfigError: When a keyword threshold is missing from the configuration.
    """
    if not _google_active(ctx):
        return RuleResult()
    cfg = _Config.load(ctx.thresholds)
    accounts = _google_accounts(ctx)
    coverage = _coverage(ctx, cfg, accounts)
    findings = tuple(_coverage_finding(ctx, cfg, item) for item in coverage.values())
    return RuleResult(findings=findings).merge(
        _evaluate_k1(ctx, cfg, accounts, coverage),
        _evaluate_k2(ctx, cfg, accounts, coverage),
        _evaluate_k3(ctx, cfg),
    )
