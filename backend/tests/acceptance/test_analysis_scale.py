"""Scale acceptance test: the Keywords rules and precedence on a 180-day, 20,000-term report.

AAP 0.1.1 sets the performance target: "a custom-window run on a typical account (one Google
customer, a few LinkedIn accounts, 180 days of data) computes in under 30 s", and AAP 0.1.3
puts a typical account at "thousands to low millions of rows". The search-term report is what
grows with the account, and two costs grew faster than it: K2 compared every converting term
with every other window term, re-tokenising each one per comparison, and precedence rebuilt
every item's evidence, re-freezing and re-validating all of it, at each step that extended it.

This module runs the ``default`` catalogue world over a 180-day custom window with 20,000
deterministic synthetic search terms added to its Google account, evaluates the Keywords
section and applies precedence, and holds the run to deterministic work bounds (each distinct
term tokenised a small constant number of times, each item's evidence constructed once) as
well as to the AAP's 30-second figure, which those bounds leave a wide margin under. K2's
adjacent terms on a sample of items are also checked against a brute-force scan of the report,
so the index that makes the run fast is shown to change no result. No database, no network.
"""

from __future__ import annotations

import hashlib
import itertools
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from ads_engine.analysis import keywords, precedence, text
from ads_engine.analysis.context import (
    K1,
    K2,
    AnalysisContext,
    Candidate,
    Evidence,
    SearchTermRow,
)
from ads_engine.analysis.text import tokenize
from ads_engine.simulation.scenarios import FIXED_NOW, SCENARIOS

type ContextFactory = Callable[..., AnalysisContext]
"""The root ``make_context`` fixture: ``make_context(scenario, *, thresholds=None, now=None)``."""

TERMS = 20_000
"""Synthetic search terms added to the report: the volume the AAP 0.1.1 target is held to."""

WINDOW_DAYS = 180
"""The custom window, ``window.max_days``: the longest window the product computes."""

TARGET_SECONDS = 30.0
"""AAP 0.1.1: a custom-window run on a typical account computes in under 30 s."""

TOKENIZE_CALLS_PER_TERM = 3
"""Tokenisations allowed per distinct report term: K2's pool check and its index use two."""

_VOCABULARY = (
    "attribution",
    "analytics",
    "marketing",
    "software",
    "platform",
    "tool",
    "revenue",
    "forecasting",
    "pricing",
    "dashboard",
    "reporting",
    "crm",
    "pipeline",
    "b2b",
    "saas",
    "data",
    "insights",
    "campaign",
    "roi",
    "measurement",
    "multi",
    "touch",
    "model",
    "customer",
    "journey",
    "lead",
    "scoring",
    "enterprise",
    "cloud",
    "integration",
    "api",
    "automation",
    "sales",
    "funnel",
    "conversion",
    "tracking",
    "demand",
    "generation",
    "abm",
    "account",
    "based",
    "intelligence",
    "predictive",
    "budget",
    "spend",
    "optimization",
    "channel",
    "mix",
    "media",
    "performance",
    "growth",
    "startup",
    "small",
    "business",
    "team",
    "agency",
    "review",
    "comparison",
    "alternative",
    "free",
    "trial",
    "demo",
    "template",
    "guide",
    "example",
    "metrics",
    "kpi",
    "benchmark",
    "report",
)
"""B2B search vocabulary the synthetic terms draw two to four distinct words from."""

_CONVERTING_PERCENT = 15
"""Share of synthetic terms with conversions (3 to 7 each), so K2 fires on many of them."""

_SAMPLED_K2_ITEMS = 12
"""K2 items whose adjacent terms are recomputed by brute force over the whole report."""


def _draws(label: str) -> Iterator[int]:
    """An endless, deterministic stream of 64-bit integers: SHA-256 of ``label:counter``."""
    for counter in itertools.count():
        digest = hashlib.sha256(f"{label}:{counter}".encode()).digest()
        yield int.from_bytes(digest[:8], "big")


def _synthetic_terms(ctx: AnalysisContext) -> tuple[SearchTermRow, ...]:
    """:data:`TERMS` distinct ``NONE`` search terms spread over the context's Google ad groups.

    Each term is two to four distinct vocabulary words. Every term has 20 to 139 clicks at
    1.50 to 4.49 per click; :data:`_CONVERTING_PERCENT` percent also have 3 to 7 conversions.
    """
    draws = _draws("analysis-scale")

    def pick(count: int) -> int:
        return next(draws) % count

    groups = sorted(
        (group.id, group.campaign_id)
        for group in ctx.ad_groups.values()
        if group.campaign_id in ctx.campaigns
        and ctx.campaigns[group.campaign_id].source == "google"
    )
    assert groups, "the default world has Google ad groups"
    existing = {row.search_term for row in ctx.search_terms}
    terms: dict[str, None] = {}
    while len(terms) < TERMS:
        pool = list(_VOCABULARY)
        words = [pool.pop(pick(len(pool))) for _ in range((2, 3, 3, 4)[pick(4)])]
        term = " ".join(words)
        if term not in existing:
            terms.setdefault(term, None)
    rows = []
    for term in terms:
        ad_group_id, campaign_id = groups[pick(len(groups))]
        clicks = 20 + pick(120)
        conversions = Decimal(3 + pick(5)) if pick(100) < _CONVERTING_PERCENT else Decimal(0)
        cost = (Decimal(clicks) * Decimal(150 + pick(300)) / 100).quantize(Decimal("0.01"))
        rows.append(
            SearchTermRow(
                ad_group_id=ad_group_id,
                campaign_id=campaign_id,
                search_term=term,
                term_status="NONE",
                matched_criterion_id=None,
                matched_keyword_text=None,
                matched_match_type=None,
                cost=cost,
                clicks=clicks,
                impressions=clicks * 12,
                conversions=conversions,
            )
        )
    return tuple(rows)


@pytest.fixture(scope="module")
def scale_context() -> AnalysisContext:
    """The ``default`` world over a 180-day custom window, with 20,000 more search terms."""
    base = SCENARIOS["default"]
    spec = replace(base.context, window=(-WINDOW_DAYS, -1), kind="custom")
    ctx = replace(base, context=spec).build_context(seed=42, now=FIXED_NOW)
    assert ctx.window.days == WINDOW_DAYS
    return replace(ctx, search_terms=(*ctx.search_terms, *_synthetic_terms(ctx)), cache={})


def _brute_force_adjacent(
    ctx: AnalysisContext, candidate: Candidate, term: str
) -> list[dict[str, Any]]:
    """K2's adjacent terms for ``term``, recomputed by scanning every term of its account.

    AAP 0.5.4.3: up to ``k2.adjacent_max_terms`` other window search terms with token Jaccard
    at least ``k2.adjacent_jaccard``, not already keywords, never excluded, with at least
    ``k2.adjacent_min_clicks`` clicks, ordered by conversions then clicks (then term).
    """
    thresholds = ctx.thresholds
    numerator, denominator = thresholds.ratio("k2.adjacent_jaccard").as_integer_ratio()
    max_terms = thresholds.int("k2.adjacent_max_terms")
    min_clicks = thresholds.int("k2.adjacent_min_clicks")
    # A K2 item depends on its term's campaign, whose account owns the report it reads.
    campaign = ctx.campaigns[candidate.depends_on[0].id]
    members = {cid for cid, info in ctx.campaigns.items() if info.account_id == campaign.account_id}
    keyword_tokens = {
        tokenize(keyword.text)
        for keyword in ctx.keywords.values()
        if keyword.campaign_id in members or keyword.campaign_id not in ctx.campaigns
    }
    totals: dict[str, list[Any]] = {}
    excluded: set[str] = set()
    for row in ctx.search_terms:
        if row.campaign_id not in members:
            continue
        if row.term_status in {"EXCLUDED", "ADDED_EXCLUDED"}:
            excluded.add(row.search_term)
        entry = totals.setdefault(row.search_term, [0, Decimal(0)])
        entry[0] += row.clicks
        entry[1] += row.conversions
    source = tokenize(term)
    # Every term here is ASCII, so caseless, whitespace-normalised spelling is this key.
    own_key = " ".join(term.casefold().split())
    matches = []
    for other, (clicks, conversions) in totals.items():
        tokens = tokenize(other)
        shared = len(source & tokens)
        union = len(source | tokens)
        if (
            union
            and shared * denominator >= union * numerator
            and " ".join(other.casefold().split()) != own_key
            and tokens not in keyword_tokens
            and other not in excluded
            and clicks >= min_clicks
        ):
            matches.append((other, clicks, conversions))
    matches.sort(key=lambda match: (-match[2], -match[1], match[0]))
    return [{"term": other, "clicks": clicks} for other, clicks, _ in matches[:max_terms]]


def test_a_180_day_run_over_20000_search_terms_stays_within_its_work_and_time_bounds(
    scale_context: AnalysisContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AAP 0.1.1: the Keywords rules and precedence finish well under 30 s at 20,000 terms.

    The work is bounded deterministically, independent of the host's load: each distinct
    report term is tokenised at most :data:`TOKENIZE_CALLS_PER_TERM` times however many K2
    items look for adjacent terms, and each item's evidence is constructed exactly once, by
    its rule, however many precedence steps extend it. The wall-clock bound is the AAP's own
    figure, far above what those bounds take.
    """
    ctx = scale_context
    distinct = len({row.search_term for row in ctx.search_terms})
    assert distinct >= TERMS
    tokenised: list[str] = []
    constructed: list[Evidence] = []
    real_tokenize = text.tokenize
    real_post_init = Evidence.__post_init__

    def counting_tokenize(value: str) -> frozenset[str]:
        tokenised.append(value)
        return real_tokenize(value)

    def counting_post_init(self: Evidence) -> None:
        constructed.append(self)
        real_post_init(self)

    monkeypatch.setattr(text, "tokenize", counting_tokenize)
    monkeypatch.setattr(keywords, "tokenize", counting_tokenize)
    monkeypatch.setattr(Evidence, "__post_init__", counting_post_init)

    started = time.perf_counter()
    result = keywords.evaluate(ctx)
    evaluated = time.perf_counter()
    built_by_rules = len(constructed)
    out = precedence.apply_precedence([result], ctx)
    finished = time.perf_counter()
    monkeypatch.undo()

    k1 = [item for item in result.candidates if item.rule_id == K1]
    k2 = [item for item in result.candidates if item.rule_id == K2]
    assert len(k1) > 1_000, "the report is large enough to give K1 thousands of items"
    assert len(k2) > 1_000, "and K2 thousands of expansion candidates to find neighbours for"
    assert len(out.all_items()) == len(result.candidates)

    assert len(tokenised) <= TOKENIZE_CALLS_PER_TERM * distinct, (
        f"{len(tokenised)} tokenisations for {distinct} distinct terms"
    )
    assert built_by_rules == len(result.candidates), "each rule builds one evidence per item"
    assert len(constructed) == built_by_rules, "precedence copies evidence without rebuilding it"

    for item in out.all_items():
        thresholds = item.candidate.evidence.thresholds
        assert "window.trusted_min_days" in thresholds
        assert "sources.stale_after_hours" in thresholds
        assert item.candidate.data_status is not None

    elapsed = finished - started
    assert elapsed < TARGET_SECONDS, (
        f"keywords {evaluated - started:.2f} s + precedence {finished - evaluated:.2f} s"
    )


def test_k2_adjacent_terms_at_scale_equal_a_brute_force_scan_of_the_report(
    scale_context: AnalysisContext,
) -> None:
    """AAP 0.5.4.3 K2: the indexed search returns what scanning all 20,000 terms returns.

    For a spread of K2 items, the adjacent terms, their order and their clicks equal the
    brute-force selection over the whole account report, and none repeats the term itself.
    """
    ctx = scale_context
    result = keywords.evaluate(ctx)
    k2 = [item for item in result.candidates if item.rule_id == K2]
    step = max(1, len(k2) // _SAMPLED_K2_ITEMS)
    sampled = k2[::step][:_SAMPLED_K2_ITEMS]
    assert len(sampled) == _SAMPLED_K2_ITEMS
    with_neighbours = 0

    for item in sampled:
        term = str(item.evidence.metric("term").value)
        shown = [
            {"term": str(entry["term"]), "clicks": entry["clicks"]}
            for entry in item.evidence.extra["adjacent_terms"]
        ]
        assert shown == _brute_force_adjacent(ctx, item, term), term
        assert term not in {entry["term"] for entry in shown}
        with_neighbours += bool(shown)
    assert with_neighbours == _SAMPLED_K2_ITEMS, "every sampled item has neighbours to compare"
